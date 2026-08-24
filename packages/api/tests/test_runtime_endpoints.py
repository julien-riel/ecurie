"""`/runtime/residents` et `/runtime/admission` — la mémoire, et ce qu'un job coûterait.

Le budget d'essai est de 16 Gio et le seuil de lourdeur celui de la config, 8 Gio :
les chiffres sont donc ceux du parc réel à l'échelle près, et un candidat de
9 Gio y est lourd comme `sdxl-base` l'est sur la machine.

Deux propriétés tiennent ce module :

- **rien n'est chargé, rien n'est tué.** `ecurie ps` appelle `prune()`, qui tue
  les workers devenus injoignables ; un `GET` que l'UI rafraîchit toutes les deux
  secondes ne peut pas se le permettre. Les entrées périmées sont rapportées,
  pas balayées ;
- **le pic se calcule avec l'entrée**, pas seulement avec le variant. C'est ce
  qui permet au bandeau de dire « ce morceau de 30 s demanderait 24,2 Gio »
  plutôt que d'afficher un bouton grisé.
"""

import os

from ecurie_core.config import HEAVY_THRESHOLD_RATIO

GIB = 1 << 30
PID_MORT = 4_194_303  # au-delà de pid_max sur macOS : sûrement libre, jamais le nôtre

ECHELLE = {
    "parameter": "speed",
    "base_bytes": 1 * GIB,
    "bytes_per_unit": 2 * GIB,
    "measured_range": [0.5, 2.0],
    "r_squared": 0.98,
}

# Même pente, ajustée sur un intervalle plus étroit : tout ce qui sort de
# [1,0 ; 1,5] est alors une conjecture, et doit se dire comme telle.
ECHELLE_ETROITE = {**ECHELLE, "measured_range": [1.0, 1.5]}


# --- l'état de la mémoire ---------------------------------------------------------


def test_un_parc_vide_annonce_le_budget_et_la_politique(client):
    corps = client.get("/runtime/residents").json()

    assert corps["budget_bytes"] == 16 * GIB
    assert corps["budget_source"] == "budget d'essai, injecté"
    assert corps["budget_measured"] is False
    assert corps["used_bytes"] == 0
    assert corps["free_bytes"] == 16 * GIB
    # Le seuil servi est celui qu'appliquera l'admission, donc celui que la part
    # par défaut tire du budget de cette machine — pas les 8 Gio de la machine de
    # référence, qui ne veulent rien dire sous un budget de 16 Gio.
    assert corps["policy"] == {
        "budget_bytes": 16 * GIB,
        "max_heavy_resident": 1,
        "heavy_threshold_bytes": int(16 * GIB * HEAVY_THRESHOLD_RATIO),
    }
    assert corps["residents"] == []
    assert corps["stale"] == []
    assert corps["admission"] is None


def test_un_resident_occupe_le_budget_et_dit_s_il_est_lourd(client, residents):
    residents("tts-test@essai", 9 * GIB, runtime="mlx-audio", env="mlx-audio", warmup_ms=2400)

    corps = client.get("/runtime/residents").json()

    (résident,) = corps["residents"]
    assert résident["ref"] == "tts-test@essai"
    assert résident["peak_bytes"] == 9 * GIB
    assert résident["heavy"] is True
    assert résident["warmup_ms"] == 2400
    assert résident["busy"] is False
    assert corps["used_bytes"] == 9 * GIB
    assert corps["free_bytes"] == 7 * GIB


def test_un_resident_en_plein_job_se_voit(client, residents):
    """Sans cette information, un refus « parce qu'un résident est occupé »
    renverrait vers un écran qui n'en dit rien."""
    residents("tts-test@essai", 2 * GIB, busy_by=os.getpid())

    (résident,) = client.get("/runtime/residents").json()["residents"]

    assert résident["busy"] is True
    assert résident["busy_by"] == os.getpid()


def test_un_resident_epingle_se_voit(client, residents):
    residents("tts-test@essai", 2 * GIB, pinned=True)

    (résident,) = client.get("/runtime/residents").json()["residents"]

    assert résident["pinned"] is True


def test_ce_que_le_worker_a_annonce_au_chargement_est_transmis(client, residents):
    """`runtime.voices` du contrat se remplit avec ceci : sans les options du
    worker, la liste déroulante des voix reste vide."""
    residents("tts-test@essai", 2 * GIB, options={"voices": ["alice", "bruno"]})

    (résident,) = client.get("/runtime/residents").json()["residents"]

    assert résident["options"] == {"voices": ["alice", "bruno"]}


# --- entrées périmées : rapportées, jamais balayées --------------------------------


