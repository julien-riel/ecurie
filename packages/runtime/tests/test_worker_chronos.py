"""La prévision de série temporelle — ce qui se vérifie sans poids et sans torch.

Une capacité, un runtime, et trois choses qui tiennent dans du code pur : le
câblage (runtime, capacité) → adaptateur, l'écrêtage des quantiles, et la
projection des colonnes de `predict_df` sur celles du contrat. Ce sont
exactement les trois pièces dont dépend l'honnêteté de la sortie — le reste, la
prévision elle-même, demande les poids et vit dans `ecurie bench`.

L'écrêtage mérite qu'on le teste ici plutôt qu'au banc, et pour une raison qui a
coûté cher ailleurs : la bibliothèque d'amont **rabat un quantile hors plage tout
en nommant sa colonne du niveau demandé**. Un banc au vert ne regarde pas ce
qu'un fichier contient ; ces tests-ci regardent.

Ils tournent en CI, sur des machines sans Apple Silicon, sans poids et sans venv
de runtime : rien de ce fichier n'importe torch, pandas ni chronos, pas plus que
l'adaptateur lui-même au niveau de son module.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from ecurie_runtime.envs import NOT_YET, worker_module
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.chronos_forecast import (
    COLONNES_ECARTEES,
    CONTEXTE_MAX,
    HORIZON_MAX,
    QUANTILE_MAX,
    QUANTILE_MIN,
    ChronosForecastWorker,
    colonnes_quantiles,
    ecreter_quantiles,
    nom_niveau,
    plan_prevision,
    resolve_table,
    weights_dir,
)

# --- câblage ----------------------------------------------------------------------


def test_la_capacite_est_servie_par_son_adaptateur():
    assert worker_module("chronos", "time-series-forecast").endswith("chronos_forecast")


def test_le_runtime_chronos_n_a_pas_d_adaptateur_par_defaut():
    """Un runtime est une famille de bibliothèques, pas une promesse d'API commune.

    Sans capacité il n'y a rien à servir, et le message nomme celle qui existe
    plutôt que de laisser tomber sur un « adaptateur non livré » qui
    n'apprendrait rien à qui se trompe de contrat.
    """
    assert worker_module("chronos", None) is None
    assert "time-series-forecast" in NOT_YET["chronos"]


# --- écrêtage des quantiles -------------------------------------------------------


def test_un_niveau_hors_plage_est_ecrete_et_le_dit():
    """LE piège de cette famille, et la raison d'être de cette fonction.

    Demander 0,001 ne lève rien chez l'amont : la colonne rendue s'appelle
    « 0.001 » et contient, au bit près, la valeur de 0,01. Écrêter ici est ce qui
    permet au CSV de nommer ses colonnes d'après ce qui a réellement été calculé.
    """
    niveaux, avertissements = ecreter_quantiles([0.001, 0.5, 0.999])

    assert niveaux == (QUANTILE_MIN, 0.5, QUANTILE_MAX)
    assert len(avertissements) == 1
    assert "0.001" in avertissements[0] and "0.999" in avertissements[0]


def test_deux_niveaux_rabattus_sur_la_meme_borne_ne_font_qu_une_colonne():
    """Sans cette déduplication, le CSV porterait deux colonnes identiques sous
    deux noms différents — ce qui a toutes les apparences d'un éventail et n'en
    est pas un."""
    niveaux, avertissements = ecreter_quantiles([0.001, 0.005, 0.5])

    assert niveaux == (QUANTILE_MIN, 0.5)
    assert any("double" in message for message in avertissements)


def test_les_niveaux_sont_tries_meme_demandes_en_desordre():
    """Le tracé remplit entre le premier et le dernier niveau : non triés,
    l'éventail se replierait sur lui-même."""
    niveaux, avertissements = ecreter_quantiles([0.9, 0.1, 0.5])

    assert niveaux == (0.1, 0.5, 0.9)
    assert avertissements == []


