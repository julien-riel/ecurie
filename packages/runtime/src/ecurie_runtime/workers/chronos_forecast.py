"""Adaptateur `chronos` : une série de nombres entre, un éventail de quantiles sort.

Première capacité du parc qui ne voit ni n'entend rien. Le modèle est petit et
son appel tient en une ligne — `Chronos2Pipeline.predict_df(...)`, chargement
mesuré à 0,06 s. Tout le travail de cet adaptateur est autour, et chacune des
quatre pièces répond à un défaut **mesuré** sur cette machine.

**Les quantiles hors plage sont rabattus sous une étiquette qui ment.** Demander
0,001 ne lève rien : la bibliothèque rend une colonne nommée « 0.001 » qui
contient, au bit près, la valeur du niveau 0,01 (`np.allclose` vérifié). Elle
étiquette avec le niveau **demandé** ce qu'elle a calculé au niveau **rabattu**.
Et son avertissement passe par `logging`, pas par `warnings.warn` : un
`catch_warnings(record=True)` autour de l'appel rend une liste vide. Un
adaptateur ne peut donc pas lire les niveaux réels dans la sortie — il écrête
lui-même avant d'appeler, nomme ses colonnes d'après son propre écrêtage, et
remonte l'écart en avertissement. Sans cela un intervalle annoncé à 99,8 % en
vaudrait 98, et rien ne le dirait.

**Un trou dans les horodatages est le piège de cette famille, et le correctif
intuitif est le mauvais.** Sans grille régulière, `predict_df` lève
« Could not infer frequency for series … ». Passer `freq="h"` fait taire l'erreur
et le modèle lit les points comme contigus : le trou est refermé en silence,
aucun avertissement, forme de sortie normale. Le correctif juste est de
réindexer soi-même sur la grille et de laisser des NaN — mesuré, 30 NaN en
entrée donnent zéro NaN en sortie. Le champ `freq` du contrat nomme une
fréquence ambiguë ; il ne masque pas un trou, et cet adaptateur ne le laisse pas
faire.

**`predict_df` rend deux colonnes que le contrat ne décrit pas.** Neuf colonnes
pour cinq niveaux demandés : `target_name` — la bibliothèque accepte plusieurs
cibles — et `predictions`, doublon exact de la médiane. Un `to_csv` direct
livrerait un fichier que rien ne documente. Elles sont écartées ici, nommément.

**Une covariable future n'existe que si elle a un passé.** Appris au premier
lancement, et pas avant : `future_df cannot contain columns not present in df`.
Le modèle apprend le lien entre la covariable et la cible sur la fenêtre de
contexte, puis l'applique aux valeurs à venir ; une colonne qui n'apparaîtrait
qu'au futur ne lui dirait rien. Le message d'amont parle de colonnes en trop, ce
qui envoie retirer la covariable au lieu de compléter l'historique — l'adaptateur
le vérifie donc lui-même et le dit dans l'autre sens.

**Le CPU bat MPS, et ce n'est pas un repli.** Un processus par périphérique,
médiane de cinq passages après deux tours de chauffe, horizon 168 : CPU
35 / 55 / 139 ms aux contextes 512 / 2048 / 8192, MPS 28 / 58 / 190 ms. MPS ne
gagne qu'à 512 et perd de 37 % à 8192 — le modèle est trop petit pour que le
transfert vers Metal se rembourse. Le pic se mesure donc au RSS, qui dit vrai
tant que rien n'est alloué sur Metal ; le variant `mps` relève en plus
`driver_allocated_memory`, dont le RSS ne sait rien.

Rien de torch, pandas ni chronos n'est importé au niveau du module (voir
`workers/__init__.py`) : la CI importe tous les adaptateurs sans Apple Silicon.
"""

import gc
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecurie_runtime.workers.base import (
    InferRequest,
    InferResult,
    ProgressFn,
    Worker,
    WorkerError,
    main,
    peak_rss_bytes,
)

ENV_NAME = "chronos"
REPAIR = f"ecurie env sync {ENV_NAME}"

PREVISIONS_CSV = "previsions.csv"
PREVISIONS_JSON = "previsions.json"
GRAPHIQUE_PNG = "graphique.png"

TABLEAUX = {".csv", ".txt"}

