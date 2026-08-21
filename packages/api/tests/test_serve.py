"""`ecurie serve` — ce que la commande garantit avant d'écouter.

Le test qui compte est celui du refus : l'API dit où sont les poids sur le
disque, ce que la machine a en mémoire, et lancera bientôt des jobs. Publiée sur
un réseau, elle donne tout cela à qui passe. L'écoute non locale doit donc être
un choix explicite, jamais un défaut ni une conséquence d'un `--host` recopié.
"""

import pytest
from ecurie_api import cli as cli_mod
from ecurie_core.cli import app as cli_app
from ecurie_runtime.budget import Budget
from typer.testing import CliRunner

runner = CliRunner()


@pytest.mark.parametrize(
    "host, local",
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("127.0.0.2", True),  # tout le /8 revient à la machine
        ("0.0.0.0", False),
        ("192.168.1.10", False),
        ("::", False),
        ("mon-mac.local", False),  # un nom qu'on ne résout pas est traité comme non local
    ],
)
def test_les_adresses_locales_sont_reconnues(host, local):
    assert cli_mod._is_local(host) is local


def test_serve_est_greffee_sur_la_cli_racine():
    résultat = runner.invoke(cli_app, ["--help"])

    assert résultat.exit_code == 0
    assert "serve" in résultat.output


def test_une_adresse_non_locale_est_refusee_sans_expose(ecurie_home):
    résultat = runner.invoke(cli_app, ["serve", "--host", "0.0.0.0"])

    assert résultat.exit_code == 1
    assert "--expose" in résultat.output


def test_avec_expose_la_commande_va_jusqu_a_ecouter(ecurie_home, monkeypatch):
    """On ne vérifie pas qu'elle sert, seulement qu'elle ne refuse plus : le reste
    demanderait un vrai socket, et c'est le travail des tests d'endpoints."""
    appels = {}
    monkeypatch.setattr(
        cli_mod.uvicorn, "run", lambda app, **kwargs: appels.update(kwargs)
    )
    monkeypatch.setattr(
        "ecurie_api.state.detect_budget", lambda config, repo_root=None: Budget(1 << 34, "essai")
    )

    résultat = runner.invoke(cli_app, ["serve", "--host", "0.0.0.0", "--expose", "--port", "9999"])

    assert résultat.exit_code == 0, résultat.output
    assert appels["host"] == "0.0.0.0"
    assert appels["port"] == 9999


def test_la_commande_dit_le_depot_le_registre_et_le_budget(ecurie_home, monkeypatch):
    """Un `ecurie serve` lancé du mauvais dossier rendrait un registre vide sans
    que rien ne l'explique."""
    monkeypatch.setattr(cli_mod.uvicorn, "run", lambda app, **kwargs: None)
    monkeypatch.setattr(
        "ecurie_api.state.detect_budget",
        lambda config, repo_root=None: Budget(19_069_665_280, "metal, via le mlx de runtimes/x"),
    )

    résultat = runner.invoke(cli_app, ["serve"])

    assert résultat.exit_code == 0, résultat.output
    assert "Dépôt" in résultat.output
    assert "capacité(s)" in résultat.output
    # Le budget mémoire se lit en Gio, comme le seuil de lourdeur et comme
    # l'UI l'affiche : « 19.07 Go » est un chiffre écrit nulle part ailleurs.
    assert "17.76 Gio (metal" in résultat.output
    assert "/docs" in résultat.output


def test_une_origine_supplementaire_est_annoncee(ecurie_home, monkeypatch):
    monkeypatch.setattr(cli_mod.uvicorn, "run", lambda app, **kwargs: None)
    monkeypatch.setattr(
        "ecurie_api.state.detect_budget", lambda config, repo_root=None: Budget(1 << 34, "essai")
    )

    résultat = runner.invoke(cli_app, ["serve", "--cors-origin", "http://localhost:4200"])

    assert "http://localhost:4200" in résultat.output
