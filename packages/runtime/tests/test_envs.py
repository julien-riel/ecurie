"""Environnements isolés des runtimes (CONCEPTION.md §5.3).

Les arborescences `runtimes/<env>/` sont fabriquées à la main : un `pyproject.toml`
et un faux `.venv/bin/python` suffisent à décrire les trois états qu'un env peut
prendre. Rien n'est exécuté — ni `uv sync`, qui demande le réseau, ni le python du
venv, qui n'est ici qu'un fichier.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from ecurie_core.models import Variant
from ecurie_runtime import envs
from ecurie_runtime.envs import (
    NOT_YET,
    EnvError,
    RuntimeEnv,
    check_envs,
    env_for,
    list_envs,
    runtime_src_dir,
    spec_for_variant,
    sync_command,
    sync_env,
)


def _declare(repo_root: Path, nom: str, *, venv: bool = True) -> Path:
    """Un env sous `runtimes/<nom>/`, synchronisé ou non."""
    dossier = repo_root / "runtimes" / nom
    dossier.mkdir(parents=True)
    (dossier / "pyproject.toml").write_text(
        f'[project]\nname = "ecurie-runtime-{nom}"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    if venv:
        binaires = dossier / ".venv" / "bin"
        binaires.mkdir(parents=True)
        (binaires / "python").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    return dossier


def _variant(runtime: str = "mlx-audio", **champs) -> Variant:
    return Variant.model_validate(
        {
            "id": "essai",
            "runtime": runtime,
            "source": {"kind": "local", "path": "/poids/modele"},
            **champs,
        }
    )


def _python(repo_root: Path, nom: str) -> str:
    return str(repo_root / "runtimes" / nom / ".venv" / "bin" / "python")


# --- les trois états d'un env -------------------------------------------------


def test_un_env_non_declare_se_repare_en_ecrivant_son_pyproject(tmp_path):
    env = env_for(tmp_path, "mlx-audio")
    assert env.declared is False
    assert env.synced is False
    assert env.status == "non déclaré"
    assert env.repair_hint == "créer runtimes/mlx-audio/pyproject.toml (voir CONCEPTION.md §5.3)"


def test_un_env_declare_sans_venv_est_a_synchroniser(tmp_path):
    _declare(tmp_path, "mlx-audio", venv=False)
    env = env_for(tmp_path, "mlx-audio")
    assert env.declared is True
    assert env.synced is False
    assert env.status == "à synchroniser"
    assert env.repair_hint == "ecurie env sync mlx-audio"


def test_un_env_dont_le_venv_existe_est_pret(tmp_path):
    dossier = _declare(tmp_path, "mlx-audio")
    env = env_for(tmp_path, "mlx-audio")
    assert env.status == "prêt"
    assert env.python == dossier / ".venv" / "bin" / "python"
    assert env.repair_hint == "ecurie env sync mlx-audio"


def test_env_for_situe_l_env_sous_runtimes(tmp_path):
    env = env_for(tmp_path, "diffusers-mps")
    assert env == RuntimeEnv(name="diffusers-mps", root=tmp_path / "runtimes" / "diffusers-mps")
    assert env.pyproject == tmp_path / "runtimes" / "diffusers-mps" / "pyproject.toml"


# --- synchronisation ----------------------------------------------------------


def test_sync_command_cible_le_projet_de_l_env(tmp_path):
    env = env_for(tmp_path, "diffusers-mps")
    dossier = str(tmp_path / "runtimes" / "diffusers-mps")

    assert sync_command(env) == ["uv", "sync", "--project", dossier]
    assert sync_command(env, uv_bin="/opt/homebrew/bin/uv") == [
        "/opt/homebrew/bin/uv",
        "sync",
        "--project",
        dossier,
    ]


def test_sync_env_refuse_un_env_non_declare_sans_rien_executer(tmp_path, monkeypatch):
    """Sans pyproject, `uv sync` créerait un projet vide et le rapporterait comme un
    succès : l'env resterait faux, et la panne ressortirait au premier worker."""

    def interdit(*args, **kwargs):
        raise AssertionError("uv a été lancé alors que l'env n'est pas déclaré")

    monkeypatch.setattr(envs, "subprocess", SimpleNamespace(run=interdit))

    with pytest.raises(EnvError) as exc:
        sync_env(env_for(tmp_path, "mlx-audio"), repo_root=tmp_path)
    assert "runtimes/mlx-audio/pyproject.toml absent" in str(exc.value)
    assert "créer runtimes/mlx-audio/pyproject.toml" in str(exc.value)


