"""Adaptateur mlx-vlm, chemin **détection** : où sont les choses, pas ce qu'on en dit.

Quatrième emploi des mêmes poids que `workers.mlx_vlm` (lecture de document),
`workers.mlx_vlm_describe` (description) et `workers.mlx_vlm_video` (vidéo).
Module distinct pour la raison habituelle, et ici elle est plus forte
qu'ailleurs : les trois autres rendent du texte, celui-ci rend une **structure**,
et tout le travail de l'adaptateur est de la faire exister.

**La convention de coordonnées est mesurée, pas supposée.** Le modèle rend des
boîtes sur une grille normalisée 0–1000, quelle que soit la taille de l'image.
Constaté sur une scène dont les positions sont connues par fabrication : les
centres tombent juste après multiplication par `côté / 1000`, et une boîte y
dépassait 768 sur une image de 768 pixels — ce qui suffit à écarter l'hypothèse
des pixels absolus. Un client qui recevrait ces millièmes croirait recevoir des
pixels et tracerait ses cadres au tiers de leur place ; la conversion est donc
faite ici, une fois, et le contrat promet des pixels absolus.

**Un modèle de langue ne rend pas du JSON, il rend du texte qui y ressemble.**
Blocs ``` autour, virgule finale, clé manquante, boîte à trois nombres : ce sont
les formes qu'on a vues, et l'analyse les traverse sans faire échouer le job.
Une détection qui perd un objet mal formé vaut mieux qu'un job perdu pour un
objet mal formé — mais ce qui est écarté est **compté**, sinon l'écart entre ce
que le modèle a dit et ce que le client reçoit ne se voit nulle part.

**Il n'y a pas de score de confiance, et c'est délibéré.** Un VLM en produit un
si on le lui demande, et il est décoratif : rien dans son entraînement ne le
calibre. Le contrat n'en déclare pas.

Rien de mlx n'est importé au niveau du module (voir `workers/__init__.py`).
"""

import gc
import json
import re
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
from ecurie_runtime.workers.mlx_vlm import REPAIR, Runtime, import_runtime
from ecurie_runtime.workers.mlx_vlm_describe import resolve_image

OUTPUT_JSON = "objects.json"
OUTPUT_OVERLAY = "overlay.png"

# La grille sur laquelle le modèle raisonne. Ce n'est pas un réglage : c'est un
# fait mesuré sur ces poids, et il change avec la famille de modèles — d'où sa
# place ici, dans l'adaptateur, et non dans le contrat.
GRILLE = 1000.0

CONSIGNE = (
    "Localise dans l'image ce qui est demandé. Réponds **uniquement** par un "
    "tableau JSON, sans texte autour et sans bloc de code, de la forme "
    '[{"label": "nom", "bbox": [x1, y1, x2, y2]}]. Les coordonnées vont de 0 à '
    "1000. Si tu ne trouves rien, réponds []."
)


def build_prompt(targets: str | None, max_objects: int) -> str:
    """La consigne, composée depuis les champs du contrat."""
    quoi = (targets or "").strip()
    demande = (
        f"Cherche : {quoi}."
        if quoi
        else "Cherche les objets principaux, et nomme-les toi-même."
    )
    return f"{demande}\n\n{CONSIGNE} Rends au plus {max_objects} objets."


def extraire_json(texte: str) -> list[Any]:
    """Le tableau JSON d'une réponse de modèle, quoi qu'il y ait autour.

    Trois formes vues en pratique : le tableau nu, le tableau dans un bloc de
    code, et le tableau précédé d'une phrase. On prend le premier crochet
    ouvrant et le dernier fermant — un modèle qui bavarde après le tableau ne
    doit pas coûter la détection.
    """
    nettoyé = re.sub(r"^\s*```(?:json)?|```\s*$", "", texte.strip(), flags=re.M).strip()
    début, fin = nettoyé.find("["), nettoyé.rfind("]")
    if début < 0 or fin <= début:
        return []
    try:
        valeur = json.loads(nettoyé[début : fin + 1])
    except json.JSONDecodeError:
        return []
    return valeur if isinstance(valeur, list) else []


