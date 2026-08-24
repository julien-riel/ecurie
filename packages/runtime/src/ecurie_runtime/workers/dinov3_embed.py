"""Adaptateur `torch-vision`, chemin **empreinte visuelle** : DINOv3 et DINOv2 sur timm.

Le patron de `uniface_embed` transposé à une image entière, et le seul écart tient
en un mot : **rien n'est redressé**. La reconnaissance faciale doit son pouvoir à
l'alignement sur cinq points d'ancrage, qui fait qu'une même personne de face et
de trois quarts donne deux vecteurs proches. Une pièce mécanique n'a pas cinq
points d'ancrage : ici l'image entre telle quelle, et ce qui est au bord compte
autant que ce qui est au centre.

Le module s'appelle `dinov3_embed` et sert aussi `dinov2` : les deux familles
arrivent par le miroir timm, se chargent par le même appel et ne diffèrent que
par ce que `forward_features` rend — une carte de traits pour la ConvNeXt, une
suite de jetons pour le ViT. Un second fichier qui n'aurait différé que par une
constante n'aurait rien séparé.

**Trois décisions valent d'être écrites, parce qu'aucune n'est un réglage.**

*Le fond composé sous l'alpha.* `Image.open(p).convert("RGB")` est un piège
actif : `bench/assets/cube.png` est transparent sur 70,5 % de sa surface et ses
canaux RGB y valent exactement zéro, si bien que `convert` sert la silhouette sur
fond **noir**. Mesuré à 256 pixels sur ce chemin-ci, les deux lectures du même
fichier donnent 0,9254 de cosinus chez `dinov3@convnext-small` et 0,8148 chez
`dinov2@vit-base` — quand le cube et la sphère sont à 0,6856 et 0,5560. La façon
de lire le fichier pèse le quart, puis les deux cinquièmes, de ce qui sépare deux
objets. On compose donc sur un fond fixé, et le document de sortie le nomme.

*L'agrégation des jetons.* CLS ou moyenne des patches, ce sont deux espaces
vectoriels et non deux réglages : sur les poids de `dinov2@vit-base`, la requête
« cube » de l'épreuve des trois solides tombe du rang 2 au rang 6 quand on passe
de l'un à l'autre. Le choix appartient donc au variant
(`options.pooling`) et non au contrat, et il est écrit dans le document. Les ViT
DINOv2 et DINOv3 portent `num_prefix_tokens = 5` — un CLS et **quatre
registres** — que l'adaptateur lit sur le modèle : une moyenne naïve sur
`forward_features` mélangerait les registres aux patches et rendrait un vecteur
qui n'est celui d'aucun espace. Une ConvNeXt n'a ni CLS ni registres ; on lui
demande `cls` et l'adaptateur refuse, plutôt que de servir autre chose sous ce
nom.

*La définition soumise.* `max_side` est ramené au pas du réseau — 32 pour la
ConvNeXt, 14 pour un ViT patch 14 — parce que 256 pixels ne veulent pas dire la
même chose des deux côtés. Le document porte la définition **réellement**
soumise, pas celle demandée.

Rien de torch ni de timm n'est importé au niveau du module (voir `workers/__init__.py`).
"""

import json
import math
from pathlib import Path
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

SORTIE_JSON = "embedding.json"

# Fond opaque composé sous le canal alpha. Blanc et non gris : c'est la valeur
# qui laisse une silhouette claire lisible, et surtout c'est une constante du
# module et non un paramètre — deux vecteurs produits sur deux fonds différents
# ne sont pas comparables, et un paramètre l'aurait laissé arriver.
FOND = (255, 255, 255)
FOND_NOM = "#FFFFFF"

# Le défaut du contrat, repris ici pour le cas où un variant n'en déclare aucun.
DEFAUT_COTE = 256

# Pas d'alignement de repli, quand le modèle ne dit pas le sien. 32 est la
# réduction des familles convolutives du parc ; un ViT annonce le sien par
# `patch_embed.patch_size` et n'y tombe jamais.
PAS_DE_REPLI = 32

