"""Adaptateur de séparation de pistes — ce qui se vérifie sans Apple Silicon.

Deux choses se décident hors du modèle, et ce sont elles qui se testent ici : le
passage des quatre sorties du réseau aux deux ou quatre pistes que le contrat
déclare, et le sort de `shifts`, que la bibliothèque MLX n'expose pas.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from ecurie_runtime.envs import WORKER_MODULES, WORKER_MODULES_BY_CAPABILITY, worker_module
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.demucs_separate import (
    ACCOMPAGNEMENT,
    ENV_NAME,
    SOURCES_ATTENDUES,
    DemucsSeparateWorker,
    pistes_du_contrat,
    plan_separation,
)

REPO_ROOT = Path(__file__).parents[3]
CONTRAT = REPO_ROOT / "registry" / "capabilities" / "audio-separation.json"
MANIFESTE = REPO_ROOT / "registry" / "models" / "htdemucs-mlx.yaml"


def plan(**champs):
    return plan_separation(
        entree=champs.pop("entree", {}),
        params=champs.pop("params", {}),
        defaults=champs.pop("defaults", {}),
    )


# --- imports paresseux -------------------------------------------------------


def test_module_importable_sans_mlx():
    code = (
        "import sys, ecurie_runtime.workers.demucs_separate as m;"
        "print(m.DemucsSeparateWorker.name, 'mlx' in sys.modules, 'mlx_audiogen' in sys.modules)"
    )
    résultat = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert résultat.returncode == 0, résultat.stderr
    assert résultat.stdout.split() == ["demucs-separate", "False", "False"]


# --- choix de l'adaptateur et environnement ----------------------------------


def test_la_capacite_choisit_cet_adaptateur():
    """Sans cette entrée, le superviseur lançait le worker TTS du runtime — dans
    un env où le paquet `mlx_audio` n'existe même pas."""
    assert worker_module("mlx-audio", "audio-separation").endswith("demucs_separate")
    assert worker_module("mlx-audio", None) == WORKER_MODULES["mlx-audio"]
    assert ("mlx-audio", "audio-separation") in WORKER_MODULES_BY_CAPABILITY


def test_l_adaptateur_annonce_l_environnement_du_manifeste():
    """L'adaptateur ne tourne pas dans l'env du TTS, et son message de réparation
    doit nommer celui que le variant déclare par `runtime_env`."""
    assert ENV_NAME == "mlx-audiogen"
    assert f"runtime_env: {ENV_NAME}" in MANIFESTE.read_text()


# --- fidélité au contrat -----------------------------------------------------


def test_les_pistes_declarees_par_le_contrat():
    propriétés = json.loads(CONTRAT.read_text())["output"]["properties"]["tracks"]
    assert propriétés["required"] == ["vocals"]
    déclarées = set(propriétés["properties"])
    assert {"vocals", ACCOMPAGNEMENT}.issubset(déclarées)
    assert set(SOURCES_ATTENDUES).issubset(déclarées | {ACCOMPAGNEMENT})


# --- résolution d'un job -----------------------------------------------------


def test_deux_pistes_par_defaut():
    assert plan().stems == 2


def test_le_job_prime_sur_le_variant():
    assert plan(entree={"stems": 4}, defaults={"stems": 2}).stems == 4


def test_un_nombre_de_pistes_hors_contrat_est_refuse():
    with pytest.raises(WorkerError) as échec:
        plan(entree={"stems": 3})
    assert "2 ou 4" in str(échec.value)


def test_une_tranche_negative_est_refusee():
    with pytest.raises(WorkerError):
        plan(entree={"segment_seconds": 0})


def test_shifts_est_signale_et_non_applique():
    """Le contrat le déclare, la bibliothèque ne l'expose pas : l'ignorer en
    silence ferait croire à une passe moyennée qui n'a pas eu lieu."""
    avertissements = plan(entree={"shifts": 5}).warnings
    assert avertissements and "shifts" in avertissements[0]
    assert plan(entree={"shifts": 1}).warnings == ()


# --- passage des sources aux pistes du contrat -------------------------------


def test_quatre_pistes_rendent_les_quatre_sources():
    sources = {"drums": 1, "bass": 2, "other": 4, "vocals": 8}
    assert pistes_du_contrat(sources, 4, sum) == sources


def test_deux_pistes_somment_tout_sauf_la_voix():
    sources = {"drums": 1, "bass": 2, "other": 4, "vocals": 8}
    pistes = pistes_du_contrat(sources, 2, lambda parts: sum(parts[1:], parts[0]))
    assert set(pistes) == {"vocals", ACCOMPAGNEMENT}
    assert pistes["vocals"] == 8
    assert pistes[ACCOMPAGNEMENT] == 7  # 1 + 2 + 4, et pas la voix


def test_l_absence_de_voix_est_une_erreur_parlante():
    with pytest.raises(WorkerError) as échec:
        pistes_du_contrat({"drums": 1}, 2, sum)
    assert "vocals" in str(échec.value)


# --- worker ------------------------------------------------------------------


def test_un_job_sans_fichier_est_refuse_avant_toute_lecture():
    worker = DemucsSeparateWorker()
    worker._np = object()  # le refus doit tomber avant le moindre calcul
    worker._pipeline = object()
    from ecurie_runtime.workers.base import InferRequest

    with pytest.raises(WorkerError) as échec:
        worker.infer(
            InferRequest(job_id="j", input={}, params={}, output_dir=Path(".")), lambda *_: None
        )
    assert "audio" in str(échec.value)