def convertir(
    brut: list[Any], largeur: int, hauteur: int, max_objects: int
) -> tuple[list[dict], int]:
    """Millièmes du modèle → pixels de l'image fournie. Rend aussi les écartés.

    L'écrêtage aux bornes n'est pas de la coquetterie : le modèle rend
    régulièrement une boîte qui déborde de la grille, et une coordonnée négative
    ferait échouer le tracé bien après, dans un composant qui n'y peut rien.
    """
    objets: list[dict] = []
    écartés = 0
    for entrée in brut:
        boîte = entrée.get("bbox") if isinstance(entrée, dict) else None
        if not isinstance(boîte, (list, tuple)) or len(boîte) != 4:
            écartés += 1
            continue
        try:
            x1, y1, x2, y2 = (float(v) for v in boîte)
        except (TypeError, ValueError):
            écartés += 1
            continue
        px = [
            max(0, min(largeur, round(x1 * largeur / GRILLE))),
            max(0, min(hauteur, round(y1 * hauteur / GRILLE))),
            max(0, min(largeur, round(x2 * largeur / GRILLE))),
            max(0, min(hauteur, round(y2 * hauteur / GRILLE))),
        ]
        if px[2] <= px[0] or px[3] <= px[1]:
            écartés += 1  # boîte vide ou inversée : rien à montrer
            continue
        libellé = str(entrée.get("label") or entrée.get("name") or "objet").strip()
        objets.append({"label": libellé or "objet", "bbox": px})
        if len(objets) >= max_objects:
            break
    return objets, écartés


def tracer(image_path: Path, objets: list[dict], cible: Path) -> None:
    """Les boîtes sur l'image d'origine. Une liste de nombres ne se relit pas."""
    from PIL import Image, ImageDraw

    with Image.open(image_path) as source:
        vue = source.convert("RGB")
    dessin = ImageDraw.Draw(vue)
    épaisseur = max(2, vue.width // 300)
    for objet in objets:
        x1, y1, x2, y2 = objet["bbox"]
        dessin.rectangle([x1, y1, x2, y2], outline=(220, 40, 40), width=épaisseur)
        dessin.text((x1 + épaisseur, y1 + épaisseur), objet["label"], fill=(220, 40, 40))
    vue.save(cible)


class MlxVlmDetectWorker(Worker):
    """Détection et ancrage d'objets, par modèle vision-langage."""

    name = "mlx-vlm-detect"

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

        return {"grid": GRILLE, "versions": self._versions()}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._runtime is None or self._model is None:
            raise WorkerError("modèle non chargé")
        runtime = self._runtime

        image = resolve_image(request.get("image"), request.output_dir)
        targets = self._reglage(request, "targets", None)
        max_objects = int(self._reglage(request, "max_objects", 50))
        max_tokens = int(self._reglage(request, "max_tokens", 1024))
        température = float(self._reglage(request, "temperature", 0.0))

        from PIL import Image as PILImage

        with PILImage.open(image) as ouverte:
            largeur, hauteur = ouverte.size

        runtime.mx.reset_peak_memory()
        if request.seed is not None:
            runtime.mx.random.seed(int(request.seed))

        progress(10, "détection en cours")
        début = time.monotonic()
        invite = runtime.apply_chat_template(
            self._processor, self._config, build_prompt(targets, max_objects), num_images=1
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
            raise WorkerError(f"détection impossible : {type(exc).__name__}: {exc}") from exc
        calcul = time.monotonic() - début

        texte = getattr(résultat, "text", None)
        texte = (texte if texte is not None else str(résultat)).strip()
        jetons = int(getattr(résultat, "generation_tokens", 0) or 0)

        progress(85, "mise en pixels")
        objets, écartés = convertir(extraire_json(texte), largeur, hauteur, max_objects)

        (request.output_dir / OUTPUT_JSON).write_text(
            json.dumps(objets, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        sortie: dict[str, Any] = {"objects": OUTPUT_JSON, "count": len(objets)}
        if objets:
            progress(92, "tracé des boîtes")
            tracer(image, objets, request.output_dir / OUTPUT_OVERLAY)
            sortie["overlay"] = OUTPUT_OVERLAY

        return InferResult(
            output=sortie,
            metrics={
                "objects": len(objets),
                "discarded": écartés,
                "image_width": largeur,
                "image_height": hauteur,
                "generation_tokens": jetons,
                "tokens_per_second": round(jetons / calcul, 2) if calcul > 0 else None,
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


if __name__ == "__main__":
    raise SystemExit(main(MlxVlmDetectWorker))
