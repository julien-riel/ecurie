"""Charge type de `robot-action` : une vue de table calculée, sans robot dessus.

    uv run --project runtimes/lerobot python tools/bench_assets.py atelier_robot

**Pourquoi calculer une scène plutôt que reprendre une image de LIBERO.** Le
banc est versionné, public et figé pour des années ; les rendus de LIBERO sont
des extraits d'un jeu de données tiers, et le README du banc pose que ses entrées
« n'ont ni licence ni provenance à suivre ». La même règle a écarté les chips
Sen1Floods11 de `geo-segment` et les photographies de visages de `face-detect`.
Ici elle coûte moins cher qu'ailleurs : ce banc mesure un coût, et le coût de
cette capacité ne dépend pas de ce que l'image représente — le réseau ramène
toute image à 512² puis à 256², et le nombre de jetons visuels est fixe.

**Ce que cette scène ne prétend pas être.** Elle n'est pas une observation de
LIBERO. Le modèle a été entraîné sur des rendus MuJoCo d'un bras Franka vu de
face, avec le bras dans le champ ; il n'y a ici ni bras, ni pince, ni la
signature visuelle de ce simulateur. Les sept nombres que le modèle rendra sur
cette image sont donc hors distribution, et le banc ne dit pas s'ils sont bons —
il ne le dirait pas davantage sur une vraie image, faute de robot pour les
exécuter. Ce que la scène doit garantir est plus modeste et suffisant : être la
**même** image à chaque exécution, et contenir les objets que les consignes de la
charge nomment, pour que la comparaison entre deux consignes porte sur une scène
où les deux ont un sens.

**Deux objets nommables, et c'est tout ce qu'il faut.** Un cube rouge et une
sphère bleue posés sur un plan de travail. La charge oppose « pick up the red
cube » à « push the blue ball to the left » : deux consignes qui désignent chacune
un objet réellement présent, et dont les gestes attendus diffèrent — saisir et
pousser. Si le modèle rendait le même tronçon pour les deux, le canal de langue
serait mort, et c'est le seul contrôle sémantique faisable sans robot.

**Rendu par lancer de rayons analytique**, comme les visages et les solides du
banc : une caméra à sténopé, un plan de table, un mur de fond, une boîte et une
sphère, intersections exactes et éclairage de Lambert. Aucun tirage au sort, donc
aucune graine à fixer — deux exécutions donnent le même fichier à l'octet, ce qui
a été vérifié avant de le committer.

**Le côté est 512, et il est mesuré.** `resize_imgs_with_padding` vaut
`[512, 512]` dans le `config.json` de la politique : une image carrée de 512
traverse `resize_with_pad` sans être ni rééchantillonnée ni bourrée de noir. Une
image plus grande serait réduite, une plus petite agrandie, et les bandes noires
d'un format non carré occuperaient des jetons visuels pour ne rien montrer. C'est
la seule taille qui fait entrer dans le réseau exactement les pixels du fichier.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

#: numpy et Pillow suffisent, et les deux sont dans l'env de la capacité servie.
#: La recette n'a besoin ni de torch, ni de lerobot : elle ne fait que de la
#: géométrie.
ENV = "lerobot"

#: Le côté qui traverse `resize_with_pad` sans rien changer — voir l'en-tête.
COTE = 512

#: Caméra à sténopé : position, point visé, ouverture verticale en degrés. Elle
#: regarde la table d'un peu au-dessus, comme une caméra de poignet ne le ferait
#: pas et comme une caméra d'épaule le fait — c'est le cadrage sous lequel une
#: scène de manipulation se lit, objets séparés et plan de travail visible.
CAMERA = np.array([0.0, -0.62, 0.42])
CIBLE = np.array([0.0, 0.0, 0.06])
OUVERTURE_DEG = 42.0

#: Direction de la lumière, normalisée à l'usage. Une seule source, venant de la
#: gauche et de haut : deux sources donneraient des faces également éclairées, et
#: c'est l'écart entre les faces qui fait lire un cube comme un volume.
LUMIERE = np.array([-0.45, -0.35, 0.82])

#: Part de lumière ambiante. Sans elle, les faces détournées de la source sont
#: noires et le cube perd une arête sur trois.
AMBIANTE = 0.34

#: Le cube rouge : centre et demi-côté, en mètres. Un cube de 5 cm posé sur la
#: table, soit l'ordre de grandeur des objets que LIBERO fait manipuler.
CUBE_CENTRE = np.array([-0.085, 0.015, 0.025])
CUBE_DEMI = 0.025
CUBE_COULEUR = np.array([0.72, 0.11, 0.09])

#: La sphère bleue : centre et rayon. Posée à droite, assez loin du cube pour
#: qu'aucune consigne ne puisse désigner les deux.
BALLE_CENTRE = np.array([0.095, -0.01, 0.028])
BALLE_RAYON = 0.028
BALLE_COULEUR = np.array([0.13, 0.30, 0.68])

#: Le plan de travail, à z = 0, et le mur de fond, à y = +0,38 m. Deux teintes
#: mates : un plan de travail brillant renverrait des reflets qu'un lancer de
#: rayons à un rebond ne sait pas calculer, et qui ressembleraient à des objets.
TABLE_COULEUR = np.array([0.68, 0.57, 0.42])
TABLE_RAINURE = np.array([0.60, 0.49, 0.35])
MUR_Y = 0.38
MUR_COULEUR = np.array([0.55, 0.58, 0.60])

#: Pas des rainures du plan de travail, en mètres. Elles ne décorent pas : sans
#: texture, un plan de Lambert uniforme est un aplat, et rien dans l'image ne
#: donne l'échelle ni la profondeur.
RAINURE_PAS = 0.055
RAINURE_LARGEUR = 0.006

CIBLES = ("atelier-table-512.png",)


def _normaliser(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def rayons(cote: int) -> tuple[np.ndarray, np.ndarray]:
    """Origine et direction du rayon de chaque pixel, pour une caméra à sténopé.

    Le repère est celui de la scène : x vers la droite, y vers le fond, z vers le
    haut. La base de la caméra est construite depuis l'axe de visée et la
    verticale du monde, ce qui interdit tout roulis — une image de manipulation
    penchée ne veut rien dire.
    """
    avant = _normaliser(CIBLE - CAMERA)
    droite = _normaliser(np.cross(avant, np.array([0.0, 0.0, 1.0])))
    haut = np.cross(droite, avant)

    demi = np.tan(np.radians(OUVERTURE_DEG) / 2.0)
    # Centres de pixels, et non bords : un demi-pixel de décalage suffit à rendre
    # une image asymétrique, ce qui se voit sur les arêtes verticales du cube.
    axe = (np.arange(cote) + 0.5) / cote * 2.0 - 1.0
    u, v = np.meshgrid(axe * demi, -axe * demi, indexing="xy")

    direction = avant + u[..., None] * droite + v[..., None] * haut
    return np.broadcast_to(CAMERA, (cote, cote, 3)), _normaliser(direction)


def _plan(origine: np.ndarray, direction: np.ndarray, axe: int, valeur: float) -> np.ndarray:
    """Distance à un plan perpendiculaire à `axe`, ou l'infini quand il est derrière."""
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (valeur - origine[..., axe]) / direction[..., axe]
    return np.where(t > 1e-4, t, np.inf)


