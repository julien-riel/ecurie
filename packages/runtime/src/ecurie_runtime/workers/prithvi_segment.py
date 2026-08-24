"""Adaptateur `terratorch`, chemin **segmentation satellite** : Prithvi-EO-2.0 + UperNet.

Première capacité du parc dont l'entrée n'est pas une image. Six bandes entrent,
une carte de classes géoréférencée sort. Le travail de cet adaptateur tient en
quatre gestes, et aucun n'est évident — trois d'entre eux répondent à un défaut
qui **ne lève rien** quand on l'ignore.

**Le préfixe des poids.** Les 381 clés du point de contrôle sont toutes préfixées
`model.`. Chargées telles quelles avec `strict=False`, elles laissent 368 poids
manquants et 381 inattendus, et le réseau tourne : forme de sortie normale,
aucune exception, et du bruit à la place du masque. Dépouillées, c'est 0 et 0.
C'est le seul contrôle qui prouve que les poids sont arrivés, et il est
éliminatoire ici (`exiger_chargement_complet`).

**Le backbone pré-entraîné du `config.yaml`.** Le fichier porte
`backbone_pretrained: true` : construit tel quel, le modèle irait chercher
1,33 Go sur le réseau avant de poser le fine-tune par-dessus. Un worker ne
télécharge jamais. Le drapeau est donc renversé avant l'appel à la fabrique, et
le chemin a été vérifié avec `HF_HUB_OFFLINE=1` — construction en 4,4 s, aucune
requête.

**La taille que Metal refuse.** Le module pyramidal du décodeur agrège vers une
grille de 6 × 6 sur une carte réduite d'un facteur 64 (32 par le patch, 2 encore
par `decoder_scale_modules`), et MPS exige que la division tombe juste :
« Adaptive pool MPS: input sizes must be divisible by output sizes ». Le côté doit
donc être un multiple de 192. Mesuré : 384, 576, 768 et 960 passent ; 224, 256,
480 et **512** échouent — c'est-à-dire la taille native des chips que le dépôt
d'amont publie en exemple. `PYTORCH_ENABLE_MPS_FALLBACK=1` n'y change rien,
l'erreur venant du noyau Metal et non du dispatch. L'adaptateur rembourre donc au
multiple supérieur puis recadre, et le dit dans ses avertissements quand la
demande l'y a obligé.

**Le tuilage.** Une scène plus grande qu'une tuile est découpée, les logits sont
accumulés sur la scène entière, et l'argmax vient à la fin. Accumuler plutôt que
recoller des masques : sur une frontière de tuile le modèle n'a rien vu d'un
côté, et deux décisions binaires mises bout à bout laissent une couture que la
somme des logits n'a pas.

**La légende appartient au manifeste, pas à ce fichier.** Le modèle servi ici a
deux classes, eau et non-eau ; le suivant en aura d'autres. `options.class_names`
et `options.class_of_interest` disent lesquelles, et l'adaptateur se contente de
compter — une table d'espèces thématiques codée ici aurait vieilli au deuxième
modèle.

Rien de torch, rasterio ni terratorch n'est importé au niveau du module (voir
`workers/__init__.py`).
"""

import json
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
    ECHELLE_REFLECTANCE,
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

MASQUE_TIF = "masque.tif"
MASQUE_PNG = "masque.png"
SURIMPRESSION_PNG = "surimpression.png"
CLASSES_JSON = "classes.json"

#: Le pas que Metal impose au décodeur. Voir l'en-tête : ce n'est pas un arrondi
#: de confort, c'est la condition pour que `adaptive_avg_pool2d` accepte de
#: tourner sur ce périphérique.
PAS_MPS = 192

DEFAUT_TUILE = 576

