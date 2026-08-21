"""Le socle du service : index, santé, origines autorisées, imprévus.

Une propriété est testée ici plutôt que dans les routes parce qu'elle vaut pour
toutes : **un registre en erreur ne fait pas taire le serveur.** La CLI, elle,
s'arrête — c'est le bon geste avant d'exécuter quelque chose. Une UI à qui on ne
rend rien parce qu'un manifeste sur six a une révision non épinglée n'a aucun
moyen d'apprendre lequel, et l'utilisateur se retrouve devant un écran vide.
"""

from ecurie_api.app import create_app
from fastapi.testclient import TestClient


def test_l_index_dit_sur_quel_depot_on_sert(client):
    """Un `ecurie serve` lancé du mauvais dossier rend un registre vide : il faut
    pouvoir s'en apercevoir sans deviner."""
    corps = client.get("/").json()

    assert corps["service"] == "ecurie"
    assert corps["root"].endswith("depot")
    assert corps["models"] == 1
    assert corps["capabilities"] == 1
    assert corps["registry_errors"] == 0
    assert corps["docs"] == "/docs"


def test_healthz_repond_sans_toucher_au_registre(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_le_schema_openapi_est_servi(client):
    """L'UI programme contre ce schéma : s'il ne se génère pas, rien ne se génère."""
    schéma = client.get("/openapi.json").json()

    assert schéma["info"]["title"] == "Écurie"
    for chemin in (
        "/registry/capabilities",
        "/registry/models",
        "/store/summary",
        "/runtime/residents",
        "/runtime/admission",
    ):
        assert chemin in schéma["paths"], chemin


def test_les_origines_locales_de_vite_sont_autorisees(client):
    """Sans cet en-tête, l'UI de développement ne peut pas appeler l'API du tout."""
    réponse = client.get("/healthz", headers={"Origin": "http://localhost:5173"})

    assert réponse.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_une_origine_inconnue_n_est_pas_autorisee(client):
    """`allow_origins=["*"]` laisserait n'importe quelle page ouverte dans le
    navigateur interroger le parc : la boucle locale ne protège pas de cela."""
    réponse = client.get("http://testserver/healthz", headers={"Origin": "https://ailleurs.test"})

    assert réponse.status_code == 200
    assert "access-control-allow-origin" not in réponse.headers


def test_une_origine_supplementaire_s_ajoute_aux_defauts(state_factory, parc):
    app = create_app(state_factory(parc), cors_origins=["http://localhost:4200"])
    client = TestClient(app)

    réponse = client.get("/healthz", headers={"Origin": "http://localhost:4200"})

    assert réponse.headers["access-control-allow-origin"] == "http://localhost:4200"


def test_un_registre_en_erreur_est_servi_quand_meme(client_factory, depot):
    """Une révision flottante sur un modèle actif est une erreur du registre.

    La CLI refuse d'exécuter, et elle a raison. L'API, elle, sert les manifestes
    valides et transporte l'erreur : c'est la seule façon pour l'UI de dire
    *lequel* est cassé.
    """
    depot.capability("text-to-speech").env("mlx-audio").model("bon")
    (depot.root / "registry" / "models" / "casse.yaml").write_text(
        "id: casse\n"
        "capability: text-to-speech\n"
        "license: apache-2.0\n"
        "status: active\n"
        "variants:\n"
        "  - id: v1\n"
        "    runtime: mlx-audio\n"
        "    source:\n"
        "      kind: huggingface\n"
        "      repo: essai/casse\n"
        "      revision: '0000000'\n"
    )
    client = client_factory(depot)

    corps = client.get("/registry/models").json()

    assert [m["id"] for m in corps["models"]] == ["bon", "casse"]
    erreurs = [i for i in corps["issues"] if i["severity"] == "error"]
    assert any("révision non épinglée" in i["message"] for i in erreurs)
    assert client.get("/").json()["registry_errors"] >= 1


def test_un_impreu_se_dit_au_lieu_de_disparaitre(state_factory, parc, monkeypatch):
    """« Internal Server Error » envoie lire le journal du serveur pour rien."""
    from ecurie_api.state import AppState

    def casse(self):
        raise RuntimeError("le disque a disparu")

    monkeypatch.setattr(AppState, "registry", casse)
    client = TestClient(create_app(state_factory(parc)), raise_server_exceptions=False)

    réponse = client.get("/registry/models")

    assert réponse.status_code == 500
    assert réponse.json() == {
        "detail": "RuntimeError : le disque a disparu",
        "path": "/registry/models",
    }
