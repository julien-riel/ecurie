"""Adaptateur mflux/text-to-image — ce qui se vérifie sans Apple Silicon ni mlx.

Comme pour `diffusers_mps`, ces tests tournent dans le venv d'Écurie, sans le
runtime : un import de mlx remonté au niveau du module, une famille inconnue
qui passerait en silence, ou un manifeste qui désigne une famille que
l'adaptateur ne connaît pas se voient ici, pas au premier job.
"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from ecurie_runtime.envs import worker_module
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.mflux_image import (
    ENV_NAME,
    FAMILLES,
    MfluxImageWorker,
    famille_de,
)

REPO_ROOT = Path(__file__).parents[3]
MODELS = REPO_ROOT / "registry" / "models"


def test_module_importable_sans_mlx():
    code = (
        "import sys, ecurie_runtime.workers.mflux_image as m;"
        "print(m.ENV_NAME, 'mlx' in sys.modules, 'mflux' in sys.modules)"
    )
    sortie = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    assert sortie == [ENV_NAME, "False", "False"]


def test_aiguillage_declare():
    assert worker_module("mflux", "text-to-image") == "ecurie_runtime.workers.mflux_image"


def test_famille_inconnue_refusee_avec_les_valeurs_admises():
    with pytest.raises(WorkerError, match="flux2-klein-4b"):
        famille_de({"mflux_model": "sdxl"})
    with pytest.raises(WorkerError):
        famille_de({})


def manifestes_mflux_text_to_image():
    for chemin in sorted(MODELS.glob("*.yaml")):
        doc = yaml.safe_load(chemin.read_text())
        if doc.get("capability") != "text-to-image":
            continue
        for variant in doc.get("variants", []):
            if variant.get("runtime") == "mflux":
                yield f"{doc['id']}@{variant['id']}", variant


@pytest.mark.parametrize(
    ("ref", "variant"),
    list(manifestes_mflux_text_to_image()),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_chaque_manifeste_designe_une_famille_connue(ref, variant):
    """Une faute de frappe dans `mflux_model` ne doit pas attendre le chargement."""
    assert (variant.get("options") or {}).get("mflux_model") in FAMILLES, ref


def test_au_moins_les_trois_familles_au_registre():
    désignées = {
        (v.get("options") or {}).get("mflux_model") for _, v in manifestes_mflux_text_to_image()
    }
    assert {"flux2-klein-4b", "qwen-image", "z-image-turbo"} <= désignées


def test_load_refuse_un_dossier_absent(tmp_path):
    with pytest.raises(WorkerError, match="poids introuvables"):
        MfluxImageWorker().load(
            {"weights_path": str(tmp_path / "absent"), "options": {"mflux_model": "qwen-image"}}
        )
