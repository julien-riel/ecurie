"""`/registry/capabilities` et `/registry/models`.

Ce que ces deux routes doivent garantir tient en trois points, et le reste en
découle :

1. **le schéma d'entrée arrive intact.** L'UI le rend tel quel ; une clé perdue
   en route — `x-ui`, `x-options-from`, une borne — est un widget qui change de
   nature ou une validation qui disparaît, sans que rien ne le dise ;
2. **« déclaré » et « exécutable » ne sont pas confondus.** Un variant `tier: hot`
   dont les poids ont été mis en corbeille reste `hot` au manifeste. L'Atelier
   qui le proposerait enverrait l'utilisateur au-devant d'un échec ;
3. **chaque empêchement porte sa réparation.** Trois choses peuvent manquer et
   elles n'appellent pas le même geste : `ecurie pull`, `ecurie env sync`,
   `ecurie bench`.
"""

import json

GIB = 1 << 30


def _capacité(corps: dict, cap_id: str) -> dict:
    return next(c for c in corps["capabilities"] if c["id"] == cap_id)


def _variant(corps: dict, ref: str) -> dict:
    return next(v for m in corps["models"] for v in m["variants"] if v["ref"] == ref)


# --- contrats de capacité -------------------------------------------------------


def test_le_schema_d_entree_arrive_intact(client, parc):
    """Comparaison au fichier lui-même, pas à une copie : c'est le contrat que
    RJSF va rendre, et la moindre clé perdue change le formulaire."""
    contrat = json.loads(
        (parc.root / "registry" / "capabilities" / "text-to-speech.json").read_text()
    )

    capacité = _capacité(client.get("/registry/capabilities").json(), "text-to-speech")

    assert capacité["input"] == contrat["input"]
    assert capacité["output"] == contrat["output"]
    assert capacité["input"]["properties"]["text"]["x-ui"] == "textarea"
    assert capacité["input"]["properties"]["voice"]["x-options-from"] == "runtime.voices"


def test_la_capacite_dit_son_titulaire_ses_modeles_et_ce_qui_tourne(client):
    capacité = _capacité(client.get("/registry/capabilities").json(), "text-to-speech")

    assert capacité["title"] == "Synthèse vocale"
    assert capacité["models"] == ["tts-test"]
    assert capacité["ready_variants"] == ["tts-test@essai"]
    assert capacité["incumbent"] == "tts-test"
    assert capacité["required"] == ["text"]
    assert capacité["defaults"] == {"speed": 1.0}
    assert capacité["composite"] is False


def test_les_types_de_media_de_sortie_choisissent_le_visualiseur(client):
    capacité = _capacité(client.get("/registry/capabilities").json(), "text-to-speech")

    assert capacité["output_media_types"] == {"audio": "audio/wav"}


def test_une_capacite_sans_modele_le_dit_au_lieu_de_disparaitre(client_factory, depot):
    """Un contrat committé dont aucun modèle du parc ne relève : l'Atelier doit
    pouvoir l'afficher grisé plutôt que de laisser croire qu'il n'existe pas."""
    depot.capability("text-to-speech").capability("text-to-music").env("mlx-audio").model()
    client = client_factory(depot)

    musique = _capacité(client.get("/registry/capabilities").json(), "text-to-music")

    assert musique["models"] == []
    assert musique["ready_variants"] == []
    assert musique["incumbent"] is None


def test_une_capacite_avec_un_modele_mais_aucun_variant_pret(client_factory, depot):
    """Le cas qui trompe : des modèles au registre, rien d'exécutable. Une liste
    de modèles non vide et `ready_variants` vide sont deux informations
    différentes, et l'UI a besoin des deux."""
    depot.capability("text-to-speech").env("mlx-audio").model(peak_bytes=None)
    client = client_factory(depot)

    capacité = _capacité(client.get("/registry/capabilities").json(), "text-to-speech")

    assert capacité["models"] == ["tts-test"]
    assert capacité["ready_variants"] == []


# --- manifestes ------------------------------------------------------------------


def test_le_variant_porte_sa_reference_son_profil_et_son_environnement(client):
    variant = _variant(client.get("/registry/models").json(), "tts-test@essai")

    assert variant["ref"] == "tts-test@essai"
    assert variant["tier"] == "hot"
    assert variant["runtime"] == "mlx-audio"
    assert variant["env"] == "mlx-audio"
    assert variant["profile"]["peak_unified_memory_bytes"] == 2 * GIB
    assert variant["profile"]["measured_at"] == "2026-08-20"
    assert variant["ready"] is True
    assert variant["blockers"] == []
    assert variant["weights_path"].endswith("poids")


def test_le_drapeau_lourd_suit_le_seuil_de_la_politique(client_factory, depot):
    """8 Gio pile n'est pas lourd, un octet de plus l'est. C'est ce chiffre que
    le bandeau de ressources traduit par « lancer déchargera X »."""
    depot.capability("text-to-speech").env("mlx-audio")
    depot.model("pile", peak_bytes=8 * GIB, incumbent=True)
    depot.model("au-dessus", peak_bytes=8 * GIB + 1, incumbent=False)
    client = client_factory(depot)

    corps = client.get("/registry/models").json()

    assert _variant(corps, "pile@essai")["heavy"] is False
    assert _variant(corps, "au-dessus@essai")["heavy"] is True


