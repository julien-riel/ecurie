"""Adaptateur Depth Anything 3 : la distance de chaque pixel, depuis une seule image.

Premier — et pour l'instant seul — adaptateur de `depth-estimation`, et premier
worker du parc à tourner sur l'environnement `depth-anything`. Son isolement
n'est pas une précaution de principe : le paquet d'amont impose `numpy<2`, quand
les trois modèles de `torch-vision` tournent sur numpy 2. Les faire cohabiter
aurait rétrogradé trois modèles mesurés pour en ajouter un quatrième.

**Ce que le modèle rend, et ce qu'il ne rend pas.** Une carte de profondeur
**relative** : deux photos de la même scène ne donnent pas la même unité, et
aucune n'est en mètres. Le contrat ne promet donc pas d'échelle métrique, et
`near`/`far` sont là pour que les valeurs normalisées du PNG redeviennent
interprétables — sans elles, l'image ne dit qu'un ordre.

**Trois sorties plutôt qu'une, et chacune répond à un défaut observé.** Le PNG
16 bits porte les valeurs, mais s'affiche noir dans presque toutes les
visionneuses : un résultat faux y passerait inaperçu. L'aperçu colorisé existe
pour qu'il se voie. Et la confiance dit où ne pas croire la profondeur — les
bords d'objets et les surfaces uniformes, qui sont précisément les endroits
qu'on regarde.

**Un mot sur OpenMP.** Le chargement lie deux copies du runtime OpenMP — celle
de torch et celle qu'apporte `pycolmap` — et la bibliothèque avorte le processus
plutôt que de continuer. `KMP_DUPLICATE_LIB_OK` est posé ici, avant tout import
lourd, parce qu'il n'y a pas d'autre issue : les deux paquets sont nécessaires
au chemin d'inférence, et aucun ne se recompile.

Rien de torch n'est importé au niveau du module (voir `workers/__init__.py`).
"""

import gc
import json
import os
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

DEPTH_NAME = "depth.png"
PREVIEW_NAME = "preview.png"
CONFIDENCE_NAME = "confidence.png"
INTRINSICS_NAME = "camera.json"

ENV_NAME = "depth-anything"
REPAIR = f"ecurie env sync {ENV_NAME}"

IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}

# Le modèle raisonne sur une grille carrée et rend sa sortie à cette taille. La
# remettre à l'échelle de l'image d'entrée inventerait des distances qui n'ont
# pas été estimées, ce que le contrat refuse explicitement.
DEFAULT_RES = 504

# Deux copies d'OpenMP dans le même processus : torch en apporte une, pycolmap
# l'autre. Sans ce drapeau, la bibliothèque avorte le processus au chargement.
# Posé avant l'import de torch, sinon il n'a plus d'effet.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")


