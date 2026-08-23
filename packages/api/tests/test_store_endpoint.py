"""`/store` — les trois chiffres, le plan à blanc, le tiering. Sans jamais scanner.

Les chiffres attendus sont exacts, comme dans les tests de `ecurie_store` : c'est
la seule façon de savoir qu'un jour où ils changeront, c'est le calcul qui aura
changé et pas la fixture. Le parc d'essai tient en deux fichiers, et la réponse
doit dire :

    apparent 2000  ·  réel unique 1000  ·  récupérable 1000 (duplication)

parce que les deux gestionnaires détiennent le même contenu sur le même volume.

Les trois routes partagent une propriété que trois tests gardent séparément :
elles ne **scannent** pas — un `GET` ne prend pas trente secondes — et elles
n'**écrivent** pas. La seconde est ce qui les sépare de la CLI, dont `store plan`
dépose un fichier et `store tier` copie des giga-octets.
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
        recoverable[poste] for poste in ("duplication_bytes", "hf_stale_bytes", "orphan_bytes")
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


# --- le plan de récupération, à blanc ------------------------------------------------


def test_le_plan_sans_scan_ne_propose_rien(client):
    corps = client.get("/store/plan").json()

    assert corps["scanned"] is False
    assert corps["plan"] is None
    assert "ecurie store scan" in corps["hint"]


def test_le_plan_dit_les_actions_et_le_gain_de_la_cli(client, locations):
    """Le gain annoncé par le plan est celui du rapport de `status` : les deux
    passent par `classify`, et c'est cette identité qu'on garde ici."""
    locations(_parc_duplique(), last_scan_at="2026-08-20T10:00:00+00:00")

    corps = client.get("/store/plan").json()
    plan = corps["plan"]

    assert corps["scanned"] is True
    assert plan["total_bytes_reclaimed"] == 1000
    assert plan["by_reason"] == {"duplication": 1000}
    (action,) = plan["actions"]
    assert action["kind"] == "hardlink"
    assert action["keep"] == "/hub/model.safetensors"
    assert action["replace"] == ["/ollama/blobs/sha256-aaa"]
    # Le gain du plan et celui du résumé sont le même octet, compté une fois.
    résumé = client.get("/store/summary").json()["figures"]
    assert résumé["recoverable"]["total_known_bytes"] == plan["total_bytes_reclaimed"]


def test_chaque_action_porte_l_empreinte_a_reverifier(client, locations):
    """Un plan sans empreintes s'appliquerait à l'aveugle sur un disque qui a pu
    bouger depuis le scan. C'est `stats` qui permet à `apply` de refuser."""
    locations(_parc_duplique(), last_scan_at="2026-08-20T10:00:00+00:00")

    (action,) = client.get("/store/plan").json()["plan"]["actions"]

    assert set(action["stats"]) == {"/hub/model.safetensors", "/ollama/blobs/sha256-aaa"}
    assert action["stats"]["/hub/model.safetensors"]["inode"] == 10


def test_le_plan_donne_le_libelle_francais_de_chaque_poste(client, locations):
    """Le front n'entretient pas sa propre table de traduction : celle de la CLI
    voyage avec le plan, et un poste ajouté demain arrive avec son libellé."""
    locations(_parc_duplique(), last_scan_at="2026-08-20T10:00:00+00:00")

    corps = client.get("/store/plan").json()

    assert corps["labels"]["duplication"] == "duplication inter-gestionnaires"
    assert set(corps["plan"]["by_reason"]) <= set(corps["labels"])


def test_verified_only_ecarte_ce_qui_n_a_jamais_ete_relu(client, locations):
    """« Suffisant pour compter, jamais pour effacer » : un hash annoncé par un
    gestionnaire n'est pas une preuve de contenu, et l'option le dit."""
    locations(_parc_duplique(), last_scan_at="2026-08-20T10:00:00+00:00")

    corps = client.get("/store/plan", params={"verified_only": True}).json()

    assert corps["plan"]["actions"] == []
    assert corps["plan"]["ignored"][0]["reason"] == "hash-annonce-non-verifie"
    assert "--verified-only" in corps["command"]


def test_lire_le_plan_n_ecrit_aucun_fichier(client, locations, ecurie_home):
    """`ecurie store plan` dépose un fichier dans `~/.ecurie/plans/`, parce que
    `apply` en exige un. Un `GET` ne fabrique pas de document."""
    locations(_parc_duplique(), last_scan_at="2026-08-20T10:00:00+00:00")

    client.get("/store/plan")

    assert not (ecurie_home / "plans").exists()


# --- le tiering ------------------------------------------------------------------------


