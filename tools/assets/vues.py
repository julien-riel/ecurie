"""Charge type de `multiview-to-3d` : une scène, trente-deux points de vue.

    uv run --project runtimes/depth-anything python tools/bench_assets.py vues

**Une seule scène et une seule caméra qui se déplace**, contrairement à `nuages`
qui fige trois objets différents. Ce que cette capacité coûte ne dépend pas de ce
qu'on photographie mais de **combien** de photos entrent : les trois cas du banc
prennent les 4, 16 puis 32 premières vues du même arc, et la pente mesurée porte
alors sur le nombre de vues et sur rien d'autre.

**La caméra est perspective, et c'est la condition d'existence de la recette.**
`tools/golden_assets.py:rendre_solide()` est orthographique — ses origines de
rayons sont translatées sur une grille et ses directions sont constantes. Une
image sans focale ne donne rien à estimer à un modèle qui estime une focale, et
les poses rendues n'auraient aucune vérité terrain à laquelle se comparer. La
caméra d'ici est un sténopé de champ 50°, ce qui fait une focale vraie de
**555,4 px** à 518 (soit 540,4 px ramenée à la grille de traitement de 504) : le
nombre est écrit dans le fichier de charge, et c'est contre lui que se juge
l'`focal_length_px` rendue par un job.

**Le damier du sol n'est pas décoratif.** Deux vues d'une sphère lisse sur fond
uni n'ont aucun point commun identifiable, et la reconstruction n'a rien à
apparier. Le sol porte donc un damier plus une modulation fine, et les deux
solides sont posés dessus — c'est ce qui rend la scène reconstructible, et c'est
pourquoi la recette ne peut pas se contenter d'un objet flottant dans le vide.

**Ce que cette charge ne mesure pas.** Aucune photographie réelle : pas de flou
de bougé, pas de bruit de capteur, pas d'exposition qui change d'une vue à
l'autre, pas d'objet qui bouge entre deux prises. La justesse des poses relevée
sur cette scène est donc une borne haute. Un banc mesure un coût ; celui-ci ne
dira jamais que le modèle décroche sur des photos prises à main levée.

**Aucune donnée à licencier.** Tout est calculé — marche de rayons sur une
fonction de distance signée, ombrage de Lambert, ciel en dégradé. Ni provenance
à suivre, ni consentement à recueillir, et la vérité terrain (position exacte de
chaque caméra, focale exacte) est connue par construction, ce qu'aucune prise de
vue réelle ne donnerait.

La recette a été exécutée deux fois avant d'être committée : sha256 identiques.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image

#: numpy pour la marche de rayons, Pillow pour écrire. Rien d'autre — la recette
#: est déclarée sur `depth-anything` parce que c'est l'env de la capacité, pas
#: parce qu'elle aurait besoin de torch.
ENV = "depth-anything"

#: Trente-deux vues, soit le `maxItems` du contrat. Le cas le plus lourd du banc
#: est ainsi le plus lourd que le contrat autorise : un profil qui s'arrêterait
#: à seize sous-déclarerait de 1,5 Go les jobs que la capacité accepte.
VUES = 32

#: Côté des images. 518 = 37 × 14, la grille native des encodeurs à patchs de 14
#: dont DA3 hérite ; le modèle redimensionne ensuite à `process_res`.
TAILLE = 518

#: Champ horizontal, en degrés. Donne f = (518/2) / tan(25°) = 555,4 px.
CHAMP = 50.0

#: L'arc parcouru par la caméra, en degrés d'azimut, et son élévation. Soixante-dix
#: degrés pour trente-deux vues font 2,3° entre deux vues voisines : assez pour
#: que la parallaxe existe, assez peu pour que l'appariement ne soit jamais le
#: facteur limitant. Ce qu'on mesure est un coût, pas une difficulté.
AZIMUT = (-35.0, 35.0)
ELEVATION = 20.0
DISTANCE = 3.4
CIBLE = (0.0, 0.40, 0.0)

#: Sur-échantillonnage. Deux suffisent à effacer l'escalier des silhouettes ; au-delà
#: le coût quadruple sans que le modèle voie la différence.
SURECHANTILLON = 2

#: Pas de marche et portée. 128 pas suffisent parce que la scène est bornée par
#: un sol : un rayon qui manque les solides touche le plan, il ne part pas à
#: l'infini en consommant tous les pas.
PAS = 128
PORTEE = 24.0

CIBLES = tuple(f"multivue-arc-{rang:03d}.png" for rang in range(VUES))


# --- la scène ----------------------------------------------------------------


def _sdf(p: np.ndarray) -> np.ndarray:
    """Distance signée à la scène : sol, cube arrondi, sphère.

    Le sol est un plan et non une boîte : une distance exacte, donc une marche
    qui converge en quelques pas sur la moitié de l'image.
    """
    sol = p[:, 1]

    q = np.abs(p - np.array([-0.62, 0.42, 0.10])) - np.array([0.34, 0.34, 0.34])
    cube = np.linalg.norm(np.maximum(q, 0.0), axis=1) + np.minimum(q.max(axis=1), 0.0) - 0.07

    sphere = np.linalg.norm(p - np.array([0.66, 0.34, -0.16]), axis=1) - 0.34

    return np.minimum(np.minimum(sol, cube), sphere)


def _materiau(p: np.ndarray) -> np.ndarray:
    """Couleur de base au point touché, sol texturé compris.

    Le damier seul donnerait des appariements ambigus — toutes les cases se
    ressemblent. La modulation fine qui s'y ajoute casse cette périodicité sans
    tirer quoi que ce soit au sort : c'est une somme de sinus, elle se refabrique
    à l'identique.
    """
    couleur = np.empty_like(p)

    damier = (np.floor(p[:, 0] * 2.0) + np.floor(p[:, 2] * 2.0)) % 2.0
    grain = 0.05 * np.sin(p[:, 0] * 21.0) * np.sin(p[:, 2] * 17.0)
    clair = np.array([0.78, 0.74, 0.68])
    sombre = np.array([0.30, 0.29, 0.31])
    couleur[:] = np.where(damier[:, None] < 0.5, clair, sombre) + grain[:, None]

    # Les deux solides écrasent la couleur du sol là où ce sont eux qu'on touche.
    q = np.abs(p - np.array([-0.62, 0.42, 0.10])) - np.array([0.34, 0.34, 0.34])
    cube = np.linalg.norm(np.maximum(q, 0.0), axis=1) + np.minimum(q.max(axis=1), 0.0) - 0.07
    sphere = np.linalg.norm(p - np.array([0.66, 0.34, -0.16]), axis=1) - 0.34
    couleur[cube < 2e-3] = np.array([0.80, 0.36, 0.22])
    couleur[sphere < 2e-3] = np.array([0.24, 0.42, 0.66])
    return np.clip(couleur, 0.0, 1.0)


def _ciel(directions: np.ndarray) -> np.ndarray:
    """Dégradé vertical. Uni, donc sans texture : le ciel n'est pas reconstructible."""
    hauteur = np.clip(directions[:, 1] * 0.5 + 0.5, 0.0, 1.0)[:, None]
    return np.array([0.62, 0.70, 0.82]) * (1 - hauteur) + np.array([0.20, 0.34, 0.58]) * hauteur


