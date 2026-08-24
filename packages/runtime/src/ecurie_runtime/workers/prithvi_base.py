"""Socle commun des adaptateurs `terratorch` : lecture raster, tuilage, mémoire.

Deux capacités s'y appuient — `geo-segment` et `geo-embed` — et elles partagent
plus que la pile : la même façon d'ouvrir un GeoTIFF, de choisir six bandes parmi
treize, de les mettre à l'échelle des réflectances, de découper une scène en
tuiles et de mesurer ce que Metal consomme. Tout ce qui les sépare — le décodeur,
la sortie, la contrainte de taille — vit dans leurs modules respectifs.

**Ce que ce socle existe pour empêcher.** La sélection de bandes est le seul
réglage de cette famille dont une erreur ne se voit pas dans le résultat : un
masque calculé sur le rouge, le proche infrarouge et la vapeur d'eau a exactement
l'aspect d'un masque juste. Deux lectures divergentes de `band_indices` dans deux
adaptateurs auraient donc produit deux jobs incomparables sans qu'aucun n'échoue,
et c'est la raison principale de ce fichier.

**La mesure du pic, ensuite.** Sur mémoire unifiée, ni le RSS ni le compteur du
pilote Metal ne suffit seul : le premier ne voit pas ce que Metal réserve, le
second ne voit pas ce que Python garde. Les deux consomment le même budget, et
c'est le plus grand qui doit entrer au profil. `driver_allocated_memory` redescend
par ailleurs aussi vite qu'il monte — le maximum se tient à chaque relevé plutôt
que se lit une fois à la fin.

Rien de torch ni de rasterio n'est importé au niveau du module (voir
`workers/__init__.py`).
"""

import gc
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecurie_runtime.workers.base import Worker, WorkerError, peak_rss_bytes

ENV_NAME = "terratorch"
REPAIR = f"ecurie env sync {ENV_NAME}"

RASTERS = {".tif", ".tiff"}

#: Le modèle lit six canaux et pas un de plus : `patch_embed.proj.weight` a la
#: forme (1024, 6, 1, 16, 16), donc six canaux d'entrée en dur. Ce n'est pas une
#: préférence de fiche de modèle, c'est la convolution elle-même qui refuse.
CANAUX = 6

#: Les six bandes, dans l'ordre où le réseau les attend. Ce sont les noms que
#: `terratorch` emploie dans ses tables de normalisation, et c'est par eux que
#: l'adaptateur y retrouve les moyennes et écarts-types.
BANDES = ("BLUE", "GREEN", "RED", "NIR_NARROW", "SWIR_1", "SWIR_2")

#: Facteur des produits Sentinel-2 et HLS : la réflectance y est stockée en
#: entiers valant dix mille fois sa valeur. Lu dans `config.yaml` du fine-tune
#: (`constant_scale: 0.0001`) quand il en porte un, repris ici sinon.
ECHELLE_REFLECTANCE = 1e-4


def import_torch() -> Any:
    try:
        import torch
    except ImportError as exc:
        raise WorkerError(
            f"runtime torch indisponible dans cet environnement ({exc}) — `{REPAIR}`"
        ) from exc
    return torch


def import_rasterio() -> Any:
    try:
        import rasterio
    except ImportError as exc:
        raise WorkerError(
            f"rasterio absent de l'environnement ({exc}) — `{REPAIR}`. C'est lui qui "
            "apporte GDAL, sans quoi un GeoTIFF n'est qu'un TIFF"
        ) from exc
    return rasterio


def import_numpy() -> Any:
    try:
        import numpy
    except ImportError as exc:
        raise WorkerError(f"numpy absent de l'environnement ({exc}) — `{REPAIR}`") from exc
    return numpy


# --- ce qui se vérifie sans poids ---------------------------------------------


@dataclass(frozen=True)
class Scene:
    """Une scène lue : ses six bandes et ce qu'il faut pour rendre la sortie.

    `crs` et `transform` sont facultatifs — un TIFF nu se segmente aussi bien, il
    n'a simplement rien à léguer à la sortie. Les rendre obligatoires aurait
    refusé des jobs que le modèle sait faire.
    """

    bandes: Any  # np.ndarray (6, H, W) float32, déjà normalisé
    largeur: int
    hauteur: int
    crs: str | None
    transform: Any
    bounds: dict[str, float] | None
    nombre_bandes: int
    indices: tuple[int, ...]