#: Teintes des classes dans le PNG et la surimpression. Fixées ici et non tirées
#: d'une table du modèle : ce sont des couleurs d'affichage, et deux jobs de la
#: même scène doivent se superposer à l'œil. Le gris est la classe de fond, le
#: reste vient d'une palette qualitative lisible en niveaux de gris.
TEINTES = (
    (55, 60, 66),
    (43, 130, 210),
    (222, 143, 44),
    (86, 168, 92),
    (183, 74, 138),
    (196, 60, 60),
)


class PrithviSegmentWorker(PrithviWorker):
    """Une scène multi-bandes vers une carte de classes géoréférencée."""

    name = "prithvi-segment"

    def __init__(self) -> None:
        super().__init__()
        self.identite: dict[str, Any] = {}
        self.noms_classes: tuple[str, ...] = ()
        self.classe_interet: int = 1
        self.nombre_classes: int = 0

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
            import yaml
            from terratorch.models import EncoderDecoderFactory
        except ImportError as exc:
            raise WorkerError(
                f"runtime terratorch indisponible dans cet environnement ({exc}) — "
                f"`{REPAIR}`. Une installation sans borne sur torchgeo échoue ici même, "
                "par « cannot import name 'utils' from 'torchgeo.trainers' »"
            ) from exc

        chemin = weights_dir(variant)
        config = _lire_config(chemin, yaml)
        arguments = dict(config["arguments"])

        # Renversé avant la fabrique, jamais après : `build_model` construit le
        # backbone, et c'est cette construction-là qui téléchargerait.
        arguments["backbone_pretrained"] = False

        self.nombre_classes = int(arguments.get("num_classes") or 0)
        if self.nombre_classes < 2:
            raise WorkerError(
                f"`num_classes` vaut {self.nombre_classes} dans la configuration d'amont : "
                "une segmentation a au moins deux classes"
            )

        try:
            self.model = EncoderDecoderFactory().build_model(task="segmentation", **arguments)
        except Exception as exc:  # noqa: BLE001 — code amont : le message importe plus que le type
            raise WorkerError(
                f"construction du modèle impossible : {type(exc).__name__}: {exc} — "
                f"`{REPAIR}` si la pile a bougé"
            ) from exc

        poids = unique_fichier(chemin, "*.pt", "point de contrôle")
        rapport = self.model.load_state_dict(
            depouiller_prefixe(_etat(torch, poids), "model."), strict=False
        )
        exiger_chargement_complet(rapport, f"chargement de {poids.name}")

        self.model = self.model.eval().to("mps")
        self.poser_normalisation(*_normalisation(config["bandes"]), config["echelle"])
        self.noms_classes = noms_classes(self.options.get("class_names"), self.nombre_classes)
        self.classe_interet = classe_interet(
            self.options.get("class_of_interest"), self.nombre_classes
        )
        self.identite = {
            "ref": variant.get("ref"),
            "repo": variant.get("repo"),
            "revision": variant.get("revision"),
            "backbone": str(arguments.get("backbone") or "?"),
            "decoder": str(arguments.get("decoder") or "?"),
            "bands": list(config["bandes"]),
        }
        self.mps_counters()

        return {
            "classes": list(self.noms_classes),
            "class_of_interest": self.classe_interet,
            "tile_multiple": PAS_MPS,
            "reflectance_scale": self.echelle,
            "versions": self.versions(),
        }

    # --- inférence -----------------------------------------------------------

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.model is None or self.torch is None:
            raise WorkerError("infer avant load — aucun modèle en mémoire")
        np = self.np
        avertissements: list[str] = []

        source = resolve_raster(request.get("raster"), request.output_dir)
        tuile = int(self.reglage(request, "tile_size", DEFAUT_TUILE))
        recouvrement = int(self.reglage(request, "overlap", 0))
        if tuile < 1:
            raise WorkerError(f"tile_size = {tuile} : une tuile a au moins un pixel")

        progress(5, "lecture du raster")
        scène = self.lire_scene(source, self.reglage(request, "band_indices", None))
        if not scène.crs:
            avertissements.append(
                f"{source.name} ne porte aucun système de coordonnées : la carte de classes "
                "est produite, mais elle ne se superpose à rien. C'est un TIFF, pas un "
                "GeoTIFF, et les champs `crs` et `bounds` restent absents de la sortie"
            )

        départs_y = plan_tuiles(scène.hauteur, tuile, recouvrement)
        départs_x = plan_tuiles(scène.largeur, tuile, recouvrement)
        total = len(départs_y) * len(départs_x)

        # Accumulation des logits sur la scène entière, puis un seul argmax.
        # Recoller des masques déjà décidés laisserait une couture aux frontières
        # de tuile, là où le modèle n'a vu qu'un côté du contexte.
        cumul = np.zeros((self.nombre_classes, scène.hauteur, scène.largeur), dtype="float32")

        rembourrées: set[tuple[int, int]] = set()
        for rang, (y0, x0) in enumerate(
            ((y, x) for y in départs_y for x in départs_x), start=1
        ):
            hauteur = min(tuile, scène.hauteur - y0)
            largeur = min(tuile, scène.largeur - x0)
            progress(
                10 + int(70 * rang / max(1, total)),
                f"tuile {rang}/{total}",
            )
            logits, forme = self._segmenter(
                scène.bandes[:, y0 : y0 + hauteur, x0 : x0 + largeur]
            )
            if forme != (hauteur, largeur):
                rembourrées.add(forme)
            cumul[:, y0 : y0 + hauteur, x0 : x0 + largeur] += logits

        if rembourrées:
            avertissements.append(
                "tuiles rembourrées avant le réseau : "
                + ", ".join(f"{haut}×{large}" for haut, large in sorted(rembourrées))
                + f" — le décodeur n'accepte sur Metal que des côtés multiples de {PAS_MPS}, "
                "et la scène ou la tuile demandée n'en est pas un. Le résultat est recadré "
                "à la taille d'origine ; seul le temps de calcul est perdu"
            )

        progress(85, "écriture des sorties")
        carte = cumul.argmax(axis=0).astype("uint8")
        comptes = [int((carte == valeur).sum()) for valeur in range(self.nombre_classes)]
        pixels = int(carte.size)
        couverture = comptes[self.classe_interet] / pixels if pixels else 0.0

        self._ecrire_geotiff(request.output_dir / MASQUE_TIF, carte, scène)
        self._ecrire_png(request.output_dir / MASQUE_PNG, carte)
        self._ecrire_surimpression(request.output_dir / SURIMPRESSION_PNG, carte, scène)

        document = {
            **self.identite,
            "band_indices": list(scène.indices),
            "raster_bands": scène.nombre_bandes,
            "width": scène.largeur,
            "height": scène.hauteur,
            "crs": scène.crs,
            "bounds": scène.bounds,
            "tile_size": tuile,
            "overlap": recouvrement,
            "tiles": total,
            "class_of_interest": self.classe_interet,
            "coverage": round(couverture, 6),
            "classes": [
                {
                    "value": valeur,
                    "name": self.noms_classes[valeur],
                    "pixels": comptes[valeur],
                    "fraction": round(comptes[valeur] / pixels, 6) if pixels else 0.0,
                }
                for valeur in range(self.nombre_classes)
            ],
            "warnings": avertissements,
        }
        (request.output_dir / CLASSES_JSON).write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        sortie: dict[str, Any] = {
            "mask_geotiff": MASQUE_TIF,
            "mask": MASQUE_PNG,
            "overlay": SURIMPRESSION_PNG,
            "classes": CLASSES_JSON,
            "coverage": round(couverture, 6),
        }
        if scène.crs:
            sortie["crs"] = scène.crs
            if scène.bounds:
                sortie["bounds"] = scène.bounds

        métriques: dict[str, Any] = {
            "coverage": round(couverture, 6),
            "tiles": total,
            "tile_size": tuile,
            "overlap": recouvrement,
            "width": scène.largeur,
            "height": scène.hauteur,
            "band_indices": list(scène.indices),
            "class_pixels": comptes,
            **self.mps_counters(),
            "peak_memory_bytes": self.peak_memory_bytes(),
        }
        if avertissements:
            métriques["warnings"] = avertissements
        return InferResult(output=sortie, metrics=métriques)

    # --- détails -------------------------------------------------------------

    def _segmenter(self, tuile: Any) -> tuple[Any, tuple[int, int]]:
        """Une fenêtre normalisée vers ses logits, recadrés à la taille demandée.

        Rend aussi la forme réellement soumise au réseau : c'est elle qui dit si
        un rembourrage a eu lieu, et c'est ce que le job remonte en avertissement
        plutôt que de le taire.
        """
        torch = self.torch
        hauteur, largeur = int(tuile.shape[-2]), int(tuile.shape[-1])
        cible_h = aligner_multiple(hauteur, PAS_MPS)
        cible_l = aligner_multiple(largeur, PAS_MPS)

        entrée = torch.from_numpy(tuile).unsqueeze(0).to("mps")
        entrée = self.rembourrer(entrée, cible_h, cible_l)
        try:
            with torch.no_grad():
                brut = self.model(entrée)
        except RuntimeError as exc:
            raise WorkerError(
                f"segmentation impossible sur une tuile {cible_h}×{cible_l} : {exc} — "
                f"le décodeur n'accepte sur Metal que des côtés multiples de {PAS_MPS}"
            ) from exc
        logits = getattr(brut, "output", brut)
        torch.mps.synchronize()
        self.mps_counters()
        recadré = logits[0, :, :hauteur, :largeur].float().cpu().numpy()
        return recadré, (cible_h, cible_l)

    def _ecrire_geotiff(self, cible: Path, carte: Any, scène: Any) -> None:
        """La carte de classes, avec le repère de l'entrée quand elle en avait un.

        C'est ce fichier qui sépare cette capacité d'`image-segment` : un PNG se
        regarde, celui-ci se mesure en hectares. Le CRS et la transformation
        affine sont **recopiés**, jamais recalculés — la carte a exactement les
        dimensions de l'entrée, donc sa géométrie est la sienne.
        """
        profil = {
            "driver": "GTiff",
            "height": int(carte.shape[0]),
            "width": int(carte.shape[1]),
            "count": 1,
            "dtype": "uint8",
            "compress": "deflate",
            "predictor": 2,
            "zlevel": 9,
        }
        if scène.crs:
            profil["crs"] = scène.crs
            profil["transform"] = scène.transform
        with self.rasterio.open(cible, "w", **profil) as sortie:
            sortie.write(carte, 1)
            sortie.set_band_description(1, "classe")

    def _ecrire_png(self, cible: Path, carte: Any) -> None:
        np = self.np
        palette = np.array(
            [TEINTES[valeur % len(TEINTES)] for valeur in range(self.nombre_classes)],
            dtype="uint8",
        )
        _enregistrer_png(palette[carte], cible)

    def _ecrire_surimpression(self, cible: Path, carte: Any, scène: Any) -> None:
        """La classe teintée par-dessus une composition en couleurs naturelles.

        Les trois bandes visibles sont **dénormalisées** plutôt que relues sur le
        disque : garder une seconde copie du raster en mémoire pour un aperçu
        aurait doublé le poste le plus lourd du côté Python, et l'opération est
        exactement réversible.

        L'étirement sur les centiles 2 et 98 n'est pas cosmétique : une scène
        satellite brute est presque noire, et un masque posé sur du noir ne se
        vérifie pas. Il est fait **bande par bande**, comme le font les visionneuses
        géospatiales : un étirement commun aux trois garderait la dominante bleue
        de l'atmosphère et rendrait le rouge illisible.
        """
        np = self.np
        visibles = scène.bandes[:3] * self.ecarts[:3] + self.moyennes[:3]
        rvb = np.stack([visibles[2], visibles[1], visibles[0]], axis=-1)
        bas = np.percentile(rvb, 2.0, axis=(0, 1))
        haut = np.percentile(rvb, 98.0, axis=(0, 1))
        étendue = np.maximum(haut - bas, 1e-6)
        fond = (np.clip((rvb - bas) / étendue, 0.0, 1.0) * 255.0).astype("uint8")

        palette = np.array(
            [TEINTES[valeur % len(TEINTES)] for valeur in range(self.nombre_classes)],
            dtype="float32",
        )
        teinte = palette[carte]
        # Le fond n'est pas teinté : la classe 0 est ce qu'on ne cherche pas, et
        # la recouvrir cacherait justement ce à quoi on compare.
        marque = (carte != 0)[..., None]
        mélange = np.where(marque, 0.45 * fond + 0.55 * teinte, fond)
        _enregistrer_png(mélange.astype("uint8"), cible)