# Les niveaux extrêmes sur lesquels Chronos-2 a été entraîné, lus dans
# `chronos_config.quantiles` de son config.json. Hors de ces bornes la
# bibliothèque rabat sans le dire ; le contrat les reprend, et cet adaptateur
# écrête une seconde fois — un worker peut être appelé sans passer par la
# validation du contrat, et c'est alors ici que la sortie cesse de mentir.
QUANTILE_MIN = 0.01
QUANTILE_MAX = 0.99

# Ce que le modèle produit en un seul passage : 64 tuiles de 16 pas
# (`max_output_patches` × `output_patch_size`). Au-delà il déroule, et la falaise
# est nette — 1024 pas coûtent 47 ms, 1025 en coûtent 215.
HORIZON_MAX = 1024
CONTEXTE_MAX = 8192

# Les deux colonnes que `predict_df` ajoute et que le contrat ne décrit pas.
# Écartées, pas renommées : `predictions` est le doublon exact de la médiane, et
# la garder ferait croire à une septième prévision.
COLONNES_ECARTEES = ("target_name", "predictions")

DEVICES = ("cpu", "mps")

# Au-delà, le PNG devient une colonne de vignettes illisibles. Le CSV et le JSON
# portent toutes les séries ; c'est le tracé qui s'arrête, et il le dit.
PANNEAUX_MAX = 4


# --- ce qui se vérifie sans poids ---------------------------------------------


@dataclass(frozen=True)
class Demande:
    """Ce qui a été demandé, résolu, et ce qui n'a pas pu l'être."""

    horizon: int
    contexte: int
    quantiles: tuple[float, ...]
    freq: str
    colonne_serie: str
    colonne_horodatage: str
    colonne_valeur: str
    graphique: bool
    warnings: tuple[str, ...] = ()


def plan_prevision(
    *,
    entree: Mapping[str, Any],
    params: Mapping[str, Any],
    defaults: Mapping[str, Any],
) -> Demande:
    """Traduit une demande du protocole en réglages de prévision.

    Fonction pure, sans torch ni pandas : c'est tout ce qui se vérifie sans les
    poids — la priorité des trois couches, le refus d'un horizon que le modèle ne
    sait pas rendre, et l'écrêtage des quantiles, qui est la pièce dont dépend
    l'honnêteté de la sortie.
    """
    couches = (entree, params, defaults)

    horizon = _entier("horizon", 24, 1, HORIZON_MAX, couches)
    contexte = _entier("contexte", CONTEXTE_MAX, 64, CONTEXTE_MAX, couches)

    niveaux, avertissements = ecreter_quantiles(_reglage("quantiles", *couches))

    freq = str(_reglage("freq", *couches) or "").strip()
    graphique = _reglage("graphique", *couches)

    return Demande(
        horizon=horizon,
        contexte=contexte,
        quantiles=niveaux,
        freq=freq,
        colonne_serie=str(_reglage("colonne_serie", *couches) or "item_id"),
        colonne_horodatage=str(_reglage("colonne_horodatage", *couches) or "timestamp"),
        colonne_valeur=str(_reglage("colonne_valeur", *couches) or "target"),
        graphique=True if graphique is None else bool(graphique),
        warnings=tuple(avertissements),
    )


def ecreter_quantiles(brut: Any) -> tuple[tuple[float, ...], list[str]]:
    """Les niveaux réellement calculables, et ce qu'on a dû corriger pour y arriver.

    Trois corrections, dans cet ordre : écrêtage sur la plage d'entraînement,
    suppression des doublons — deux niveaux rabattus sur la même borne
    donneraient deux colonnes identiques —, et tri croissant, sans quoi
    l'éventail du tracé se replierait sur lui-même.

    Chacune est dite. Un job qui demande [0,001 ; 0,5 ; 0,999] et reçoit
    [0,01 ; 0,5 ; 0,99] a reçu autre chose que ce qu'il demandait, et c'est
    exactement le genre d'écart qu'un banc au vert ne regarde pas.
    """
    if brut is None:
        brut = [0.1, 0.25, 0.5, 0.75, 0.9]
    if isinstance(brut, (int, float)) and not isinstance(brut, bool):
        brut = [brut]
    if not isinstance(brut, Sequence) or isinstance(brut, (str, bytes)):
        raise WorkerError(
            f"quantiles : liste de nombres attendue, reçu {type(brut).__name__}"
        )

    valeurs: list[float] = []
    for item in brut:
        try:
            valeurs.append(float(item))
        except (TypeError, ValueError) as exc:
            raise WorkerError(f"quantiles : {item!r} n'est pas un nombre") from exc
    if not valeurs:
        raise WorkerError("quantiles : au moins un niveau est nécessaire")

    avertissements: list[str] = []
    rabattus = [v for v in valeurs if v < QUANTILE_MIN or v > QUANTILE_MAX]
    if rabattus:
        avertissements.append(
            f"niveaux hors plage écrêtés sur [{QUANTILE_MIN} ; {QUANTILE_MAX}] : "
            f"{', '.join(nom_niveau(v) for v in rabattus)} — Chronos-2 n'a pas été "
            "entraîné au-delà, et la bibliothèque y répondrait par la valeur de la "
            "borne sous l'étiquette du niveau demandé"
        )

    écrêtés = [min(max(v, QUANTILE_MIN), QUANTILE_MAX) for v in valeurs]
    niveaux = sorted(set(écrêtés))
    if len(niveaux) < len(écrêtés):
        avertissements.append(
            f"{len(écrêtés) - len(niveaux)} niveau(x) en double après écrêtage, "
            "retiré(s) : deux colonnes identiques ne sont pas un éventail"
        )
    return tuple(niveaux), avertissements