def verifier_bandes(brut: Any, nombre_bandes: int | None = None) -> tuple[int, ...]:
    """Les six indices de bandes, comptés à partir de zéro, ou un refus qui dit quoi corriger.

    Six exactement : le réseau n'a pas d'autre nombre de canaux, et lui en donner
    cinq échouerait dans la convolution par un message qui parle de formes de
    tenseurs. Le refus tombe ici, avec la raison.

    Vérifiés aussi contre le nombre de bandes du fichier quand on le connaît. Un
    indice qui dépasse est l'erreur la plus probable de cette famille : le défaut
    du contrat suppose un produit à treize bandes, et un raster déjà réduit aux
    six utiles se lit avec [0, 1, 2, 3, 4, 5].
    """
    if brut is None:
        raise WorkerError(
            "band_indices absent : le contrat en porte un défaut, et un worker appelé "
            "sans passer par lui doit le nommer — six indices sont attendus"
        )
    if isinstance(brut, (str, bytes)) or not hasattr(brut, "__iter__"):
        raise WorkerError(
            f"band_indices : liste de six entiers attendue, reçu {type(brut).__name__}"
        )

    indices: list[int] = []
    for valeur in brut:
        if isinstance(valeur, bool) or not isinstance(valeur, (int, float)):
            raise WorkerError(f"band_indices : {valeur!r} n'est pas un numéro de bande")
        if float(valeur) != int(valeur):
            raise WorkerError(f"band_indices : {valeur!r} n'est pas entier")
        indices.append(int(valeur))

    if len(indices) != CANAUX:
        raise WorkerError(
            f"band_indices : {len(indices)} bande(s) données, {CANAUX} attendues "
            f"({', '.join(BANDES)}) — la première convolution du réseau a {CANAUX} canaux "
            "d'entrée en dur, ce nombre ne se règle pas"
        )
    if any(i < 0 for i in indices):
        raise WorkerError("band_indices : les numéros de bande partent de zéro, jamais négatifs")
    if nombre_bandes is not None:
        hors = [i for i in indices if i >= nombre_bandes]
        if hors:
            raise WorkerError(
                f"band_indices : la bande {', '.join(str(i) for i in hors)} n'existe pas — "
                f"le raster en porte {nombre_bandes}, numérotées de 0 à {nombre_bandes - 1}. "
                "Le défaut du contrat suppose un produit à treize bandes ; un raster déjà "
                "réduit aux six bandes utiles se lit avec [0, 1, 2, 3, 4, 5]"
            )
    return tuple(indices)


