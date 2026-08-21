"""Fabrique les fichiers d'entrée des golden sets, à partir de leur manifeste.

    uv run --project runtimes/mlx-vlm python tools/golden_assets.py

Deux recettes, toutes deux déterministes et sans réseau :

- `page` rend une page de document depuis son texte de référence. Le manifeste
  reste l'autorité : c'est `reference.text_file` qui est rendu, si bien qu'une
  page et sa vérité terrain ne peuvent pas diverger. Les tabulations du fichier
  marquent les colonnes d'un tableau, l'indentation est conservée telle quelle,
  et la comparaison au score normalise tout cela — voir `normalization` dans
  `registry/schema/golden.schema.json` ;
- `solide` rend un objet 3D en RGBA à fond réellement transparent, par lancer de
  rayons sur une fonction de distance signée. Pas de photo, pas de moteur 3D,
  pas de licence à suivre : une centaine de lignes de numpy et le même résultat
  à chaque exécution.

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
