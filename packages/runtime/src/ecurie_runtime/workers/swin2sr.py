"""Adaptateur `torch-vision`, chemin **agrandissement** : Swin2SR sur PyTorch/MPS.

Il sert `image-upscale`, la capacité qui rend utilisable la sortie d'un
générateur travaillant en 1024. Swin2SR est natif dans `transformers`, donc
aucun code amont n'est exécuté depuis le dépôt de poids — contrairement au
détourage, qui lui en charge.

Deux particularités du modèle que le contrat doit absorber plutôt que subir :

- **le facteur d'agrandissement est celui des poids**, pas un réglage. Un jeu
  entraîné en ×4 ne sait pas faire ×2. Le contrat expose quand même `scale`,
  parce qu'un agrandisseur par diffusion, lui, l'honore ; ici le worker refuse
  explicitement un facteur qui n'est pas celui du variant, plutôt que de rendre
  une image d'une taille que personne n'a demandée ;
- **l'entrée doit être un multiple de la fenêtre d'attention** (8 pixels). On
  complète par réflexion avant, on recadre après. Sans cela, le modèle refuse
  les définitions impaires, qui sont la majorité des images réelles.
"""

from typing import Any

from ecurie_runtime.workers.base import (
    InferRequest,
    InferResult,
    ProgressFn,
    WorkerError,
    main,
)
from ecurie_runtime.workers.torch_vision import (
    REPAIR,
    TorchVisionWorker,
    import_pil,
    import_torch,
    resolve_image,
    weights_dir,
)

OUTPUT_IMAGE = "image.png"

FENETRE = 8  # taille de fenêtre de Swin2SR : l'entrée doit en être un multiple
DEFAULT_MAX_SIDE = 4096


class Swin2srWorker(TorchVisionWorker):
    """Agrandissement par transformeur Swin : plus de pixels, pas plus d'objets."""

    name = "swin2sr"

    def __init__(self) -> None:
        super().__init__()
        self.facteur: int = 0

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        torch = import_torch()
        self.torch = torch
        self.ensure_mps(torch)
        self.defaults = dict(variant.get("defaults") or {})
        self.options = dict(variant.get("options") or {})

        try:
            from transformers import Swin2SRForImageSuperResolution
        except ImportError as exc:
            raise WorkerError(
                f"Swin2SR indisponible dans cet environnement ({exc}) — `{REPAIR}`"
            ) from exc

        chemin = weights_dir(variant)
        try:
            modèle = Swin2SRForImageSuperResolution.from_pretrained(str(chemin))
        except Exception as exc:  # noqa: BLE001 — poids incomplets, config inattendue
            raise WorkerError(
                f"chargement de Swin2SR impossible : {type(exc).__name__}: {exc}"
            ) from exc

        self.model = modèle.to("mps").eval()
        # Le facteur vit dans la config des poids : c'est le modèle qui décide,
        # et le déclarer ici permet de refuser une demande incompatible **avant**
        # de calculer une image que l'utilisateur devra jeter.
        self.facteur = int(getattr(modèle.config, "upscale", 0) or 0)
        if self.facteur <= 0:
            raise WorkerError(
                "ces poids ne déclarent aucun facteur d'agrandissement "
                "(config.upscale absent) : impossible de savoir ce qu'ils produisent"
            )
        self.mps_counters()

        return {
            "scales": [self.facteur],
            "window": FENETRE,
            "versions": self.versions(),
        }

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.model is None or self.torch is None:
            raise WorkerError("modèle non chargé")
        torch = self.torch
        Image = import_pil()

        source = resolve_image(request.get("image"), request.output_dir)
        demandé = int(self.reglage(request, "scale", self.facteur))
        if demandé != self.facteur:
            raise WorkerError(
                f"ce variant agrandit d'un facteur {self.facteur}, pas {demandé} — "
                "le facteur est celui des poids, pas un réglage ; choisir le variant "
                "entraîné pour le facteur voulu"
            )
        plafond = int(self.reglage(request, "max_side", DEFAULT_MAX_SIDE))

        progress(10, "préparation")
        with Image.open(source) as ouverte:
            image = ouverte.convert("RGB")
        largeur, hauteur = image.size

        attendu = max(largeur, hauteur) * self.facteur
        if attendu > plafond:
            raise WorkerError(
                f"agrandir {largeur}×{hauteur} d'un facteur {self.facteur} donnerait "
                f"{largeur * self.facteur}×{hauteur * self.facteur}, au-delà du plafond "
                f"de {plafond} px demandé — relever `max_side`, ou réduire l'entrée"
            )

        entrée = self._tenseur(image, torch)
        rembourré, (haut_p, large_p) = _completer(entrée, torch)

        progress(35, "agrandissement")
        with torch.no_grad():
            sortie = self.model(pixel_values=rembourré)
        reconstruction = sortie.reconstruction
        torch.mps.synchronize()
        self.mps_counters()

        progress(78, "encodage PNG")
        # Le rembourrage a été agrandi lui aussi : on recadre à la taille utile,
        # sinon l'image porte une bordure réfléchie de quelques pixels.
        cible_h = hauteur * self.facteur
        cible_l = largeur * self.facteur
        reconstruction = reconstruction[:, :, :cible_h, :cible_l]
        agrandie = self._image(reconstruction, Image)
        agrandie.save(request.output_dir / OUTPUT_IMAGE, format="PNG")

        compteurs = self.mps_counters()
        self.torch.mps.empty_cache()

        return InferResult(
            output={
                "image": OUTPUT_IMAGE,
                "width": agrandie.width,
                "height": agrandie.height,
            },
            metrics={
                "input_width": largeur,
                "input_height": hauteur,
                "scale": self.facteur,
                "padded_height": haut_p,
                "padded_width": large_p,
                "output_pixels": agrandie.width * agrandie.height,
                **compteurs,
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    # --- détails -------------------------------------------------------------

    def _tenseur(self, image: Any, torch: Any) -> Any:
        import numpy as np

        tableau = np.asarray(image, dtype="float32") / 255.0
        tenseur = torch.from_numpy(tableau).permute(2, 0, 1).unsqueeze(0)
        return tenseur.to("mps")

    def _image(self, tenseur: Any, Image: Any) -> Any:
        import numpy as np

        tableau = tenseur.squeeze(0).clamp(0, 1).permute(1, 2, 0).float().cpu().numpy()
        return Image.fromarray((tableau * 255.0).round().astype(np.uint8), mode="RGB")


def _completer(tenseur: Any, torch: Any) -> tuple[Any, tuple[int, int]]:
    """Complète en bas et à droite jusqu'au multiple de fenêtre le plus proche.

    Par réflexion et non par du noir : une bordure noire crée un bord franc que
    le modèle agrandit consciencieusement, et le recadrage laisse alors une
    frange sombre sur deux côtés de l'image.
    """
    _, _, hauteur, largeur = tenseur.shape
    manque_h = (FENETRE - hauteur % FENETRE) % FENETRE
    manque_l = (FENETRE - largeur % FENETRE) % FENETRE
    if not manque_h and not manque_l:
        return tenseur, (hauteur, largeur)
    complété = torch.nn.functional.pad(
        tenseur, (0, manque_l, 0, manque_h), mode="reflect"
    )
    return complété, (hauteur + manque_h, largeur + manque_l)


if __name__ == "__main__":
    raise SystemExit(main(Swin2srWorker))
