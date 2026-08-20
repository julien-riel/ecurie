from pathlib import Path

from ecurie_core import config as config_mod
from ecurie_core.config import Config, ScanConfig, load_config, render_config


def test_premier_lancement_genere_la_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ECURIE_HOME", str(tmp_path / "home"))
    hf = tmp_path / "hub"
    hf.mkdir()
    monkeypatch.setattr(
        config_mod,
        "_AUTODETECT",
        {"hf_hub": hf, "ollama": tmp_path / "absent", "lmstudio": tmp_path / "absent2"},
    )

    config = load_config()
    assert config.scan.hf_hub == hf
    assert config.scan.ollama is None
    assert (tmp_path / "home" / "config.toml").is_file()

    # Relecture : le fichier généré se recharge à l'identique.
    assert load_config() == config


def test_rendu_toml_complet_est_relisible(tmp_path, monkeypatch):
    monkeypatch.setenv("ECURIE_HOME", str(tmp_path))
    config = Config(
        memory_budget=17_000_000_000,
        scan=ScanConfig(
            hf_hub=Path("/tmp/hub"),
            comfy=[Path("/opt/comfy")],
            declared=[Path("/Volumes/Parc/modeles")],
        ),
    )
    (tmp_path / "config.toml").write_text(render_config(config))
    assert load_config() == config
    assert config.state_db == tmp_path / "state.db"