def test_des_niveaux_dans_la_plage_ne_produisent_aucun_avertissement():
    niveaux, avertissements = ecreter_quantiles([0.1, 0.25, 0.5, 0.75, 0.9])

    assert niveaux == (0.1, 0.25, 0.5, 0.75, 0.9)
    assert avertissements == []


def test_une_liste_vide_est_refusee():
    with pytest.raises(WorkerError, match="au moins un niveau"):
        ecreter_quantiles([])


def test_un_niveau_qui_n_est_pas_un_nombre_est_refuse_en_le_citant():
    with pytest.raises(WorkerError, match="médiane"):
        ecreter_quantiles(["médiane"])


def test_les_noms_de_colonnes_ne_trainent_pas_de_zeros():
    """« 0.1 » et non « 0.100000 » : c'est le nom que porte la colonne du CSV, et
    c'est celui que la bibliothèque emploie aussi, ce qui permet de l'apparier
    par nom plutôt que par position."""
    assert nom_niveau(0.1) == "0.1"
    assert nom_niveau(0.25) == "0.25"
    assert nom_niveau(0.01) == "0.01"


# --- projection des colonnes ------------------------------------------------------


COLONNES_PREDICT_DF = [
    "item_id",
    "timestamp",
    "target_name",
    "predictions",
    "0.1",
    "0.5",
    "0.9",
]

IGNOREES = ("item_id", "timestamp", *COLONNES_ECARTEES)


def test_les_deux_colonnes_en_trop_sont_ecartees_nommement():
    """`predict_df` rend neuf colonnes pour cinq niveaux : `target_name` parce
    que la bibliothèque accepte plusieurs cibles, et `predictions` qui est le
    doublon exact de la médiane. Un `to_csv` direct livrerait un fichier que le
    contrat ne décrit pas."""
    retenues = colonnes_quantiles(COLONNES_PREDICT_DF, (0.1, 0.5, 0.9), IGNOREES)

    assert retenues == ["0.1", "0.5", "0.9"]
    assert set(COLONNES_ECARTEES) == {"target_name", "predictions"}


def test_des_colonnes_nommees_autrement_sont_reprises_par_position():
    """L'appariement par nom est un confort, pas une dépendance : si l'amont
    changeait sa façon de nommer, l'ordre des niveaux demandés reste la seule
    chose qu'il promet."""
    colonnes = ["item_id", "timestamp", "target_name", "predictions", "q10", "q50", "q90"]

    assert colonnes_quantiles(colonnes, (0.1, 0.5, 0.9), IGNOREES) == ["q10", "q50", "q90"]


def test_un_compte_de_colonnes_inattendu_refuse_le_job():
    """Refuser plutôt que livrer : un CSV dont on ne sait plus ce que disent les
    colonnes est pire qu'un job en échec."""
    colonnes = ["item_id", "timestamp", "target_name", "predictions", "0.1", "0.9"]

    with pytest.raises(WorkerError, match="a changé"):
        colonnes_quantiles(colonnes, (0.1, 0.5, 0.9), IGNOREES)


# --- résolution de la demande -----------------------------------------------------


def test_l_entree_du_job_prime_sur_les_defauts_du_manifeste():
    plan = plan_prevision(
        entree={"horizon": 48},
        params={},
        defaults={"horizon": 24, "contexte": 512},
    )

    assert plan.horizon == 48
    assert plan.contexte == 512


def test_les_noms_de_colonnes_ont_les_defauts_de_predict_df():
    plan = plan_prevision(entree={}, params={}, defaults={})

    assert (plan.colonne_serie, plan.colonne_horodatage, plan.colonne_valeur) == (
        "item_id",
        "timestamp",
        "target",
    )
    assert plan.quantiles == (0.1, 0.25, 0.5, 0.75, 0.9)
    assert plan.graphique is True


def test_un_horizon_au_dela_du_passage_unique_est_refuse():
    """1024 pas, c'est 64 tuiles de 16 : au-delà le modèle déroule, et la falaise
    est réelle — 1024 pas coûtent 47 ms, 1025 en coûtent 215."""
    with pytest.raises(WorkerError, match=str(HORIZON_MAX)):
        plan_prevision(entree={"horizon": HORIZON_MAX + 1}, params={}, defaults={})