def nom_niveau(valeur: float) -> str:
    """Le nom de colonne d'un niveau : « 0.1 », « 0.25 », « 0.99 ».

    Écrit ici plutôt que repris de la sortie d'amont, et c'est le cœur du
    problème : les colonnes de `predict_df` portent le niveau **demandé**, même
    quand la valeur est celle d'un autre. Nommer d'après ce qu'on a réellement
    fait calculer est ce qui rend le CSV vrai.
    """
    return f"{valeur:g}"


def colonnes_quantiles(
    colonnes: Sequence[str], niveaux: Sequence[float], ignorees: Sequence[str]
) -> list[str]:
    """Les colonnes de `predict_df` qui portent les quantiles, dans l'ordre demandé.

    Par nom quand la bibliothèque les nomme comme on s'y attend, par position
    sinon. Le compte, lui, ne se négocie pas : une sortie qui ne rend pas autant
    de colonnes que de niveaux demandés n'est plus celle que le contrat décrit,
    et il vaut mieux refuser le job que livrer un CSV dont on ne sait plus ce que
    disent les colonnes.
    """
    exclues = set(ignorees)
    restantes = [c for c in colonnes if c not in exclues]
    if len(restantes) != len(niveaux):
        raise WorkerError(
            f"la bibliothèque rend {len(restantes)} colonne(s) de quantile pour "
            f"{len(niveaux)} niveau(x) demandé(s) ({', '.join(restantes) or 'aucune'}) : "
            "la forme de `predict_df` a changé, et le CSV ne serait plus celui que "
            "le contrat décrit"
        )
    return [
        str(niveau) if str(niveau) in restantes else brut
        for niveau, brut in zip(niveaux, restantes, strict=True)
    ]


def _reglage(nom: str, *couches: Mapping[str, Any]) -> Any:
    """Première valeur définie, de la plus prioritaire à la moins : entrée, job, manifeste."""
    for couche in couches:
        valeur = couche.get(nom)
        if valeur is not None:
            return valeur
    return None


def _entier(
    nom: str, defaut: int, plancher: int, plafond: int, couches: tuple[Mapping[str, Any], ...]
) -> int:
    valeur = _reglage(nom, *couches)
    if valeur is None:
        return defaut
    try:
        entier = int(valeur)
    except (TypeError, ValueError) as exc:
        raise WorkerError(f"{nom} : entier attendu, reçu {valeur!r}") from exc
    if not plancher <= entier <= plafond:
        raise WorkerError(
            f"{nom} = {entier} : le contrat borne ce paramètre à [{plancher} ; {plafond}]"
        )
    return entier


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


def resolve_table(valeur: Any, job_dir: Path, champ: str) -> Path:
    """Le chemin d'un CSV, relatif au dossier du job quand il l'est."""
    brut = str(valeur or "").strip()
    if not brut:
        raise WorkerError(f"aucun tableau en entrée : le champ `{champ}` est vide")
    chemin = Path(brut).expanduser()
    if not chemin.is_absolute():
        chemin = job_dir / chemin
    if not chemin.is_file():
        raise WorkerError(f"{champ} introuvable : {chemin}")
    if chemin.suffix.lower() not in TABLEAUX:
        raise WorkerError(
            f"format non géré : {chemin.suffix or '(sans extension)'} — "
            f"attendu {', '.join(sorted(TABLEAUX))}"
        )
    return chemin


# --- l'adaptateur -------------------------------------------------------------