def test_une_entree_dont_le_processus_est_mort_est_rapportee(client, residents, ecurie_home):
    residents("tts-test@essai", 2 * GIB, pid=PID_MORT)

    corps = client.get("/runtime/residents").json()

    assert corps["residents"] == []
    assert corps["stale"] == [
        {
            "ref": "tts-test@essai",
            "pid": PID_MORT,
            "holds_memory": False,
            "socket": str(ecurie_home / "tts-test_essai.sock"),
        }
    ]
    assert corps["used_bytes"] == 0


def test_un_worker_vivant_sans_socket_est_signale_comme_retenant_sa_memoire(
    client, residents, tmp_path
):
    """Le cas qui coûte cher : le processus vit, il tient ses gigaoctets, et il ne
    figure plus au budget. `holds_memory` est ce qui permet à l'UI de proposer
    `ecurie unload --force` plutôt que d'afficher « rien à signaler »."""
    residents("tts-test@essai", 9 * GIB, socket=tmp_path / "socket-jamais-cree.sock")

    corps = client.get("/runtime/residents").json()

    assert corps["residents"] == []
    assert corps["stale"][0]["holds_memory"] is True


def test_lire_les_residents_ne_tue_ni_ne_nettoie_rien(client, residents, ecurie_home):
    """`ecurie ps` appelle `prune()`, qui tue. Une lecture rafraîchie toutes les
    deux secondes ne peut pas tuer un processus que personne n'a désigné."""
    residents("tts-test@essai", 2 * GIB, pid=PID_MORT)
    avant = (ecurie_home / "residents.json").read_text()

    client.get("/runtime/residents")

    assert (ecurie_home / "residents.json").read_text() == avant


# --- simuler l'admission d'un variant ---------------------------------------------


def test_for_chiffre_ce_que_le_prochain_job_couterait(client):
    corps = client.get("/runtime/residents", params={"for": "tts-test"}).json()

    admission = corps["admission"]
    assert admission["ref"] == "tts-test@essai"
    assert admission["admitted"] is True
    assert admission["peak_bytes"] == 2 * GIB
    assert admission["headroom_bytes"] == 14 * GIB
    assert admission["evict"] == []


def test_for_nomme_ce_qui_serait_decharge(client_factory, depot, residents):
    """« lancer déchargera X » est ce que le bandeau doit dire avant de lancer,
    pas après."""
    depot.capability("text-to-speech").env("mlx-audio")
    depot.model("gros", peak_bytes=9 * GIB)
    residents("autre@v1", 9 * GIB)
    client = client_factory(depot)

    admission = client.get("/runtime/residents", params={"for": "gros"}).json()["admission"]

    assert admission["admitted"] is True
    assert admission["evict"] == ["autre@v1"]


def test_for_rend_un_refus_lisible(client_factory, depot):
    """« ce modèle demanderait 20 Gio, au-delà des 16 disponibles » vaut mieux
    qu'un bouton grisé — c'est l'exigence du §4 du plan."""
    depot.capability("text-to-speech").env("mlx-audio").model("enorme", peak_bytes=20 * GIB)
    client = client_factory(depot)

    admission = client.get("/runtime/residents", params={"for": "enorme"}).json()["admission"]

    assert admission["admitted"] is False
    assert "décharger ne changerait rien" in admission["reason"]


def test_for_sur_une_reference_inconnue_est_un_404_qui_nomme_le_parc(client):
    réponse = client.get("/runtime/residents", params={"for": "inexistant"})

    assert réponse.status_code == 404
    assert "tts-test" in réponse.json()["detail"]


# --- admission d'un job précis -----------------------------------------------------


def test_l_admission_resout_les_defauts_du_contrat_et_du_variant(client_factory, depot):
    depot.capability("text-to-speech").env("mlx-audio").model(defaults={"speed": 1.5})
    client = client_factory(depot)

    corps = client.post(
        "/runtime/admission", json={"ref": "tts-test", "input": {"text": "Bonjour."}}
    ).json()

    assert corps["input"] == {"speed": 1.5, "text": "Bonjour."}
    assert corps["input_errors"] == []
    assert corps["ready"] is True


def test_le_pic_attendu_suit_l_entree_quand_le_profil_declare_une_pente(client_factory, depot):
    """Trente secondes de musique coûtent le double de quinze. Un bandeau qui
    l'ignorerait annoncerait le pire cas pour tout, ou le meilleur pour tout."""
    depot.capability("text-to-speech").env("mlx-audio")
    depot.model(peak_bytes=5 * GIB, peak_scaling=ECHELLE)
    client = client_factory(depot)

    def pic(speed: float) -> int:
        corps = client.post(
            "/runtime/admission",
            json={"ref": "tts-test", "input": {"text": "Bonjour.", "speed": speed}},
        ).json()
        return corps["admission"]["peak_bytes"]

    assert pic(1.0) == 3 * GIB  # 1 Gio de base + 2 Gio × 1,0
    assert pic(2.0) == 5 * GIB