def aligner_multiple(valeur: int, pas: int) -> int:
    """Le multiple de `pas` immédiatement supérieur ou égal, et jamais moins d'un pas."""
    pas = max(1, int(pas))
    valeur = int(valeur)
    if valeur <= pas:
        return pas
    return -(-valeur // pas) * pas


def plan_tuiles(cote: int, tuile: int, recouvrement: int = 0) -> tuple[int, ...]:
    """Les abscisses de départ des tuiles couvrant `cote` pixels, sans jamais déborder.

    Le dernier départ est **recollé au bord** plutôt que prolongé au-delà : une
    tuile qui sortirait de la scène devrait être rembourrée, et un rembourrage au
    milieu d'une image qu'on possède est de l'invention là où il y a de la donnée.
    Le prix est un recouvrement supplémentaire sur la dernière tuile, que
    l'accumulation des logits absorbe sans couture.

    Une scène plus petite qu'une tuile rend un seul départ : c'est le rembourrage
    du bord qui s'en occupe, et lui seul.
    """
    cote = max(1, int(cote))
    tuile = max(1, int(tuile))
    recouvrement = max(0, int(recouvrement))
    if recouvrement >= tuile:
        raise WorkerError(
            f"overlap = {recouvrement} : il doit rester strictement sous `tile_size` "
            f"({tuile}), sinon deux tuiles voisines ne progressent plus"
        )
    if cote <= tuile:
        return (0,)
    pas = tuile - recouvrement
    départs = list(range(0, cote - tuile + 1, pas))
    if départs[-1] + tuile < cote:
        départs.append(cote - tuile)
    return tuple(départs)


def depouiller_prefixe(poids: dict[str, Any], prefixe: str) -> dict[str, Any]:
    """Les poids dont la clé commence par `prefixe`, ce préfixe retiré.

    **C'est le contrôle le plus important de cette famille, et le seul qui ne
    lève rien quand il manque.** Les points de contrôle publiés ici rangent leurs
    tenseurs sous un préfixe hérité de l'entraînement — `model.` pour le
    fine-tune, `encoder.` pour l'encodeur pré-entraîné. Chargés tels quels avec
    `strict=False`, ils laissent le réseau à ses valeurs d'initialisation : il
    tourne, il rend une sortie de forme normale, et cette sortie est du bruit.
    Aucune exception, aucun avertissement.
    """
    retenus = {
        clé[len(prefixe) :]: tenseur for clé, tenseur in poids.items() if clé.startswith(prefixe)
    }
    if not retenus:
        raise WorkerError(
            f"aucune clé préfixée `{prefixe}` dans le point de contrôle "
            f"({len(poids)} clé(s) lues, dont « {', '.join(list(poids)[:3])} ») — "
            "la disposition du fichier d'amont a changé, et charger sans ce préfixe "
            "donnerait un réseau non initialisé qui rend du bruit sans échouer"
        )
    return retenus


def exiger_chargement_complet(rapport: Any, quoi: str) -> None:
    """Refuse un chargement partiel. 0 manquant, 0 inattendu, ou rien.

    `load_state_dict(strict=False)` est employé ici pour pouvoir **lire** le
    rapport, jamais pour tolérer un écart : c'est la seule preuve disponible que
    les poids sont arrivés là où on croit. `strict=True` lèverait bien, mais son
    message énumère des centaines de clés et ne dit pas ce qu'il faut corriger.
    """
    manquants = list(getattr(rapport, "missing_keys", []) or [])
    inattendus = list(getattr(rapport, "unexpected_keys", []) or [])
    if not manquants and not inattendus:
        return
    raise WorkerError(
        f"{quoi} : {len(manquants)} poids manquant(s) et {len(inattendus)} inattendu(s) — "
        f"exemple manquant « {manquants[0] if manquants else '—'} », inattendu "
        f"« {inattendus[0] if inattendus else '—'} ». Un réseau chargé à moitié ne lève "
        "rien et rend du bruit : le job est refusé plutôt que servi"
    )


def weights_dir(variant: dict[str, Any]) -> Path:
    """Le dossier de poids transmis par le superviseur, vérifié avant usage."""
    brut = str(variant.get("weights_path") or "").strip()
    if not brut:
        raise WorkerError("aucun chemin de poids transmis par le superviseur")
    chemin = Path(brut)
    if not chemin.is_dir():
        raise WorkerError(
            f"poids introuvables : {chemin} — le superviseur transmet un chemin local "
            "déjà vérifié, un worker ne télécharge jamais"
        )
    return chemin


def unique_fichier(dossier: Path, motif: str, quoi: str) -> Path:
    """L'unique fichier du dossier correspondant au motif, ou un refus qui compte."""
    trouvés = sorted(dossier.glob(motif))
    if not trouvés:
        raise WorkerError(
            f"{quoi} introuvable dans {dossier} (motif « {motif} ») — vérifier les "
            f"`allow_patterns` du manifeste, puis `ecurie pull`"
        )
    if len(trouvés) > 1:
        raise WorkerError(
            f"{len(trouvés)} fichiers correspondent à « {motif} » dans {dossier} "
            f"({', '.join(f.name for f in trouvés)}) : le manifeste doit désigner "
            "lequel plutôt que de laisser l'adaptateur deviner"
        )
    return trouvés[0]


def resolve_raster(valeur: Any, job_dir: Path, champ: str = "raster") -> Path:
    """Le chemin du raster, relatif au dossier du job quand il l'est.

    Le superviseur copie l'entrée dans le dossier du job et transmet un chemin
    relatif — c'est ce qui rend le job rejouable ailleurs. Un chemin absolu reste
    accepté : le banc d'essai en passe.
    """
    brut = str(valeur or "").strip()
    if not brut:
        raise WorkerError(f"aucun raster en entrée : le champ `{champ}` est vide")
    chemin = Path(brut).expanduser()
    if not chemin.is_absolute():
        chemin = job_dir / chemin
    if not chemin.is_file():
        raise WorkerError(f"{champ} introuvable : {chemin}")
    if chemin.suffix.lower() not in RASTERS:
        raise WorkerError(
            f"format non géré : {chemin.suffix or '(sans extension)'} — attendu "
            f"{', '.join(sorted(RASTERS))}. Cette capacité ne lit pas d'image : un PNG "
            "ne peut pas porter le proche infrarouge, qui est ce qui la distingue"
        )
    return chemin


# --- l'adaptateur --------------------------------------------------------------


class PrithviWorker(Worker):
    """Base des adaptateurs satellite : device, lecture raster, compteurs mémoire."""

    def __init__(self) -> None:
        self.torch: Any = None
        self.np: Any = None
        self.rasterio: Any = None
        self.model: Any = None
        self.defaults: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.moyennes: Any = None
        self.ecarts: Any = None
        self.echelle: float = ECHELLE_REFLECTANCE
        self._peak_driver: int = 0

    # --- device --------------------------------------------------------------

    def ensure_mps(self, torch: Any) -> None:
        """Metal exigé, jamais de repli silencieux vers le processeur.

        Le CPU sait faire tourner ces poids, et sans la contrainte de taille du
        décodeur — mesuré, il rend la même couverture à 0,001 près. Il n'est
        pourtant pas servi : un variant qui retomberait dessus en silence ferait
        mesurer deux chemins différents sous un seul profil, et un pic relevé sur
        Metal ne dit rien de ce que coûte le même job sans lui.
        """
        if not torch.backends.mps.is_available():
            raise WorkerError(
                "backend MPS indisponible (torch.backends.mps.is_available() est faux) — "
                "cet adaptateur ne sert que sur Apple Silicon ; vérifier que "
                f"runtimes/{ENV_NAME}/.venv utilise un Python arm64"
            )

    # --- réglages ------------------------------------------------------------

    def reglage(self, request: Any, nom: str, defaut: Any) -> Any:
        """Entrée du job, puis options du variant, puis défauts du manifeste."""
        valeur = request.get(nom)
        if valeur is not None:
            return valeur
        for couche in (self.options, self.defaults):
            if couche.get(nom) is not None:
                return couche[nom]
        return defaut

    # --- lecture du raster ---------------------------------------------------

    def lire_scene(self, chemin: Path, indices_bruts: Any) -> Scene:
        """Ouvre le GeoTIFF, en tire six bandes normalisées, garde de quoi rendre la sortie.

        La normalisation est faite ici, une fois pour toutes, et non dans chaque
        adaptateur : deux barèmes divergents auraient donné deux jobs qui ne
        s'expliquent pas l'un l'autre. Les valeurs, elles, viennent de l'amont —
        du jeu de données du fine-tune ou de la configuration de l'encodeur —
        parce que ce sont elles qui décident de ce que le réseau voit.
        """
        rasterio = self.rasterio
        try:
            source = rasterio.open(chemin)
        except Exception as exc:  # noqa: BLE001 — remonte traduit
            raise WorkerError(
                f"raster illisible ({chemin.name}) : {type(exc).__name__}: {exc} — "
                "attendu un GeoTIFF multi-bandes"
            ) from exc
        with source as raster:
            indices = verifier_bandes(indices_bruts, raster.count)
            # rasterio numérote ses bandes à partir de 1, le contrat à partir de
            # 0 comme le script d'amont. La conversion est ici et nulle part
            # ailleurs : dupliquée, elle finirait par diverger d'une unité, ce
            # qui décale toutes les bandes sans rien casser.
            brut = raster.read([i + 1 for i in indices]).astype("float32")
            crs = str(raster.crs) if raster.crs else None
            transform = raster.transform
            bornes = raster.bounds if crs else None
            largeur, hauteur, compte = raster.width, raster.height, raster.count

        if self.moyennes is None or self.ecarts is None:
            raise WorkerError("normalisation non chargée : `load` n'a pas été appelé")
        normalisé = (brut * self.echelle - self.moyennes) / self.ecarts

        return Scene(
            bandes=normalisé.astype("float32"),
            largeur=largeur,
            hauteur=hauteur,
            crs=crs,
            transform=transform,
            bounds=(
                {
                    "left": float(bornes.left),
                    "bottom": float(bornes.bottom),
                    "right": float(bornes.right),
                    "top": float(bornes.top),
                }
                if bornes is not None
                else None
            ),
            nombre_bandes=compte,
            indices=indices,
        )

    def poser_normalisation(self, moyennes: Any, ecarts: Any, echelle: float) -> None:
        np = self.np
        self.moyennes = np.asarray(moyennes, dtype="float32").reshape(CANAUX, 1, 1)
        self.ecarts = np.asarray(ecarts, dtype="float32").reshape(CANAUX, 1, 1)
        if float(self.ecarts.min()) <= 0.0:
            raise WorkerError(
                "écart-type nul ou négatif dans la table de normalisation d'amont : "
                "la division rendrait des infinis que le réseau accepterait sans broncher"
            )
        self.echelle = float(echelle)

    def rembourrer(self, tenseur: Any, cible_h: int, cible_l: int) -> Any:
        """Étend une tuile jusqu'à la taille exigée par le réseau, par recopie du bord.

        Recopie plutôt que zéros : après normalisation, un zéro vaut la moyenne du
        jeu d'entraînement, c'est-à-dire une réflectance moyenne inventée là où il
        n'y a rien. Prolonger le bord n'invente pas de spectre. Mesuré sur les
        chips d'exemple, les trois modes tombent au millième près ; c'est donc un
        choix de principe, et il est écrit comme tel.
        """
        haut, large = tenseur.shape[-2], tenseur.shape[-1]
        bas, droite = max(0, cible_h - haut), max(0, cible_l - large)
        if not bas and not droite:
            return tenseur
        return self.torch.nn.functional.pad(tenseur, (0, droite, 0, bas), mode="replicate")

    # --- mémoire -------------------------------------------------------------

    def mps_counters(self) -> dict[str, int]:
        """Compteurs MPS instantanés. Aucun n'est un pic — les noms le disent."""
        mps = getattr(self.torch, "mps", None) if self.torch is not None else None
        if mps is None:
            return {}
        try:
            compteurs = {
                "mps_current_allocated_bytes": int(mps.current_allocated_memory()),
                "mps_driver_allocated_bytes": int(mps.driver_allocated_memory()),
                "mps_recommended_max_bytes": int(mps.recommended_max_memory()),
            }
        except (AttributeError, RuntimeError):
            return {}
        self._peak_driver = max(self._peak_driver, compteurs["mps_driver_allocated_bytes"])
        return compteurs

    def peak_memory_bytes(self) -> int | None:
        """Le plus grand du pic RSS et du maximum relevé chez le pilote Metal.

        Les deux, et non l'un ou l'autre : le RSS ne voit pas la mémoire Metal, le
        pilote ne voit pas ce que Python garde, et sur mémoire unifiée les deux
        sortent du même budget. Ici l'écart est réel — les treize bandes du
        raster, les logits accumulés et le masque vivent côté Python, les poids
        et les activations côté Metal.
        """
        self.mps_counters()
        return max(self._peak_driver, peak_rss_bytes() or 0) or None

    def unload(self) -> None:
        """Rend la mémoire au budget, pas seulement à Python."""
        self.model = None
        if self.torch is not None:
            gc.collect()
            try:
                self.torch.mps.empty_cache()
            except (AttributeError, RuntimeError):
                pass

    # --- versions ------------------------------------------------------------

    def versions(self) -> dict[str, str]:
        """torch, terratorch, torchgeo et rasterio.

        torchgeo y figure alors qu'aucun adaptateur ne l'importe : c'est la
        dépendance dont la borne décide que `import terratorch` fonctionne ou
        non, et un profil mesuré sans elle ne dirait pas sous quoi il vaut.
        """
        versions: dict[str, str] = {}
        for nom, module in (
            ("torch", "torch"),
            ("terratorch", "terratorch"),
            ("torchgeo", "torchgeo"),
            ("rasterio", "rasterio"),
        ):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version is None:
                try:
                    from importlib.metadata import version as métadonnée

                    version = métadonnée(module)
                except Exception:  # noqa: BLE001 — la version est une commodité, pas un contrat
                    continue
            versions[nom] = str(version)
        return versions