def test_un_contexte_au_dela_de_la_fenetre_du_modele_est_refuse():
    with pytest.raises(WorkerError, match=str(CONTEXTE_MAX)):
        plan_prevision(entree={"contexte": CONTEXTE_MAX + 1}, params={}, defaults={})


def test_un_horizon_qui_n_est_pas_un_entier_nomme_le_parametre():
    with pytest.raises(WorkerError, match="horizon"):
        plan_prevision(entree={"horizon": "deux jours"}, params={}, defaults={})


def test_l_ecretage_remonte_dans_les_avertissements_de_la_demande():
    plan = plan_prevision(entree={"quantiles": [0.001, 0.5]}, params={}, defaults={})

    assert plan.quantiles == (QUANTILE_MIN, 0.5)
    assert plan.warnings and "écrêt" in plan.warnings[0]


# --- poids et entrées -------------------------------------------------------------


def test_un_chemin_de_poids_absent_est_refuse_avec_ce_qui_repare():
    with pytest.raises(WorkerError, match="superviseur"):
        weights_dir({})


def test_un_dossier_de_poids_inexistant_dit_qu_un_worker_ne_telecharge_pas(tmp_path: Path):
    with pytest.raises(WorkerError) as échec:
        weights_dir({"weights_path": str(tmp_path / "absent")})

    assert "ne télécharge jamais" in str(échec.value)


def test_un_csv_relatif_se_resout_dans_le_dossier_du_job(tmp_path: Path):
    """Le superviseur copie l'entrée dans le dossier du job et transmet un chemin
    relatif : c'est ce qui rend un job rejouable ailleurs."""
    (tmp_path / "inputs").mkdir()
    fichier = tmp_path / "inputs" / "conso.csv"
    fichier.write_bytes(b"")

    assert resolve_table("inputs/conso.csv", tmp_path, "serie") == fichier


def test_un_champ_de_tableau_vide_nomme_le_champ_fautif():
    with pytest.raises(WorkerError, match="covariables_futures"):
        resolve_table("", Path("."), "covariables_futures")


def test_un_format_non_gere_liste_ceux_qui_le_sont(tmp_path: Path):
    fichier = tmp_path / "conso.parquet"
    fichier.write_bytes(b"")

    with pytest.raises(WorkerError) as échec:
        resolve_table(str(fichier), tmp_path, "serie")

    assert ".csv" in str(échec.value)


# --- choix du périphérique --------------------------------------------------------


def _torch_feint(*, mps: bool) -> SimpleNamespace:
    return SimpleNamespace(backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps)))


def test_le_processeur_est_le_defaut_et_ce_n_est_pas_un_repli():
    """MESURÉ : au contexte 8192, le CPU rend 139 ms là où MPS en met 190. Le
    modèle est trop petit pour que le transfert vers Metal se rembourse."""
    worker = ChronosForecastWorker()

    assert worker._choisir_device(_torch_feint(mps=True)) == "cpu"


def test_le_variant_mps_est_refuse_quand_metal_est_absent():
    """Refusé plutôt que replié : un variant nommé `mps` existe pour être comparé
    au variant CPU, et retomber en silence sur le processeur ferait mesurer deux
    fois la même chose sous deux noms."""
    worker = ChronosForecastWorker()
    worker._options = {"device": "mps"}

    with pytest.raises(WorkerError, match="MPS indisponible"):
        worker._choisir_device(_torch_feint(mps=False))


def test_un_peripherique_inconnu_liste_ceux_qui_existent():
    worker = ChronosForecastWorker()
    worker._options = {"device": "cuda"}

    with pytest.raises(WorkerError) as échec:
        worker._choisir_device(_torch_feint(mps=True))

    assert "cpu" in str(échec.value) and "mps" in str(échec.value)