# --- fonctions pures ----------------------------------------------------------
#
# Elles ne touchent ni torch ni rasterio : c'est ce qui les rend vérifiables en
# CI, sans Apple Silicon, sans poids et sans venv de runtime.


def noms_classes(brut: Any, nombre: int) -> tuple[str, ...]:
    """La légende du manifeste, complétée par des noms neutres si elle est courte.

    Complétée plutôt que refusée : une légende absente n'empêche pas de segmenter,
    et un job qui échouerait pour un libellé manquant serait une capacité rendue
    indisponible par une question d'affichage. En revanche, une légende **plus
    longue** que le nombre de classes est refusée : elle signale un manifeste écrit
    pour d'autres poids, et le contresens ne s'arrêterait pas au libellé.
    """
    if brut is None:
        return tuple(f"classe {valeur}" for valeur in range(nombre))
    if isinstance(brut, (str, bytes)) or not hasattr(brut, "__iter__"):
        raise WorkerError(
            f"`options.class_names` : liste de libellés attendue, reçu {type(brut).__name__}"
        )
    noms = [str(nom) for nom in brut]
    if len(noms) > nombre:
        raise WorkerError(
            f"`options.class_names` porte {len(noms)} libellés pour {nombre} classes : "
            "ce manifeste décrit d'autres poids que ceux qui viennent d'être chargés"
        )
    return tuple(noms + [f"classe {valeur}" for valeur in range(len(noms), nombre)])


