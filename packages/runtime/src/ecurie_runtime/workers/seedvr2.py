"""Adaptateur SeedVR2 : agrandir une image par diffusion, sur `mflux`.

Second adaptateur de `image-upscale`, et le premier à ne pas être un réseau
convolutif. `workers.swin2sr` apprend un facteur fixe et le rend en une passe
déterministe : deux exécutions donnent le même pixel. Celui-ci est un modèle de
diffusion — il **régénère** les détails au lieu de les interpoler, ce qui donne
des textures là où l'autre lisse, et invente parfois ce qui n'était pas là.

Les deux ne se remplacent donc pas, et c'est le genre de choix qu'un contrat ne
tranche pas à la place de l'utilisateur : sur une photo, inventer un grain est
ce qu'on veut ; sur une capture d'écran, inventer un caractère est une faute.

**Le premier worker du parc à ne pas charger ses poids lui-même.** `mflux` a son
propre résolveur : on lui donne un nom de modèle, il va chercher ses fichiers
dans le cache Hugging Face. Le chemin que le superviseur transmet sert donc à
vérifier que les poids sont là — c'est la promesse du parc, un worker ne
télécharge jamais — mais ce n'est pas lui qu'on passe à la bibliothèque.

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

OUTPUT_IMAGE = "image.png"

ENV_NAME = "mflux"
REPAIR = f"ecurie env sync {ENV_NAME}"

IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}

# Ce que l'amont recommande comme point de départ, et ce que le manifeste peut
# corriger. Zéro désactive le pré-sous-échantillonnage.
DEFAULT_SOFTNESS = 0.0


class SeedVR2Worker(Worker):
    """Agrandissement par diffusion, sur le moteur de mflux."""

    name = "seedvr2"

    def __init__(self) -> None:
        self._mx: Any = None
        self._model: Any = None
        self._defaults: dict[str, Any] = {}
        self._options: dict[str, Any] = {}

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        chemin = Path(str(variant.get("weights_path") or ""))
        if not chemin.is_dir():
            raise WorkerError(
                f"poids introuvables : {chemin} — le superviseur transmet un chemin local "
                f"déjà vérifié, un worker ne télécharge jamais ({REPAIR} si l'env est en cause)"
            )
        try:
            import mlx.core as mx
            from mflux.models.common.config import ModelConfig
            from mflux.models.seedvr2 import SeedVR2
        except ImportError as exc:
            raise WorkerError(
                f"runtime mflux indisponible dans cet environnement ({exc}) — `{REPAIR}`"
            ) from exc

        self._defaults = dict(variant.get("defaults") or {})
        self._options = dict(variant.get("options") or {})
        self._mx = mx
        # mflux résout ses propres fichiers depuis le cache : on ne lui passe pas
        # le chemin, on s'est contenté de vérifier qu'il existe. Le jour où il
        # accepterait un chemin local, c'est ici que cela se brancherait.
        self._model = SeedVR2(model_config=ModelConfig.seedvr2_3b())
        return {"versions": self._versions()}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._model is None:
            raise WorkerError("modèle non chargé")
        from PIL import Image

        source = self._image(request)
        scale = self._reglage(request, "scale", 2)
        max_side = int(self._reglage(request, "max_side", 2048))
        softness = float(self._reglage(request, "softness", DEFAULT_SOFTNESS))
        graine = int(request.seed) if request.seed is not None else 0

        with Image.open(source) as ouverte:
            largeur, hauteur = ouverte.size
            transparente = ouverte.mode in ("RGBA", "LA", "P")
            if transparente:
                # Le modèle ne connaît pas l'alpha et l'aplatit sur du **noir**,
                # ce qui transforme un fond transparent en cadre sombre sans que
                # rien ne le signale — observé sur une image RGBA du banc. On
                # aplatit donc soi-même, sur blanc : c'est le fond que presque
                # tout le monde attend derrière une image détourée, et surtout
                # c'est écrit ici plutôt que subi trois couches plus bas.
                source = _aplatir(ouverte, request.output_dir)
        cible = resolution_cible(largeur, hauteur, scale, max_side)

        self._mx.reset_peak_memory()
        progress(15, f"agrandissement ×{scale} ({cible} px sur le petit côté)")
        début = time.monotonic()
        try:
            produite = self._model.generate_image(
                seed=graine,
                image_path=str(source),
                resolution=cible,
                softness=softness,
            )
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte utile
            raise WorkerError(
                f"agrandissement impossible : {type(exc).__name__}: {exc}"
            ) from exc
        calcul = time.monotonic() - début

        progress(90, "écriture")
        destination = request.output_dir / OUTPUT_IMAGE
        produite.save(str(destination))
        with Image.open(destination) as finie:
            sortie_l, sortie_h = finie.size

        return InferResult(
            output={"image": OUTPUT_IMAGE, "width": sortie_l, "height": sortie_h},
            metrics={
                "input_width": largeur,
                "input_height": hauteur,
                "scale": scale,
                "softness": softness,
                # Le facteur réellement obtenu, qui n'est pas toujours celui
                # demandé : `max_side` peut avoir mordu, et le modèle arrondit
                # sa grille. Le taire ferait passer un agrandissement bridé pour
                # celui qu'on avait demandé.
                "effective_scale": round(sortie_l / largeur, 3) if largeur else None,
                # Dit qu'une transparence a été aplatie : la sortie ne peut plus
                # la porter, et le silence ferait passer un fond inventé pour le
                # fond d'origine.
                "alpha_flattened": transparente,
                "infer_ms": int(calcul * 1000),
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def unload(self) -> None:
        self._model = None
        gc.collect()
        if self._mx is not None:
            self._mx.clear_cache()

    def peak_memory_bytes(self) -> int | None:
        if self._mx is None:
            return peak_rss_bytes()
        try:
            return int(self._mx.get_peak_memory())
        except Exception:  # noqa: BLE001 — une mesure ratée ne fait pas échouer un job
            return peak_rss_bytes()

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
        for nom, module in (("mlx", "mlx.core"), ("mflux", "mflux")):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


def _aplatir(image: Any, dossier: Path) -> Path:
    """Une image à canal alpha, posée sur du blanc, écrite à côté du job."""
    from PIL import Image

    fond = Image.new("RGB", image.size, (255, 255, 255))
    rgba = image.convert("RGBA")
    fond.paste(rgba, mask=rgba.split()[-1])
    cible = dossier / "entree-aplatie.png"
    fond.save(cible)
    return cible


def resolution_cible(largeur: int, hauteur: int, scale: Any, max_side: int) -> int:
    """Le petit côté visé, en pixels — ce que `generate_image` attend.

    Deux conventions se rencontrent ici et il faut les traduire. Le contrat parle
    d'un **facteur** (`scale`) et d'un plafond sur le **grand** côté
    (`max_side`) ; mflux, lui, attend une résolution absolue sur le **petit**
    côté. Confondre les deux sur une image en 16/9 donnerait un agrandissement
    de près du double de ce qui a été demandé.

    Le plafond mord sur le résultat, pas sur l'entrée : c'est la seule façon de
    garantir que `max_side` veut dire quelque chose. Le facteur effectivement
    obtenu est rapporté dans les métriques, faute de quoi un agrandissement bridé
    passerait pour celui qu'on avait demandé.
    """
    petit, grand = (largeur, hauteur) if largeur <= hauteur else (hauteur, largeur)
    if petit <= 0 or grand <= 0:
        raise WorkerError("image de taille nulle")
    facteur = float(scale) if scale else 2.0
    if facteur <= 0:
        raise WorkerError(f"facteur d'agrandissement invalide : {scale!r}")
    if max_side > 0 and grand * facteur > max_side:
        facteur = max_side / grand
    return max(1, round(petit * facteur))


if __name__ == "__main__":
    raise SystemExit(main(SeedVR2Worker))
