"""Adaptateur SAM 3 : segmenter ce qu'on **nomme**, pas ce qu'on montre.

Deuxième adaptateur de `image-segment`, et le premier à ne pas suivre un clic.
`workers.sam2` reçoit un point ou une boîte et rend le contour de ce qui se
trouve dessous ; celui-ci reçoit un mot — « le chien », « les panneaux » — et
rend une instance par objet qui lui ressemble. C'est la même capacité au sens du
contrat : une image entre, un masque sort, et c'est l'utilisateur qui désigne.
Seule la façon de désigner change, et le contrat a gagné un `prompt` pour elle.

**L'invite textuelle n'est pas facultative ici**, et c'est la principale
différence à connaître. `Sam3Predictor.predict` l'exige ; sans elle il n'y a
rien à chercher. Un job sans `prompt` est donc refusé avec la phrase qui le dit,
plutôt que servi par un mot générique qui ramasserait n'importe quoi et
rendrait un masque plausible — le pire des deux, puisque personne ne verrait
qu'il n'a pas été demandé.

**Un mot ramasse toutes les instances.** Là où SAM 2 propose l'objet, sa partie
et sa sous-partie — trois lectures d'un même clic —, SAM 3 rend N objets
distincts. Les deux remplissent `candidates`, mais ce que la liste contient n'est
pas la même chose, et le champ `instances` des métriques le dit : deux masques
chez SAM 2 sont deux hypothèses, deux masques ici sont deux chiens.

Rien de mlx n'est importé au niveau du module (voir `workers/__init__.py`).

Écrit contre `mlx-vlm` 0.6.15, dont `models/sam3/` porte l'encodeur de texte, le
décodeur de masques et un traqueur vidéo. Seul le chemin image est employé : le
suivi suppose une capacité que le registre ne déclare pas.
"""

import gc
import json
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
from ecurie_runtime.workers.mlx_vlm import IMAGES, REPAIR, resolve_document

MASK_NAME = "mask.png"
OVERLAY_NAME = "overlay.png"
CANDIDATES_NAME = "candidates.json"

# Sous ce score, une instance est écartée. Le défaut est celui du prédicteur ;
# un variant qui connaît le sien l'écrit dans ses `options`.
SCORE_MINIMUM = 0.5