def _parc_deporte() -> list[LocationRecord]:
    """Un variant sur le disque, un autre déjà parti sur un volume démonté."""
    return [
        LocationRecord(
            path="/parc/tts/model.safetensors",
            manager="declared",
            size=4000,
            mtime=0.0,
            device=1,
            inode=20,
            link_kind="plain",
            sha256=SHA,
            variant_ref="tts-test@essai",
            meta={"nlink": 1},
        ),
        LocationRecord(
            path="/parc/image/sdxl.safetensors",
            manager="declared",
            size=0,
            mtime=0.0,
            device=1,
            inode=21,
            link_kind="symlink",
            variant_ref="sdxl@fp16",
            meta={"target": "/Volumes/Parc/sdxl.safetensors", "available": False},
        ),
    ]


def test_le_tiering_sans_scan_montre_quand_meme_les_volumes(client):
    """Les volumes viennent de la configuration, pas du disque observé : les
    taire tant qu'on n'a pas scanné cacherait la moitié de ce qui explique
    pourquoi un variant froid est indisponible."""
    corps = client.get("/store/tiering").json()

    assert corps["scanned"] is False
    assert "ecurie store scan" in corps["hint"]


def test_un_variant_deporte_sur_un_volume_absent_est_nomme(client, locations):
    locations(_parc_deporte(), last_scan_at="2026-08-20T10:00:00+00:00")

    corps = client.get("/store/tiering").json()

    (froid,) = corps["cold"]
    assert froid["path"] == "/parc/image/sdxl.safetensors"
    assert froid["target"] == "/Volumes/Parc/sdxl.safetensors"
    assert froid["available"] is False
    assert froid["variant_ref"] == "sdxl@fp16"


def test_les_variants_sont_peses_du_plus_lourd_au_plus_leger(client, locations):
    locations(_parc_deporte(), last_scan_at="2026-08-20T10:00:00+00:00")

    variants = client.get("/store/tiering").json()["variants"]

    assert [v["ref"] for v in variants] == ["tts-test@essai", "sdxl@fp16"]
    assert variants[0]["bytes"] == 4000
    assert variants[0]["freed_bytes"] == 4000
    assert variants[0]["tierable"] is True
    # Celui qui est déjà parti ne pèse plus rien, et ne se redéporte pas.
    assert variants[1]["bytes"] == 0
    assert variants[1]["tiered_links"] == 1
    assert variants[1]["tierable"] is False


def test_un_volume_declare_mais_demonte_a_une_place_libre_inconnue(
    client_factory, parc, config, monkeypatch, tmp_path
):
    """`null`, et non zéro : un volume débranché n'est pas un volume plein."""
    monté = tmp_path / "monte"
    monté.mkdir()
    monkeypatch.setattr(config, "tier_volumes", [monté, tmp_path / "jamais-branche"])

    volumes = client_factory(parc).get("/store/tiering").json()["volumes"]

    assert [v["mounted"] for v in volumes] == [True, False]
    assert volumes[0]["free_bytes"] is not None
    assert volumes[1]["free_bytes"] is None


def test_sans_volume_declare_la_reponse_dit_quoi_faire(client, locations):
    locations(_parc_deporte(), last_scan_at="2026-08-20T10:00:00+00:00")

    corps = client.get("/store/tiering").json()

    assert corps["volumes"] == []
    assert "tier_volumes" in corps["hint"]


def test_aucune_phrase_rendue_ne_porte_de_balisage(client, locations):
    """Ces chaînes finissent dans un navigateur, pas dans un terminal.

    Un accent grave autour d'une commande est la convention des docstrings du
    dépôt, et elle est fausse dès qu'un `hint` traverse l'API : la page l'affiche
    tel quel, au milieu d'une phrase française. Les blockers tiennent déjà cette
    règle — « mesurer avec ecurie bench <ref> » — et l'écran Parc l'a apprise
    d'une capture d'écran, là où trois suites de tests cherchant des sous-chaînes
    ne voyaient rien.
    """
    routes = ("/store/summary", "/store/plan", "/store/tiering")

    # Deux états, deux familles de phrases : « lancer un scan » avant, « ces
    # chiffres sont périmés » après.
    avant = [client.get(route).json().get("hint") for route in routes]
    locations(
        _parc_deporte(),
        last_scan_at="2026-08-20T10:00:00+00:00",
        last_apply_at="2026-08-20T11:00:00+00:00",
    )
    après = [client.get(route).json().get("hint") for route in routes]

    # Les libellés de poste voyagent avec le plan et s'affichent tels quels dans
    # l'écran : la même règle vaut pour eux.
    étiquettes = list(client.get("/store/plan").json()["labels"].values())

    assert any(avant) and any(après), "aucun hint rendu : le test ne prouve rien"
    assert étiquettes
    for phrase in avant + après + étiquettes:
        assert phrase is None or "`" not in phrase, phrase


def test_lire_le_tiering_ne_met_rien_en_quarantaine(client, locations, ecurie_home):
    """Déporter copie des giga-octets et met des originaux en quarantaine. La
    route montre ce que ça donnerait ; c'est `ecurie store tier` qui le fait."""
    locations(_parc_deporte(), last_scan_at="2026-08-20T10:00:00+00:00")

    client.get("/store/tiering")

    assert not (ecurie_home / "trash").exists()
