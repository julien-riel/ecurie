"""Adaptateur mlx-vlm, chemin **description d'image** : ce que le modèle voit.

Même runtime, même bibliothèque et **exactement les mêmes poids** que
`workers.mlx_vlm`, qui sert `document-to-text`. Un module distinct malgré tout,
pour la raison déjà retenue entre la voix et la chanson de `mlx-audio` : les deux
chemins ne se ressemblent ni dans l'appel, ni dans la sortie. Transcrire, c'est
rasteriser un PDF page à page, recoller les transcriptions et rendre une
structure ; décrire, c'est une image, une consigne et un texte. Les mêler
donnerait un fichier qui commence par un aiguillage et ne se relit plus.

C'est aussi la démonstration la moins chère de ce que promet le §4 de
l'architecture : ajouter une capacité au parc n'a demandé ici ni téléchargement,
ni environnement de plus, ni une ligne du superviseur. Un contrat, un manifeste,
et cet adaptateur.

Rien de mlx n'est importé au niveau du module (voir `workers/__init__.py`).
"""

import gc
import time
from pathlib import Path
from typing import Any

from ecurie_runtime.workers.base import (
    InferRequest,
    InferResult,
    ProgressFn,
    Worker,
    WorkerError,
    main,
    peak_rss_bytes,
    sans_raisonnement,
)
from ecurie_runtime.workers.mlx_vlm import (
    IMAGES,
    REPAIR,
    Runtime,
    composer_invite,
    import_runtime,
    resolve_document,
    thinking_demande,
)

OUTPUT_TEXT = "text.txt"

# Longueur visée, traduite en consigne. Le plafond de jetons du contrat reste
# maître : `detail` oriente, il ne coupe pas.
DETAILS = {
    "bref": "Réponds en une seule phrase.",
    "normal": "Réponds en trois à cinq phrases.",
    "détaillé": (
        "Décris en détail : le sujet principal, l'arrière-plan, les objets "
        "visibles, les couleurs dominantes et le texte lisible s'il y en a."
    ),
}

DESCRIPTION = (
    "Décris cette image. Ne rapporte que ce que tu vois : pas d'interprétation, "
    "pas de supposition sur ce qui se passe hors du cadre."
)
QUESTION = (
    "Réponds à la question suivante en te fondant uniquement sur cette image. "
    "Si l'image ne permet pas de répondre, dis-le plutôt que de deviner."
)


def build_prompt(question: str | None, detail: str, langue: str | None) -> str:
    """La consigne envoyée au modèle, composée depuis les champs du contrat.

    L'ordre compte : la tâche, puis la longueur, puis la langue. Une consigne de
    langue placée avant la tâche se fait oublier au bout de quelques dizaines de
    jetons, et le modèle répond dans la langue de la question.
    """
    demande = (question or "").strip()
    if demande:
        consigne = f"{QUESTION}\n\nQuestion : {demande}"
    else:
        consigne = DESCRIPTION
    consigne += " " + DETAILS.get(detail, DETAILS["normal"])
    if langue and str(langue).strip() and str(langue).strip().lower() not in ("auto", ""):
        consigne += f" Rédige ta réponse en {str(langue).strip()}."
    return consigne


def resolve_image(valeur: Any, job_dir: Path) -> Path:
    chemin = resolve_document(valeur, job_dir)
    if chemin.suffix.lower() not in IMAGES:
        # Un PDF est un document, pas une image : le renvoyer vers la capacité
        # qui sait le rasteriser vaut mieux que d'échouer dans mlx-vlm sur un
        # fichier qu'il n'ouvrira pas.
        raise WorkerError(
            f"format non géré pour une description : {chemin.suffix or '(sans extension)'} — "
            f"images acceptées : {', '.join(sorted(IMAGES))}. Pour un PDF, "
            "c'est la capacité document-to-text qu'il faut."
        )
    return chemin


