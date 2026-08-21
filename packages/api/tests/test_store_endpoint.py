"""`/store/summary` — les trois chiffres, sans jamais scanner.

Les chiffres attendus sont exacts, comme dans les tests de `ecurie_store` : c'est
la seule façon de savoir qu'un jour où ils changeront, c'est le calcul qui aura
changé et pas la fixture. Le parc d'essai tient en deux fichiers, et la réponse
doit dire :

    apparent 2000  ·  réel unique 1000  ·  récupérable 1000 (duplication)

parce que les deux gestionnaires détiennent le même contenu sur le même volume.
"""

from ecurie_store.db import LocationRecord

SHA = "a" * 64


def _parc_duplique() -> list[LocationRecord]:
    """Le même contenu détenu par deux gestionnaires : le cas de la duplication."""
    return [
        LocationRecord(
            path="/hub/model.safetensors",
            manager="hf",
            size=1000,
            mtime=0.0,
            device=1,
            inode=10,
            link_kind="plain",
            sha256=SHA,
            variant_ref="tts-test@essai",
            meta={"nlink": 1},
        ),
        LocationRecord(
            path="/ollama/blobs/sha256-aaa",
            manager="ollama",
            size=1000,
            mtime=0.0,
            device=1,
            inode=11,
            link_kind="plain",
            sha256=SHA,
            variant_ref=None,
            meta={"nlink": 1},
        ),
    ]


def test_sans_scan_la_reponse_dit_inconnu_et_non_zero(client):
    """Des chiffres à zéro se liraient « le parc est vide ». Un parc jamais
    regardé n'est pas un parc vide — c'est la règle du poste « jamais utilisé »
    du plan de GC, appliquée ici."""
    corps = client.get("/store/summary").json()

    assert corps["scanned"] is False
    assert corps["figures"] is None
    assert corps["last_scan_at"] is None
    assert "ecurie store scan" in corps["hint"]


def test_un_scan_qui_n_a_rien_trouve_n_est_pas_un_scan_absent(client, locations):
    """Zéro fichier après un scan est un chiffre vrai, et il faut le dire comme tel :
    renvoyer « lancer un scan » à qui vient d'en lancer un serait faux."""
    locations([], last_scan_at="2026-08-20T10:00:00+00:00")

    corps = client.get("/store/summary").json()

    assert corps["scanned"] is True
    assert corps["figures"]["apparent_bytes"] == 0
    assert corps["hint"] is None


def test_les_trois_chiffres_sont_ceux_de_la_cli(client, locations):
    locations(_parc_duplique(), last_scan_at="2026-08-20T10:00:00+00:00")

    figures = client.get("/store/summary").json()["figures"]

    assert figures["apparent_bytes"] == 2000
    assert figures["real_unique_bytes"] == 1000
    assert figures["recoverable"]["duplication_bytes"] == 1000
    assert figures["recoverable"]["total_known_bytes"] == 1000
    assert figures["by_manager"] == {"hf": [1000, 1], "ollama": [1000, 1]}


def test_le_total_recuperable_est_calcule_pour_l_ui(client, locations):
    """`total_known_bytes` est une propriété : elle ne sort pas d'`asdict`. Sans
    elle, l'UI referait l'addition des quatre postes et pourrait annoncer un
    total qui ne serait pas celui de `ecurie store status`."""
    locations(_parc_duplique(), last_scan_at="2026-08-20T10:00:00+00:00")

    recoverable = client.get("/store/summary").json()["figures"]["recoverable"]

    assert "total_known_bytes" in recoverable
    assert recoverable["total_known_bytes"] == sum(
        recoverable[poste]
        for poste in ("duplication_bytes", "hf_stale_bytes", "orphan_bytes")
    )


def test_l_arbre_de_duplication_nomme_les_chemins(client, locations):
    """L'écran Parc en fait une liste dépliable : sans les chemins, il ne reste
    qu'un nombre sur lequel on ne peut rien décider."""
    locations(_parc_duplique(), last_scan_at="2026-08-20T10:00:00+00:00")

    duplicates = client.get("/store/summary").json()["figures"]["duplicates"]

    assert len(duplicates) == 1
    assert duplicates[0]["sha256"] == SHA
    assert duplicates[0]["reclaimable_bytes"] == 1000
    assert duplicates[0]["paths"] == ["/hub/model.safetensors", "/ollama/blobs/sha256-aaa"]


def test_les_fichiers_hors_registre_sont_comptes_a_part(client, locations):
    locations(_parc_duplique(), last_scan_at="2026-08-20T10:00:00+00:00")

    figures = client.get("/store/summary").json()["figures"]

    assert figures["unresolved_count"] == 1
    assert figures["unresolved_bytes"] == 1000


def test_le_poste_jamais_utilise_reste_inconnu_sans_telemetrie(client, locations):
    locations(_parc_duplique(), last_scan_at="2026-08-20T10:00:00+00:00")

    corps = client.get("/store/summary").json()

    assert corps["telemetry"] == {
        "conclusive": False,
        "first_run_at": None,
        "unused_after_days": 90,
    }
    assert corps["figures"]["recoverable"]["unused_known"] is False


def test_le_seuil_du_poste_jamais_utilise_se_regle(client, locations):
    locations(_parc_duplique(), last_scan_at="2026-08-20T10:00:00+00:00")

    corps = client.get("/store/summary", params={"unused_after_days": 30}).json()

    assert corps["telemetry"]["unused_after_days"] == 30


def test_un_plan_applique_depuis_le_scan_perime_les_chiffres(client, locations):
    """Ces octets ont déjà bougé : les présenter avec assurance ferait planifier
    une récupération sur un disque qui n'existe plus."""
    locations(
        _parc_duplique(),
        last_scan_at="2026-08-20T10:00:00+00:00",
        last_apply_at="2026-08-20T11:00:00+00:00",
    )

    corps = client.get("/store/summary").json()

    assert corps["stale"] is True
    assert "ecurie store scan" in corps["hint"]


def test_lire_le_resume_ne_declenche_aucun_scan(client, config):
    """La route est un `GET` : elle ne doit pas écrire dans la base d'état, ni
    prendre les dizaines de secondes d'un scan pour répondre."""
    from ecurie_store.db import StateDB

    client.get("/store/summary")

    db = StateDB(config.state_db)
    try:
        assert db.get_kv("last_scan_at") is None
        assert db.locations() == []
    finally:
        db.close()
