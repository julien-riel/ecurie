"""Les commandes d'exécution vues du terminal.

On éprouve ici le câblage et ce que l'utilisateur lit : un refus doit nommer ce
qui existe, un chargement doit annoncer ce qu'il décharge, un job réussi doit
donner le chemin de son manifeste. Le superviseur lui-même est éprouvé ailleurs ;
seule sa fabrique de workers est remplacée, pour ne pas exiger un venv de runtime.
"""

import json

import pytest
from ecurie_core.cli import app
from ecurie_core.config import render_config
from ecurie_runtime import cli as runtime_cli
from ecurie_runtime.supervisor import Supervisor
from ecurie_runtime.worker import Timeouts
from typer.testing import CliRunner

GIB = 1 << 30
runner = CliRunner()


@pytest.fixture
def cli(parc, ecurie_home, config, fake_spec_factory, monkeypatch):
    """Une CLI branchée sur un parc synthétique et des workers d'essai."""
    (ecurie_home / "config.toml").write_text(render_config(config))
    monkeypatch.chdir(parc.root)

    def build(root, registry, cfg):
        return Supervisor(
            root,
            registry,
            cfg,
            home=ecurie_home,
            timeouts=Timeouts(load_s=30, infer_s=30, ping_s=5, grace_s=2),
            spec_factory=fake_spec_factory({}),
        )

    monkeypatch.setattr(runtime_cli, "_supervisor", build)

    def invoke(*args: str):
        return runner.invoke(app, list(args))

    return invoke


def test_ps_annonce_le_budget_et_sa_provenance(parc, cli):
    parc.capability().model()
    résultat = cli("ps")
    assert résultat.exit_code == 0
    assert "Budget mémoire unifiée" in résultat.stdout
    assert "config" in résultat.stdout  # la provenance est dite, pas seulement le chiffre
    assert "aucun modèle résident" in résultat.stdout


def test_un_job_complet_puis_ps_puis_unload(parc, cli):
    parc.capability().model()
    lancement = cli("run", "tts-test", "-p", "text=Bonjour le parc.", "--seed", "3")
    assert lancement.exit_code == 0, lancement.stdout
    assert "manifest.json" in lancement.stdout

    résidents = cli("ps", "--json")
    payload = json.loads(résidents.stdout)
    assert [e["ref"] for e in payload["residents"]] == ["tts-test@essai"]
    assert payload["used_bytes"] == 2 * GIB
    assert payload["free_bytes"] == payload["budget_bytes"] - payload["used_bytes"]

    déchargement = cli("unload", "tts-test@essai")
    assert déchargement.exit_code == 0
    assert "déchargé" in déchargement.stdout
    assert json.loads(cli("ps", "--json").stdout)["residents"] == []


def test_run_json_donne_un_manifeste_rejouable(parc, cli):
    parc.capability().model()
    résultat = cli("run", "tts-test", "-p", "text=essai", "--json")
    try:
        assert résultat.exit_code == 0, résultat.stdout
        manifeste = json.loads(résultat.stdout)
        assert manifeste["ok"] and manifeste["input"]["text"] == "essai"
        assert manifeste["revision"] is None  # source locale : pas de révision à épingler
        assert manifeste["input_hash"]
    finally:
        cli("unload", "--all", "--force")


def test_un_parametre_inconnu_est_refuse_avec_la_liste_des_bons(parc, cli):
    parc.capability().model()
    résultat = cli("run", "tts-test", "-p", "vitesse=2")
    assert résultat.exit_code == 1
    assert "vitesse" in résultat.stdout and "speed" in résultat.stdout
    assert json.loads(cli("ps", "--json").stdout)["residents"] == []