class Sam3Worker(Worker):
    """Segmentation par concept, sur le moteur de mlx-vlm."""

    name = "sam3"

    def __init__(self) -> None:
        self._mx: Any = None
        self._predictor: Any = None
        self._defaults: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._seuil = SCORE_MINIMUM
        self._peak_load = 0

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        chemin = Path(str(variant.get("weights_path") or ""))
        if not chemin.is_dir():
            raise WorkerError(
                f"poids introuvables : {chemin} — le superviseur transmet un chemin local "
                f"déjà vérifié, un worker ne télécharge jamais ({REPAIR} si l'env est en cause)"
            )
        try:
            import mlx.core as mx
            from mlx_vlm import load
            from mlx_vlm.models.sam3.generate import Sam3Predictor
        except ImportError as exc:
            raise WorkerError(
                f"runtime mlx-vlm indisponible ou sans SAM 3 ({exc}) — `{REPAIR}`"
            ) from exc

        self._defaults = dict(variant.get("defaults") or {})
        self._options = dict(variant.get("options") or {})
        self._seuil = float(self._options.get("score_threshold", SCORE_MINIMUM))

        model, processor = load(str(chemin))
        self._mx = mx
        self._predictor = Sam3Predictor(model, processor, score_threshold=self._seuil)
        self._peak_load = self._pic_mlx() or 0
        return {"score_threshold": self._seuil, "versions": self._versions()}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._predictor is None:
            raise WorkerError("modèle non chargé")

        concept = str(request.get("prompt") or "").strip()
        if not concept:
            raise WorkerError(
                "« prompt » est obligatoire pour SAM 3 : il segmente ce qu'on lui nomme, "
                "pas ce qu'on lui montre. Pour désigner d'un point ou d'une boîte, c'est "
                "sam2-hiera-small qui sert cette capacité."
            )
        # PIL après les gardes d'entrée, et non avant : un job refusé pour une
        # raison qu'on connaît déjà ne doit pas dépendre d'un import. La règle
        # vaut au-delà du confort — l'environnement d'Écurie n'a pas PIL, seul
        # celui du runtime l'a, et une garde qui lèverait `ModuleNotFoundError`
        # au lieu de sa propre phrase serait indéchiffrable.
        from PIL import Image

        image_path = self._image(request)
        seuil = float(self._reglage(request, "score_threshold", self._seuil))
        max_side = int(self._reglage(request, "max_side", 1024))

        with Image.open(image_path) as ouverte:
            image = ouverte.convert("RGB")
            image = _reduire(image, max_side)
            largeur, hauteur = image.size

            self._mx.reset_peak_memory()
            progress(15, f"recherche de « {concept} »")
            début = time.monotonic()
            try:
                détection = self._predictor.predict(image, concept, score_threshold=seuil)
            except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte utile
                raise WorkerError(
                    f"segmentation impossible : {type(exc).__name__}: {exc}"
                ) from exc
            calcul = time.monotonic() - début

            masques = getattr(détection, "masks", None)
            # `or []` serait le réflexe, et il lève : la vérité d'un tableau
            # numpy vide est ambiguë, et le prédicteur rend des tableaux, pas des
            # listes. Le cas se produit exactement quand le concept est absent de
            # l'image — c'est-à-dire souvent.
            bruts = getattr(détection, "scores", None)
            scores = [float(s) for s in bruts] if bruts is not None and len(bruts) else []
            if masques is None or len(scores) == 0:
                # Un concept absent de l'image n'est pas une panne : c'est une
                # réponse, et elle doit se lire comme telle plutôt que comme un
                # job raté. Le masque vide le dit, l'overlay le montre.
                progress(90, "aucune instance trouvée")
                _masque_vide(largeur, hauteur, request.output_dir / MASK_NAME)
                self._overlay(image, request.output_dir / MASK_NAME, request.output_dir)
                (request.output_dir / CANDIDATES_NAME).write_text("[]", encoding="utf-8")
                return InferResult(
                    output={
                        "mask": MASK_NAME,
                        "overlay": OVERLAY_NAME,
                        "candidates": CANDIDATES_NAME,
                        "score": 0.0,
                        "coverage": 0.0,
                    },
                    metrics=self._metriques(concept, 0, seuil, calcul),
                )

            progress(80, "écriture des masques")
            ordre = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            meilleur = ordre[0]
            couverture = _ecrire_masque(masques[meilleur], request.output_dir / MASK_NAME)

            candidats = []
            for rang, index in enumerate(ordre[1:], start=1):
                nom = f"candidate-{rang}.png"
                _ecrire_masque(masques[index], request.output_dir / nom)
                candidats.append({"mask": nom, "score": round(scores[index], 4)})
            (request.output_dir / CANDIDATES_NAME).write_text(
                json.dumps(candidats, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            progress(92, "surimpression")
            self._overlay(image, request.output_dir / MASK_NAME, request.output_dir)

        return InferResult(
            output={
                "mask": MASK_NAME,
                "overlay": OVERLAY_NAME,
                "candidates": CANDIDATES_NAME,
                "score": round(scores[meilleur], 4),
                "coverage": round(couverture, 4),
            },
            metrics=self._metriques(concept, len(scores), seuil, calcul),
        )

    def unload(self) -> None:
        self._predictor = None
        self._peak_load = 0
        gc.collect()
        if self._mx is not None:
            self._mx.clear_cache()

    def peak_memory_bytes(self) -> int | None:
        pic = self._pic_mlx()
        if pic is None:
            return peak_rss_bytes()
        return max(pic, self._peak_load)

    # --- détails -------------------------------------------------------------

    def _metriques(self, concept: str, instances: int, seuil: float, calcul: float) -> dict:
        return {
            # « instances » et non « masks_proposed » comme chez SAM 2 : deux
            # masques là-bas sont deux lectures d'un même clic, deux masques ici
            # sont deux objets. Le même nom pour les deux tromperait la
            # comparaison que le golden set fera entre eux.
            "instances": instances,
            "prompt": concept,
            "score_threshold": seuil,
            "infer_ms": int(calcul * 1000),
            "peak_memory_bytes": self.peak_memory_bytes(),
        }

    def _image(self, request: InferRequest) -> Path:
        chemin = resolve_document(request.get("image"), request.output_dir)
        if chemin.suffix.lower() not in IMAGES:
            raise WorkerError(
                f"format non géré : {chemin.suffix or '(sans extension)'} — "
                f"images acceptées : {', '.join(sorted(IMAGES))}"
            )
        return chemin

    def _overlay(self, image: Any, masque_path: Path, dossier: Path) -> None:
        """Le masque teinté sur l'image : un contour qui a mordu ne se voit que là."""
        from PIL import Image

        with Image.open(masque_path) as ouvert:
            masque = ouvert.convert("L").resize(image.size, Image.NEAREST)
        teinte = Image.new("RGB", image.size, (220, 40, 40))
        Image.composite(Image.blend(image, teinte, 0.45), image, masque).save(
            dossier / OVERLAY_NAME
        )

    def _reglage(self, request: InferRequest, nom: str, defaut: Any) -> Any:
        valeur = request.get(nom)
        if valeur is not None:
            return valeur
        for couche in (self._options, self._defaults):
            if couche.get(nom) is not None:
                return couche[nom]
        return defaut

    def _pic_mlx(self) -> int | None:
        if self._mx is None:
            return None
        try:
            return int(self._mx.get_peak_memory())
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


def _reduire(image: Any, max_side: int, filtre: Any = None) -> Any:
    """Ramène le plus grand côté sous la borne, en gardant les proportions.

    Le contrat expose `max_side` parce que le coût d'une segmentation suit la
    surface : doubler le côté quadruple le travail, pour un contour qui ne gagne
    presque rien passé le millier de pixels.
    """
    plus_grand = max(image.size)
    if max_side <= 0 or plus_grand <= max_side:
        return image

    if filtre is None:
        # Résolu ici et pas au module : cette fonction est du calcul pur à un
        # appel près, et l'environnement d'Écurie — celui des tests — n'a pas
        # PIL. Le paramètre existe pour qu'ils puissent l'éprouver sans lui.
        from PIL import Image as PILImage

        filtre = PILImage.LANCZOS

    facteur = max_side / plus_grand
    # Le plancher à 1 n'est pas théorique : une image très allongée arrondit son
    # petit côté à zéro, et PIL lève sur une taille nulle.
    taille = (max(1, round(image.width * facteur)), max(1, round(image.height * facteur)))
    return image.resize(taille, filtre)


def _ecrire_masque(masque: Any, cible: Path) -> float:
    """Écrit un masque binaire et rend la part de l'image qu'il couvre."""
    import numpy as np
    from PIL import Image

    plan = np.asarray(masque)
    while plan.ndim > 2:
        plan = plan[0]
    binaire = plan > 0
    Image.fromarray((binaire * 255).astype("uint8"), mode="L").save(cible)
    return float(binaire.mean())


def _masque_vide(largeur: int, hauteur: int, cible: Path) -> None:
    from PIL import Image

    Image.new("L", (largeur, hauteur), 0).save(cible)


if __name__ == "__main__":
    raise SystemExit(main(Sam3Worker))