class ChronosForecastWorker(Worker):
    """Prévision probabiliste zéro-shot, par Chronos-2."""

    name = "chronos-forecast"

    def __init__(self) -> None:
        self._pipeline: Any = None
        self._pd: Any = None
        self._torch: Any = None
        self._plt: Any = None
        self._device = "cpu"
        self._defaults: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._peak_driver = 0

    # --- chargement ----------------------------------------------------------

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        chemin = weights_dir(variant)
        try:
            import matplotlib
            import pandas as pd
            import torch
            from chronos import Chronos2Pipeline

            # Avant `pyplot`, sinon le choix du backend n'a plus d'effet et
            # matplotlib cherche un serveur graphique que le worker n'a pas.
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError as exc:
            raise WorkerError(
                f"runtime chronos indisponible dans cet environnement ({exc}) — `{REPAIR}`"
            ) from exc

        self._defaults = dict(variant.get("defaults") or {})
        self._options = dict(variant.get("options") or {})
        self._pd, self._torch, self._plt = pd, torch, plt
        self._device = self._choisir_device(torch)

        try:
            self._pipeline = Chronos2Pipeline.from_pretrained(
                str(chemin), device_map=self._device
            )
        except Exception as exc:  # noqa: BLE001 — remonte avec la réparation
            raise WorkerError(
                f"chargement de Chronos-2 impossible depuis {chemin} : "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        return {
            "device": self._device,
            "quantile_range": [QUANTILE_MIN, QUANTILE_MAX],
            "horizon_max": HORIZON_MAX,
            "context_max": CONTEXTE_MAX,
            "versions": self._versions(),
        }

    def _choisir_device(self, torch: Any) -> str:
        """Le périphérique du variant, ou le CPU. Un `mps` sans MPS est refusé.

        Refusé plutôt que replié : un variant nommé `mps` existe pour être
        comparé au variant CPU, et retomber en silence sur le processeur ferait
        mesurer deux fois la même chose sous deux noms.
        """
        demandé = str(self._options.get("device") or "cpu").strip().lower()
        if demandé not in DEVICES:
            raise WorkerError(
                f"options.device = {demandé!r} : ce runtime ne sert que {', '.join(DEVICES)}"
            )
        if demandé == "mps" and not torch.backends.mps.is_available():
            raise WorkerError(
                "backend MPS indisponible (torch.backends.mps.is_available() est faux) — "
                "ce variant ne sert que sur Apple Silicon ; le variant `cpu` est de toute "
                "façon le plus rapide au-delà de deux mille pas de contexte"
            )
        return demandé

    # --- exécution -----------------------------------------------------------

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._pipeline is None:
            raise WorkerError("infer avant load — aucun modèle en mémoire")

        plan = plan_prevision(
            entree=request.input, params=request.params, defaults=self._defaults
        )
        avertissements = list(plan.warnings)

        progress(5, "lecture de la série")
        chemin = resolve_table(request.get("serie"), request.output_dir, "serie")
        cadre = self._lire_csv(chemin, "serie")
        self._exiger_colonnes(cadre, plan, chemin, "serie")

        progress(15, "mise sur grille régulière")
        cadre, offset, trous = self._regulariser(cadre, plan)
        if trous:
            avertissements.append(
                f"{trous} pas manquant(s) dans les horodatages, comblé(s) par des valeurs "
                "inconnues : le modèle les traite comme tels et ne referme pas le trou. "
                "Renseigner `freq` ne l'aurait pas corrigé, cela l'aurait masqué"
            )

        cadre, contexte_utilise = self._tronquer(cadre, plan)

        futur = None
        source_futur = request.get("covariables_futures")
        if source_futur:
            progress(25, "lecture des covariables futures")
            futur = self._lire_covariables(source_futur, request, plan, cadre)

        progress(35, "prévision")
        sortie = self._predire(cadre, futur, plan, offset)

        progress(70, "mise en forme")
        colonnes = colonnes_quantiles(list(sortie.columns), plan.quantiles, self._ecartees(plan))
        table = self._projeter(sortie, colonnes, plan)

        n_series = int(table[plan.colonne_serie].nunique())
        # Une prévision qui rend des valeurs inconnues n'est pas une prévision.
        # Le compte est relevé même quand il est nul : c'est le premier chiffre à
        # regarder quand une sortie a l'air normale.
        inconnues = int(table[[nom_niveau(q) for q in plan.quantiles]].isna().sum().sum())
        if inconnues:
            avertissements.append(
                f"{inconnues} valeur(s) inconnue(s) dans la prévision : la série d'entrée "
                "est probablement vide sur toute la fenêtre de contexte"
            )

        tracées = 0
        if plan.graphique:
            progress(80, "tracé de l'éventail")
            tracées = self._tracer(cadre, table, plan, offset, request.output_dir / GRAPHIQUE_PNG)
            if tracées < n_series:
                avertissements.append(
                    f"{n_series} séries prévues, {tracées} tracées : le PNG s'arrête aux "
                    "premières pour rester lisible. Le CSV et le JSON les portent toutes"
                )

        progress(90, "écriture des sorties")
        table.to_csv(request.output_dir / PREVISIONS_CSV, index=False, lineterminator="\n")
        document = self._document(table, plan, offset, contexte_utilise, avertissements)
        (request.output_dir / PREVISIONS_JSON).write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        resultat: dict[str, Any] = {
            "previsions": PREVISIONS_CSV,
            "previsions_json": PREVISIONS_JSON,
            "quantiles": [float(q) for q in plan.quantiles],
            "horizon": plan.horizon,
            "freq": offset.freqstr,
            "n_series": n_series,
            "contexte_utilise": contexte_utilise,
        }
        if plan.graphique:
            resultat["graphique"] = GRAPHIQUE_PNG

        metriques: dict[str, Any] = {
            "device": self._device,
            "horizon": plan.horizon,
            "contexte_utilise": contexte_utilise,
            "n_series": n_series,
            "quantiles": len(plan.quantiles),
            "freq": offset.freqstr,
            "trous_combles": trous,
            "nan_en_sortie": inconnues,
            # Dit nommément, plutôt que laissé au silence : la sortie d'amont
            # porte deux colonnes de plus que le contrat, et savoir lesquelles
            # ont disparu vaut mieux que de constater qu'il en manque.
            "colonnes_ecartees": list(COLONNES_ECARTEES),
            "peak_memory_bytes": self.peak_memory_bytes(),
        }
        if avertissements:
            metriques["warnings"] = avertissements
        return InferResult(output=resultat, metrics=metriques)

    # --- lecture et mise en forme --------------------------------------------

    def _lire_csv(self, chemin: Path, champ: str) -> Any:
        try:
            return self._pd.read_csv(chemin)
        except Exception as exc:  # noqa: BLE001 — remonte traduit
            raise WorkerError(
                f"{champ} illisible ({chemin.name}) : {type(exc).__name__}: {exc} — "
                "attendu un CSV séparé par des virgules, point décimal"
            ) from exc

    def _exiger_colonnes(self, cadre: Any, plan: Demande, chemin: Path, champ: str) -> None:
        """Refuse avant tout calcul, et traduit la cause plutôt que de la relayer.

        La bibliothèque garde la porte — elle nomme les colonnes manquantes et
        refuse une cible non numérique — mais son message parle de colonnes quand
        la cause est presque toujours ailleurs : un CSV français au point-virgule
        arrive en **une** colonne, dont le nom contient les trois autres. Dire
        « colonne manquante » à qui a exporté depuis un tableur français l'envoie
        chercher au mauvais endroit.
        """
        attendues = [plan.colonne_serie, plan.colonne_horodatage, plan.colonne_valeur]
        manquantes = [c for c in attendues if c not in cadre.columns]
        if not manquantes:
            return
        présentes = ", ".join(str(c) for c in cadre.columns) or "aucune"
        indice = ""
        if len(cadre.columns) == 1 and any(
            sep in str(cadre.columns[0]) for sep in (";", "\t", "|")
        ):
            indice = (
                " — le fichier n'a qu'une colonne, dont le nom contient un point-virgule "
                "ou une tabulation : c'est le séparateur qui est en cause, pas les noms. "
                "Réexporter en CSV séparé par des virgules, avec le point décimal"
            )
        raise WorkerError(
            f"{champ} ({chemin.name}) : colonne(s) absente(s) — {', '.join(manquantes)}. "
            f"Colonnes lues : {présentes}{indice}"
        )

    def _regulariser(self, cadre: Any, plan: Demande) -> tuple[Any, Any, int]:
        """Chaque série sur sa propre grille régulière, les manques laissés inconnus.

        C'est la pièce qui décide de la justesse du résultat. `predict_df` refuse
        une série dont il ne peut pas inférer la fréquence, et le raccourci qui
        consiste à lui passer `freq` fait disparaître le refus **sans** faire
        disparaître le trou : le modèle lit alors deux points distants de trois
        heures comme s'ils se suivaient. Réindexer et laisser des NaN est le seul
        correctif qui dise la vérité au modèle.
        """
        pd = self._pd
        série = cadre[plan.colonne_horodatage]
        try:
            horodatages = pd.to_datetime(série)
        except Exception as exc:  # noqa: BLE001 — remonte traduit
            raise WorkerError(
                f"colonne d'horodatage `{plan.colonne_horodatage}` illisible en dates : "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        cadre = cadre.assign(**{plan.colonne_horodatage: horodatages})

        valeurs = pd.to_numeric(cadre[plan.colonne_valeur], errors="coerce")
        if valeurs.isna().all():
            raise WorkerError(
                f"colonne de valeur `{plan.colonne_valeur}` : aucune valeur numérique "
                f"(type lu : {cadre[plan.colonne_valeur].dtype}) — vérifier le séparateur "
                "décimal, une virgule décimale n'est pas lue comme un nombre"
            )
        cadre = cadre.assign(**{plan.colonne_valeur: valeurs})

        offset = self._offset(cadre, plan)
        morceaux = []
        trous = 0
        for identifiant, groupe in cadre.groupby(plan.colonne_serie, sort=True):
            groupe = groupe.sort_values(plan.colonne_horodatage)
            index = pd.DatetimeIndex(groupe[plan.colonne_horodatage])
            grille = pd.date_range(index.min(), index.max(), freq=offset)
            absents = index.difference(grille)
            if len(absents):
                raise WorkerError(
                    f"série {identifiant!r} : {len(absents)} horodatage(s) ne tombent pas "
                    f"sur une grille de pas {offset.freqstr} (le premier étant "
                    f"{absents[0]}) — préciser `freq`, ou rééchantillonner la série avant "
                    "de la soumettre. Aucune valeur n'est écartée en silence"
                )
            trous += len(grille) - len(index)
            rempli = (
                groupe.set_index(plan.colonne_horodatage)
                .reindex(grille)
                .rename_axis(plan.colonne_horodatage)
                .reset_index()
            )
            rempli[plan.colonne_serie] = identifiant
            morceaux.append(rempli)

        régulier = pd.concat(morceaux, ignore_index=True)
        return régulier, offset, trous

    def _offset(self, cadre: Any, plan: Demande) -> Any:
        """Le pas de temps : celui qu'on impose, ou l'écart le plus fréquent."""
        pd = self._pd
        if plan.freq:
            try:
                return pd.tseries.frequencies.to_offset(plan.freq)
            except Exception as exc:  # noqa: BLE001 — remonte traduit
                raise WorkerError(
                    f"freq = {plan.freq!r} n'est pas un alias de fréquence pandas "
                    f"({type(exc).__name__}) — par exemple « h », « D », « 15min »"
                ) from exc
        écarts = (
            cadre.sort_values([plan.colonne_serie, plan.colonne_horodatage])
            .groupby(plan.colonne_serie)[plan.colonne_horodatage]
            .diff()
            .dropna()
        )
        if écarts.empty:
            raise WorkerError(
                "impossible de déduire le pas de temps : chaque série n'a qu'un point. "
                "Une prévision suppose une histoire"
            )
        mode = écarts.mode()
        if mode.empty:
            raise WorkerError("impossible de déduire le pas de temps des horodatages")
        return pd.tseries.frequencies.to_offset(mode.iloc[0])

    def _tronquer(self, cadre: Any, plan: Demande) -> tuple[Any, int]:
        """Les `contexte` derniers pas de chaque série, et ce qui a réellement servi.

        Tronqué ici plutôt que délégué au `context_length` de `predict_df` : le
        tracé et le `contexte_utilise` de la sortie doivent décrire exactement la
        fenêtre que le modèle a vue, et deux endroits qui coupent finissent par
        couper différemment.
        """
        morceaux = []
        vu = 0
        for _, groupe in cadre.groupby(plan.colonne_serie, sort=True):
            queue = groupe.sort_values(plan.colonne_horodatage).tail(plan.contexte)
            vu = max(vu, len(queue))
            morceaux.append(queue)
        return self._pd.concat(morceaux, ignore_index=True), vu

    def _lire_covariables(
        self, source: Any, request: InferRequest, plan: Demande, cadre: Any
    ) -> Any:
        """Le tableau des valeurs connues d'avance, vérifié contre l'historique.

        **Une covariable future n'existe que si elle a un passé**, et c'est le
        premier lancement qui l'a appris : `future_df cannot contain columns not
        present in df`. Le modèle apprend le lien entre la covariable et la cible
        sur la fenêtre de contexte, puis l'applique aux valeurs à venir ; une
        colonne qui n'apparaîtrait que dans le futur ne lui dirait rien. Le
        message d'amont, lui, parle de colonnes en trop, ce qui envoie retirer la
        covariable plutôt que compléter l'historique.
        """
        chemin = resolve_table(source, request.output_dir, "covariables_futures")
        futur = self._lire_csv(chemin, "covariables_futures")
        for colonne in (plan.colonne_serie, plan.colonne_horodatage):
            if colonne not in futur.columns:
                raise WorkerError(
                    f"covariables_futures ({chemin.name}) : colonne `{colonne}` absente — "
                    "le tableau des covariables porte les mêmes colonnes d'identifiant et "
                    "d'horodatage que la série"
                )
        orphelines = [c for c in futur.columns if c not in cadre.columns]
        if orphelines:
            raise WorkerError(
                f"covariables_futures ({chemin.name}) : {', '.join(orphelines)} n'existe(nt) "
                "pas dans la série. Une covariable doit être fournie AUSSI pour le passé, "
                "dans le CSV de la série : c'est sur la fenêtre de contexte que le modèle "
                "apprend son lien avec la valeur à prévoir"
            )
        futur = futur.assign(
            **{plan.colonne_horodatage: self._pd.to_datetime(futur[plan.colonne_horodatage])}
        )
        attendu = plan.horizon * int(futur[plan.colonne_serie].nunique())
        if len(futur) != attendu:
            raise WorkerError(
                f"covariables_futures : {len(futur)} ligne(s) pour un horizon de "
                f"{plan.horizon} pas sur {futur[plan.colonne_serie].nunique()} série(s), "
                f"soit {attendu} attendues — le tableau doit couvrir exactement l'horizon"
            )
        return futur

    def _predire(self, cadre: Any, futur: Any, plan: Demande, offset: Any) -> Any:
        try:
            return self._pipeline.predict_df(
                cadre,
                future_df=futur,
                id_column=plan.colonne_serie,
                timestamp_column=plan.colonne_horodatage,
                target=plan.colonne_valeur,
                prediction_length=plan.horizon,
                # Déjà écrêtés : la bibliothèque ne verra jamais un niveau hors
                # plage, donc n'aura jamais à en rabattre un sous une étiquette
                # qui ment.
                quantile_levels=[float(q) for q in plan.quantiles],
                freq=offset.freqstr,
            )
        except Exception as exc:  # noqa: BLE001 — remonte avec le contexte utile
            raise WorkerError(
                f"prévision impossible : {type(exc).__name__}: {exc}"
            ) from exc

    def _ecartees(self, plan: Demande) -> tuple[str, ...]:
        return (plan.colonne_serie, plan.colonne_horodatage, *COLONNES_ECARTEES)

    def _projeter(self, sortie: Any, colonnes: Sequence[str], plan: Demande) -> Any:
        """Les neuf colonnes de `predict_df` ramenées à celles que le contrat décrit."""
        table = sortie[[plan.colonne_serie, plan.colonne_horodatage, *colonnes]].copy()
        table.columns = [
            plan.colonne_serie,
            plan.colonne_horodatage,
            *[nom_niveau(q) for q in plan.quantiles],
        ]
        for niveau in plan.quantiles:
            table[nom_niveau(niveau)] = table[nom_niveau(niveau)].round(6)
        return table

    def _document(
        self,
        table: Any,
        plan: Demande,
        offset: Any,
        contexte_utilise: int,
        avertissements: list[str],
    ) -> dict[str, Any]:
        """Le même éventail en JSON, avec de quoi savoir d'où viennent ces nombres."""
        séries = []
        for identifiant, groupe in table.groupby(plan.colonne_serie, sort=True):
            séries.append(
                {
                    "id": str(identifiant),
                    "timestamps": [
                        t.isoformat() for t in groupe[plan.colonne_horodatage]
                    ],
                    "quantiles": {
                        nom_niveau(q): [float(v) for v in groupe[nom_niveau(q)]]
                        for q in plan.quantiles
                    },
                }
            )
        return {
            "model": "chronos-2",
            "device": self._device,
            "horizon": plan.horizon,
            "freq": offset.freqstr,
            "context_used": contexte_utilise,
            "quantiles": [float(q) for q in plan.quantiles],
            "columns": {
                "series": plan.colonne_serie,
                "timestamp": plan.colonne_horodatage,
                "value": plan.colonne_valeur,
            },
            "warnings": avertissements,
            "series": séries,
        }

    # --- tracé ---------------------------------------------------------------

    def _tracer(self, histoire: Any, table: Any, plan: Demande, offset: Any, cible: Path) -> int:
        """L'éventail au-dessus de la fin de l'historique. Rend le nombre de séries tracées.

        Le CSV se rend en texte brut dans l'UI ; sur cent soixante-huit lignes de
        sept colonnes, une prévision qui part de travers ne se voit pas. Ce tracé
        est le seul endroit où elle se voit, et c'est pour cela qu'il est activé
        par défaut.

        matplotlib est importé au chargement et non ici : son import coûte près
        d'une seconde, et la payer au premier job la ferait passer pour de la
        latence de prévision — le banc l'attribuerait au cas le plus léger de sa
        charge type, celui qui la mérite le moins.
        """
        plt = self._plt
        identifiants = list(dict.fromkeys(table[plan.colonne_serie]))[:PANNEAUX_MAX]
        figure, axes = plt.subplots(
            len(identifiants), 1, figsize=(11, 3.2 * len(identifiants)), squeeze=False
        )
        bas, haut = nom_niveau(plan.quantiles[0]), nom_niveau(plan.quantiles[-1])
        médiane = self._nom_median(plan)

        for axe, identifiant in zip(axes[:, 0], identifiants, strict=True):
            passé = histoire[histoire[plan.colonne_serie] == identifiant]
            fenêtre = max(2 * plan.horizon, 96)
            passé = passé.tail(fenêtre)
            futur = table[table[plan.colonne_serie] == identifiant]
            axe.plot(
                passé[plan.colonne_horodatage],
                passé[plan.colonne_valeur],
                linewidth=0.9,
                color="#3b3b3b",
                label=f"observé ({len(passé)} derniers pas)",
            )
            if bas != haut:
                axe.fill_between(
                    futur[plan.colonne_horodatage],
                    futur[bas],
                    futur[haut],
                    alpha=0.25,
                    color="#1f77b4",
                    linewidth=0,
                    label=f"quantiles {bas} – {haut}",
                )
            axe.plot(
                futur[plan.colonne_horodatage],
                futur[médiane],
                linewidth=1.2,
                color="#1f77b4",
                label=f"quantile {médiane}",
            )
            axe.set_title(f"{identifiant} — {plan.horizon} pas de « {offset.freqstr} »")
            axe.legend(loc="upper left", fontsize=8)
            axe.grid(alpha=0.2)

        figure.tight_layout()
        figure.savefig(cible, dpi=110)
        plt.close(figure)
        return len(identifiants)

    def _nom_median(self, plan: Demande) -> str:
        """Le niveau le plus proche de 0,5 : c'est lui qu'on trace en trait plein.

        Prendre 0,5 sans vérifier tracerait une courbe absente quand l'utilisateur
        n'a demandé que des bornes — un éventail sans centre est un cas légitime.
        """
        return nom_niveau(min(plan.quantiles, key=lambda q: abs(q - 0.5)))

    # --- mémoire et versions -------------------------------------------------

    def peak_memory_bytes(self) -> int | None:
        """Le RSS, et sur MPS le maximum des relevés du pilote.

        Sur CPU le RSS dit vrai : rien n'est alloué sur Metal. Sur MPS il ne
        compte pas la mémoire du pilote, et `driver_allocated_memory` redescend
        aussi vite qu'il monte — le maximum se tient à chaque relevé plutôt que
        se lit une fois à la fin.
        """
        rss = peak_rss_bytes() or 0
        if self._device != "mps" or self._torch is None:
            return rss or None
        try:
            self._peak_driver = max(
                self._peak_driver, int(self._torch.mps.driver_allocated_memory())
            )
        except (AttributeError, RuntimeError):
            pass
        return max(rss, self._peak_driver) or None

    def unload(self) -> None:
        self._pipeline = None
        gc.collect()
        if self._torch is not None and self._device == "mps":
            try:
                self._torch.mps.empty_cache()
            except (AttributeError, RuntimeError):
                pass

    def _versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}
        for nom, module in (
            ("torch", "torch"),
            ("chronos-forecasting", "chronos"),
            ("pandas", "pandas"),
        ):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


if __name__ == "__main__":
    raise SystemExit(main(ChronosForecastWorker))