class MlxVlmDescribeWorker(Worker):
    """Description d'image et question sur image, par modèle vision-langage."""

    name = "mlx-vlm-describe"

    def __init__(self) -> None:
        self._runtime: Runtime | None = None
        self._model: Any = None
        self._processor: Any = None
        self._config: Any = None
        self._defaults: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._thinking = False
        self._peak_load = 0

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        runtime = import_runtime()
        chemin = Path(str(variant.get("weights_path") or ""))
        if not chemin.is_dir():
            raise WorkerError(
                f"poids introuvables : {chemin} — le superviseur transmet un chemin local "
                f"déjà vérifié, un worker ne télécharge jamais ({REPAIR} si l'env est en cause)"
            )

        self._defaults = dict(variant.get("defaults") or {})
        self._options = dict(variant.get("options") or {})
        self._thinking = thinking_demande(self._options, self._defaults)

        model, processor = runtime.load(str(chemin))
        self._runtime = runtime
        self._model = model
        self._processor = processor
        self._config = runtime.load_config(str(chemin))
        self._peak_load = self._pic_mlx() or 0

        return {
            # Liste ouverte, comme pour la transcription : un VLM répond dans
            # toutes les langues qu'il connaît, et une liste fermée en refuserait
            # qu'il maîtrise très bien.
            "languages": [],
            "details": sorted(DETAILS),
            "versions": self._versions(),
        }

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._runtime is None or self._model is None:
            raise WorkerError("modèle non chargé")
        runtime = self._runtime

        image = resolve_image(request.get("image"), request.output_dir)
        question = self._reglage(request, "question", None)
        detail = str(self._reglage(request, "detail", "normal"))
        langue = self._reglage(request, "language", None)
        max_tokens = int(self._reglage(request, "max_tokens", 512))
        température = float(self._reglage(request, "temperature", 0.2))

        consigne = build_prompt(question, detail, langue)

        runtime.mx.reset_peak_memory()
        if request.seed is not None:
            runtime.mx.random.seed(int(request.seed))

        progress(10, "description en cours")
        début = time.monotonic()
        invite = composer_invite(
            runtime,
            self._processor,
            self._config,
            consigne,
            num_images=1,
            thinking=self._thinking,
        )
        try:
            résultat = runtime.generate(
                self._model,
                self._processor,
                invite,
                image=[str(image)],
                max_tokens=max_tokens,
                temperature=température,
                verbose=False,
            )
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte utile
            raise WorkerError(f"description impossible : {type(exc).__name__}: {exc}") from exc
        calcul = time.monotonic() - début

        texte = getattr(résultat, "text", None)
        texte = (texte if texte is not None else str(résultat)).strip()
        # Le brouillon de raisonnement n'est pas la réponse : s'il en reste un
        # malgré `enable_thinking`, il est séparé ici plutôt que livré au client.
        texte, _raisonnement = sans_raisonnement(texte)
        jetons = int(getattr(résultat, "generation_tokens", 0) or 0)

        progress(92, "écriture")
        (request.output_dir / OUTPUT_TEXT).write_text(texte, encoding="utf-8")

        return InferResult(
            output={
                "text": OUTPUT_TEXT,
                "tokens_generated": jetons,
                "finish_reason": _fin(résultat, jetons, max_tokens),
            },
            metrics={
                "characters": len(texte),
                "generation_tokens": jetons,
                "tokens_per_second": round(jetons / calcul, 2) if calcul > 0 else None,
                "detail": detail,
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def unload(self) -> None:
        self._model = None
        self._processor = None
        self._peak_load = 0
        gc.collect()
        if self._runtime is not None:
            self._runtime.mx.clear_cache()

    def peak_memory_bytes(self) -> int | None:
        pic = self._pic_mlx()
        if pic is None:
            return peak_rss_bytes()
        return max(pic, self._peak_load)

    # --- détails -------------------------------------------------------------

    def _reglage(self, request: InferRequest, nom: str, defaut: Any) -> Any:
        valeur = request.get(nom)
        if valeur is not None:
            return valeur
        for couche in (self._options, self._defaults):
            if couche.get(nom) is not None:
                return couche[nom]
        return defaut

    def _pic_mlx(self) -> int | None:
        if self._runtime is None:
            return None
        try:
            return int(self._runtime.mx.get_peak_memory())
        except Exception:  # noqa: BLE001 — une mesure ratée ne fait pas échouer un job
            return None

    def _versions(self) -> dict[str, str]:
        versions = {}
        for nom, module in (("mlx", "mlx.core"), ("mlx-vlm", "mlx_vlm")):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


def _fin(résultat: Any, jetons: int, max_tokens: int) -> str:
    """« length » quand la réponse est tronquée, « stop » sinon.

    Le contrat n'admet que ces deux valeurs, et la distinction n'est pas
    cosmétique : une description coupée au plafond ne doit pas être notée comme
    une description ratée.
    """
    brut = getattr(résultat, "finish_reason", None)
    if isinstance(brut, str) and brut.strip().lower() in ("length", "max_tokens"):
        return "length"
    return "length" if jetons >= max_tokens else "stop"


if __name__ == "__main__":
    raise SystemExit(main(MlxVlmDescribeWorker))
