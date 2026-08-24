"""Charge type de `time-series-forecast` : une série horaire, trois fenêtres.

    uv run --project runtimes/chronos python tools/bench_assets.py series

**Une seule série, vue par trois fenêtres emboîtées.** Les 8192 points sont
tirés une fois ; les fichiers de 2048 et 512 en sont la **queue**, si bien que
les trois cas voient la même histoire et se terminent au même horodatage. C'est
ce qui permet à la pente du pic de porter sur le contexte et sur rien d'autre —
même idée que la scène unique de `depth-estimation` rendue à trois définitions.
Trois fichiers plutôt qu'un seul lu trois fois : la lecture du CSV fait partie du
coût que l'utilisateur paie, et la masquer donnerait une latence qui ne
correspond à aucun job réel.

**Deux saisonnalités, délibérément.** Un cycle journalier (24 pas) et un cycle
hebdomadaire (168 pas). Le second tient à peine dans la fenêtre de 512 : c'est
exactement ce qu'on veut mesurer, puisque c'est le contexte qui décide de ce que
le modèle peut encore voir.

**Aucune donnée à licencier.** La série est calculée, pas relevée : ni
provenance à suivre, ni consentement à recueillir, et elle se refabrique à
l'identique. Le bruit vient d'un `default_rng(20260824)` — PCG64, dont la suite
est stable d'une version de numpy à l'autre, contrairement au générateur hérité.

La recette a été exécutée deux fois avant d'être committée : sha256 identiques.
Ce sont ces empreintes que le fichier de charge inscrit, et non les tailles en
octets — celles-ci dépendent du format d'horodatage et de la fin de ligne, que
la recette fixe ici mais qu'un rapport écrit ailleurs ne fixe pas.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

#: numpy suffit, mais il n'est pas dans l'env racine d'Écurie — voir
#: `tools/assets/__init__.py`. `chronos` est l'env de la capacité servie.
ENV = "chronos"

#: Le plus long d'abord : les deux autres en sont la queue.
LONGUEURS = (8192, 2048, 512)

CIBLES = tuple(f"serie-horaire-{n}.csv" for n in LONGUEURS)

GRAINE = 20260824
DEBUT = datetime(2026, 1, 1, 0, 0, 0)
PAS = timedelta(hours=1)
SERIE = "serie-a"

# Les colonnes que `predict_df` attend par défaut, et que le contrat reprend
# comme valeurs par défaut de `colonne_serie`, `colonne_horodatage` et
# `colonne_valeur`. Une charge type qui les renommerait exercerait le
# renommage plutôt que le modèle.
COLONNES = ("item_id", "timestamp", "target")


def serie(n: int) -> np.ndarray:
    """Le niveau de consommation simulé, en `n` pas horaires.

    Composée plutôt que tirée d'un bruit pur : un modèle de prévision confronté à
    du bruit blanc rend la moyenne et ne coûte rien à évaluer. Ici il y a une
    tendance à retrouver, deux périodes à démêler, et un bruit qui empêche de
    recopier le passé.
    """
    t = np.arange(n, dtype="float64")
    tirage = np.random.default_rng(GRAINE)
    valeurs = (
        100.0
        + 12.0 * np.sin(2.0 * np.pi * t / 24.0)
        + 6.0 * np.sin(2.0 * np.pi * t / 168.0)
        + 0.004 * t
        + tirage.normal(0.0, 1.5, size=n)
    )
    return np.round(valeurs, 4)


def produire(dossier: Path, *, force: bool = False) -> list[Path]:
    complète = serie(max(LONGUEURS))
    écrits: list[Path] = []
    for longueur, nom in zip(LONGUEURS, CIBLES, strict=True):
        cible = dossier / nom
        if cible.exists() and not force:
            print(f"  {nom} : déjà là, laissé tel quel")
            continue
        # La queue, pas la tête : les trois fenêtres se terminent au même
        # horodatage, donc regardent la même fin d'histoire.
        valeurs = complète[len(complète) - longueur :]
        premier = DEBUT + PAS * (len(complète) - longueur)
        _ecrire(cible, valeurs, premier)
        print(f"  {nom} : {longueur} pas, {cible.stat().st_size} octets")
        écrits.append(cible)
    return écrits


def _ecrire(cible: Path, valeurs: np.ndarray, premier: datetime) -> None:
    """CSV en format long, une ligne par (série, horodatage).

    `lineterminator` est posé explicitement : le défaut du module `csv` est
    `\\r\\n`, et une charge type figée dont les fins de ligne dépendraient de la
    plateforme ne se refabriquerait pas à l'identique.
    """
    with open(cible, "w", encoding="utf-8", newline="") as flux:
        greffier = csv.writer(flux, lineterminator="\n")
        greffier.writerow(COLONNES)
        for rang, valeur in enumerate(valeurs):
            horodatage = (premier + PAS * rang).isoformat(sep=" ")
            greffier.writerow([SERIE, horodatage, f"{valeur:.4f}"])