# --- de quoi lancer un worker -------------------------------------------------


def test_un_runtime_livre_lance_son_module_avec_le_python_de_l_env(tmp_path):
    _declare(tmp_path, "mlx-audio")
    spec = spec_for_variant(tmp_path, _variant("mlx-audio"), ref="tts-test@essai")

    assert spec.argv == [
        _python(tmp_path, "mlx-audio"),
        "-m",
        "ecurie_runtime.workers.mlx_audio",
    ]
    assert spec.cwd == tmp_path
    assert spec.label == "tts-test@essai"


def test_le_variant_choisit_son_env_par_runtime_env(tmp_path):
    _declare(tmp_path, "diffusers-mps")
    _declare(tmp_path, "sdxl-vieux")
    variant = _variant("diffusers-mps", runtime_env="sdxl-vieux")

    spec = spec_for_variant(tmp_path, variant, ref="img@sdxl")
    assert spec.argv == [
        _python(tmp_path, "sdxl-vieux"),
        "-m",
        "ecurie_runtime.workers.diffusers_mps",
    ]


def test_un_env_non_declare_refuse_le_lancement_avec_sa_reparation(tmp_path):
    with pytest.raises(EnvError) as exc:
        spec_for_variant(tmp_path, _variant("mlx-audio"), ref="tts-test@essai")
    assert "tts-test@essai : environnement runtimes/mlx-audio/ absent" in str(exc.value)
    assert "créer runtimes/mlx-audio/pyproject.toml" in str(exc.value)


def test_un_env_sans_venv_refuse_le_lancement_avec_sa_reparation(tmp_path):
    """Le refus doit venir avant le lancement : un worker démarré avec l'interpréteur
    d'Écurie échouerait plus tard, plus loin, et pour une raison illisible."""
    _declare(tmp_path, "mlx-audio", venv=False)

    with pytest.raises(EnvError) as exc:
        spec_for_variant(tmp_path, _variant("mlx-audio"), ref="tts-test@essai")
    assert str(exc.value) == (
        "tts-test@essai : venv de runtimes/mlx-audio/ absent — ecurie env sync mlx-audio"
    )


def test_un_runtime_custom_execute_l_entrypoint_du_manifeste(tmp_path):
    dossier = _declare(tmp_path, "hunyuan3d")
    (dossier / "run.py").write_text("# point d'entrée du runtime\n", encoding="utf-8")
    variant = _variant("custom", runtime_env="hunyuan3d", entrypoint="runtimes/hunyuan3d/run.py")

    spec = spec_for_variant(tmp_path, variant, ref="mesh@shape")
    assert spec.argv == [_python(tmp_path, "hunyuan3d"), str(dossier / "run.py")]
    assert spec.label == "mesh@shape"


def test_un_runtime_custom_sans_entrypoint_declare_est_refuse(tmp_path):
    _declare(tmp_path, "custom")

    with pytest.raises(EnvError) as exc:
        spec_for_variant(tmp_path, _variant("custom"), ref="mesh@shape")
    assert str(exc.value) == "mesh@shape : runtime custom sans entrypoint dans le manifeste"


def test_un_entrypoint_absent_du_disque_est_refuse_avant_le_lancement(tmp_path):
    _declare(tmp_path, "hunyuan3d")
    variant = _variant("custom", runtime_env="hunyuan3d", entrypoint="runtimes/hunyuan3d/run.py")

    with pytest.raises(EnvError) as exc:
        spec_for_variant(tmp_path, variant, ref="mesh@shape")
    assert str(exc.value) == "mesh@shape : entrypoint introuvable — runtimes/hunyuan3d/run.py"