AGREGATIONS = ("mean", "cls")


class Dinov3EmbedWorker(TorchVisionWorker):
    """Image entière vers un vecteur — l'empreinte visuelle du parc."""

    name = "dinov3-embed"

    def __init__(self) -> None:
        super().__init__()
        self.cfg: dict[str, Any] = {}
        self.identite: dict[str, Any] = {}
        self.pooling: str = "mean"
        self.pas: int = PAS_DE_REPLI
        self.prefixe: int = 0
        self.jetons: bool = False
        self.dimensions: int = 0

    # --- chargement ----------------------------------------------------------

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        torch = import_torch()
        self.torch = torch
        self.ensure_mps(torch)
        self.defaults = dict(variant.get("defaults") or {})
        self.options = dict(variant.get("options") or {})

        try:
            import timm
        except ImportError as exc:
            raise WorkerError(f"timm absent de l'environnement ({exc}) — `{REPAIR}`") from exc

        chemin = weights_dir(variant)
        config = _lire_config(chemin)
        poids = chemin / "model.safetensors"
        if not poids.is_file():
            raise WorkerError(
                f"model.safetensors absent de {chemin} — les dépôts timm servent aussi "
                "pytorch_model.bin, mais les `allow_patterns` du manifeste ne prennent "
                "que le safetensors : vérifier `ecurie pull`"
            )

        # Le nom **taggé** et non l'architecture seule. `create_model("convnext_small")`
        # charge bien les octets qu'on lui désigne, mais hérite du `pretrained_cfg` du
        # tag par défaut du dépôt timm — ici `in12k_ft_in1k`, avec un `crop_pct` de 0,95
        # et une licence apache-2.0 qui n'est pas celle de ces poids-là. La
        # normalisation et la définition d'entraînement se lisent dans cette
        # configuration : la prendre au mauvais tag revient à prétraiter selon un autre
        # modèle, et rien n'échouerait.
        nom = _nom_timm(config)
        modèle = _creer(timm, nom, str(poids))
        self.cfg = dict(getattr(modèle, "pretrained_cfg", None) or {})

        self.model = modèle.eval().to("mps")
        self.pas = _pas(modèle)
        self.prefixe = int(getattr(modèle, "num_prefix_tokens", 0) or 0)
        # Un modèle à jetons annonce combien il en réserve devant les patches ;
        # une carte de traits n'a pas cet attribut. C'est ce qui décide si `cls`
        # a un sens, et le refus tombe ici plutôt qu'au premier job.
        self.jetons = getattr(modèle, "num_prefix_tokens", None) is not None
        self.pooling = verifier_agregation(self.options.get("pooling"), self.jetons, nom)
        self.dimensions = int(getattr(modèle, "num_features", 0) or 0)
        self.identite = {
            "ref": variant.get("ref"),
            "repo": variant.get("repo"),
            "revision": variant.get("revision"),
            "architecture": nom,
            "implementation": f"timm {_version_timm(timm)} / torch",
        }
        self.mps_counters()

        return {
            "pooling": self.pooling,
            "dimensions": self.dimensions,
            "grid_step": self.pas,
            "prefix_tokens": self.prefixe,
            "background": FOND_NOM,
            "versions": self.versions(),
        }

    # --- inférence -----------------------------------------------------------

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.model is None or self.torch is None:
            raise WorkerError("modèle non chargé")

        source = resolve_image(request.get("image"), request.output_dir)
        côté = int(self.reglage(request, "max_side", DEFAUT_COTE))
        normaliser = bool(self.reglage(request, "normalize", True))

        progress(10, "préparation")
        vecteur, soumise, origine = self._encoder(source, côté)
        if normaliser:
            vecteur = normaliser_l2(vecteur)

        similarité: float | None = None
        comparaison = self.reglage(request, "compare_to", None)
        if comparaison:
            progress(60, "seconde image")
            seconde = resolve_image(comparaison, request.output_dir, "compare_to")
            autre, _, _ = self._encoder(seconde, côté)
            # `normalize` ne change rien à ce nombre-là, et c'est voulu : le
            # cosinus est invariant d'échelle. Un job qui aurait rendu deux
            # similarités selon un réglage d'affichage aurait été incomparable
            # avec lui-même.
            similarité = cosinus(vecteur, autre)

        progress(85, "écriture")
        document = {
            # Ce bloc n'est pas de la courtoisie : deux modèles de cette capacité
            # rendent des vecteurs de même longueur qui n'appartiennent pas au
            # même espace — `dinov3@convnext-small` et `dinov2@vit-base` en font
            # tous deux 768. Un cosinus entre les deux est un nombre qui ne veut
            # rien dire, et rien d'autre que ces lignes ne l'empêcherait.
            **self.identite,
            "pooling": self.pooling,
            "prefix_tokens": self.prefixe if self.jetons else None,
            "background": FOND_NOM,
            "requested_max_side": côté,
            "input_size": list(soumise),
            "source_size": list(origine),
            "normalized": normaliser,
            "dimensions": len(vecteur),
            "similarity": similarité,
            "embedding": [round(float(v), 6) for v in vecteur],
        }
        chemin = request.output_dir / SORTIE_JSON
        chemin.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")

        sortie: dict[str, Any] = {"embedding": SORTIE_JSON, "dimensions": len(vecteur)}
        if similarité is not None:
            sortie["similarity"] = similarité

        compteurs = self.mps_counters()
        return InferResult(
            output=sortie,
            metrics={
                # Le cosinus est répété dans les métriques, et ce n'est pas une
                # redondance : c'est le seul nombre lisible que ce job produise,
                # et la ligne de télémétrie n'affiche pas les sorties. Sans lui,
                # un terminal ne montre d'une empreinte visuelle que sa taille.
                **({"similarity": similarité} if similarité is not None else {}),
                "pooling": self.pooling,
                "model_width": soumise[0],
                "model_height": soumise[1],
                "requested_max_side": côté,
                "dimensions": len(vecteur),
                "vector_norm": round(norme(vecteur), 6),
                **compteurs,
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def versions(self) -> dict[str, str]:
        """torch et transformers du socle, plus timm — qui décide ici de tout.

        C'est la version de timm, et elle seule, qui sait ou ne sait pas ce
        qu'est `convnext_small.dinov3_lvd1689m` : un profil mesuré sans elle ne
        dirait pas sous quoi il vaut.
        """
        versions = super().versions()
        try:
            import timm
        except ImportError:
            return versions
        return {**versions, "timm": _version_timm(timm)}

    # --- détails -------------------------------------------------------------

    def _encoder(
        self, chemin: Path, côté: int
    ) -> tuple[list[float], tuple[int, int], tuple[int, int]]:
        """Un fichier vers un vecteur brut, avec les deux définitions en jeu."""
        torch = self.torch
        Image = import_pil()

        with Image.open(chemin) as ouverte:
            # Composition explicite plutôt que `convert("RGB")` : sous un alpha
            # nul, les canaux de couleur des assets du dépôt valent zéro, et la
            # conversion directe sert un sujet clair sur fond noir.
            rgba = ouverte.convert("RGBA")
            origine = rgba.size
            fond = Image.new("RGBA", origine, (*FOND, 255))
            image = Image.alpha_composite(fond, rgba).convert("RGB")

        soumise = definition(origine, côté, self.pas)
        image = image.resize(soumise, Image.BICUBIC)

        import numpy as np

        moyenne = np.array(self.cfg.get("mean") or (0.485, 0.456, 0.406), dtype="float32")
        écart = np.array(self.cfg.get("std") or (0.229, 0.224, 0.225), dtype="float32")
        tableau = (np.asarray(image, dtype="float32") / 255.0 - moyenne) / écart
        tenseur = torch.from_numpy(tableau).permute(2, 0, 1).unsqueeze(0).to("mps")

        with torch.no_grad():
            traits = self.model.forward_features(tenseur)
            vecteur = self._agreger(traits)
        torch.mps.synchronize()
        self.mps_counters()
        return [float(v) for v in vecteur.float().cpu().numpy().reshape(-1)], soumise, origine

    def _agreger(self, traits: Any) -> Any:
        """Carte de traits ou suite de jetons vers un seul vecteur.

        Le rang du tenseur tranche, et non une table d'architectures qui aurait
        vieilli à la première famille suivante. Une carte (B, C, H, W) passe par
        la tête du modèle — moyenne sur les positions **puis** la normalisation
        que ces poids-là ont apprise, ce qui est l'empreinte que timm publie. Une
        suite (B, N, C) est découpée à `num_prefix_tokens`, lu sur le modèle.
        """
        if traits.ndim == 4:
            if self.pooling != "mean":
                raise WorkerError(
                    f"agrégation `{self.pooling}` impossible : ce modèle rend une carte de "
                    "traits, sans jeton CLS ni registre — corriger `options.pooling` du "
                    "manifeste"
                )
            return self.model.forward_head(traits, pre_logits=True)
        if self.pooling == "cls":
            return traits[:, 0]
        return traits[:, self.prefixe :].mean(dim=1)


# --- fonctions pures ---------------------------------------------------------
#
# Elles ne touchent ni torch ni timm : c'est ce qui les rend vérifiables en CI,
# sans Apple Silicon, sans poids et sans venv de runtime.


def verifier_agregation(demandée: Any, jetons: bool, nom: str = "ce modèle") -> str:
    """L'agrégation du variant, ou un refus qui dit quoi corriger.

    Le défaut est `mean` : c'est la seule valeur qu'une carte de traits sait
    honorer, et elle a un sens pour les deux formes. Un manifeste qui demande
    `cls` à une architecture sans CLS est refusé au chargement — servir la
    moyenne sous ce nom donnerait un vecteur d'un autre espace, et rien
    n'échouerait.
    """
    valeur = str(demandée or "mean").strip().lower()
    if valeur not in AGREGATIONS:
        raise WorkerError(
            f"agrégation inconnue : {valeur!r} — attendu {' ou '.join(AGREGATIONS)} "
            "dans `options.pooling` du manifeste"
        )
    if valeur == "cls" and not jetons:
        raise WorkerError(
            f"agrégation `cls` impossible sur {nom} : cette architecture rend une carte "
            "de traits, sans jeton CLS ni registre — `options.pooling: mean`"
        )
    return valeur


def aligner(valeur: float, pas: int) -> int:
    """Le multiple du pas le plus proche, et jamais moins d'un pas.

    Un réseau à patches ne sait pas quoi faire d'un reste : `max_side: 256` sur
    des patches de 14 vaut 252, et c'est cette valeur-là qui part au document.
    """
    pas = max(1, int(pas))
    return max(pas, int(round(float(valeur) / pas)) * pas)


def definition(origine: tuple[int, int], côté: int, pas: int) -> tuple[int, int]:
    """Définition soumise au réseau : proportions gardées, côtés alignés au pas.

    Le plus grand côté vise `max_side`, l'autre suit. Aucun recadrage : la
    capacité encode l'image entière, et rogner pour tenir dans un carré ferait
    disparaître le bord — c'est-à-dire, sur une pièce photographiée de loin, le
    sujet.
    """
    largeur, hauteur = (max(1, int(v)) for v in origine)
    facteur = max(1, int(côté)) / max(largeur, hauteur)
    return aligner(largeur * facteur, pas), aligner(hauteur * facteur, pas)


def norme(vecteur: list[float]) -> float:
    return math.sqrt(sum(v * v for v in vecteur))


def normaliser_l2(vecteur: list[float]) -> list[float]:
    """Norme 1, ou le vecteur tel quel s'il est nul — diviser par zéro ne dit rien."""
    n = norme(vecteur)
    return [v / n for v in vecteur] if n > 0 else list(vecteur)


def cosinus(a: list[float], b: list[float]) -> float | None:
    """Cosinus entre deux vecteurs, ou None quand l'un des deux est nul."""
    if len(a) != len(b):
        raise WorkerError(
            f"vecteurs de longueurs différentes ({len(a)} et {len(b)}) : "
            "les deux images n'ont pas été encodées par le même modèle"
        )
    dénominateur = norme(a) * norme(b)
    if dénominateur <= 0:
        return None
    return round(sum(x * y for x, y in zip(a, b, strict=True)) / dénominateur, 4)


# --- lecture de l'amont ------------------------------------------------------


def _lire_config(chemin: Path) -> dict[str, Any]:
    fichier = chemin / "config.json"
    if not fichier.is_file():
        raise WorkerError(
            f"config.json absent de {chemin} — un dépôt timm en publie un, "
            "vérifier les `allow_patterns` du manifeste"
        )
    try:
        return json.loads(fichier.read_text())
    except (OSError, ValueError) as exc:
        raise WorkerError(f"config.json illisible : {exc}") from exc


def _nom_timm(config: dict[str, Any]) -> str:
    """`architecture` et son `tag`, tels que le dépôt les déclare.

    Sans le tag, timm retombe sur la configuration par défaut de l'architecture :
    une autre normalisation, un autre `crop_pct`, une autre licence — et pas la
    moindre erreur pour le signaler.
    """
    architecture = str(config.get("architecture") or "").strip()
    if not architecture:
        raise WorkerError(
            "config.json ne déclare pas d'`architecture` : ce dépôt n'est pas un "
            "dépôt de modèle timm"
        )
    tag = str(((config.get("pretrained_cfg") or {}).get("tag")) or "").strip()
    return f"{architecture}.{tag}" if tag else architecture


def _creer(timm: Any, nom: str, poids: str) -> Any:
    """Le modèle timm, poids lus sur le disque, jamais sur le réseau.

    `dynamic_img_size` est tenté d'abord : sans lui, un ViT à table de positions
    n'accepte que la définition de son pré-entraînement — 518 pixels pour DINOv2,
    ce qui rendrait `max_side` inopérant. Les architectures convolutives n'ont
    pas de table à interpoler et refusent le mot-clé par un `TypeError` ; timm ne
    l'annonce nulle part, c'est le seul moyen de le savoir.
    """
    arguments = dict(
        pretrained=True, num_classes=0, pretrained_cfg_overlay=dict(file=poids)
    )
    try:
        return timm.create_model(nom, dynamic_img_size=True, **arguments)
    except TypeError:
        pass
    except Exception as exc:  # noqa: BLE001 — code amont : le message importe plus que le type
        raise WorkerError(f"chargement impossible ({nom}) : {type(exc).__name__}: {exc}") from exc
    try:
        return timm.create_model(nom, **arguments)
    except Exception as exc:  # noqa: BLE001
        raise WorkerError(
            f"chargement impossible ({nom}) : {type(exc).__name__}: {exc}. "
            f"Une architecture inconnue de cette version de timm se voit ici — `{REPAIR}`"
        ) from exc


def _pas(modèle: Any) -> int:
    """Le pas d'alignement du réseau, lu sur lui plutôt que supposé.

    Un ViT le porte dans `patch_embed.patch_size` ; une ConvNeXt dans la dernière
    réduction de son `feature_info`. Une table d'architectures aurait vieilli à la
    première famille suivante.
    """
    patches = getattr(getattr(modèle, "patch_embed", None), "patch_size", None)
    if patches is not None:
        taille = patches[0] if isinstance(patches, (tuple, list)) else patches
        return max(1, int(taille))
    infos = getattr(modèle, "feature_info", None)
    try:
        dernier = infos[-1]  # type: ignore[index]
        réduction = dernier["reduction"] if isinstance(dernier, dict) else dernier.reduction
        return max(1, int(réduction))
    except (TypeError, KeyError, IndexError, AttributeError, ValueError):
        return PAS_DE_REPLI


def _version_timm(timm: Any) -> str:
    return str(getattr(timm, "__version__", "?"))


if __name__ == "__main__":
    raise SystemExit(main(Dinov3EmbedWorker))