def _normales(points: np.ndarray) -> np.ndarray:
    """Normales par différences finies centrées, pour un ombrage qui donne le relief."""
    eps = 1.5e-3
    normales = np.empty_like(points)
    for axe in range(3):
        decalage = np.zeros(3)
        decalage[axe] = eps
        normales[:, axe] = _sdf(points + decalage) - _sdf(points - decalage)
    longueurs = np.linalg.norm(normales, axis=1, keepdims=True)
    return normales / np.maximum(longueurs, 1e-9)


# --- la caméra ---------------------------------------------------------------


def pose(azimut_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Position et repère de la caméra pour un azimut donné.

    Rend (œil, droite, haut, avant). C'est la vérité terrain des poses : le
    fichier de charge n'inscrit que la focale, mais quiconque veut juger la
    reconstruction relit ces trois lignes.
    """
    a = math.radians(azimut_deg)
    e = math.radians(ELEVATION)
    cible = np.array(CIBLE)
    oeil = cible + DISTANCE * np.array(
        [math.sin(a) * math.cos(e), math.sin(e), math.cos(a) * math.cos(e)]
    )
    avant = cible - oeil
    avant /= np.linalg.norm(avant)
    droite = np.cross(avant, np.array([0.0, 1.0, 0.0]))
    droite /= np.linalg.norm(droite)
    haut = np.cross(droite, avant)
    return oeil, droite, haut, avant


def focale(taille: int = TAILLE) -> float:
    """Focale en pixels, telle que la géométrie de la caméra l'impose."""
    return (taille / 2.0) / math.tan(math.radians(CHAMP) / 2.0)


def rendre(azimut_deg: float, taille: int = TAILLE, *, surechantillon: int = SURECHANTILLON):
    """Une vue, par marche de rayons sur la SDF."""
    if surechantillon > 1:
        grande = rendre(azimut_deg, taille * surechantillon, surechantillon=1)
        return grande.resize((taille, taille), Image.LANCZOS)

    oeil, droite, haut, avant = pose(azimut_deg)
    f = focale(taille)

    axes = (np.arange(taille, dtype=np.float64) + 0.5) - taille / 2.0
    u, v = np.meshgrid(axes / f, -axes / f)
    n = taille * taille
    directions = (
        u.reshape(n, 1) * droite[None, :]
        + v.reshape(n, 1) * haut[None, :]
        + avant[None, :]
    )
    directions /= np.linalg.norm(directions, axis=1, keepdims=True)
    origines = np.repeat(oeil[None, :], n, axis=0)

    distance = np.zeros(n)
    vivants = np.ones(n, dtype=bool)
    for _ in range(PAS):
        points = origines + distance[:, None] * directions
        d = _sdf(points)
        touche = d < 5e-4
        vivants &= ~touche & (distance < PORTEE)
        distance = np.where(vivants, distance + np.maximum(0.92 * d, 5e-5), distance)
        if not vivants.any():
            break

    points = origines + distance[:, None] * directions
    atteint = (_sdf(points) < 2e-3) & (distance < PORTEE)

    image = _ciel(directions)
    if atteint.any():
        touches = points[atteint]
        normales = _normales(touches)
        lumiere = np.array([0.45, 0.80, 0.40])
        lumiere = lumiere / np.linalg.norm(lumiere)
        # Produit scalaire écrit à la main plutôt que `@` : sur cette machine,
        # le matmul d'Accelerate lève trois avertissements flottants (division
        # par zéro, dépassement, valeur invalide) sur un tableau de deux cent
        # mille lignes, alors que le résultat est juste. Un avertissement qui ne
        # correspond à rien est pire qu'inutile — il fait chercher un bogue.
        diffus = np.clip((normales * lumiere).sum(axis=1), 0.0, 1.0)[:, None]
        # Une brume qui s'épaissit avec la distance : c'est ce qui empêche le
        # damier de rester net jusqu'à l'horizon, où il n'apporte plus que du
        # crénelage.
        brume = np.clip((distance[atteint] - 3.0) / 9.0, 0.0, 0.85)[:, None]
        couleur = _materiau(touches) * (0.28 + 0.72 * diffus)
        image[atteint] = couleur * (1 - brume) + np.array([0.60, 0.66, 0.76]) * brume

    octets = (np.clip(image, 0.0, 1.0) ** (1 / 2.2) * 255.0 + 0.5).astype("uint8")
    return Image.fromarray(octets.reshape(taille, taille, 3), mode="RGB")


# --- point d'entrée ----------------------------------------------------------


def produire(dossier: Path, *, force: bool = False) -> list[Path]:
    ecrits: list[Path] = []
    for rang, fichier in enumerate(CIBLES):
        cible = dossier / fichier
        if cible.exists() and not force:
            print(f"  {fichier} : déjà là, laissé tel quel")
            continue
        azimut = AZIMUT[0] + (AZIMUT[1] - AZIMUT[0]) * rang / (VUES - 1)
        image = rendre(azimut)
        # PNG plutôt que JPEG : l'encodeur JPEG de Pillow n'est pas garanti
        # stable d'une version à l'autre, et une charge type se refabrique.
        # `optimize=False` pour la même raison — le choix des filtres dépendrait
        # de la version de zlib.
        image.save(cible, format="PNG", optimize=False)
        print(
            f"  {fichier} : azimut {azimut:+6.2f}°, {cible.stat().st_size} octets"
        )
        ecrits.append(cible)
    return ecrits