@pytest.mark.parametrize("runtime", sorted(NOT_YET))
def test_un_runtime_non_livre_au_v03_est_refuse_avec_sa_raison(tmp_path, runtime):
    # L'env est prêt : le refus porte bien sur l'adaptateur manquant, pas sur le venv.
    _declare(tmp_path, runtime)

    with pytest.raises(EnvError) as exc:
        spec_for_variant(tmp_path, _variant(runtime), ref=f"m@{runtime}")
    assert str(exc.value) == f"m@{runtime} : runtime {runtime!r} — {NOT_YET[runtime]}"


# --- l'environnement du worker ------------------------------------------------


def test_le_pythonpath_du_worker_designe_le_dossier_d_ecurie_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    _declare(tmp_path, "mlx-audio")

    spec = spec_for_variant(tmp_path, _variant("mlx-audio"), ref="tts-test@essai")
    assert spec.env_vars["PYTHONPATH"] == str(runtime_src_dir())
    # Le venv du runtime n'a pas Écurie installé : sans ce chemin, le worker ne
    # trouve pas le module que la ligne de commande lui demande de lancer.
    assert (Path(spec.env_vars["PYTHONPATH"]) / "ecurie_runtime" / "__init__.py").is_file()


def test_un_pythonpath_existant_est_conserve_derriere_le_notre(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/un/chemin/a/moi")
    _declare(tmp_path, "mlx-audio")

    spec = spec_for_variant(tmp_path, _variant("mlx-audio"), ref="tts-test@essai")
    assert spec.env_vars["PYTHONPATH"] == f"{runtime_src_dir()}{os.pathsep}/un/chemin/a/moi"


def test_le_worker_est_lance_hors_ligne(tmp_path):
    """`ecurie pull` est le seul chemin vers le réseau, et il est explicite. Sans
    cette barrière, un `from_pretrained` ferait apparaître des gigaoctets dans un
    cache que le scan ne rattache à aucun variant."""
    _declare(tmp_path, "mlx-audio")

    spec = spec_for_variant(tmp_path, _variant("mlx-audio"), ref="tts-test@essai")
    assert spec.env_vars["HF_HUB_OFFLINE"] == "1"
    assert spec.env_vars["PYTHONUNBUFFERED"] == "1"


def test_extra_env_complete_l_environnement_du_worker(tmp_path):
    _declare(tmp_path, "mlx-audio")

    spec = spec_for_variant(
        tmp_path, _variant("mlx-audio"), ref="tts-test@essai", extra_env={"ECURIE_MESURE": "1"}
    )
    assert spec.env_vars["ECURIE_MESURE"] == "1"
    assert spec.env_vars["HF_HUB_OFFLINE"] == "1"


# --- inventaire ---------------------------------------------------------------


def test_list_envs_ignore_les_fichiers_et_les_dossiers_caches(tmp_path):
    _declare(tmp_path, "mlx-audio")
    _declare(tmp_path, "diffusers-mps", venv=False)
    (tmp_path / "runtimes" / "README.md").write_text("les envs isolés\n", encoding="utf-8")
    (tmp_path / "runtimes" / ".DS_Store").write_text("", encoding="utf-8")
    (tmp_path / "runtimes" / ".cache").mkdir()

    assert [e.name for e in list_envs(tmp_path)] == ["diffusers-mps", "mlx-audio"]
    assert [e.status for e in list_envs(tmp_path)] == ["à synchroniser", "prêt"]
    assert list_envs(tmp_path / "depot-sans-runtimes") == []


def test_check_envs_rapporte_un_probleme_par_variant(tmp_path):
    _declare(tmp_path, "mlx-audio")
    _declare(tmp_path, "diffusers-mps", venv=False)
    variants = [
        ("tts-test@essai", _variant("mlx-audio")),
        ("img@sdxl", _variant("diffusers-mps")),
        ("autre-img@sdxl", _variant("diffusers-mps")),
        ("mesh@shape", _variant("custom", runtime_env="hunyuan3d", entrypoint="run.py")),
    ]

    # Deux variants sur le même env défaillant font deux lignes : c'est la liste des
    # variants injouables que `ecurie env` doit montrer, pas celle des envs.
    assert check_envs(tmp_path, variants) == [
        "img@sdxl : ecurie env sync diffusers-mps",
        "autre-img@sdxl : ecurie env sync diffusers-mps",
        "mesh@shape : runtimes/hunyuan3d/pyproject.toml absent",
    ]
