"""Adaptateur mlx-vlm, chemin **description de vidéo** : ce que le modèle voit se dérouler.

Troisième emploi des mêmes poids que `workers.mlx_vlm` (lecture de document) et
`workers.mlx_vlm_describe` (description d'image) — même dépôt, même révision,
même worker chargé en mémoire. Un module distinct pour la raison déjà retenue
entre les deux autres : ni l'appel, ni la sortie ne se ressemblent. Décrire une
image, c'est une image et une consigne ; décrire une vidéo, c'est d'abord
décider **ce que le modèle verra**, et cette décision est tout l'adaptateur.

Un modèle ne regarde pas un film : il regarde les images qu'on lui donne. Trois
choses en découlent, et le contrat les expose parce qu'aucune ne se devine.

**Le budget d'images est le vrai réglage.** `fps` dit la cadence voulue,
`max_frames` la borne dure. Une minute à 8 im/s ferait 480 images, soit bien
plus de jetons visuels que le budget mémoire de la machine n'en tient. C'est
`max_frames` qui décide, et la cadence effective en découle — d'où la sortie
`sampled_fps`, qui n'est pas celle demandée dès que la borne a mordu.

**Il y a deux chemins, et le « natif » ne marche pas ici.** C'était la première
version de cet adaptateur : `processor_handles_video(processor)` rend vrai pour
Qwen3-VL, `resolve_video_inputs` laisse alors passer le fichier tel quel, et
`generate(video=[…])` compose une invite qui contient un unique
`<|vision_start|><|video_pad|><|vision_end|>`. Le modèle répond — et il décrit
une scène **figée**, au singulier, en niant tout mouvement. Mesuré sur une vidéo
où un cube traverse le cadre de part en part :

    chemin natif   : « Aucun des objets ne se déplace. Tous sont immobiles. »
    images fixes   : « Le cube commence à se déplacer vers la droite… »

Les trois cas de la charge type rendaient d'ailleurs la même réponse au
caractère près et la même durée à trois millisecondes, pour 4, 8 et 16 images
demandées : le budget n'était pas appliqué, parce que rien n'était échantillonné.

Cet adaptateur **décode donc toujours lui-même**. `fps` et `max_frames`
retrouvent le sens que le contrat leur donne, `frames_sampled` compte des images
réellement vues, et la sortie `native_video` reste dans le contrat pour dire quel
chemin a servi — elle vaudra vrai le jour où ce chemin transmettra autre chose
qu'un jeton de remplissage.

**Le décodage n'ajoute aucune dépendance.** `mlx_vlm.utils.load_video` passe par
OpenCV, déjà installé dans cet environnement — vérifié sur place plutôt que
supposé, parce qu'un import manquant ne se découvre qu'au premier job.

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
)
from ecurie_runtime.workers.mlx_vlm import (
    REPAIR,
    Runtime,
    import_runtime,
)

OUTPUT_TEXT = "text.txt"

# Conteneurs que le décodeur d'OpenCV ouvre sans extension supplémentaire. La
# liste est fermée pour la même raison que celle des images : un `.mkv` refusé
# avec la liste des formats acceptés vaut mieux qu'un décodage qui rend zéro
# image et un message d'erreur venu de trois couches plus bas.
VIDEOS = {".mp4", ".mov", ".m4v", ".avi", ".webm"}

DESCRIPTION = (
    "Décris ce qui se passe dans cette vidéo, dans l'ordre. Ne rapporte que ce "
    "que tu vois : pas d'interprétation, pas de supposition sur ce qui se passe "
    "hors du cadre ou entre deux images."
)
QUESTION = (
    "Réponds à la question suivante en te fondant uniquement sur cette vidéo. "
    "Si elle ne permet pas de répondre, dis-le plutôt que de deviner."
)


def build_prompt(question: str | None) -> str:
    """La consigne envoyée au modèle, composée depuis les champs du contrat."""
    demande = (question or "").strip()
    if demande:
        return f"{QUESTION}\n\nQuestion : {demande}"
    return DESCRIPTION


def resolve_video(valeur: Any, job_dir: Path) -> Path:
    """Le chemin de la vidéo, relatif au job ou absolu, avec son format vérifié."""
    brut = str(valeur or "").strip()
    if not brut:
        raise WorkerError("aucune vidéo fournie")
    chemin = Path(brut).expanduser()
    if not chemin.is_absolute():
        chemin = job_dir / chemin
    if not chemin.is_file():
        raise WorkerError(f"vidéo introuvable : {chemin}")
    if chemin.suffix.lower() not in VIDEOS:
        raise WorkerError(
            f"format non géré : {chemin.suffix or '(sans extension)'} — "
            f"formats acceptés : {', '.join(sorted(VIDEOS))}"
        )
    return chemin


class MlxVlmVideoWorker(Worker):
    """Description de vidéo et question sur vidéo, par modèle vision-langage."""

    name = "mlx-vlm-video"

    def __init__(self) -> None:
        self._runtime: Runtime | None = None
        self._model: Any = None
        self._processor: Any = None
        self._config: Any = None
        self._defaults: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
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

        model, processor = runtime.load(str(chemin))
        self._runtime = runtime
        self._model = model
        self._processor = processor
        self._config = runtime.load_config(str(chemin))
        self._peak_load = self._pic_mlx() or 0

        return {
            "languages": [],
            "native_video": self._processeur_gere_la_video(),
            "versions": self._versions(),
        }

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._runtime is None or self._model is None:
            raise WorkerError("modèle non chargé")
        runtime = self._runtime

        vidéo = resolve_video(request.get("video"), request.output_dir)
        question = self._reglage(request, "question", None)
        fps = float(self._reglage(request, "fps", 1.0))
        max_frames = int(self._reglage(request, "max_frames", 32))
        max_tokens = int(self._reglage(request, "max_tokens", 512))
        température = float(self._reglage(request, "temperature", 0.2))

        consigne = build_prompt(question)

        runtime.mx.reset_peak_memory()
        if request.seed is not None:
            runtime.mx.random.seed(int(request.seed))

        progress(8, "échantillonnage de la vidéo")
        plan = self._echantillonner(vidéo, fps, max_frames)

        progress(20, f"description en cours ({plan['frames_sampled']} image(s))")
        début = time.monotonic()
        try:
            résultat = self._generer(consigne, plan, max_tokens, température)
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte utile
            raise WorkerError(f"description impossible : {type(exc).__name__}: {exc}") from exc
        calcul = time.monotonic() - début

        texte = getattr(résultat, "text", None)
        texte = (texte if texte is not None else str(résultat)).strip()
        jetons = int(getattr(résultat, "generation_tokens", 0) or 0)

        progress(92, "écriture")
        (request.output_dir / OUTPUT_TEXT).write_text(texte, encoding="utf-8")

        return InferResult(
            output={
                "text": OUTPUT_TEXT,
                "frames_sampled": plan["frames_sampled"],
                "sampled_fps": plan["sampled_fps"],
                "native_video": plan["native_video"],
                "tokens_generated": jetons,
                "finish_reason": _fin(résultat, jetons, max_tokens),
            },
            metrics={
                "characters": len(texte),
                "generation_tokens": jetons,
                "tokens_per_second": round(jetons / calcul, 2) if calcul > 0 else None,
                "frames_sampled": plan["frames_sampled"],
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

    # --- échantillonnage ------------------------------------------------------

    def _echantillonner(self, vidéo: Path, fps: float, max_frames: int) -> dict[str, Any]:
        """Décide ce que le modèle verra, et rend de quoi le dire au client.

        On appelle `sample_video_frames` et `subsample_evenly` directement, sans
        passer par `resolve_video_inputs` : celui-ci court-circuite le décodage
        dès que le processeur *déclare* savoir lire une vidéo, ce que Qwen3-VL
        fait sans que le résultat le montre (voir l'en-tête du module). Le
        raccourci était le défaut ; le contourner est la correction.
        """
        from mlx_vlm.generate.video import sample_video_frames, subsample_evenly

        images, fps_effectif = sample_video_frames([str(vidéo)], fps=fps)
        if not images:
            raise WorkerError(
                f"aucune image décodée depuis {vidéo.name} — fichier illisible ou vide"
            )
        retenues = subsample_evenly(images, max(2, max_frames))
        return {
            "images": retenues,
            "videos": [],
            "native_video": False,
            "frames_sampled": len(retenues),
            "sampled_fps": float(fps_effectif or fps),
        }

    def _generer(
        self,
        consigne: str,
        plan: dict[str, Any],
        max_tokens: int,
        température: float,
    ) -> Any:
        runtime = self._runtime
        assert runtime is not None
        images = plan["images"]
        invite = runtime.apply_chat_template(
            self._processor, self._config, consigne, num_images=len(images)
        )
        return runtime.generate(
            self._model,
            self._processor,
            invite,
            image=images,
            max_tokens=max_tokens,
            temperature=température,
            verbose=False,
        )

    def _processeur_gere_la_video(self) -> bool:
        try:
            from mlx_vlm.generate.video import processor_handles_video

            return bool(processor_handles_video(self._processor))
        except Exception:  # noqa: BLE001 — l'annonce du chargement ne fait pas échouer un job
            return False

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
    """« length » quand la réponse est tronquée, « stop » sinon."""
    brut = getattr(résultat, "finish_reason", None)
    if isinstance(brut, str) and brut.strip().lower() in ("length", "max_tokens"):
        return "length"
    return "length" if jetons >= max_tokens else "stop"


if __name__ == "__main__":
    raise SystemExit(main(MlxVlmVideoWorker))