class DepthAnythingWorker(Worker):
    """Profondeur monoculaire et paramètres de caméra, par Depth Anything 3."""

    name = "depth-anything"

    def __init__(self) -> None:
        self._torch: Any = None
        self._np: Any = None
        self._model: Any = None
        self._device = "cpu"
        self._defaults: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._peak_driver: int = 0

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        chemin = Path(str(variant.get("weights_path") or ""))
        if not chemin.is_dir():
            raise WorkerError(
                f"poids introuvables : {chemin} — le superviseur transmet un chemin local "
                f"déjà vérifié, un worker ne télécharge jamais ({REPAIR} si l'env est en cause)"
            )
        try:
            import numpy as np
            import torch
            from depth_anything_3.api import DepthAnything3
        except ImportError as exc:
            raise WorkerError(
                f"runtime depth-anything indisponible dans cet environnement ({exc}) — `{REPAIR}`"
            ) from exc

        self._defaults = dict(variant.get("defaults") or {})
        self._options = dict(variant.get("options") or {})
        self._torch = torch
        self._np = np
        # MPS quand il est là, sinon le processeur. Le modèle fait 411 M de
        # paramètres : sur CPU il tourne, mais l'écart se compte en dizaines de
        # secondes par image.
        self._device = "mps" if torch.backends.mps.is_available() else "cpu"

        modèle = DepthAnything3.from_pretrained(str(chemin))
        self._model = modèle.to(self._device).eval()
        return {"device": self._device, "versions": self._versions()}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._model is None:
            raise WorkerError("modèle non chargé")
        np = self._np
        from PIL import Image

        image_path = self._image(request)
        process_res = int(self._reglage(request, "process_res", DEFAULT_RES))
        palette = str(self._reglage(request, "colormap", "turbo"))

        progress(10, "estimation de la profondeur")
        with Image.open(image_path) as ouverte:
            tableau = np.array(ouverte.convert("RGB"))

        début = time.monotonic()
        try:
            with self._torch.no_grad():
                prédiction = self._model.inference(
                    [tableau],
                    # Chaîne vide et non `None` : l'amont fait `"gs" in export_format`
                    # sans vérifier, et `None` lève un TypeError qui ne parle de rien.
                    export_format="",
                    process_res=process_res,
                )
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte utile
            raise WorkerError(
                f"estimation impossible : {type(exc).__name__}: {exc}"
            ) from exc
        calcul = time.monotonic() - début
        # Relevé **ici**, et non seulement à la fin : c'est le seul moment où les
        # tampons du réseau sont encore alloués. Les post-traitements qui suivent
        # rendent la mémoire, et le pilote redescend avant qu'on ait rien lu.
        self._mps_counters()

        profondeur = np.asarray(prédiction.depth)[0]
        près, loin = float(profondeur.min()), float(profondeur.max())

        progress(80, "écriture des cartes")
        _ecrire_16bits(np, profondeur, près, loin, request.output_dir / DEPTH_NAME)
        _ecrire_apercu(np, profondeur, près, loin, palette, request.output_dir / PREVIEW_NAME)

        sortie: dict[str, Any] = {
            "depth": DEPTH_NAME,
            "preview": PREVIEW_NAME,
            "near": round(près, 6),
            "far": round(loin, 6),
        }

        confiance = getattr(prédiction, "conf", None)
        if confiance is not None:
            plan = np.asarray(confiance)[0]
            _ecrire_16bits(np, plan, float(plan.min()), float(plan.max()),
                           request.output_dir / CONFIDENCE_NAME)
            sortie["confidence"] = CONFIDENCE_NAME

        focale = _camera(np, prédiction, request.output_dir / INTRINSICS_NAME)
        if focale is not None:
            sortie["focal_length_px"] = round(focale, 3)
            sortie["intrinsics"] = INTRINSICS_NAME

        return InferResult(
            output=sortie,
            metrics={
                "process_res": process_res,
                "device": self._device,
                "infer_ms": int(calcul * 1000),
                # L'écart entre les deux bornes dit si la scène a de la
                # profondeur ou si elle est plate : une carte dont `near` et
                # `far` se touchent est le symptôme d'une image sans relief, ou
                # d'une estimation qui a échoué sans le dire.
                "depth_range": round(loin - près, 6),
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def unload(self) -> None:
        self._model = None
        gc.collect()
        if self._torch is not None and self._device == "mps":
            self._torch.mps.empty_cache()

    def peak_memory_bytes(self) -> int | None:
        """Le pic vu du pilote Metal, et non le RSS du processus.

        **Corrigé le 24 août 2026, et le profil committé avant cette date était
        faux d'un facteur 3,4.** Ce worker tourne sur MPS, et `ru_maxrss`
        n'impute pas les tampons Metal au processus : mesuré sur une
        reconstruction à trente-deux vues, le RSS restait figé à 3,75 Go pendant
        que le pilote en réservait 12,78. `diffusers_mps.py` documente ce piège
        depuis le v0.3 — celui-ci est tombé dedans quand même, et cela se lisait
        dans son profil : une pente de pic nulle avec un R² de 1,0, c'est-à-dire
        une consommation qui ne bouge pas quand l'entrée triple.

        Le chiffre du contrôle d'admission en dépend, donc le sous-déclarer est
        exactement l'OOM que tout ceci existe pour empêcher.
        """
        self._mps_counters()
        return max(self._peak_driver, peak_rss_bytes() or 0) or None

    def _mps_counters(self) -> dict[str, int]:
        """Relevé instantané, qui nourrit au passage le maximum retenu.

        `driver_allocated_memory()` redescend aussi vite qu'il monte : le
        maximum se tient à chaque relevé, il ne se lit pas une fois à la fin.
        """
        mps = getattr(self._torch, "mps", None) if self._torch is not None else None
        if mps is None:
            return {}
        try:
            compteurs = {
                "mps_current_allocated_bytes": int(mps.current_allocated_memory()),
                "mps_driver_allocated_bytes": int(mps.driver_allocated_memory()),
            }
        except (AttributeError, RuntimeError):
            return {}
        self._peak_driver = max(self._peak_driver, compteurs["mps_driver_allocated_bytes"])
        return compteurs

    # --- détails -------------------------------------------------------------

    def _image(self, request: InferRequest) -> Path:
        brut = str(request.get("image") or "").strip()
        if not brut:
            raise WorkerError("aucune image fournie")
        chemin = Path(brut).expanduser()
        if not chemin.is_absolute():
            chemin = request.output_dir / chemin
        if not chemin.is_file():
            raise WorkerError(f"image introuvable : {chemin}")
        if chemin.suffix.lower() not in IMAGES:
            raise WorkerError(
                f"format non géré : {chemin.suffix or '(sans extension)'} — "
                f"images acceptées : {', '.join(sorted(IMAGES))}"
            )
        return chemin

    def _reglage(self, request: InferRequest, nom: str, defaut: Any) -> Any:
        valeur = request.get(nom)
        if valeur is not None:
            return valeur
        for couche in (self._options, self._defaults):
            if couche.get(nom) is not None:
                return couche[nom]
        return defaut

    def _versions(self) -> dict[str, str]:
        versions = {}
        for nom, module in (("torch", "torch"), ("depth-anything-3", "depth_anything_3")):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


def normaliser(np: Any, plan: Any, près: float, loin: float) -> Any:
    """Ramène une carte entre 0 et 1. Une carte plate rend zéro plutôt que NaN.

    `loin - près` vaut zéro sur une image sans relief — un mur, un fond uni — et
    la division qui suivrait rendrait des NaN qu'un PNG écrit en noir : le
    symptôme aurait alors la même tête qu'une estimation ratée.
    """
    étendue = loin - près
    if étendue <= 0:
        return np.zeros_like(plan, dtype="float32")
    return ((plan - près) / étendue).astype("float32")


def _ecrire_16bits(np: Any, plan: Any, près: float, loin: float, cible: Path) -> None:
    """La carte en PNG 16 bits : 65 536 niveaux plutôt que 256.

    Huit bits suffiraient à regarder, pas à s'en servir : une profondeur
    quantifiée sur 256 niveaux fait apparaître des marches sur un dégradé doux,
    et c'est précisément ce qu'un fond de scène est.
    """
    from PIL import Image

    échelle = normaliser(np, plan, près, loin)
    Image.fromarray((échelle * 65535).astype("uint16"), mode="I;16").save(cible)


def _ecrire_apercu(np: Any, plan: Any, près: float, loin: float, palette: str, cible: Path) -> None:
    """La même carte, colorisée, pour qu'un résultat faux se voie."""
    from PIL import Image

    échelle = normaliser(np, plan, près, loin)
    if palette == "gris":
        Image.fromarray((échelle * 255).astype("uint8"), mode="L").save(cible)
        return
    Image.fromarray(_coloriser(np, échelle, palette), mode="RGB").save(cible)


def _coloriser(np: Any, échelle: Any, palette: str) -> Any:
    """Une palette lisible, sans dépendre de matplotlib.

    Les points d'ancrage suffisent : ce qu'on demande à un aperçu est de rendre
    un relief visible, pas de reproduire une palette au point près. En tirer une
    dépendance de plus pour ce seul usage serait cher payé.
    """
    ancres = {
        "turbo": ((0.19, 0.07, 0.23), (0.15, 0.66, 0.85), (0.55, 0.93, 0.35),
                  (0.97, 0.74, 0.15), (0.73, 0.13, 0.05)),
        "magma": ((0.00, 0.00, 0.02), (0.28, 0.06, 0.41), (0.68, 0.21, 0.42),
                  (0.98, 0.55, 0.38), (0.99, 0.99, 0.75)),
    }[palette if palette in ("turbo", "magma") else "turbo"]

    points = np.asarray(ancres, dtype="float32")
    position = np.clip(échelle, 0.0, 1.0) * (len(points) - 1)
    bas = np.floor(position).astype("int32")
    haut = np.clip(bas + 1, 0, len(points) - 1)
    reste = (position - bas)[..., None]
    couleurs = points[bas] * (1 - reste) + points[haut] * reste
    return (couleurs * 255).astype("uint8")


def _camera(np: Any, prédiction: Any, cible: Path) -> float | None:
    """Écrit les paramètres de caméra et rend la focale. None si le modèle n'en donne pas."""
    intrinsèques = getattr(prédiction, "intrinsics", None)
    if intrinsèques is None:
        return None
    matrice = np.asarray(intrinsèques)[0]
    extrinsèques = getattr(prédiction, "extrinsics", None)
    charge = {
        "intrinsics": matrice.tolist(),
        "extrinsics": np.asarray(extrinsèques)[0].tolist() if extrinsèques is not None else None,
        "note": (
            "Profondeur relative : ces paramètres situent la caméra dans un repère "
            "propre à cette image, sans échelle métrique."
        ),
    }
    cible.write_text(json.dumps(charge, ensure_ascii=False, indent=2), encoding="utf-8")
    return float(matrice[0][0])


if __name__ == "__main__":
    raise SystemExit(main(DepthAnythingWorker))
