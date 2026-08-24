"""Charge type de `pointcloud-to-cad` : trois pièces, trois familles de construction.

    uv run --project runtimes/cad-recode python tools/bench_assets.py nuages

**Trois pièces plutôt qu'une pièce à trois définitions**, contrairement aux
charges de `image-matting` ou `depth-estimation`. La raison est que le coût de
cette capacité ne suit aucun paramètre d'entrée : le pic est dominé par les
3,09 Go de poids, et la durée par le **nombre de jetons produits**, qui dépend de
la pièce et n'est connu qu'après coup. `n_points`, seul paramètre candidat, vaut
256 dans les trois cas — c'est la valeur d'entraînement, et la faire varier
mesurerait la dégradation hors distribution plutôt qu'un coût. Le pic bouge bien
avec lui, mais par une **marche** et non par une pente : mesuré dans quatre
processus séparés, 3,30 Gio à 64 points, 3,32 à 128, 4,42 à 256, 4,54 à 512, soit
R² = 0,77 pour un ajustement linéaire — sous le seuil de 0,9 en dessous duquel le
banc jette la pente de toute façon. La charge n'a donc pas de
`scaling_parameter`, et le profil garde le pire cas.

**Ce que ces trois pièces ne montrent pas.** Ce sont trois constructions à une
seule opération, et le modèle les rend justes. Éprouvée hors banc, une équerre à
quatre opérations — semelle percée de deux trous, plus un montant — revient avec
la bonne silhouette et **sans ses perçages**, remplacés par deux encoches dans le
profil extrudé (Chamfer ×1000 : 0,951, contre 0,000 sur le cube). Une charge type
mesure un coût, pas une qualité ; celle-ci ne dira jamais que le modèle a perdu
un trou.

Ce que chacune éprouve :

    cube             la construction la plus courte du dialecte — `.box(...)`,
                     un solide convexe à six faces planes ;
    cylindre-perce   une révolution ET un perçage traversant, c'est-à-dire une
                     esquisse à deux cercles dont le second est soustractif ;
    piece-en-l       un profil polygonal à six segments, extrudé — la famille de
                     constructions la plus longue à écrire, donc la plus lente.

**Les nuages sont figés à 256 points, pas à 8192.** C'est le choix qui rend cette
charge reproductible sans dépendre de la version de trimesh : les points soumis
au modèle sont *dans le fichier*, et l'adaptateur les prend tels quels. Faire
porter à la charge un maillage, en laissant l'échantillonnage au job, ferait
dépendre la mesure du tirage de `sample_surface` — dont la graine est justement
le piège de cette famille (`np.random.seed()` est inerte depuis trimesh 5.0.0).
Le revers est assumé : la charge n'exerce ni l'échantillonnage de surface ni la
lecture d'une scène GLB. Ces deux chemins-là sont éprouvés par les tests et par
les vrais jobs, pas par le banc.

**Aucune donnée à licencier.** Les trois solides sont construits par CadQuery,
pas relevés : ni provenance à suivre, ni consentement à recueillir, et ils se
refabriquent à l'identique. C'est aussi ce qui donne une vérité terrain — on
connaît le volume exact de chaque pièce, ce qu'aucun scan ne donnerait.

La recette a été exécutée deux fois avant d'être committée : sha256 identiques.
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq
import numpy as np
import trimesh

#: cadquery pour construire, trimesh pour tesseller et écrire, numpy pour choisir.
ENV = "cad-recode"

#: La graine de `sample_surface`, en **argument nommé**. `np.random.seed()` est
#: inerte avec trimesh 5.0.0 — mesuré, trois appels donnent trois empreintes
#: différentes. C'est la recette de reproductibilité du démonstrateur amont, et
#: elle ne marche pas.
GRAINE = 42

#: Points tirés de la surface avant l'échantillonnage du plus lointain, comme
#: l'amont. C'est le second passage qui choisit, et il choisit par distance.
PRE_POINTS = 8192

#: La valeur d'entraînement du modèle. Les fichiers la figent.
POINTS = 256

#: Tolérances de tessellation de l'amont : linéaire puis angulaire.
TESSELLATION = (0.001, 0.1)


def cube() -> cq.Workplane:
    """Un cube de 200 mm — la construction la plus courte du dialecte."""
    return cq.Workplane("XY").box(200, 200, 200)


def cylindre_perce() -> cq.Workplane:
    """Un cylindre de rayon 100 percé de part en part d'un trou de rayon 40."""
    return (
        cq.Workplane("XY")
        .sketch()
        .circle(100)
        .circle(40, mode="s")
        .finalize()
        .extrude(-200)
    )