def test_un_variant_sans_profil_ne_dit_pas_qu_il_est_leger(client_factory, depot):
    """`heavy: false` pour un pic inconnu se lirait « il tient », et c'est
    exactement l'hypothèse que l'admission refuse de faire."""
    depot.capability("text-to-speech").env("mlx-audio").model(peak_bytes=None)
    client = client_factory(depot)

    variant = _variant(client.get("/registry/models").json(), "tts-test@essai")

    assert variant["heavy"] is None
    assert variant["profile"] is None


# --- ce qui empêche d'exécuter, et le geste qui le lève --------------------------


def test_des_poids_absents_renvoient_vers_pull(client_factory, depot, tmp_path):
    depot.capability("text-to-speech").env("mlx-audio")
    depot.model(weights=str(tmp_path / "jamais-telecharge"))
    client = client_factory(depot)

    variant = _variant(client.get("/registry/models").json(), "tts-test@essai")

    assert variant["ready"] is False
    assert variant["weights_path"] is None
    assert any("introuvable" in b for b in variant["blockers"])


def test_un_environnement_non_synchronise_renvoie_vers_env_sync(client_factory, depot):
    depot.capability("text-to-speech").env("mlx-audio", synced=False).model()
    client = client_factory(depot)

    variant = _variant(client.get("/registry/models").json(), "tts-test@essai")

    assert variant["ready"] is False
    assert any("ecurie env sync mlx-audio" in b for b in variant["blockers"])


def test_un_profil_non_mesure_renvoie_vers_bench(client_factory, depot):
    depot.capability("text-to-speech").env("mlx-audio").model(peak_bytes=None)
    client = client_factory(depot)

    variant = _variant(client.get("/registry/models").json(), "tts-test@essai")

    assert variant["ready"] is False
    assert any("ecurie bench tts-test@essai" in b for b in variant["blockers"])


def test_les_empechements_sont_tous_rendus_pas_seulement_le_premier(
    client_factory, depot, tmp_path
):
    """Un variant fraîchement ajouté au registre les cumule souvent tous les
    trois. Les découvrir un par un coûte trois allers-retours."""
    depot.capability("text-to-speech").env("mlx-audio", synced=False)
    depot.model(weights=str(tmp_path / "absent"), peak_bytes=None)
    client = client_factory(depot)

    variant = _variant(client.get("/registry/models").json(), "tts-test@essai")

    assert len(variant["blockers"]) == 3


# --- filtre par capacité ---------------------------------------------------------


def test_le_filtre_par_capacite_ne_rend_que_la_capacite_demandee(client_factory, depot):
    depot.capability("text-to-speech").capability("text-to-music").env("mlx-audio")
    depot.model("voix", capability="text-to-speech")
    depot.model("chanson", capability="text-to-music")
    client = client_factory(depot)

    corps = client.get("/registry/models", params={"capability": "text-to-music"}).json()

    assert [m["id"] for m in corps["models"]] == ["chanson"]


def test_une_capacite_inconnue_est_un_404_qui_nomme_celles_qui_existent(client):
    """Rendre une liste vide se lirait « aucun modèle pour cette capacité », ce
    qui est faux et met sur une mauvaise piste."""
    réponse = client.get("/registry/models", params={"capability": "text-to-mesh"})

    assert réponse.status_code == 404
    assert "text-to-speech" in réponse.json()["detail"]


# --- rechargement à chaud --------------------------------------------------------


def test_un_manifeste_ajoute_apparait_sans_redemarrer(client, parc):
    """« Ajouter un modèle = ajouter un YAML » perdrait beaucoup s'il fallait
    relancer le serveur pour voir le YAML qu'on vient d'écrire."""
    assert len(client.get("/registry/models").json()["models"]) == 1

    parc.model("second", incumbent=False)

    assert [m["id"] for m in client.get("/registry/models").json()["models"]] == [
        "second",
        "tts-test",
    ]


def test_un_manifeste_supprime_disparait(client, parc):
    parc.model("second", incumbent=False)
    assert len(client.get("/registry/models").json()["models"]) == 2

    (parc.root / "registry" / "models" / "second.yaml").unlink()

    assert [m["id"] for m in client.get("/registry/models").json()["models"]] == ["tts-test"]


def test_un_contrat_corrige_est_relu(client, parc):
    """Le cas qui compte pour l'UI : le formulaire change quand le contrat change."""
    chemin = parc.root / "registry" / "capabilities" / "text-to-speech.json"
    contrat = json.loads(chemin.read_text())
    contrat["title"] = "Synthèse vocale (révisée)"
    chemin.write_text(json.dumps(contrat, ensure_ascii=False))

    capacité = _capacité(client.get("/registry/capabilities").json(), "text-to-speech")

    assert capacité["title"] == "Synthèse vocale (révisée)"


def test_le_registre_n_est_pas_recharge_quand_rien_ne_bouge(client, parc, monkeypatch):
    """Le rechargement est conditionnel, pas systématique : revalider le registre
    à chaque requête coûterait la lecture de tous les manifestes pour rien."""
    from ecurie_api import state as state_mod

    client.get("/registry/models")
    appels = []
    vrai = state_mod.load_registry
    monkeypatch.setattr(
        state_mod, "load_registry", lambda root: (appels.append(root), vrai(root))[1]
    )

    client.get("/registry/models")
    client.get("/registry/capabilities")
    assert appels == []

    parc.model("second", incumbent=False)
    client.get("/registry/models")
    assert len(appels) == 1
