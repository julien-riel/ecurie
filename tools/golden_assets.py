"""Fabrique les fichiers d'entrée des golden sets, à partir de leur manifeste.

    uv run --project runtimes/mlx-vlm python tools/golden_assets.py

Cinq recettes, toutes déterministes et sans réseau :

- `page` rend une page de document depuis son texte de référence. Le manifeste
  reste l'autorité : c'est `reference.text_file` qui est rendu, si bien qu'une
  page et sa vérité terrain ne peuvent pas diverger. Les tabulations du fichier
  marquent les colonnes d'un tableau, l'indentation est conservée telle quelle,
  et la comparaison au score normalise tout cela — voir `normalization` dans
  `registry/schema/golden.schema.json` ;
- `solide` rend un objet 3D en RGBA à fond réellement transparent, par lancer de
  rayons sur une fonction de distance signée. Pas de photo, pas de moteur 3D,
  pas de licence à suivre : une centaine de lignes de numpy et le même résultat
  à chaque exécution ;
- `scene` compose des solides sur un fond opaque **et rend l'alpha exact avec** :
  la vérité terrain du détourage est calculée en même temps que l'image, non
  annotée après coup ;
- `musique` fabrique un mélange à quatre pistes dont on connaît exactement les
  composantes, puisque c'est nous qui les additionnons ;
- `portrait` compose des visages calculés sur un fond. Même procédé que `solide`,
  et la même raison en plus forte : une charge type est versionnée, publique et
  figée pour des années, ce qui est exactement ce qu'on ne fait pas du portrait
  de quelqu'un. Le visage calculé n'a ni identité, ni consentement à recueillir,
  ni licence à suivre.

**Ce script existe surtout pour ne pas répéter l'oubli du banc d'essai.** Les
images de `registry/evals/bench/assets/` ont été produites par une recette
« déterministe » qui n'a jamais été committée : elles sont donc des données
orphelines, qu'on ne sait plus refaire ni expliquer. Une entrée de jeu d'essai
sans sa recette est une entrée dont on ne peut plus dire ce qu'elle contient.

Il n'écrase jamais un fichier existant sans `--force`, et c'est la règle
append-only rendue mécanique : une image de golden set qui change en silence
détruit la comparabilité de tous les résultats antérieurs.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

RACINE = Path(__file__).resolve().parents[1]
GOLDEN = RACINE / "registry" / "evals" / "golden"

# Polices système de macOS, nommées explicitement : une police « par défaut »
# changerait de machine en machine, et les pages cesseraient d'être reproductibles.
POLICES = {
    "serif": "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "serif-gras": "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
    "sans": "/System/Library/Fonts/Supplemental/Arial.ttf",
    "sans-gras": "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "mono": "/System/Library/Fonts/Supplemental/Courier New.ttf",
    "mono-gras": "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
}

PAGE = (1240, 1754)  # A4 à 150 ppp
MARGE = 96
ENCRE = (24, 24, 24)
PAPIER = (252, 251, 248)


class RecetteError(RuntimeError):
    """Recette impossible à exécuter — le message dit ce qui manque."""


# --- pages de document ---------------------------------------------------------


def police(nom: str, taille: int, gras: bool = False) -> ImageFont.FreeTypeFont:
    clé = f"{nom}-gras" if gras else nom
    chemin = POLICES.get(clé)
    if chemin is None or not Path(chemin).is_file():
        raise RecetteError(
            f"police introuvable pour {clé!r} ({chemin}) — "
            "les pages sont rendues avec les polices système de macOS"
        )
    return ImageFont.truetype(chemin, taille)


def _decouper(texte: str, fonte, largeur: int, dessin) -> list[str]:
    """Coupe un paragraphe à la largeur donnée, au mot."""
    lignes: list[str] = []
    courante = ""
    for mot in texte.split():
        essai = f"{courante} {mot}".strip()
        if dessin.textlength(essai, font=fonte) <= largeur or not courante:
            courante = essai
        else:
            lignes.append(courante)
            courante = mot
    if courante:
        lignes.append(courante)
    return lignes


def _rendre_lignes(dessin, lignes, x, y, fonte, interligne, couleur=ENCRE) -> int:
    pas = int(fonte.size * interligne)
    for ligne in lignes:
        dessin.text((x, y), ligne, font=fonte, fill=couleur)
        y += pas
    return y


def rendre_page(texte: str, source: dict) -> Image.Image:
    layout = source.get("layout", "paragraphe")
    nom_police = source.get("font", "serif")
    taille = int(source.get("size", 26))
    interligne = float(source.get("interligne", 1.5))
    degrade = source.get("degrade")
    couleur = (130, 130, 130) if degrade == "contraste" else ENCRE
    fond = (235, 235, 233) if degrade == "contraste" else PAPIER

    image = Image.new("RGB", PAGE, fond)
    dessin = ImageDraw.Draw(image)
    fonte = police(nom_police, taille)
    largeur = PAGE[0] - 2 * MARGE
    y = MARGE

    if layout == "code":
        # Chasse fixe, aucun repliage : la mise en ligne fait partie du contenu.
        y = _rendre_lignes(dessin, texte.split("\n"), MARGE, y, fonte, 1.4, couleur)
    elif layout == "liste":
        for ligne in texte.split("\n"):
            creux = len(ligne) - len(ligne.lstrip(" "))
            if not ligne.strip():
                y += int(taille * interligne * 0.5)
                continue
            dessin.text(
                (MARGE + creux * taille // 2, y), ligne.strip(), font=fonte, fill=couleur
            )
            y += int(taille * interligne)
    elif layout == "tableau":
        y = _rendre_tableau(dessin, texte, fonte, taille, interligne, couleur, largeur)
    elif layout == "formulaire":
        y = _rendre_formulaire(dessin, texte, fonte, nom_police, taille, interligne, couleur)
    elif layout == "titres":
        y = _rendre_titres(dessin, texte, source, nom_police, taille, interligne, couleur, largeur)
    elif layout == "colonnes":
        y = _rendre_colonnes(dessin, texte, fonte, taille, interligne, couleur, largeur)
    else:
        for paragraphe in texte.split("\n\n"):
            if not paragraphe.strip():
                continue
            lignes = _decouper(paragraphe.replace("\n", " "), fonte, largeur, dessin)
            y = _rendre_lignes(dessin, lignes, MARGE, y, fonte, interligne, couleur)
            y += int(taille * interligne * 0.6)

    # Une page A4 aux trois quarts blanche n'est pas une page de document : on la
    # coupe sous le texte, en gardant la marge du bas.
    hauteur = min(PAGE[1], max(int(y) + MARGE, 520))
    # Niveaux de gris : c'est ce qu'un numériseur de document rend, et cela divise
    # par trois le poids d'une page bruitée, où la compression PNG ne peut rien.
    return _degrader(image.crop((0, 0, PAGE[0], hauteur)), degrade, fond).convert("L")


def _rendre_tableau(dessin, texte, fonte, taille, interligne, couleur, largeur) -> int:
    """Colonnes calées sur leur contenu, pas sur une division égale de la page.

    Des colonnes de largeur fixe font déborder la plus longue étiquette sur sa
    voisine, et la page ne dit alors plus ce que la vérité terrain affirme
    qu'elle dit.
    """
    lignes = texte.split("\n")
    grilles = [ligne.split("\t") for ligne in lignes]
    colonnes = max(len(g) for g in grilles)

    gouttiere = taille
    taquets = [MARGE]
    for index in range(colonnes - 1):
        plus_large = max(
            (dessin.textlength(g[index], font=fonte) for g in grilles if len(g) > index + 1),
            default=0.0,
        )
        taquets.append(int(taquets[-1] + plus_large + gouttiere))

    y = MARGE
    for grille in grilles:
        if not any(cellule.strip() for cellule in grille):
            y += int(taille * interligne * 0.5)
            continue
        if len(grille) == 1:  # ligne de texte libre au milieu du document
            dessin.text((MARGE, y), grille[0], font=fonte, fill=couleur)
        else:
            for index, cellule in enumerate(grille):
                dessin.text((taquets[index], y), cellule, font=fonte, fill=couleur)
        y += int(taille * interligne)
    return y


def _rendre_formulaire(dessin, texte, fonte, nom_police, taille, interligne, couleur) -> int:
    taquet = MARGE + 380
    gras = police(nom_police, taille, gras=True)
    y = MARGE
    for ligne in texte.split("\n"):
        if not ligne.strip():
            y += int(taille * interligne * 0.5)
            continue
        étiquette, séparateur, valeur = ligne.partition(" : ")
        if séparateur:
            dessin.text((MARGE, y), f"{étiquette} :", font=fonte, fill=couleur)
            dessin.text((taquet, y), valeur, font=fonte, fill=couleur)
        else:
            dessin.text((MARGE, y), ligne, font=gras, fill=couleur)
        y += int(taille * interligne)
    return y


def _rendre_titres(dessin, texte, source, nom_police, taille, interligne, couleur, largeur) -> int:
    niveaux = {int(k): int(v) for k, v in (source.get("headings") or {}).items()}
    corps = police(nom_police, taille)
    y = MARGE
    for index, ligne in enumerate(texte.split("\n")):
        if not ligne.strip():
            y += int(taille * interligne * 0.5)
            continue
        niveau = niveaux.get(index)
        if niveau:
            fonte = police(nom_police, taille + (14 if niveau == 1 else 6), gras=True)
            dessin.text((MARGE, y), ligne, font=fonte, fill=couleur)
            y += int(fonte.size * 1.7)
        else:
            lignes = _decouper(ligne, corps, largeur, dessin)
            y = _rendre_lignes(dessin, lignes, MARGE, y, corps, interligne, couleur)
            y += int(taille * interligne * 0.4)
    return y


def _rendre_colonnes(dessin, texte, fonte, taille, interligne, couleur, largeur) -> int:
    """Deux colonnes équilibrées : c'est l'ordre de lecture qui est éprouvé."""
    gouttiere = 56
    colonne = (largeur - gouttiere) // 2
    lignes: list[str] = []
    for paragraphe in texte.split("\n\n"):
        if not paragraphe.strip():
            continue
        lignes.extend(_decouper(paragraphe.replace("\n", " "), fonte, colonne, dessin))
        lignes.append("")
    while lignes and lignes[-1] == "":
        lignes.pop()
    milieu = (len(lignes) + 1) // 2
    bas = _rendre_lignes(dessin, lignes[:milieu], MARGE, MARGE, fonte, interligne, couleur)
    return max(
        bas,
        _rendre_lignes(
            dessin, lignes[milieu:], MARGE + colonne + gouttiere, MARGE, fonte, interligne, couleur
        ),
    )


def _degrader(image: Image.Image, degrade: str | None, fond) -> Image.Image:
    """Salissures déterministes : la graine est fixe, la page est reproductible."""
    if degrade == "bruit":
        rng = np.random.default_rng(20260820)
        tableau = np.asarray(image, dtype=np.int16)
        grain = rng.normal(0.0, 9.0, tableau.shape[:2])[:, :, None]
        # Éclairement inégal, comme une page photocopiée près du bord de la vitre.
        hauteur, largeur = tableau.shape[:2]
        rampe = np.linspace(-14.0, 6.0, largeur)[None, :, None]
        tableau = np.clip(tableau + grain + rampe, 0, 255).astype(np.uint8)
        return Image.fromarray(tableau)
    if degrade == "incline":
        return image.rotate(-1.6, resample=Image.BICUBIC, fillcolor=fond)
    return image


# --- solides pour la reconstruction 3D -------------------------------------------


def _sdf(forme: str, p: np.ndarray) -> np.ndarray:
    """Distance signée à la forme, évaluée sur un lot de points (N, 3)."""
    x, y, z = p[:, 0], p[:, 1], p[:, 2]
    if forme == "sphere":
        return np.linalg.norm(p, axis=1) - 0.85
    if forme == "cube":
        q = np.abs(p) - 0.62
        return np.linalg.norm(np.maximum(q, 0.0), axis=1) + np.minimum(q.max(axis=1), 0.0)
    if forme == "cylindre":
        radial = np.hypot(x, z) - 0.55
        vertical = np.abs(y) - 0.72
        d = np.stack([radial, vertical], axis=1)
        return np.linalg.norm(np.maximum(d, 0.0), axis=1) + np.minimum(d.max(axis=1), 0.0)
    if forme == "cone":
        # Cône plein, pointe en haut, base fermée.
        #
        # La distance doit être exacte, et pas seulement du bon signe : un écart
        # horizontal à la surface oblique surestime la distance perpendiculaire,
        # le lancer de rayons dépasse la paroi et ressort par la base. Le rendu
        # montre alors un disque sombre sous un cône creux, ce qui n'est pas ce
        # qu'on demande de reconstruire.
        return _sdf_cone(p, hauteur=0.78, base=0.62)
    if forme == "tore":
        q = np.stack([np.hypot(x, z) - 0.60, y], axis=1)
        return np.linalg.norm(q, axis=1) - 0.24
    if forme == "escalier":
        marches = []
        for i in range(4):
            centre = np.array([-0.52 + 0.34 * i, -0.72 + 0.20 * i, 0.0])
            demi = np.array([0.17, 0.20 + 0.20 * i, 0.34])
            q = np.abs(p - centre) - demi
            marches.append(
                np.linalg.norm(np.maximum(q, 0.0), axis=1) + np.minimum(q.max(axis=1), 0.0)
            )
        return np.min(np.stack(marches, axis=1), axis=1)
    if forme == "equerre":
        bras = []
        for centre, demi in (
            (np.array([-0.24, -0.30, 0.0]), np.array([0.52, 0.20, 0.26])),
            (np.array([-0.56, 0.12, 0.0]), np.array([0.20, 0.62, 0.26])),
        ):
            q = np.abs(p - centre) - demi
            bras.append(
                np.linalg.norm(np.maximum(q, 0.0), axis=1) + np.minimum(q.max(axis=1), 0.0)
            )
        return np.min(np.stack(bras, axis=1), axis=1)
    if forme == "etoile":
        # Prisme dont la section est une étoile à cinq branches, extrudée.
        #
        # La distance à l'étoile se calcule par repliement du plan sur un
        # cinquième de tour, puis distance au segment d'une branche. Moduler le
        # rayon par un cosinus donnerait une fleur aux bords arrondis — et, pire,
        # une fonction qui n'est pas une distance : le lancer de rayons la
        # dépasserait et laisserait des franges le long des angles rentrants.
        radial = _sdf_etoile5(np.stack([x, z], axis=1), 0.92, 0.42)
        vertical = np.abs(y) - 0.30
        d = np.stack([radial, vertical], axis=1)
        return np.linalg.norm(np.maximum(d, 0.0), axis=1) + np.minimum(d.max(axis=1), 0.0)
    raise RecetteError(f"forme inconnue : {forme!r}")


def _sdf_cone(p: np.ndarray, *, hauteur: float, base: float) -> np.ndarray:
    """Distance signée exacte à un cône fermé (Inigo Quilez, `sdCappedCone`)."""
    q = np.stack([np.hypot(p[:, 0], p[:, 2]), p[:, 1]], axis=1)
    k1 = np.array([0.0, hauteur])  # sommet : rayon nul en haut
    k2 = np.array([-base, 2.0 * hauteur])
    seuil = np.where(q[:, 1] < 0.0, base, 0.0)
    ca = np.stack([q[:, 0] - np.minimum(q[:, 0], seuil), np.abs(q[:, 1]) - hauteur], axis=1)
    t = np.clip(((k1[None, :] - q) @ k2) / (k2 @ k2), 0.0, 1.0)
    cb = q - k1[None, :] + t[:, None] * k2[None, :]
    signe = np.where((cb[:, 0] < 0.0) & (ca[:, 1] < 0.0), -1.0, 1.0)
    carre = np.minimum((ca * ca).sum(axis=1), (cb * cb).sum(axis=1))
    return signe * np.sqrt(carre)


def _sdf_etoile5(p: np.ndarray, rayon: float, creux: float) -> np.ndarray:
    """Distance signée à une étoile à cinq branches (Inigo Quilez, `sdStar5`)."""
    k1 = np.array([math.cos(math.pi / 5), -math.sin(math.pi / 5)])
    k2 = np.array([-k1[0], k1[1]])
    q = p.copy()
    q[:, 0] = np.abs(q[:, 0])
    q -= 2.0 * np.maximum(q @ k1, 0.0)[:, None] * k1[None, :]
    q -= 2.0 * np.maximum(q @ k2, 0.0)[:, None] * k2[None, :]
    q[:, 0] = np.abs(q[:, 0])
    q[:, 1] -= rayon
    ba = creux * np.array([-k1[1], k1[0]]) - np.array([0.0, 1.0])
    h = np.clip((q @ ba) / (ba @ ba), 0.0, rayon)
    reste = q - h[:, None] * ba[None, :]
    signe = np.sign(q[:, 1] * ba[0] - q[:, 0] * ba[1])
    return np.linalg.norm(reste, axis=1) * signe


def rendre_solide(forme: str, taille: int = 768, *, suréchantillon: int = 2) -> Image.Image:
    """Lancer de rayons orthographique sur la SDF, avec alpha exact hors silhouette.

    Le fond est **réellement transparent**, pas gris : le pipeline de
    reconstruction recadre sur le canal alpha, et une image opaque le prive de
    sa seule indication de silhouette.

    Le rendu se fait au double de la taille demandée puis se réduit. Sans cela
    l'alpha ne vaut que 0 ou 255, et une silhouette en escalier n'est pas
    seulement laide : elle prive le détourage de toute couverture partielle sur
    les bords, c'est-à-dire de ce qu'on lui demande précisément d'estimer.
    """
    if suréchantillon > 1:
        grande = rendre_solide(forme, taille * suréchantillon, suréchantillon=1)
        return grande.resize((taille, taille), Image.LANCZOS)

    axes = np.linspace(-1.35, 1.35, taille, dtype=np.float64)
    u, v = np.meshgrid(axes, -axes)
    n = taille * taille

    # Vue de trois quarts, légèrement en plongée : trois faces d'un cube visibles.
    lacet, tangage = math.radians(32.0), math.radians(22.0)
    avant = np.array([
        -math.sin(lacet) * math.cos(tangage),
        -math.sin(tangage),
        -math.cos(lacet) * math.cos(tangage),
    ])
    droite = np.array([math.cos(lacet), 0.0, -math.sin(lacet)])
    haut = np.cross(droite, avant)

    origines = (
        -3.2 * avant[None, :]
        + u.reshape(n, 1) * droite[None, :]
        + v.reshape(n, 1) * haut[None, :]
    )
    directions = np.repeat(avant[None, :], n, axis=0)

    # Facteur de sécurité sur le pas : une union de solides rend une distance
    # exacte à l'extérieur, mais l'extrusion d'une section 2D la surestime
    # légèrement près des arêtes. Avancer d'un pas entier y ferait traverser la
    # paroi, et le rayon ressortirait dans le vide en laissant un trou.
    distance = np.zeros(n)
    vivants = np.ones(n, dtype=bool)
    for _ in range(160):
        points = origines + distance[:, None] * directions
        d = _sdf(forme, points)
        touche = d < 1e-4
        vivants &= ~touche & (distance < 7.0)
        distance = np.where(vivants, distance + np.maximum(0.9 * d, 1e-4), distance)
        if not vivants.any():
            break

    points = origines + distance[:, None] * directions
    atteint = _sdf(forme, points) < 1e-3

    # Normales par différences finies, pour un ombrage qui donne du relief sans
    # inventer de matière : c'est la géométrie qu'on veut donner à lire.
    eps = 2e-3
    normales = np.zeros_like(points)
    for axe in range(3):
        decalage = np.zeros(3)
        decalage[axe] = eps
        normales[:, axe] = _sdf(forme, points + decalage) - _sdf(forme, points - decalage)
    norme = np.linalg.norm(normales, axis=1, keepdims=True)
    normales = np.divide(normales, np.maximum(norme, 1e-9))

    lumiere = np.array([0.42, 0.78, 0.46])
    lumiere = lumiere / np.linalg.norm(lumiere)
    diffus = np.clip(normales @ lumiere, 0.0, 1.0)
    valeur = np.clip(0.22 + 0.72 * diffus, 0.0, 1.0)

    rgba = np.zeros((n, 4), dtype=np.uint8)
    gris = (valeur * 255).astype(np.uint8)
    rgba[:, 0] = rgba[:, 1] = rgba[:, 2] = gris
    rgba[:, 3] = np.where(atteint, 255, 0)
    rgba[~atteint, :3] = 0
    return Image.fromarray(rgba.reshape(taille, taille, 4), mode="RGBA")


# --- visages : de la géométrie, et personne de réel -------------------------------
#
# La famille `face-*` a besoin d'images contenant des visages, et aucune photo ne
# peut entrer ici : une charge type est versionnée, publique et figée pour des
# années, ce qui est exactement ce qu'on ne fait pas du portrait de quelqu'un.
# Un visage calculé n'a ni identité, ni consentement à recueillir, ni licence à
# suivre — et il se refabrique à l'identique, ce que le README de ce dossier
# réclame depuis que les six premières images s'en sont trouvées privées.
#
# **Le réalisme n'est pas une coquetterie, c'est le critère de recevabilité.**
# Une première version sans paupières — le globe oculaire entier apparent — était
# trouvée par RetinaFace MobileNet à 1,00 et par SCRFD à 0,61, mais **pas du tout
# par RetinaFace ResNet-50**, le plus strict des sept. Une charge qu'un variant
# ne peut pas servir le rend non profilable, donc inadmissible : c'est ce qui est
# arrivé à SAM 3 sur `image-segment`, et la leçon a coûté assez cher pour ne pas
# la répéter. Les paupières ajoutées, les neuf détecteurs du dépôt trouvent le
# visage, ResNet-50 à 0,976.

_OEIL = np.array([0.255, 0.115, 0.455])
_GLOBE = _OEIL + np.array([0.0, 0.0, -0.075])


def _ellipsoide(p: np.ndarray, centre: np.ndarray, rayons: np.ndarray) -> np.ndarray:
    q = (p - centre) / rayons
    k0 = np.linalg.norm(q, axis=1)
    k1 = np.linalg.norm(q / rayons, axis=1)
    return np.where(k0 > 0, k0 * (k0 - 1.0) / np.maximum(k1, 1e-9), -float(min(rayons)))


def _sphere(p: np.ndarray, centre: np.ndarray, rayon: float) -> np.ndarray:
    return np.linalg.norm(p - centre, axis=1) - rayon


def _boite_sdf(p, centre, demi, arrondi: float = 0.0) -> np.ndarray:
    q = np.abs(p - centre) - demi
    return (
        np.linalg.norm(np.maximum(q, 0.0), axis=1)
        + np.minimum(q.max(axis=1), 0.0)
        - arrondi
    )


def _capsule(p, a, b, rayon: float) -> np.ndarray:
    pa, ba = p - a, b - a
    h = np.clip((pa @ ba) / (ba @ ba), 0.0, 1.0)
    return np.linalg.norm(pa - h[:, None] * ba[None, :], axis=1) - rayon


def _smin(a, b, k: float):
    """Union lissée. Sans elle, le nez est un cylindre posé sur une sphère."""
    h = np.clip(0.5 + 0.5 * (b - a) / k, 0.0, 1.0)
    return b * (1 - h) + a * h - k * h * (1 - h)


def _smax(a, b, k: float):
    return -_smin(-a, -b, k)


def _parties_visage(p: np.ndarray) -> tuple[np.ndarray, ...]:
    """Distance à chaque partie. La plus proche décide de la couleur du pixel.

    Rendre les parties séparément plutôt qu'une seule distance est ce qui permet
    de peindre l'iris sans le sculpter : c'est le contraste sombre au centre du
    blanc de l'œil qu'un détecteur de visage cherche en premier, et il n'a aucune
    épaisseur.
    """
    q = p.copy()
    q[:, 0] = np.abs(q[:, 0])  # le visage est symétrique : on n'en décrit qu'un côté

    crane = _ellipsoide(p, np.array([0.0, 0.10, -0.05]), np.array([0.62, 0.72, 0.60]))
    menton = _ellipsoide(p, np.array([0.0, -0.42, 0.02]), np.array([0.42, 0.42, 0.48]))
    peau = _smin(crane, menton, 0.22)

    orbite = _sphere(q, _OEIL + np.array([0.0, 0.0, -0.03]), 0.175)
    peau = _smax(peau, -orbite, 0.05)

    # Paupières : une coque de peau sur le globe, percée d'une fente. Voir
    # l'en-tête de section — c'est ce détail-là qui décide de la recevabilité.
    paupiere = _ellipsoide(
        q, _GLOBE + np.array([0.0, 0.0, 0.012]), np.array([0.165, 0.125, 0.145])
    )
    ouverture = _ellipsoide(
        q, _GLOBE + np.array([0.0, -0.004, 0.10]), np.array([0.108, 0.046, 0.16])
    )
    peau = _smin(peau, _smax(paupiere, -ouverture, 0.012), 0.022)

    nez = _capsule(p, np.array([0.0, 0.16, 0.42]), np.array([0.0, -0.14, 0.60]), 0.085)
    ailes = _sphere(q, np.array([0.075, -0.15, 0.52]), 0.075)
    peau = _smin(peau, _smin(nez, ailes, 0.04), 0.045)

    oreille = _ellipsoide(
        q, np.array([0.575, 0.02, -0.06]), np.array([0.075, 0.155, 0.115])
    )
    peau = _smin(peau, oreille, 0.03)

    globe = _sphere(q, _GLOBE, 0.128)

    levres = _ellipsoide(p, np.array([0.0, -0.40, 0.47]), np.array([0.20, 0.085, 0.14]))
    fente = _boite_sdf(p, np.array([0.0, -0.40, 0.60]), np.array([0.165, 0.011, 0.12]))
    levres = _smax(levres, -fente, 0.018)

    sourcil = _capsule(
        q, np.array([0.10, 0.310, 0.475]), np.array([0.355, 0.290, 0.395]), 0.042
    )

    # Cheveux : une calotte dont on retire la face avant et le bas du crâne. Sans
    # ces deux coupes, la calotte englobe le visage entier et le rendu peint le
    # front, les joues et le nez en noir.
    calotte = _ellipsoide(p, np.array([0.0, 0.13, -0.07]), np.array([0.66, 0.77, 0.65]))
    visage_nu = _boite_sdf(
        p, np.array([0.0, -0.42, 0.62]), np.array([0.47, 1.00, 0.70]), arrondi=0.14
    )
    nuque = _boite_sdf(p, np.array([0.0, -1.30, 0.10]), np.array([1.4, 1.28, 1.0]))
    cheveux = _smax(_smax(calotte, -visage_nu, 0.035), -nuque, 0.05)

    return peau, globe, levres, sourcil, cheveux


def _sdf_visage(p: np.ndarray) -> np.ndarray:
    return np.min(np.stack(_parties_visage(p), axis=1), axis=1)


def _albedo_visage(p: np.ndarray) -> np.ndarray:
    peau, globe, levres, sourcil, cheveux = _parties_visage(p)
    q = p.copy()
    q[:, 0] = np.abs(q[:, 0])

    quoi = np.argmin(np.stack([peau, globe, levres, sourcil, cheveux], axis=1), axis=1)
    valeur = np.select(
        [quoi == 0, quoi == 1, quoi == 2, quoi == 3, quoi == 4],
        [0.76, 0.94, 0.44, 0.11, 0.13],
        default=0.76,
    )

    devant = q - _GLOBE
    rayon = np.hypot(devant[:, 0], devant[:, 1])
    sur_le_globe = (quoi == 1) & (devant[:, 2] > 0.0)
    valeur = np.where(sur_le_globe & (rayon < 0.066), 0.22, valeur)  # iris
    valeur = np.where(sur_le_globe & (rayon < 0.030), 0.04, valeur)  # pupille
    return valeur


def rendre_visage(
    taille: int = 512, lacet: float = 0.0, tangage: float = 0.0, *, suréchantillon: int = 2
) -> Image.Image:
    """Un visage en RGBA, fond transparent, par lancer de rayons sur la SDF.

    Même procédé que `rendre_solide`, et pour les mêmes raisons : rien de photo-
    graphique, rien de téléchargé, le même résultat à chaque exécution. L'alpha
    est exact hors silhouette, ce qui permet de composer plusieurs sujets sur un
    fond sans halo.
    """
    if suréchantillon > 1:
        grande = rendre_visage(taille * suréchantillon, lacet, tangage, suréchantillon=1)
        return grande.resize((taille, taille), Image.LANCZOS)

    axes = np.linspace(-1.25, 1.25, taille, dtype=np.float64)
    u, v = np.meshgrid(axes, -axes)
    n = taille * taille

    lacet_rad, tangage_rad = math.radians(lacet), math.radians(tangage)
    avant = np.array([
        -math.sin(lacet_rad) * math.cos(tangage_rad),
        -math.sin(tangage_rad),
        -math.cos(lacet_rad) * math.cos(tangage_rad),
    ])
    droite = np.array([math.cos(lacet_rad), 0.0, -math.sin(lacet_rad)])
    haut = np.cross(droite, avant)

    origines = (
        -3.0 * avant[None, :]
        + u.reshape(n, 1) * droite[None, :]
        + v.reshape(n, 1) * haut[None, :]
    )
    directions = np.repeat(avant[None, :], n, axis=0)

    distance = np.zeros(n)
    vivants = np.ones(n, dtype=bool)
    for _ in range(140):
        d = _sdf_visage(origines + distance[:, None] * directions)
        vivants &= (d >= 1e-4) & (distance < 6.5)
        distance = np.where(vivants, distance + np.maximum(0.75 * d, 1e-4), distance)
        if not vivants.any():
            break

    points = origines + distance[:, None] * directions
    atteint = _sdf_visage(points) < 1.5e-3

    eps = 1.5e-3
    normales = np.zeros_like(points)
    for axe in range(3):
        décalage = np.zeros(3)
        décalage[axe] = eps
        normales[:, axe] = _sdf_visage(points + décalage) - _sdf_visage(points - décalage)
    normales /= np.maximum(np.linalg.norm(normales, axis=1, keepdims=True), 1e-9)

    lumiere = np.array([0.35, 0.55, 0.76])
    lumiere = lumiere / np.linalg.norm(lumiere)
    diffus = np.clip(normales @ lumiere, 0.0, 1.0)
    appoint = np.clip(normales @ np.array([-0.6, 0.1, 0.5]), 0.0, 1.0)
    éclairement = 0.30 + 0.62 * diffus + 0.16 * appoint

    gris = (np.clip(_albedo_visage(points) * éclairement, 0.0, 1.0) * 255).astype(np.uint8)
    rgba = np.zeros((n, 4), dtype=np.uint8)
    rgba[:, 0] = rgba[:, 1] = rgba[:, 2] = gris
    rgba[:, 3] = np.where(atteint, 255, 0)
    rgba[~atteint, :3] = 0
    return Image.fromarray(rgba.reshape(taille, taille, 4), mode="RGBA")


def rendre_portrait(source: dict) -> Image.Image:
    """Un ou plusieurs visages composés sur un fond opaque.

    Plusieurs sujets à des échelles différentes ne sont pas un ornement : c'est
    ce qui donne aux cinq capacités qui traitent visage par visage un paramètre
    d'échelle mesurable. `max_faces` ne fait varier aucun coût sur une image qui
    n'en contient qu'un.
    """
    taille = int(source.get("taille", 512))
    image = _fond(str(source.get("fond", "atelier")), taille).convert("RGB")
    sujets = source.get("sujets") or [{"lacet": 0.0, "tangage": 0.0}]

    for sujet in sujets:
        échelle = float(sujet.get("echelle", 1.0))
        côté = max(48, int(échelle * taille))
        vignette = rendre_visage(
            côté, float(sujet.get("lacet", 0.0)), float(sujet.get("tangage", 0.0))
        )
        coin = (
            int(float(sujet.get("x", 0.5)) * taille) - côté // 2,
            int(float(sujet.get("y", 0.5)) * taille) - côté // 2,
        )
        image.paste(vignette, coin, vignette.getchannel("A"))

    return image


# --- scènes : un sujet, un fond, et un alpha connu d'avance ----------------------


def _fond(nom: str, taille: int) -> Image.Image:
    """Arrière-plan opaque, déterministe, et de difficulté choisie.

    C'est le fond qui fait la difficulté d'un détourage. Un dégradé lisse se
    sépare presque tout seul ; des rayures obliques dont le contraste approche
    celui du sujet obligent le modèle à décider à la frontière, ce qui est la
    seule chose qu'on lui demande.
    """
    rng = np.random.default_rng(20260821)
    y, x = np.mgrid[0:taille, 0:taille].astype(np.float64) / taille

    if nom == "raye":
        base = 120 + 46 * np.sign(np.sin((x * 5.0 + y * 2.0) * np.pi * 6))
    elif nom == "uni":
        base = np.full((taille, taille), 168.0)
    else:  # « atelier » : fond de studio, dégradé vertical et ligne d'horizon douce
        base = 226 - 92 * y - 26 * np.abs(x - 0.5)
        base += 18 * np.clip((y - 0.72) * 6, 0, 1)

    grain = rng.normal(0.0, 2.4, (taille, taille))
    tableau = np.clip(base + grain, 0, 255).astype(np.uint8)
    couleurs = np.stack([tableau, tableau, tableau], axis=2)
    return Image.fromarray(couleurs, mode="RGB")


class Onde:
    """Un signal audio qui sait s'écrire, pour que `produire` n'ait rien à savoir.

    Les recettes rendaient jusqu'ici des `Image.Image`, dont `produire` appelle
    `.save(chemin)`. Un mélange musical n'est pas une image ; lui donner la même
    méthode évite d'aiguiller sur le type au moment d'écrire, et garde l'écriture
    du WAV — seize bits, entrelacé — à un seul endroit.

    `wave` est en bibliothèque standard : cette recette n'ajoute aucune
    dépendance à un outil qui doit pouvoir tourner partout.
    """

    def __init__(self, canaux: np.ndarray, frequence: int) -> None:
        self.canaux = canaux  # (canaux, échantillons), flottants dans [-1, 1]
        self.frequence = frequence

    def save(self, chemin: Path | str) -> None:
        import wave

        entrelacé = np.clip(self.canaux.T, -1.0, 1.0)
        entiers = (entrelacé * 32767.0).astype("<i2")
        with wave.open(str(chemin), "wb") as sortie:
            sortie.setnchannels(self.canaux.shape[0])
            sortie.setsampwidth(2)
            sortie.setframerate(self.frequence)
            sortie.writeframes(entiers.tobytes())


def rendre_musique(source: dict) -> Onde:
    """Un mélange à quatre pistes, calculé — ni enregistrement, ni licence à suivre.

    La séparation de sources se mesure sur de la musique : c'est ce que HTDemucs
    a appris, et une voix seule le lui fait rendre n'importe quoi. Faute de
    morceau libre qu'on pourrait committer, on en fabrique un dont on connaît
    exactement les quatre pistes — basse, batterie, harmonie, voix — puisque
    c'est nous qui les additionnons.

    Ce n'est pas de la musique agréable, et ça n'a pas à l'être : la charge type
    mesure un **coût**, pas une qualité (voir le README du dossier). Ce qu'elle
    doit avoir, ce sont les traits que le réseau cherche — une fondamentale
    grave tenue, des transitoires percussifs brefs, un accord soutenu, une voix
    modulée dans le médium.
    """
    fréquence = int(source.get("frequence", 44_100))
    secondes = float(source.get("secondes", 12.0))
    tempo = float(source.get("tempo", 100.0))
    graine = int(source.get("graine", 1))

    rng = np.random.default_rng(graine)
    t = np.arange(int(secondes * fréquence)) / fréquence

    # Basse : fondamentale grave tenue, deux notes alternées à la mesure.
    mesure = 240.0 / tempo  # quatre temps
    note = np.where((t % (2 * mesure)) < mesure, 55.0, 73.42)  # la1, ré2
    basse = 0.32 * np.sin(2 * np.pi * np.cumsum(note) / fréquence)

    # Harmonie : triade tenue, plus une quinte une octave au-dessus.
    harmonie = 0.10 * sum(
        np.sin(2 * np.pi * f * t) for f in (220.0, 277.18, 329.63, 659.25)
    )

    # Batterie : transitoires brefs. Une grosse caisse tombe sur le temps, une
    # caisse claire sur les contretemps — du bruit filtré par une enveloppe
    # exponentielle, ce qui suffit à en faire des attaques franches.
    batterie = np.zeros_like(t)
    temps = 60.0 / tempo
    for index in range(int(secondes / temps) + 1):
        départ = int(index * temps * fréquence)
        if départ >= len(t):
            break
        grave = index % 2 == 0
        durée = int((0.09 if grave else 0.05) * fréquence)
        durée = min(durée, len(t) - départ)
        enveloppe = np.exp(-np.arange(durée) / (fréquence * (0.02 if grave else 0.012)))
        if grave:
            corps = np.sin(2 * np.pi * 58.0 * np.arange(durée) / fréquence)
        else:
            corps = rng.standard_normal(durée)
        batterie[départ : départ + durée] += 0.45 * enveloppe * corps

    # Voix : porteuse dans le médium, vibrato et enveloppe de phrasé — de quoi
    # occuper la bande où le réseau cherche un chant, sans prétendre en être un.
    vibrato = 1.0 + 0.015 * np.sin(2 * np.pi * 5.2 * t)
    phrasé = 0.5 * (1 + np.sin(2 * np.pi * 0.25 * t - np.pi / 2)) ** 2
    voix = 0.22 * phrasé * sum(
        amplitude * np.sin(2 * np.pi * 233.08 * rang * vibrato * t)
        for rang, amplitude in ((1, 1.0), (2, 0.35), (3, 0.18))
    )

    mélange = basse + harmonie + batterie + voix
    crête = float(np.abs(mélange).max()) or 1.0
    mélange = (mélange / crête * 0.89).astype(np.float32)
    # Stéréo par un très léger décalage : un mixage réellement mono ferait de la
    # stéréo du modèle une information constante, et le pipeline duplique déjà.
    décalage = int(0.0007 * fréquence)
    droite = np.concatenate([np.zeros(décalage, dtype=np.float32), mélange[:-décalage]])
    return Onde(np.stack([mélange, droite]), fréquence)


def rendre_scene(source: dict) -> tuple[Image.Image, Image.Image]:
    """Compose des solides sur un fond opaque, et rend l'alpha exact avec.

    L'intérêt du procédé est là : le masque n'est pas annoté à la main ni deviné,
    il est **calculé en même temps que l'image**. Une vérité terrain de détourage
    obtenue autrement se discute ; celle-ci est celle qui a servi à fabriquer la
    photo.
    """
    taille = int(source.get("taille", 768))
    image = _fond(str(source.get("fond", "atelier")), taille)
    masque = Image.new("L", (taille, taille), 0)

    for objet in source.get("objets", []):
        côté = max(8, int(float(objet["echelle"]) * taille))
        vignette = rendre_solide(str(objet["forme"]), côté)
        alpha = vignette.getchannel("A")
        coin = (
            int(float(objet["x"]) * taille) - côté // 2,
            int(float(objet["y"]) * taille) - côté // 2,
        )
        image.paste(vignette, coin, alpha)
        # `paste` avec masque compose ; pour cumuler les alphas de plusieurs
        # sujets, on colle du blanc à travers l'alpha plutôt que l'alpha lui-même.
        masque.paste(Image.new("L", vignette.size, 255), coin, alpha)

    return image, masque


# --- pilotage ------------------------------------------------------------------


def manifestes(cibles: list[str]) -> list[Path]:
    """Les manifestes à produire : golden sets et charges type du banc d'essai.

    Les deux familles emploient les mêmes recettes et la même règle append-only ;
    seule diffère la question qu'elles posent — une qualité d'un côté, un coût de
    l'autre. Leur donner deux outils ferait diverger deux fabriques d'images qui
    doivent rester la même.
    """
    if not cibles:
        return [
            *sorted((RACINE / "registry" / "evals" / "golden").glob("*/manifest.json")),
            *sorted((RACINE / "registry" / "evals" / "bench").glob("*.json")),
        ]
    trouvés: list[Path] = []
    for cible in cibles:
        chemin = Path(cible)
        if not chemin.is_absolute():
            chemin = RACINE / chemin
        if chemin.is_dir():
            trouvés.append(chemin / "manifest.json")
        else:
            trouvés.append(chemin)
    for chemin in trouvés:
        if not chemin.is_file():
            raise RecetteError(f"manifeste introuvable : {chemin}")
    return trouvés


def cas_a_produire(manifeste: Path) -> list[tuple[dict, dict]]:
    document = json.loads(manifeste.read_text())
    return [(cas, cas.get("source") or {}) for cas in document["cases"]]


def chemin_entree(cas: dict) -> str | None:
    for clé in ("document", "image", "audio"):
        valeur = cas["input"].get(clé)
        if isinstance(valeur, str):
            return valeur
    return None


def fichiers_du_cas(dossier: Path, cas: dict, source: dict) -> dict[str, Image.Image]:
    """Tout ce qu'une recette produit pour un cas : l'entrée, et sa vérité terrain.

    Un cas de détourage rend deux fichiers, et un cas d'agrandissement aussi :
    l'image basse définition qu'on soumet, et l'originale qui sert de référence.
    Les produire d'un même geste est ce qui garantit qu'ils décrivent la même
    scène — les fabriquer séparément laisserait la référence dériver de l'entrée
    sans que rien ne le dise.
    """
    recette = source["recipe"]
    entrée = chemin_entree(cas)
    if entrée is None:
        raise RecetteError(f"{cas['id']} : aucun fichier d'entrée à produire")
    référence = cas.get("reference") or {}

    if recette == "page":
        texte = (dossier / référence["text_file"]).read_text()
        return {entrée: rendre_page(texte.rstrip("\n"), source)}

    if recette == "solide":
        return {entrée: rendre_solide(str(source["forme"]))}

    if recette == "musique":
        return {entrée: rendre_musique(source)}

    if recette == "portrait":
        return {entrée: rendre_portrait(source)}

    if recette == "scene":
        image, masque = rendre_scene(source)
        fichiers: dict[str, Image.Image] = {}
        réduire = int(source.get("reduire", 1))
        if réduire > 1:
            côté = image.width // réduire
            fichiers[entrée] = image.resize((côté, côté), Image.BICUBIC)
        else:
            fichiers[entrée] = image
        if référence.get("mask_file"):
            fichiers[référence["mask_file"]] = masque
        if référence.get("image_file"):
            fichiers[référence["image_file"]] = image
        return fichiers

    raise RecetteError(f"{cas['id']} : recette inconnue {recette!r}")


def produire(manifeste: Path, *, force: bool, verbeux: bool = True) -> list[Path]:
    dossier = manifeste.parent
    écrits: list[Path] = []
    for cas, source in cas_a_produire(manifeste):
        if source.get("recipe") is None:
            continue
        fichiers = fichiers_du_cas(dossier, cas, source)
        for relatif, image in fichiers.items():
            cible = dossier / relatif
            if cible.exists() and not force:
                if verbeux:
                    print(f"  = {relatif} (déjà là)")
                continue
            cible.parent.mkdir(parents=True, exist_ok=True)
            image.save(cible)
            écrits.append(cible)
            if verbeux:
                print(f"  + {relatif}")
    return écrits


def main(argv: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parseur.add_argument(
        "cibles",
        nargs="*",
        help="Dossiers de golden set ou fichiers de charge type. Défaut : tous ceux "
        "qui déclarent une recette, dans golden/ comme dans bench/.",
    )
    parseur.add_argument(
        "--force",
        action="store_true",
        help="Réécrire un fichier existant. À n'utiliser que pour refabriquer un jeu "
        "jamais évalué : une entrée qui change invalide tous les résultats antérieurs.",
    )
    args = parseur.parse_args(argv)

    total = 0
    for manifeste in manifestes(args.cibles):
        étiquette = manifeste.parent.name if manifeste.name == "manifest.json" else manifeste.stem
        print(f"{étiquette} :")
        total += len(produire(manifeste, force=args.force))
    print(f"{total} fichier(s) écrit(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