def piece_en_l() -> cq.Workplane:
    """Un profil en L à six segments, extrudé de 50 mm.

    Les cotes sont entières et tiennent dans la plage -100…+100 sur laquelle le
    modèle a été entraîné, non par nécessité — le cadrage efface l'échelle — mais
    pour que le programme attendu soit lisible par qui compare.
    """
    profil = [(0, 0), (120, 0), (120, 40), (40, 40), (40, 100), (0, 100)]
    return cq.Workplane("XY").polyline(profil).close().extrude(-50)


PIECES = {
    "cube": cube,
    "cylindre-perce": cylindre_perce,
    "piece-en-l": piece_en_l,
}

CIBLES = tuple(f"nuage-{nom}.ply" for nom in PIECES)


def maillage(forme: cq.Workplane) -> trimesh.Trimesh:
    """Le solide exact vers un maillage, par la tessellation d'OCC."""
    sommets, faces = forme.val().tessellate(*TESSELLATION)
    return trimesh.Trimesh([(v.x, v.y, v.z) for v in sommets], faces)


def cadrer(points: np.ndarray) -> np.ndarray:
    """Centrage sur les bornes, puis mise à l'échelle dans un cube de côté 2.

    Sur les bornes et non sur le centre de masse : c'est le cadrage de l'amont, et
    il ne dépend pas de la densité des points. L'adaptateur applique exactement le
    même — un nuage déjà cadré y repasse sans changer, ce qui est la propriété qui
    permet de figer le fichier ici.
    """
    minima, maxima = points.min(axis=0), points.max(axis=0)
    centré = points - (minima + maxima) / 2.0
    return centré * (2.0 / float(np.max(maxima - minima)))


def plus_lointains(points: np.ndarray, k: int) -> np.ndarray:
    """`k` points aussi écartés que possible, départ à l'indice 0.

    Réécrit ici comme dans l'adaptateur, et non importé : cette recette tourne
    dans le venv du runtime, où `ecurie_runtime` n'est pas visible. Les deux
    écritures doivent donner le même résultat, mais elles n'ont pas à être le même
    code — c'est le fichier produit qui fait foi, et il est figé.

    Le départ est à l'indice 0 comme l'amont, dont le défaut
    `random_start_point=False` a été relu : un départ tiré au sort rendrait cette
    charge irreproductible.
    """
    choisis = np.empty(k, dtype=np.int64)
    choisis[0] = 0
    distances = np.sum((points - points[0]) ** 2, axis=1)
    for rang in range(1, k):
        suivant = int(np.argmax(distances))
        choisis[rang] = suivant
        distances = np.minimum(distances, np.sum((points - points[suivant]) ** 2, axis=1))
    return choisis


def nuage(forme: cq.Workplane) -> np.ndarray:
    """Un solide vers les 256 points que le modèle verra."""
    m = maillage(forme)
    tirés, _ = trimesh.sample.sample_surface(m, PRE_POINTS, seed=GRAINE)
    points = cadrer(np.asarray(tirés, dtype="float64"))
    return points[plus_lointains(points, POINTS)]


def produire(dossier: Path, *, force: bool = False) -> list[Path]:
    écrits: list[Path] = []
    for construire, fichier in zip(PIECES.values(), CIBLES, strict=True):
        cible = dossier / fichier
        if cible.exists() and not force:
            print(f"  {fichier} : déjà là, laissé tel quel")
            continue
        forme = construire()
        points = nuage(forme)
        _ecrire(cible, points)
        volume = forme.val().Volume()
        print(
            f"  {fichier} : {len(points)} points, {cible.stat().st_size} octets, "
            f"bornes [{points.min():.3f} ; {points.max():.3f}], volume exact {volume:.0f} mm³"
        )
        écrits.append(cible)
    return écrits


def _ecrire(cible: Path, points: np.ndarray) -> None:
    """PLY binaire de points purs, en float32.

    Binaire et non ASCII : c'est ce que trimesh relit sans reformater, et un
    aller-retour exact à 3e-08 près (la précision de float32) a été vérifié avant
    de figer ces fichiers. Un PLY ASCII aurait fait dépendre l'empreinte du
    format d'écriture des flottants, qui n'est fixé nulle part.
    """
    cible.write_bytes(
        trimesh.PointCloud(points.astype("float32")).export(file_type="ply", encoding="binary")
    )