def classe_interet(brut: Any, nombre: int) -> int:
    """La classe dont `coverage` compte les pixels. Défaut : la première non-fond.

    Le défaut vaut 1 et non 0 parce que la valeur 0 est, chez tous les modèles de
    cette famille, l'absence du phénomène cherché. Un modèle qui en déciderait
    autrement doit le déclarer, et ce champ existe pour cela.
    """
    if brut is None:
        return 1 if nombre > 1 else 0
    if isinstance(brut, bool) or not isinstance(brut, (int, float)):
        raise WorkerError(
            f"`options.class_of_interest` : entier attendu, reçu {type(brut).__name__}"
        )
    valeur = int(brut)
    if not 0 <= valeur < nombre:
        raise WorkerError(
            f"`options.class_of_interest` = {valeur} : ce modèle a {nombre} classes, "
            f"numérotées de 0 à {nombre - 1}"
        )
    return valeur


def _normalisation(bandes: tuple[str, ...]) -> tuple[list[float], list[float]]:
    """Moyennes et écarts-types du jeu de données du fine-tune, lus dans terratorch.

    Lus chez l'amont plutôt que recopiés ici : ce sont eux qui décident de ce que
    le réseau voit, et une constante figée dans un adaptateur cesserait
    silencieusement d'être celle du modèle à la première mise à jour de la pile.
    """
    try:
        from terratorch.datamodules import sen1floods11
    except ImportError as exc:
        raise WorkerError(
            f"table de normalisation introuvable dans terratorch ({exc}) — `{REPAIR}`"
        ) from exc
    moyennes, écarts = sen1floods11.MEANS, sen1floods11.STDS
    manquantes = [nom for nom in bandes if nom not in moyennes or nom not in écarts]
    if manquantes:
        raise WorkerError(
            f"bande(s) sans statistique de normalisation : {', '.join(manquantes)} — "
            "la configuration d'amont nomme des bandes que le jeu de données ne connaît pas"
        )
    return [float(moyennes[nom]) for nom in bandes], [float(écarts[nom]) for nom in bandes]


