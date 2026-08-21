"""Adaptateur `torch-vision`, chemin **détourage** : BiRefNet sur PyTorch/MPS.

Il sert la capacité `image-matting`, c'est-à-dire le chaînon qui manque entre une
photo et une reconstruction 3D : le pipeline image vers maillage recadre sur le
canal alpha, et une image opaque le prive de sa seule indication de silhouette.

**Ce modèle charge du code depuis son dépôt de poids.** BiRefNet n'est pas une
architecture de `transformers` : son `config.json` déclare un `auto_map`, et
`from_pretrained` exécute le `birefnet.py` livré avec les poids. Cela ne se fait
qu'avec `trust_remote_code=True`, qui est ici un choix explicite et pas un défaut
subi : les poids arrivent par `ecurie pull` à une révision épinglée, la même que
celle du manifeste, dans un environnement isolé où rien d'Écurie n'est installé.
Changer la révision épinglée revient donc à relire du code, et c'est pour cela
que le registre l'exige.

Rien de torch n'est importé au niveau du module (voir `workers/__init__.py`).
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
OUTPUT_MASK = "mask.png"

# BiRefNet est entraîné sur des entrées carrées de 1024 : c'est la définition à
# laquelle il donne son meilleur masque. Le contrat laisse la régler parce que
# c'est elle, et elle seule, qui pilote le coût mémoire.
DEFAULT_SIDE = 1024

# Normalisation ImageNet, celle du prétraitement amont. Écrite ici plutôt que
# lue d'un processeur : le dépôt de BiRefNet n'en publie pas, et deviner une
# normalisation donne un masque plausible et faux.
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)


class BiRefNetWorker(TorchVisionWorker):
    """Détourage : un masque alpha continu, et l'image découpée avec."""

    name = "birefnet"

    def __init__(self) -> None:
        super().__init__()
        self.dtype: Any = None

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        torch = import_torch()
        self.torch = torch
        self.ensure_mps(torch)
        self.defaults = dict(variant.get("defaults") or {})
        self.options = dict(variant.get("options") or {})

        try:
            from transformers import AutoModelForImageSegmentation
        except ImportError as exc:
            raise WorkerError(f"transformers absent ({exc}) — `{REPAIR}`") from exc

        chemin = weights_dir(variant)
        try:
            modèle = AutoModelForImageSegmentation.from_pretrained(
                str(chemin), trust_remote_code=True
            )
        except Exception as exc:  # noqa: BLE001 — code amont : le message importe plus que le type
            raise WorkerError(
                f"chargement de BiRefNet impossible : {type(exc).__name__}: {exc}. "
                "Ce modèle exécute le code livré avec ses poids ; une dépendance "
                f"manquante s'y voit ici — `{REPAIR}`"
            ) from exc

        modèle = modèle.to("mps").eval()
        self.model = modèle
        # La précision est celle des poids publiés, pas un choix de l'adaptateur.
        # BiRefNet est distribué en demi-précision : imposer float32 à l'entrée
        # donne « Input type (float) and bias type (c10::Half) should be the
        # same » au premier appel, et convertir le modèle doublerait sa mémoire
        # pour une précision que l'amont n'a pas jugée nécessaire. On lit donc
        # le type des paramètres et on s'y aligne.
        self.dtype = _dtype_du_modele(modèle, torch)
        self.mps_counters()

        return {
            "default_side": DEFAULT_SIDE,
            "dtype": str(self.dtype).replace("torch.", ""),
            "versions": self.versions(),
        }

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.model is None or self.torch is None:
            raise WorkerError("modèle non chargé")
        torch = self.torch
        Image = import_pil()

        source = resolve_image(request.get("image"), request.output_dir)
        côté = int(self.reglage(request, "max_side", DEFAULT_SIDE))
        seuil = self.reglage(request, "threshold", None)
        affiner = bool(self.reglage(request, "edge_refine", True))

        progress(10, "préparation")
        with Image.open(source) as ouverte:
            image = ouverte.convert("RGB")
        largeur, hauteur = image.size

        entrée = self._tenseur(image.resize((côté, côté), Image.BILINEAR), torch)

        progress(35, "détourage")
        with torch.no_grad():
            sorties = self.model(entrée)
        # Le code amont rend une liste de cartes à résolutions croissantes ; la
        # dernière est la plus fine. Prendre la première donnerait un masque de
        # 32 pixels de côté remis à l'échelle, et il aurait l'air presque juste.
        brut = sorties[-1] if isinstance(sorties, (list, tuple)) else sorties
        if isinstance(brut, (list, tuple)):
            brut = brut[-1]
        masque = brut.sigmoid().float().cpu()
        torch.mps.synchronize()
        self.mps_counters()

        progress(70, "composition")
        tableau = masque[0].squeeze().numpy()
        alpha = Image.fromarray((tableau * 255).astype("uint8"), mode="L")
        # Retour à la définition d'origine : c'est l'image de l'utilisateur qu'on
        # détoure, pas la vignette carrée soumise au modèle.
        resample = Image.LANCZOS if affiner else Image.BILINEAR
        alpha = alpha.resize((largeur, hauteur), resample)

        if seuil is not None:
            limite = int(max(0.0, min(1.0, float(seuil))) * 255)
            alpha = alpha.point(lambda v, limite=limite: 255 if v >= limite else 0)

        découpée = image.convert("RGBA")
        découpée.putalpha(alpha)
        découpée.save(request.output_dir / OUTPUT_IMAGE, format="PNG")
        alpha.save(request.output_dir / OUTPUT_MASK, format="PNG")

        couverture = _couverture(alpha)
        compteurs = self.mps_counters()
        self.torch.mps.empty_cache()

        return InferResult(
            output={
                "image": OUTPUT_IMAGE,
                "mask": OUTPUT_MASK,
                "coverage": couverture,
            },
            metrics={
                "input_width": largeur,
                "input_height": hauteur,
                "model_side": côté,
                "coverage": couverture,
                "thresholded": seuil is not None,
                "dtype": str(self.dtype).replace("torch.", ""),
                **compteurs,
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    # --- détails -------------------------------------------------------------

    def _tenseur(self, image: Any, torch: Any) -> Any:
        """Image PIL → tenseur normalisé (1, 3, H, W) sur MPS, au type du modèle."""
        import numpy as np

        tableau = np.asarray(image, dtype="float32") / 255.0
        tableau = (tableau - np.array(MEAN, dtype="float32")) / np.array(STD, dtype="float32")
        tenseur = torch.from_numpy(tableau).permute(2, 0, 1).unsqueeze(0)
        return tenseur.to("mps", dtype=self.dtype or torch.float32)


def _dtype_du_modele(modèle: Any, torch: Any) -> Any:
    """Le type des paramètres, ou float32 si le modèle n'en a aucun à montrer."""
    for paramètre in modèle.parameters():
        return paramètre.dtype
    return torch.float32


def _couverture(alpha: Any) -> float:
    """Part de l'image occupée par le sujet, entre 0 et 1.

    Un détourage qui rend 0 ou 1 n'a rien isolé : le chiffre le dit sans qu'on
    ouvre le fichier, et c'est la métrique la moins chère pour repérer un modèle
    qui a rendu un masque vide sans échouer.
    """
    histogramme = alpha.histogram()
    total = sum(histogramme)
    if not total:
        return 0.0
    somme = sum(valeur * compte for valeur, compte in enumerate(histogramme))
    return round(somme / (255.0 * total), 4)


if __name__ == "__main__":
    raise SystemExit(main(BiRefNetWorker))