def test_un_pic_extrapole_le_dit_et_ne_descend_pas_sous_la_mesure(client_factory, depot):
    """Hors de l'intervalle ajusté, la droite est une conjecture : on la signale,
    et on ne laisse jamais un job partir sur une estimation plus optimiste que le
    pire cas réellement mesuré."""
    depot.capability("text-to-speech").env("mlx-audio")
    depot.model(peak_bytes=12 * GIB, peak_scaling=ECHELLE_ETROITE)
    client = client_factory(depot)

    admission = client.post(
        "/runtime/admission",
        json={"ref": "tts-test", "input": {"text": "Bonjour.", "speed": 2.0}},
    ).json()["admission"]

    # La droite dirait 5 Gio ; la mesure dit 12, et c'est elle qui l'emporte.
    assert admission["peak_bytes"] == 12 * GIB
    assert "extrapolé" in admission["peak_note"]


def test_une_saisie_incomplete_rend_un_chiffre_et_dit_ce_qui_manque(client):
    """Le bandeau interroge pendant la frappe : refuser de répondre priverait
    l'utilisateur du coût au moment précis où il le regarde."""
    corps = client.post("/runtime/admission", json={"ref": "tts-test", "input": {}}).json()

    assert corps["input_errors"] == ["entrée : 'text' is a required property"]
    assert corps["admission"]["admitted"] is True
    assert corps["admission"]["peak_bytes"] == 2 * GIB


def test_un_parametre_hors_contrat_est_refuse_en_422(client):
    """Ce n'est pas une saisie incomplète, c'est un client qui parle d'autre chose
    que la capacité qu'il a nommée."""
    réponse = client.post(
        "/runtime/admission", json={"ref": "tts-test", "input": {"tempo": 3}}
    )

    assert réponse.status_code == 422
    assert "n'est pas un paramètre de la capacité text-to-speech" in réponse.json()["detail"]


def test_une_valeur_typee_n_est_pas_reconvertie(client):
    """`false` passé par la conversion du terminal deviendrait la chaîne
    « False », puis le booléen vrai. L'API reçoit du JSON déjà typé."""
    corps = client.post(
        "/runtime/admission",
        json={"ref": "tts-test", "input": {"text": "Bonjour.", "speed": 0.5}},
    ).json()

    assert corps["input"]["speed"] == 0.5
    assert isinstance(corps["input"]["speed"], float)


def test_un_variant_sans_profil_est_refuse_et_renvoie_vers_bench(client_factory, depot):
    depot.capability("text-to-speech").env("mlx-audio").model(peak_bytes=None)
    client = client_factory(depot)

    corps = client.post(
        "/runtime/admission", json={"ref": "tts-test", "input": {"text": "Bonjour."}}
    ).json()

    assert corps["admission"]["admitted"] is False
    assert "ecurie bench tts-test@essai" in corps["admission"]["reason"]
    assert corps["ready"] is False


def test_l_admission_dit_aussi_ce_qui_empeche_d_executer(client_factory, depot, tmp_path):
    """L'admission mémoire et la disponibilité des poids sont deux questions
    différentes : un variant peut tenir dans le budget et n'être pas téléchargé."""
    depot.capability("text-to-speech").env("mlx-audio")
    depot.model(weights=str(tmp_path / "absent"))
    client = client_factory(depot)

    corps = client.post(
        "/runtime/admission", json={"ref": "tts-test", "input": {"text": "Bonjour."}}
    ).json()

    assert corps["admission"]["admitted"] is True
    assert corps["ready"] is False
    assert any("ecurie pull" in b or "introuvable" in b for b in corps["blockers"])


def test_l_admission_sur_une_reference_inconnue_est_un_404(client):
    réponse = client.post("/runtime/admission", json={"ref": "inexistant", "input": {}})

    assert réponse.status_code == 404


def test_l_admission_n_ecrit_rien(client, residents, ecurie_home):
    """Un `POST` par principe, une lecture par effet : deux appels identiques
    doivent donner la même réponse et ne rien laisser derrière eux."""
    residents("tts-test@essai", 2 * GIB)
    avant = (ecurie_home / "residents.json").read_text()

    premier = client.post(
        "/runtime/admission", json={"ref": "tts-test", "input": {"text": "Bonjour."}}
    ).json()
    second = client.post(
        "/runtime/admission", json={"ref": "tts-test", "input": {"text": "Bonjour."}}
    ).json()

    assert premier == second
    assert premier["admission"]["already_resident"] is True
    assert (ecurie_home / "residents.json").read_text() == avant