def _lire_config(chemin: Path, yaml: Any) -> dict[str, Any]:
    """Les arguments du modèle, ses bandes et l'échelle des réflectances.

    Tout vient du `config.yaml` que le dépôt publie à côté des poids : c'est la
    seule description de l'assemblage qui soit sûre d'être celle du fine-tune. Y
    substituer des constantes écrites ici reviendrait à construire un modèle
    voisin et à charger les poids de celui-ci dedans, ce qui ne lèverait rien tant
    que les formes coïncident.
    """
    fichier = chemin / "config.yaml"
    if not fichier.is_file():
        raise WorkerError(
            f"config.yaml absent de {chemin} — c'est lui qui décrit l'assemblage du "
            "fine-tune ; vérifier les `allow_patterns` du manifeste"
        )
    try:
        brut = yaml.safe_load(fichier.read_text())
    except Exception as exc:  # noqa: BLE001 — remonte traduit
        raise WorkerError(f"config.yaml illisible : {type(exc).__name__}: {exc}") from exc

    try:
        arguments = dict(brut["model"]["init_args"]["model_args"])
    except (KeyError, TypeError) as exc:
        raise WorkerError(
            "config.yaml ne porte pas `model.init_args.model_args` : ce n'est pas la "
            "configuration d'un fine-tune terratorch"
        ) from exc

    bandes = tuple(str(nom) for nom in (arguments.get("backbone_bands") or BANDES))
    échelle = ECHELLE_REFLECTANCE
    try:
        déclarée = brut["data"]["init_args"]["constant_scale"]
    except (KeyError, TypeError):
        déclarée = None
    if isinstance(déclarée, (int, float)) and not isinstance(déclarée, bool) and déclarée > 0:
        échelle = float(déclarée)
    return {"arguments": arguments, "bandes": bandes, "echelle": échelle}


