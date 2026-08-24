"""Charge type de `geo-segment` et `geo-embed` : une scène satellite calculée.

    uv run --project runtimes/terratorch python tools/bench_assets.py geo

**Pourquoi une scène calculée, alors que le dépôt amont en publie trois.**
`Prithvi-EO-2.0-300M-TL-Sen1Floods11` sert trois chips d'exemple, et ils
fonctionnent. Les committer ici aurait refait exactement la faute que le dépôt a
consignée comme sa dette : ce sont des extraits de Sen1Floods11, imagerie tierce
dont le projet amont ne publie aucun fichier de licence — l'API GitHub rend
`license: null`, et `LICENSE` répond 404. Le README du banc pose que les entrées
« n'ont ni licence ni provenance à suivre, et se refabriquent à l'identique », et
que sur les six actifs orphelins du dossier « on ne recommence pas ».

**Ce que la recette doit produire et qu'aucune autre du dépôt ne sait faire.**
Six bandes, dont trois invisibles à l'œil. Un PNG n'a pas de place pour le proche
infrarouge, et c'est précisément là que l'eau se sépare de l'ombre d'un nuage :
sa réflectance s'effondre au-delà de 800 nm quand celle de la végétation
culmine. Une scène RGB colorée en bleu ne ferait pas travailler le modèle sur ce
qui le distingue.

**La vérité terrain vient de la géométrie, pas d'une annotation.** Le masque
d'eau est posé d'abord — un cours sinueux et un lobe de crue, tous deux
analytiques —, et les treize bandes en découlent. La fraction d'eau est donc
comptée sur le masque avant l'écriture, à la fraction de pixel près, et c'est
elle que le fichier de charge inscrit. Un modèle qui rendrait la même couverture
sur n'importe quelle scène ne le montrerait pas ; ici la valeur attendue est
posée par construction, et non relevée sur une sortie du modèle.

**Aucune valeur attendue n'est écrite ici sur la foi d'un rapport.** Le dossier
d'instruction porte deux jeux de couvertures contradictoires pour les chips
d'exemple ; c'est précisément ce qu'une valeur attendue dont on ne peut pas
reconstituer le protocole vaut. Ce que cette recette garantit est la fraction
géométrique — le reste se mesure.

**Une seule scène, trois découpages.** Les deux charges types la relisent à trois
tailles de tuile. Pour `geo-segment` : 384, 576 et 768, seuls multiples de 192
utiles ici, parce que Metal refuse une division non entière dans le module
pyramidal du décodeur. Pour `geo-embed` : 192, 384 et 768, qui divisent
exactement 768 — les trois cas voient donc le même nombre total de patches
(2 304), découpés seize fois, quatre fois, puis une seule. Ce qui varie est la
portée de l'attention et le pic, et rien d'autre.

**Le format est celui d'un produit Sentinel-2 niveau 1C**, treize bandes int16
dans l'ordre B1…B12 avec B8A à l'indice 8, réflectance multipliée par dix mille.
C'est ce qui permet à la charge d'exercer la valeur par défaut de
`band_indices` — le seul champ du contrat dont une erreur ne se voit pas dans le
résultat. Une scène réduite aux six bandes utiles aurait pesé moitié moins et
n'aurait rien éprouvé.

**Le poids est le prix de cette famille, et il a été mesuré plutôt que subi.**
Un tel raster fait 15,3 Mio en clair. Le dossier d'instruction annonçait qu'il ne
se comprimerait pas — le bruit gaussien par pixel étant incompressible — et
concluait qu'il fallait tripler le dossier `assets/`. C'est vrai du LZW sans
prédicteur ; ce ne l'est pas ici. Trois mesures, dans l'ordre où elles ont été
faites : DEFLATE avec prédicteur horizontal en strip rend 8,97 Mio ; le pavage
par 256 et les bandes séparées ramènent à 8,33 ; et déclarer un pas
radiométrique de huit unités — quatre niveaux dans l'écart-type du bruit —
descend à **4,57 Mio**, soit moins que `parole-tts.wav`. La taille obtenue est
imprimée à chaque fabrication.

La recette a été exécutée deux fois avant d'être committée : sha256 identiques.
Le bruit vient d'un `default_rng(20260824)` — PCG64, dont la suite est stable
d'une version de numpy à l'autre, contrairement au générateur hérité.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

#: rasterio ne vit que dans l'env de la capacité servie : c'est le seul du parc
#: à porter GDAL, et l'installer ailleurs pour fabriquer un fichier de charge
#: serait payer 1,4 Gio deux fois.
ENV = "terratorch"

CIBLE = "geo-scene-crue-768.tif"
CIBLES = (CIBLE,)

GRAINE = 20260824
COTE = 768

#: Treize bandes, comme un produit Sentinel-2 niveau 1C, et dans son ordre : le
#: défaut `band_indices: [1, 2, 3, 8, 11, 12]` du contrat y désigne B2, B3, B4,
#: B8A, B11 et B12, soit bleu, vert, rouge, proche infrarouge étroit et les deux
#: moyens infrarouges — exactement les six canaux du modèle.
BANDES = ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B9", "B10", "B11", "B12")

#: Réflectances × 10 000, une signature par couverture au sol. Elles ne
#: prétendent pas reproduire une bibliothèque spectrale : ce qui compte est
#: l'ordre de grandeur et surtout la **forme** — l'eau s'effondre dans
#: l'infrarouge quand la végétation y culmine, et c'est ce contraste-là, absent
#: du rouge-vert-bleu, que le modèle exploite.
SIGNATURES = {
    "eau": (1200, 1100, 950, 800, 700, 500, 420, 380, 350, 300, 60, 150, 90),
    "vegetation": (900, 700, 800, 550, 1200, 2600, 3000, 3200, 3300, 3100, 50, 1600, 700),
    "sol": (1300, 1250, 1400, 1750, 2000, 2300, 2450, 2500, 2550, 2400, 55, 2800, 2200),
    "bati": (1500, 1450, 1500, 1600, 1700, 1800, 1850, 1900, 1900, 1800, 60, 2000, 1750),
}
COUVERTURES_TERRESTRES = ("vegetation", "sol", "bati")

#: UTM 43N plutôt que WGS 84 : les coordonnées sont alors des mètres, donc une
#: emprise et un compte de pixels se traduisent en hectares sans reprojection.
#: Le pixel de 30 m est celui des produits HLS sur lesquels ces poids ont été
#: entraînés — c'est aussi la raison pour laquelle le contrat interdit tout
#: rééchantillonnage.
CRS = "EPSG:32643"
ORIGINE = (700_000.0, 2_800_000.0)
PIXEL_M = 30.0

#: Côté d'une parcelle, en pixels. Assez grand pour que le modèle ait de la
#: structure à lire, assez petit pour que la scène ne soit pas quatre aplats.
PARCELLE = 64

#: Écart-type du bruit capteur, en unités de réflectance × 10 000, soit environ
#: un pour cent d'une valeur courante. Il existe pour que deux pixels de la même
#: parcelle ne soient pas identiques ; le monter n'ajoute rien au modèle et rend
#: le fichier incompressible.
BRUIT = 15.0

#: Pas radiométrique déclaré de la scène, en mêmes unités. C'est le seul réglage
#: de cette recette qui existe pour le poids du fichier, et il a été mesuré :
#: sans lui le raster fait 8,33 Mio, avec lui 4,57 — le bruit remplit sinon les
#: bits de poids faible de chaque échantillon, et ce sont eux qui ne se
#: compriment pas. Huit unités valent 0,0008 de réflectance, soit trois dixièmes
#: de pour cent d'une valeur courante : quatre niveaux dans l'écart-type du
#: bruit, et rien qu'un modèle puisse distinguer. La scène annonce ainsi la
#: précision qu'elle a, plutôt que seize bits dont douze sont du hasard.
PAS_RADIOMETRIQUE = 8


def masque_eau(cote: int) -> np.ndarray:
    """Le masque d'eau, posé par géométrie avant toute réflectance.

    Deux formes, pour deux raisons distinctes. Un cours sinueux traverse la
    scène de part en part : c'est une structure fine, qui survit ou non au
    découpage en tuiles, et c'est donc lui qui dira si un recouvrement manque.
    Un lobe de crue elliptique couvre une surface franche à cheval sur les
    parcelles : c'est lui qui porte l'essentiel de la fraction, et il est
    volontairement centré sur une frontière de tuile de 384.
    """
    y, x = np.mgrid[0:cote, 0:cote].astype("float64")
    u = x / cote

    # Cours d'eau : une sinusoïde à deux harmoniques, de largeur variable.
    centre = cote * (0.30 + 0.13 * np.sin(2.0 * np.pi * u) + 0.05 * np.sin(6.0 * np.pi * u + 1.1))
    demi_largeur = cote * (0.022 + 0.010 * np.sin(4.0 * np.pi * u + 0.4))
    riviere = np.abs(y - centre) <= demi_largeur

    # Lobe de crue, centré sur la frontière des tuiles de 384.
    lobe = ((x - cote * 0.5) / (cote * 0.34)) ** 2 + (
        (y - cote * 0.62) / (cote * 0.27)
    ) ** 2 <= 1.0

    return riviere | lobe


def couverture_terrestre(cote: int, tirage: np.random.Generator) -> np.ndarray:
    """Une mosaïque de parcelles : indice de signature terrestre par pixel."""
    blocs = int(np.ceil(cote / PARCELLE))
    grille = tirage.integers(0, len(COUVERTURES_TERRESTRES), size=(blocs, blocs))
    return np.kron(grille, np.ones((PARCELLE, PARCELLE), dtype=grille.dtype))[:cote, :cote]


def eclairement(cote: int) -> np.ndarray:
    """Champ multiplicatif lent : relief et angle du soleil, pas du bruit.

    Il est analytique et non tiré : c'est ce qui rend la scène compressible sans
    lui retirer sa structure. Un raster d'aplats parfaits se comprimerait mieux
    encore, mais un modèle entraîné sur des scènes réelles n'y verrait rien de
    familier.
    """
    y, x = np.mgrid[0:cote, 0:cote].astype("float64") / cote
    onde = (
        0.06 * np.sin(2.0 * np.pi * (0.8 * x + 0.3 * y))
        + 0.04 * np.sin(2.0 * np.pi * (0.35 * x - 0.9 * y) + 0.7)
        + 0.03 * np.sin(2.0 * np.pi * (2.1 * x + 1.7 * y) + 2.2)
    )
    return 1.0 + onde


def scene(cote: int = COTE) -> tuple[np.ndarray, np.ndarray]:
    """Les treize bandes int16 et le masque d'eau qui les a engendrées."""
    tirage = np.random.default_rng(GRAINE)
    eau = masque_eau(cote)
    terres = couverture_terrestre(cote, tirage)
    facteur = eclairement(cote)

    cube = np.empty((len(BANDES), cote, cote), dtype="float64")
    for bande in range(len(BANDES)):
        plan = np.empty((cote, cote), dtype="float64")
        for indice, nom in enumerate(COUVERTURES_TERRESTRES):
            plan[terres == indice] = SIGNATURES[nom][bande]
        plan[eau] = SIGNATURES["eau"][bande]
        cube[bande] = plan * facteur + tirage.normal(0.0, BRUIT, size=(cote, cote))

    # Bornage explicite : une réflectance négative n'existe pas, et le passage
    # en int16 la ferait tourner sans rien dire.
    borne = np.clip(cube, 0.0, 10_000.0)
    quantifie = (borne // PAS_RADIOMETRIQUE) * PAS_RADIOMETRIQUE
    return quantifie.astype("int16"), eau


def produire(dossier: Path, *, force: bool = False) -> list[Path]:
    cible = dossier / CIBLE
    if cible.exists() and not force:
        print(f"  {CIBLE} : déjà là, laissé tel quel")
        return []

    cube, eau = scene()
    fraction = float(eau.mean())
    _ecrire(cible, cube)
    print(
        f"  {CIBLE} : {COTE}×{COTE}, {len(BANDES)} bandes int16, "
        f"eau {fraction:.4f} par géométrie, {cible.stat().st_size} octets"
    )
    return [cible]


def _ecrire(cible: Path, cube: np.ndarray) -> None:
    """Le GeoTIFF, compressé par DEFLATE avec prédicteur horizontal.

    Le prédicteur est ce qui décide du poids du fichier : il code chaque pixel
    par son écart au précédent, ce qui ramène une parcelle uniforme à une suite
    de petites valeurs. Sans lui — et c'est ce que le LZW seul donne — le fichier
    grossit au lieu de maigrir. Les quatre combinaisons ont été mesurées sur
    cette scène : bandes séparées et pavage de 256 rendent 4,57 Mio, la bande
    entrelacée par pixel en strip 5,12, et zstd ne gagne rien sur DEFLATE au
    niveau 9. Le pavage est aussi ce que servent les fournisseurs.
    """
    import rasterio
    from rasterio.transform import from_origin

    profil = {
        "driver": "GTiff",
        "height": cube.shape[1],
        "width": cube.shape[2],
        "count": cube.shape[0],
        "dtype": "int16",
        "crs": CRS,
        "transform": from_origin(ORIGINE[0], ORIGINE[1], PIXEL_M, PIXEL_M),
        "compress": "deflate",
        "predictor": 2,
        "zlevel": 9,
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
        "interleave": "band",
    }
    with rasterio.open(cible, "w", **profil) as sortie:
        sortie.write(cube)
        # Les descriptions de bandes ne servent pas au modèle — le contrat
        # désigne les bandes par leur indice — mais elles sont ce qui permet à
        # un humain de vérifier que `band_indices` pointe bien où il croit.
        for rang, nom in enumerate(BANDES, start=1):
            sortie.set_band_description(rang, nom)