def _boite(origine: np.ndarray, direction: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Intersection d'une boîte alignée sur les axes, par la méthode des tranches.

    Rend la distance et la normale. La normale se lit sur l'axe qui a décidé de
    l'entrée — celui dont la distance d'entrée est la plus grande — plutôt qu'en
    dérivant une fonction de distance : c'est exact, et une normale approchée sur
    une arête vive donne un liseré qui ressemble à un défaut de rendu.
    """
    mini, maxi = CUBE_CENTRE - CUBE_DEMI, CUBE_CENTRE + CUBE_DEMI
    with np.errstate(divide="ignore", invalid="ignore"):
        t1 = (mini - origine) / direction
        t2 = (maxi - origine) / direction
    proche = np.minimum(t1, t2)
    lointain = np.maximum(t1, t2)
    entree = np.nanmax(proche, axis=-1)
    sortie = np.nanmin(lointain, axis=-1)

    touche = (sortie >= np.maximum(entree, 1e-4)) & (entree > 1e-4)
    axe = np.argmax(np.where(np.isnan(proche), -np.inf, proche), axis=-1)
    normale = np.zeros_like(direction)
    np.put_along_axis(normale, axe[..., None], 1.0, axis=-1)
    normale = normale * -np.sign(direction)
    return np.where(touche, entree, np.inf), normale


def _sphere(origine: np.ndarray, direction: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Intersection d'une sphère : la plus petite racine positive, et sa normale."""
    oc = origine - BALLE_CENTRE
    b = np.sum(oc * direction, axis=-1)
    c = np.sum(oc * oc, axis=-1) - BALLE_RAYON**2
    discriminant = b * b - c
    valide = discriminant > 0.0
    racine = np.sqrt(np.where(valide, discriminant, 0.0))
    t = np.where(valide & (-b - racine > 1e-4), -b - racine, np.inf)
    # L'infini est ramené à zéro **avant** la multiplication, et non après : sur
    # les pixels qui manquent la sphère, `inf * 0` vaut `nan` et non zéro, et le
    # `nan` traverse ensuite la normalisation jusqu'à l'image.
    fini = np.where(np.isfinite(t), t, 0.0)
    return t, _normaliser(origine + fini[..., None] * direction - BALLE_CENTRE)


def _table(point: np.ndarray) -> np.ndarray:
    """La teinte du plan de travail au point touché, rainures comprises."""
    reste = np.abs(np.mod(point[..., 0] + RAINURE_PAS / 2.0, RAINURE_PAS) - RAINURE_PAS / 2.0)
    creux = (reste < RAINURE_LARGEUR)[..., None]
    return np.where(creux, TABLE_RAINURE, TABLE_COULEUR)


def scene(cote: int = COTE) -> np.ndarray:
    """La scène rendue, en flottants [0, 1] de forme (cote, cote, 3)."""
    origine, direction = rayons(cote)

    t_table = _plan(origine, direction, axe=2, valeur=0.0)
    t_mur = _plan(origine, direction, axe=1, valeur=MUR_Y)
    t_cube, n_cube = _boite(origine, direction)
    t_balle, n_balle = _sphere(origine, direction)

    distances = np.stack([t_table, t_mur, t_cube, t_balle], axis=-1)
    gagnant = np.argmin(distances, axis=-1)
    t = np.min(distances, axis=-1)
    point = origine + t[..., None] * direction

    normales = np.stack(
        [
            np.broadcast_to(np.array([0.0, 0.0, 1.0]), direction.shape),
            np.broadcast_to(np.array([0.0, -1.0, 0.0]), direction.shape),
            n_cube,
            n_balle,
        ],
        axis=-2,
    )
    normale = np.take_along_axis(normales, gagnant[..., None, None], axis=-2)[..., 0, :]

    couleurs = np.stack(
        [
            _table(point),
            np.broadcast_to(MUR_COULEUR, direction.shape),
            np.broadcast_to(CUBE_COULEUR, direction.shape),
            np.broadcast_to(BALLE_COULEUR, direction.shape),
        ],
        axis=-2,
    )
    albedo = np.take_along_axis(couleurs, gagnant[..., None, None], axis=-2)[..., 0, :]

    lumiere = _normaliser(LUMIERE)
    diffus = np.clip(np.sum(normale * lumiere, axis=-1), 0.0, 1.0)

    # Les deux objets projettent leur ombre sur la table, et rien d'autre n'en
    # projette : un second rayon par pixel suffit, et il n'est tiré que pour les
    # pixels de table. Sans ombre, les objets flottent — l'ombre est le seul
    # indice qui dise qu'ils sont posés dessus et non devant.
    vers_lumiere = np.broadcast_to(lumiere, direction.shape)
    bloque = np.isfinite(_boite(point + normale * 1e-4, vers_lumiere)[0]) | np.isfinite(
        _sphere(point + normale * 1e-4, vers_lumiere)[0]
    )
    eclairement = np.where(bloque, AMBIANTE * 0.72, AMBIANTE + (1.0 - AMBIANTE) * diffus)

    # Le mur de fond est un plan **infini**, et l'ouverture est étroite : tout
    # rayon part vers l'avant, donc tout rayon le touche. Il n'y a par
    # construction aucun pixel de fond, et donc aucune couleur de fond à choisir.
    # Le contrôle est écrit plutôt que supposé : déplacer la caméra ou élargir
    # l'ouverture ferait sortir des rayons, et le rendu s'arrêterait ici au lieu
    # de produire des pixels noirs qu'on prendrait pour une ombre.
    if not np.isfinite(t).all():
        raise ValueError(
            f"{int((~np.isfinite(t)).sum())} rayon(s) ne touchent rien : la scène n'a pas "
            "de fond, et l'ouverture ou la position de la caméra viennent de changer"
        )
    return np.clip(albedo * eclairement[..., None], 0.0, 1.0)


def produire(dossier: Path, *, force: bool = False) -> list[Path]:
    écrits: list[Path] = []
    for fichier in CIBLES:
        cible = dossier / fichier
        if cible.exists() and not force:
            print(f"  {fichier} : déjà là, laissé tel quel")
            continue
        image = scene()
        Image.fromarray((image * 255.0 + 0.5).astype("uint8"), mode="RGB").save(
            cible, format="PNG", optimize=True
        )
        print(
            f"  {fichier} : {COTE}×{COTE}, {cible.stat().st_size} octets, "
            f"cube rouge en {CUBE_CENTRE.tolist()}, balle bleue en {BALLE_CENTRE.tolist()}"
        )
        écrits.append(cible)
    return écrits