def _etat(torch: Any, poids: Path) -> dict[str, Any]:
    """Le `state_dict` du point de contrôle, chargé en mode sûr.

    `weights_only=True` : le fichier est un checkpoint Lightning, mais il ne
    contient que des tenseurs et des scalaires, et torch 2.13 le lit sans
    dérogation. Le vérifier valait mieux que d'ouvrir le dépicklage de code
    arbitraire « par précaution » sur un fichier venu du réseau.
    """
    try:
        brut = torch.load(poids, map_location="cpu", weights_only=True)
    except Exception as exc:  # noqa: BLE001 — remonte traduit
        raise WorkerError(
            f"point de contrôle illisible ({poids.name}) : {type(exc).__name__}: {exc}"
        ) from exc
    état = brut.get("state_dict") if isinstance(brut, dict) else None
    if état is None and isinstance(brut, dict):
        état = brut
    if not isinstance(état, dict) or not état:
        raise WorkerError(
            f"{poids.name} ne porte pas de `state_dict` exploitable "
            f"(clés lues : {', '.join(list(brut)[:5]) if isinstance(brut, dict) else '—'})"
        )
    return état


def _enregistrer_png(tableau: Any, cible: Path) -> None:
    try:
        from PIL import Image
    except ImportError as exc:
        raise WorkerError(f"Pillow absent de l'environnement ({exc}) — `{REPAIR}`") from exc
    Image.fromarray(tableau, mode="RGB").save(cible)


if __name__ == "__main__":
    raise SystemExit(main(PrithviSegmentWorker))