def test_ps_simule_l_admission_sans_rien_charger(parc, cli):
    parc.capability().model("leger", peak_bytes=1 * GIB)
    parc.model("lourd", peak_bytes=7 * GIB)
    cli("run", "leger", "-p", "text=x")
    try:
        simulation = cli("ps", "--for", "lourd")
        assert "passerait" in simulation.stdout
        assert "leger@essai" in simulation.stdout, "il faut dire ce qui sera déchargé"
        # La simulation ne touche à rien : le résident est toujours là.
        assert [e["ref"] for e in json.loads(cli("ps", "--json").stdout)["residents"]] == [
            "leger@essai"
        ]
    finally:
        cli("unload", "--all", "--force")


def test_run_annonce_ce_qu_il_decharge(parc, cli):
    parc.capability().model("lourd-a", peak_bytes=7 * GIB)
    parc.model("lourd-b", peak_bytes=7 * GIB)
    cli("run", "lourd-a", "-p", "text=x")
    try:
        second = cli("run", "lourd-b", "-p", "text=y")
        assert second.exit_code == 0, second.stdout
        assert "déchargé pour faire de la place" in second.stdout
        assert "lourd-a@essai" in second.stdout
    finally:
        cli("unload", "--all", "--force")


def test_un_variant_sans_profil_renvoie_vers_le_banc_d_essai(parc, cli):
    parc.capability().model("sans-profil", peak_bytes=None)
    résultat = cli("run", "sans-profil", "-p", "text=x")
    assert résultat.exit_code == 1
    assert "ecurie bench" in résultat.stdout


def test_bench_json_n_emet_que_du_json(parc, cli):
    """Une sortie machine ne se lit qu'entière : un préambule la rend inexploitable."""
    parc.capability().model("a-mesurer", peak_bytes=None)
    résultat = cli("bench", "a-mesurer", "--json", "--no-write")
    assert résultat.exit_code == 0, résultat.stdout
    document = json.loads(résultat.stdout)  # échoue si quoi que ce soit d'autre est écrit
    assert document["ref"] == "a-mesurer@essai"
    assert document["profile"]["peak_unified_memory_bytes"] > 0


def test_run_json_n_emet_que_du_json(parc, cli):
    parc.capability().model()
    résultat = cli("run", "tts-test", "-p", "text=x", "--json")
    try:
        assert résultat.exit_code == 0, résultat.stdout
        assert json.loads(résultat.stdout)["ok"] is True
    finally:
        cli("unload", "--all", "--force")


def test_bench_ecrit_la_mesure_et_affiche_le_patch(parc, cli):
    parc.capability().model("a-mesurer", peak_bytes=None)
    résultat = cli("bench", "a-mesurer")
    assert résultat.exit_code == 0, résultat.stdout
    assert "décharge tout le parc" in résultat.stdout
    assert "profile:" in résultat.stdout
    mesure = parc.root / "registry" / "measurements" / "a-mesurer@essai.json"
    assert mesure.is_file()
    assert json.loads(mesure.read_text())["profile"]["peak_unified_memory_bytes"] > 0


def test_env_list_dit_comment_reparer(parc, cli):
    parc.capability().model()
    (parc.root / "runtimes" / "mlx-audio").mkdir(parents=True)
    (parc.root / "runtimes" / "mlx-audio" / "pyproject.toml").write_text("[project]\n")
    résultat = cli("env", "list")
    assert "mlx-audio" in résultat.stdout
    assert "ecurie env sync mlx-audio" in résultat.stdout


def test_un_registre_en_erreur_bloque_l_execution(parc, cli):
    """Exécuter sur un registre invalide, c'est exécuter on ne sait pas quoi."""
    parc.capability().model()
    (parc.root / "registry" / "models" / "casse.yaml").write_text("id: 'X MAJUSCULE'\n")
    résultat = cli("run", "tts-test", "-p", "text=x")
    assert résultat.exit_code == 1
    # rich replie les lignes selon la largeur du terminal : on compare le texte
    # recollé, sinon le test dépend de la taille de la fenêtre où il tourne.
    assert "ecurie registry validate" in " ".join(résultat.stdout.split())
