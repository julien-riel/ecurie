"""Adaptateur `terratorch`, chemin **empreinte de tuile satellite** : Prithvi-EO-2.0.

Le patron de `dinov3_embed` transposé à un raster, et les écarts sont trois, tous
imposés par ce qu'une scène satellite est.

**Rien n'est redimensionné.** `image-embed` ramène l'image à `max_side` parce que
le contenu d'une photo ne dépend pas de sa définition. Un pixel satellite mesure
une surface au sol — trente mètres pour ces poids — et le rééchantillonner
changerait ce que le modèle croit regarder : une parcelle deviendrait un champ,
une route un chemin. La scène est donc **découpée**, jamais mise à l'échelle, et
il en sort un vecteur par tuile plutôt qu'un par fichier.

**L'empreinte de scène est une moyenne renormalisée.** Le cosinus de
`compare_to` porte sur elle, faute de quoi comparer deux scènes de tailles
différentes n'aurait aucun sens. Elle est écrite dans le document à côté des
vecteurs de tuile, et non à leur place : c'est le découpage qui décide de ce
qu'une tuile contient, et un utilisateur qui change `tile_size` change l'espace
dans lequel il compare.

**Aucune contrainte de taille sur ce chemin.** Le multiple de 192 qu'exige
`prithvi_segment` vient de son décodeur ; mesuré ici sur l'encodeur seul,
192, 224, 256, 384, 512, 576 et 768 passent tous sur Metal. Le seul pas qui
compte est celui des patches, 16.

**Deux pièges d'amont, tous deux silencieux.** Le point de contrôle range ses 402
clés sous les préfixes `encoder.` et `decoder.` — le second étant le décodeur de
reconstruction de l'auto-encodeur masqué, dont l'empreinte n'a que faire.
Chargées sans dépouiller, elles laissent 296 poids manquants et 402 inattendus, et
l'encodeur reste à ses valeurs d'initialisation : il rend des vecteurs de la bonne
longueur, dont les cosinus sont plausibles, et rien n'échoue. Et sa table de
positions est celle de quatre dates (785 jetons) ; construire le réseau pour une
seule date donne 197 jetons et un refus par incompatibilité de forme. Le nombre de
dates est donc lu dans `config.json` plutôt que supposé, et l'interpolation
d'amont ramène ensuite la table à l'unique date qu'on lui soumet.

Rien de torch, rasterio ni terratorch n'est importé au niveau du module (voir
`workers/__init__.py`).
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
from ecurie_runtime.workers.prithvi_base import (
    BANDES,
    CANAUX,
    REPAIR,
    PrithviWorker,
    aligner_multiple,
    depouiller_prefixe,
    exiger_chargement_complet,
    import_numpy,
    import_rasterio,
    import_torch,
    plan_tuiles,
    resolve_raster,
    unique_fichier,
    weights_dir,
)

SORTIE_JSON = "embeddings.json"

#: Le pas des patches du transformeur. Une tuile qui n'en est pas un multiple est
#: rembourrée, et le job le dit : le rembourrage entre alors dans la moyenne des
#: jetons, donc dans le vecteur.
PATCH = 16

DEFAUT_TUILE = 512

AGREGATIONS = ("mean", "cls")


class PrithviEmbedWorker(PrithviWorker):
    """Une scène multi-bandes vers un vecteur par tuile, plus l'empreinte de scène."""

    name = "prithvi-embed"

    def __init__(self) -> None:
        super().__init__()
        self.identite: dict[str, Any] = {}
        self.pooling: str = "mean"
        self.dimensions: int = 0
        self.prefixe: int = 1

    # --- chargement ----------------------------------------------------------

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        torch = import_torch()
        self.torch = torch
        self.np = import_numpy()
        self.rasterio = import_rasterio()
        self.ensure_mps(torch)
        self.defaults = dict(variant.get("defaults") or {})
        self.options = dict(variant.get("options") or {})

        try:
            from terratorch.registry import BACKBONE_REGISTRY
        except ImportError as exc:
            raise WorkerError(
                f"runtime terratorch indisponible dans cet environnement ({exc}) — "
                f"`{REPAIR}`. Une installation sans borne sur torchgeo échoue ici même, "
                "par « cannot import name 'utils' from 'torchgeo.trainers' »"
            ) from exc

        chemin = weights_dir(variant)
        config = _lire_config(chemin)
        self.pooling = verifier_agregation(self.options.get("pooling"))

        try:
            self.model = BACKBONE_REGISTRY.build(
                config["architecture"],
                pretrained=False,
                # Lu et non supposé : la table de positions du point de contrôle
                # porte quatre dates, et bâtir le réseau pour une seule refuse le
                # chargement par incompatibilité de forme.
                num_frames=config["num_frames"],
                bands=list(BANDES),
            )
        except Exception as exc:  # noqa: BLE001 — code amont : le message importe plus que le type
            raise WorkerError(
                f"construction de l'encodeur impossible ({config['architecture']}) : "
                f"{type(exc).__name__}: {exc} — `{REPAIR}` si la pile a bougé"
            ) from exc

        poids = unique_fichier(chemin, "*.pt", "point de contrôle")
        état = _etat(torch, poids)
        rapport = self.model.load_state_dict(depouiller_prefixe(état, "encoder."), strict=False)
        exiger_chargement_complet(rapport, f"chargement de {poids.name}")
        écartées = sum(1 for clé in état if not clé.startswith("encoder."))

        self.model = self.model.eval().to("mps")
        self.poser_normalisation(config["mean"], config["std"], config["echelle"])
        self.dimensions = int(config["num_features"])
        self.identite = {
            "ref": variant.get("ref"),
            "repo": variant.get("repo"),
            "revision": variant.get("revision"),
            "architecture": config["architecture"],
            "bands": list(BANDES),
            "pooling": self.pooling,
        }
        self.mps_counters()

        return {
            "pooling": self.pooling,
            "dimensions": self.dimensions,
            "patch": PATCH,
            # Dit nommément plutôt que laissé au silence : ce sont les poids du
            # décodeur de reconstruction, que l'empreinte n'emploie pas. Savoir
            # combien de clés ont été écartées vaut mieux que de constater qu'il
            # en manque.
            "checkpoint_keys_ignored": écartées,
            "versions": self.versions(),
        }

    # --- inférence -----------------------------------------------------------

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.model is None or self.torch is None:
            raise WorkerError("infer avant load — aucun modèle en mémoire")
        avertissements: list[str] = []

        source = resolve_raster(request.get("raster"), request.output_dir)
        indices = self.reglage(request, "band_indices", None)
        normaliser = bool(self.reglage(request, "normalize", True))
        tuile, note = tuile_alignee(int(self.reglage(request, "tile_size", DEFAUT_TUILE)))
        if note:
            avertissements.append(note)

        progress(10, "lecture du raster")
        vecteurs, fenêtres, scène = self._encoder_fichier(source, indices, tuile)
        vecteurs, empreinte = self._agreger_scene(vecteurs, normaliser)

        similarité: float | None = None
        seconde: dict[str, Any] | None = None
        comparaison = self.reglage(request, "compare_to", None)
        if comparaison:
            progress(60, "seconde scène")
            autre_chemin = resolve_raster(comparaison, request.output_dir, "compare_to")
            autres, autres_fenêtres, autre_scène = self._encoder_fichier(
                autre_chemin, indices, tuile
            )
            # Le cosinus est invariant d'échelle, donc ramener ou non les
            # vecteurs à la norme 1 ne le déplace pas. Ce qui le déplace — de
            # 0,9653 à 0,9652, mesuré — est que `normalize` normalise **chaque
            # tuile avant la moyenne**, ce qui est une autre pondération des
            # tuiles entre elles. Le document porte le réglage employé pour cette
            # raison, et pas pour la courtoisie.
            _, autre_empreinte = self._agreger_scene(autres, normaliser)
            similarité = cosinus(empreinte, autre_empreinte)
            seconde = {
                "path": autre_chemin.name,
                "width": autre_scène.largeur,
                "height": autre_scène.hauteur,
                "band_indices": list(autre_scène.indices),
                "tiles": len(autres_fenêtres),
                "scene_embedding": [round(float(v), 6) for v in autre_empreinte],
            }
            if list(autre_scène.indices) != list(scène.indices):
                avertissements.append(
                    "les deux scènes n'ont pas été lues sur les mêmes bandes : le cosinus "
                    "compare deux espaces étrangers et ne veut rien dire"
                )

        progress(85, "écriture")
        document = {
            # Ce bloc n'est pas de la courtoisie : deux modèles de cette capacité
            # rendent des vecteurs de même longueur qui n'appartiennent pas au
            # même espace, et deux jobs du même modèle sur des bandes ou un
            # découpage différents non plus. Rien d'autre que ces lignes ne
            # l'empêcherait.
            **self.identite,
            "band_indices": list(scène.indices),
            "raster_bands": scène.nombre_bandes,
            "width": scène.largeur,
            "height": scène.hauteur,
            "crs": scène.crs,
            "tile_size": tuile,
            "patch": PATCH,
            "normalized": normaliser,
            "dimensions": self.dimensions,
            "count": len(vecteurs),
            # Le vecteur de scène est là pour être comparé ; les vecteurs de
            # tuile sont là pour qu'on sache d'où il vient. Ne rendre que le
            # premier ferait d'une moyenne une mesure.
            "scene_embedding": [round(float(v), 6) for v in empreinte],
            "tiles": [
                {
                    "x": x0,
                    "y": y0,
                    "width": largeur,
                    "height": hauteur,
                    "embedding": [round(float(v), 6) for v in vecteur],
                }
                for (y0, x0, hauteur, largeur), vecteur in zip(fenêtres, vecteurs, strict=True)
            ],
            "compare_to": seconde,
            "similarity": similarité,
            "warnings": avertissements,
        }
        (request.output_dir / SORTIE_JSON).write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        sortie: dict[str, Any] = {
            "embeddings": SORTIE_JSON,
            "count": len(vecteurs),
            "dimensions": self.dimensions,
        }
        if similarité is not None:
            sortie["similarity"] = similarité

        métriques: dict[str, Any] = {
            # Le cosinus est répété dans les métriques, et ce n'est pas une
            # redondance : c'est le seul nombre lisible que ce job produise, et
            # la ligne de télémétrie n'affiche pas les sorties.
            **({"similarity": similarité} if similarité is not None else {}),
            "pooling": self.pooling,
            "tiles": len(vecteurs),
            "tile_size": tuile,
            "width": scène.largeur,
            "height": scène.hauteur,
            "band_indices": list(scène.indices),
            "dimensions": self.dimensions,
            "scene_vector_norm": round(norme(empreinte), 6),
            **self.mps_counters(),
            "peak_memory_bytes": self.peak_memory_bytes(),
        }
        if avertissements:
            métriques["warnings"] = avertissements
        return InferResult(output=sortie, metrics=métriques)

    # --- détails -------------------------------------------------------------

    @staticmethod
    def _agreger_scene(
        vecteurs: list[list[float]], normaliser: bool
    ) -> tuple[list[list[float]], list[float]]:
        """Les vecteurs de tuile et l'empreinte de scène, sous le même régime.

        `normalize` s'applique aux deux ou à aucun : une empreinte de scène
        toujours ramenée à la norme 1 pendant que les tuiles gardent la leur
        rendrait un document dont les nombres ne s'expliquent pas entre eux. Le
        cosinus, lui, est invariant d'échelle et ne bouge dans aucun des deux cas.
        """
        if normaliser:
            vecteurs = [normaliser_l2(v) for v in vecteurs]
        empreinte = moyenne_vecteurs(vecteurs)
        return vecteurs, normaliser_l2(empreinte) if normaliser else empreinte

    def _encoder_fichier(
        self, chemin: Path, indices: Any, tuile: int
    ) -> tuple[list[list[float]], list[tuple[int, int, int, int]], Any]:
        """Un raster vers ses vecteurs de tuile, avec les fenêtres dont ils viennent."""
        scène = self.lire_scene(chemin, indices)
        vecteurs: list[list[float]] = []
        fenêtres: list[tuple[int, int, int, int]] = []
        for y0 in plan_tuiles(scène.hauteur, tuile):
            for x0 in plan_tuiles(scène.largeur, tuile):
                hauteur = min(tuile, scène.hauteur - y0)
                largeur = min(tuile, scène.largeur - x0)
                vecteurs.append(
                    self._encoder_tuile(
                        scène.bandes[:, y0 : y0 + hauteur, x0 : x0 + largeur]
                    )
                )
                fenêtres.append((y0, x0, hauteur, largeur))
        return vecteurs, fenêtres, scène

    def _encoder_tuile(self, tuile: Any) -> list[float]:
        """Une fenêtre normalisée vers un vecteur brut.

        Le nombre de jetons de tête est **déduit** de la forme rendue plutôt que
        supposé : le réseau porte un CLS aujourd'hui, une famille voisine y
        ajouterait des registres, et une moyenne qui les mêlerait aux patches
        rendrait un vecteur qui n'est celui d'aucun espace. Le déduire coûte une
        division ; le supposer ne coûte rien tant qu'on ne s'en aperçoit pas.
        """
        torch = self.torch
        hauteur, largeur = int(tuile.shape[-2]), int(tuile.shape[-1])
        cible_h = aligner_multiple(hauteur, PATCH)
        cible_l = aligner_multiple(largeur, PATCH)

        entrée = torch.from_numpy(tuile).unsqueeze(0).to("mps")
        entrée = self.rembourrer(entrée, cible_h, cible_l)
        # (B, C, T, H, W) : le réseau est temporel, et une scène d'une seule date
        # est une séquence de longueur un. Sans cette dimension il échoue dans le
        # `patch_embed` par « not enough values to unpack (expected 5, got 4) »,
        # ce qui ne dit pas qu'il manque une date.
        entrée = entrée.unsqueeze(2)
        with torch.no_grad():
            niveaux = self.model(entrée)
        dernier = niveaux[-1] if isinstance(niveaux, (list, tuple)) else niveaux
        torch.mps.synchronize()
        self.mps_counters()

        patches = (cible_h // PATCH) * (cible_l // PATCH)
        self.prefixe = jetons_de_tete(int(dernier.shape[1]), patches)
        vecteur = agreger(dernier, self.pooling, self.prefixe)
        return [float(v) for v in vecteur.float().cpu().numpy().reshape(-1)]


# --- fonctions pures ----------------------------------------------------------
#
# Elles ne touchent ni torch ni rasterio : c'est ce qui les rend vérifiables en
# CI, sans Apple Silicon, sans poids et sans venv de runtime.


def verifier_agregation(demandée: Any) -> str:
    """L'agrégation du variant, ou un refus qui dit quoi corriger.

    Le défaut est `mean`. Le choix appartient au variant et non au contrat, pour
    la raison de `dinov3` : ce sont deux espaces vectoriels et non deux réglages,
    et un contrat qui l'exposerait laisserait l'UI le présenter comme une
    préférence — après quoi deux jobs de la même scène rendraient des vecteurs
    incomparables sans que rien ne le signale.
    """
    valeur = str(demandée or "mean").strip().lower()
    if valeur not in AGREGATIONS:
        raise WorkerError(
            f"agrégation inconnue : {valeur!r} — attendu {' ou '.join(AGREGATIONS)} "
            "dans `options.pooling` du manifeste"
        )
    return valeur


def jetons_de_tete(total: int, patches: int) -> int:
    """Combien de jetons précèdent les patches, déduit des deux comptes.

    Un écart négatif signale que le réseau n'a pas rendu ce qu'on croit — moins de
    jetons que de patches attendus —, et c'est exactement le genre de dérive qui
    passe inaperçue : la moyenne serait prise sur autre chose, et le vecteur
    aurait la bonne longueur.
    """
    if patches <= 0:
        raise WorkerError("aucune position de patch : la tuile est vide")
    prefixe = int(total) - int(patches)
    if prefixe < 0:
        raise WorkerError(
            f"le réseau rend {total} jetons pour {patches} positions de patch attendues : "
            "la forme de sortie d'amont a changé, et une moyenne prise dessus ne serait "
            "l'empreinte de rien"
        )
    return prefixe


def agreger(jetons: Any, pooling: str, prefixe: int) -> Any:
    """Une suite de jetons vers un seul vecteur, jetons de tête écartés ou retenus."""
    if pooling == "cls":
        if prefixe < 1:
            raise WorkerError(
                "agrégation `cls` impossible : ce réseau ne rend aucun jeton de tête — "
                "`options.pooling: mean`"
            )
        return jetons[:, 0]
    return jetons[:, prefixe:].mean(dim=1)


def norme(vecteur: list[float]) -> float:
    return math.sqrt(sum(v * v for v in vecteur))


def normaliser_l2(vecteur: list[float]) -> list[float]:
    """Norme 1, ou le vecteur tel quel s'il est nul — diviser par zéro ne dit rien."""
    n = norme(vecteur)
    return [v / n for v in vecteur] if n > 0 else list(vecteur)


def moyenne_vecteurs(vecteurs: list[list[float]]) -> list[float]:
    """L'empreinte de scène : la moyenne des vecteurs de tuile, avant renormalisation.

    Moyenne et non concaténation : deux scènes de tailles différentes n'ont pas le
    même nombre de tuiles, et une empreinte dont la longueur dépendrait du
    découpage ne se comparerait à rien.
    """
    if not vecteurs:
        raise WorkerError("aucune tuile encodée : la scène est vide")
    longueurs = {len(v) for v in vecteurs}
    if len(longueurs) != 1:
        raise WorkerError(
            f"vecteurs de longueurs différentes ({sorted(longueurs)}) : les tuiles n'ont "
            "pas toutes été encodées par le même chemin"
        )
    compte = len(vecteurs)
    return [sum(colonne) / compte for colonne in zip(*vecteurs, strict=True)]


def cosinus(a: list[float], b: list[float]) -> float | None:
    """Cosinus entre deux vecteurs, ou None quand l'un des deux est nul."""
    if len(a) != len(b):
        raise WorkerError(
            f"vecteurs de longueurs différentes ({len(a)} et {len(b)}) : les deux scènes "
            "n'ont pas été encodées par le même modèle"
        )
    dénominateur = norme(a) * norme(b)
    if dénominateur <= 0:
        return None
    return round(sum(x * y for x, y in zip(a, b, strict=True)) / dénominateur, 4)


def tuile_alignee(demandée: int) -> tuple[int, str | None]:
    """La tuile ramenée au multiple du patch, et ce qu'il faut en dire.

    Ramenée plutôt que refusée : le contrat borne déjà ce champ à un multiple de
    16, et un worker appelé directement doit pouvoir travailler. Mais le silence
    serait pire que le refus — le rembourrage entre dans la moyenne des jetons,
    donc dans le vecteur, et deux tuiles rembourrées différemment ne se comparent
    plus tout à fait.
    """
    if demandée < 1:
        raise WorkerError(f"tile_size = {demandée} : une tuile a au moins un pixel")
    aligné = aligner_multiple(demandée, PATCH)
    if aligné == demandée:
        return aligné, None
    return aligné, (
        f"tile_size {demandée} ramené à {aligné} : le réseau découpe en patches de "
        f"{PATCH} pixels, et le reste aurait été rembourré — donc compté dans la moyenne "
        "des jetons, donc dans le vecteur"
    )


def _lire_config(chemin: Path) -> dict[str, Any]:
    """L'architecture, la normalisation et le nombre de dates, lus chez l'amont.

    Les moyennes et écarts-types viennent du `config.json` publié avec les poids,
    et non d'une table du jeu de données du fine-tune voisin : ce ne sont pas les
    mêmes chiffres — 1087 contre 1413 sur le bleu — parce que ce ne sont pas les
    mêmes images. Les recopier ici, ou les emprunter au voisin, donnerait un
    encodeur qui tourne sur des entrées décalées sans que rien n'échoue.
    """
    fichier = chemin / "config.json"
    if not fichier.is_file():
        raise WorkerError(
            f"config.json absent de {chemin} — il porte l'architecture et la "
            "normalisation ; vérifier les `allow_patterns` du manifeste"
        )
    try:
        brut = json.loads(fichier.read_text())
    except (OSError, ValueError) as exc:
        raise WorkerError(f"config.json illisible : {exc}") from exc

    architecture = str(brut.get("architecture") or "").strip()
    if not architecture:
        raise WorkerError(
            "config.json ne déclare pas d'`architecture` : ce dépôt n'est pas celui d'un "
            "encodeur terratorch"
        )
    cfg = dict(brut.get("pretrained_cfg") or {})
    moyennes = list(cfg.get("mean") or [])
    écarts = list(cfg.get("std") or [])
    if len(moyennes) != CANAUX or len(écarts) != CANAUX:
        raise WorkerError(
            f"config.json annonce {len(moyennes)} moyennes et {len(écarts)} écarts-types "
            f"pour {CANAUX} canaux : la normalisation d'amont ne décrit pas ce réseau"
        )
    return {
        "architecture": architecture,
        "num_frames": int(cfg.get("num_frames") or 1),
        "num_features": int(brut.get("num_features") or cfg.get("embed_dim") or 0),
        "mean": [float(v) for v in moyennes],
        "std": [float(v) for v in écarts],
        # Les statistiques d'amont sont déjà à l'échelle des entiers de
        # réflectance : contrairement au fine-tune, aucune remise à l'échelle
        # n'est faite avant de soustraire la moyenne.
        "echelle": 1.0,
    }


def _etat(torch: Any, poids: Path) -> dict[str, Any]:
    """Le `state_dict` du point de contrôle, chargé en mode sûr.

    `weights_only=True` : ce fichier ne contient que des tenseurs, et torch 2.13
    le lit sans dérogation. Le vérifier valait mieux que d'ouvrir le dépicklage de
    code arbitraire « par précaution » sur un fichier venu du réseau.
    """
    try:
        brut = torch.load(poids, map_location="cpu", weights_only=True)
    except Exception as exc:  # noqa: BLE001 — remonte traduit
        raise WorkerError(
            f"point de contrôle illisible ({poids.name}) : {type(exc).__name__}: {exc}"
        ) from exc
    état = brut.get("state_dict") if isinstance(brut, dict) and "state_dict" in brut else brut
    if not isinstance(état, dict) or not état:
        raise WorkerError(f"{poids.name} ne porte pas de `state_dict` exploitable")
    return état


if __name__ == "__main__":
    raise SystemExit(main(PrithviEmbedWorker))
