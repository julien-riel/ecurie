"""Le superviseur vit dans le processus du serveur (tâche 4.6).

Ce qui se joue ici n'est pas une économie d'allocation. Un superviseur reconstruit
à chaque requête ne peut pas savoir qu'un job tourne : l'occupation d'un résident
et le tour de rôle des jobs sur un même worker vivent dans sa mémoire, et rien
d'autre ne peut les porter — un pid dans `residents.json` ne distingue pas deux
jobs du même processus, ce qui est précisément la situation d'un serveur.

En contrepartie, il ne doit pas figer ce qui, lui, change : le registre se
recharge à chaud dès qu'un fichier de `registry/` bouge (CONCEPTION.md §6), et un
superviseur qui garderait le sien servirait à l'admission un manifeste que le
rechargement a déjà remplacé.
"""

from ecurie_api.app import create_app
from fastapi.testclient import TestClient


def test_le_superviseur_est_le_meme_d_une_requete_a_l_autre(client):
    premier = client.ecurie.supervisor()

    client.get("/runtime/residents")
    client.post("/runtime/admission", json={"ref": "tts-test", "input": {"text": "bonjour"}})

    assert client.ecurie.supervisor() is premier


def test_le_superviseur_suit_le_rechargement_du_registre(client, depot):
    """Il vit longtemps, il ne fige rien : « ajouter un modèle = ajouter un YAML »."""
    superviseur = client.ecurie.supervisor()
    assert set(superviseur.registry.models) == {"tts-test"}

    depot.model("tts-second")

    assert client.get("/registry/models").status_code == 200
    assert set(superviseur.registry.models) == {"tts-test", "tts-second"}


def test_le_budget_n_est_pas_redetecte_a_chaque_requete(client):
    """Le détecter lance un sous-processus dans le venv d'un runtime : une fois, pas mille."""
    superviseur = client.ecurie.supervisor()
    budget = superviseur.budget

    client.get("/runtime/residents")

    assert client.ecurie.supervisor().budget is budget


def test_l_arret_du_serveur_retire_l_occupation_publiee(state_factory, parc):
    """Les workers survivent au serveur ; l'occupation qu'il publiait, non.

    Un résident détaché est fait pour survivre à qui l'a lancé — c'est ce qui
    évite de repayer le warmup. La ligne qui dit qu'un job tourne dessus, elle,
    n'a plus aucun sens une fois le serveur arrêté : le pid mort finirait par la
    démentir, mais seulement à qui pense à le vérifier.
    """
    state = state_factory(parc)
    with TestClient(create_app(state)) as client:
        client.get("/runtime/residents")
        poignée = state.supervisor()._handle("tts-test@essai")
        poignée.job = "job-1"

    assert poignée.job is None
