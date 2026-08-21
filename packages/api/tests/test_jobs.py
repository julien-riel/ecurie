"""`/jobs` — la première surface d'écriture de l'API.

Ces tests exécutent de vrais jobs : vrai superviseur, vrai socket, vrai
protocole, vrai fichier produit. Seul le worker est d'essai, faute de venv MLX en
CI. C'est la même limite que pour le superviseur, et elle est au bon endroit —
tout ce qui se passe entre la requête HTTP et le `result` du worker est du code
de production.

Ce que ces tests gardent, au-delà du chemin heureux : ce qui est refusé **avant**
de créer un job, et ce qui ne peut l'être qu'après.
"""

import json
import time
import wave

from ecurie_api.jobs import Job

TIMEOUT_JOB_S = 30


def _attendre(client, job_id: str, timeout_s: float = TIMEOUT_JOB_S) -> dict:
    """Interroge le job jusqu'à ce qu'il soit terminé. Rend son état final."""
    limite = time.monotonic() + timeout_s
    while time.monotonic() < limite:
        corps = client.get(f"/jobs/{job_id}").json()
        if corps["state"] in ("done", "failed"):
            return corps
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} toujours {corps['state']} après {timeout_s} s")


def _lancer(client, texte: str = "Bonjour le parc.", **extra) -> dict:
    réponse = client.post(
        "/jobs", json={"ref": "tts-test", "input": {"text": texte, **extra}}
    )
    assert réponse.status_code == 202, réponse.text
    return réponse.json()


# --- le chemin complet ----------------------------------------------------------


def test_un_job_soumis_produit_un_fichier_telechargeable(client):
    """De la requête au wav : c'est le critère de sortie du jalon, vu du serveur."""
    soumis = _lancer(client)
    assert soumis["state"] in ("queued", "running")
    assert soumis["ref"] == "tts-test@essai"

    fini = _attendre(client, soumis["id"])
    assert fini["state"] == "done", fini["error"]
    assert fini["progress"] == 100
    assert fini["output"] == {"audio": "audio.wav"}, "la réponse du worker, telle qu'il l'a rendue"
    assert fini["files"]["audio"] == f"/jobs/{soumis['id']}/files/audio.wav"

    fichier = client.get(fini["files"]["audio"])
    assert fichier.status_code == 200
    # Le type de média vient du contrat, pas d'une devinette sur l'extension.
    assert fichier.headers["content-type"].startswith("audio/")
    assert fichier.content[:4] == b"RIFF"


def test_le_manifeste_du_job_est_rendu_avec_son_etat(client, tmp_path):
    """« état + manifeste » du §6 : la reproductibilité n'est pas un fichier à part."""
    fini = _attendre(client, _lancer(client)["id"])

    manifeste = fini["manifest"]
    assert manifeste["ok"] is True
    assert manifeste["model"] == "tts-test"
    assert manifeste["input"]["text"] == "Bonjour le parc."
    assert manifeste["input_hash"], "sans empreinte de l'entrée, aucune confrontation A/B"
    assert manifeste["contract"]["id"] == "text-to-speech"


def test_le_wav_produit_est_lisible(client, ecurie_home):
    """Un job « ok » dont le fichier ne s'ouvre pas serait un succès qui n'en est pas un."""
    fini = _attendre(client, _lancer(client, "Une phrase un peu plus longue à dire.")["id"])
    chemin = ecurie_home / "outputs" / fini["id"] / fini["outputs"]["audio"]

    with wave.open(str(chemin)) as fichier:
        durée = fichier.getnframes() / fichier.getframerate()
    assert durée > 0.5


def test_le_second_job_retrouve_le_worker_chaud(client):
    """La résidence se voit depuis l'API : c'est ce que le serveur apporte de plus."""
    premier = _attendre(client, _lancer(client, "Un.")["id"])
    second = _attendre(client, _lancer(client, "Deux.")["id"])

    assert premier["state"] == "done" and second["state"] == "done"
    assert not premier["reused"] and second["reused"]
    résidents = client.get("/runtime/residents").json()
    assert [r["ref"] for r in résidents["residents"]] == ["tts-test@essai"]
    assert résidents["residents"][0]["busy"] is False, "le job fini rend le worker évinçable"


def test_deux_jobs_soumis_ensemble_aboutissent_tous_les_deux(client):
    """Le tour de rôle de la 4.6, vu du serveur : ils se suivent au lieu de se marcher dessus."""
    a = _lancer(client, "Premier job.")
    b = _lancer(client, "Deuxième job.")

    fini_a = _attendre(client, a["id"])
    fini_b = _attendre(client, b["id"])

    assert fini_a["state"] == "done", fini_a["error"]
    assert fini_b["state"] == "done", fini_b["error"]
    assert sorted([fini_a["reused"], fini_b["reused"]]) == [False, True]


# --- le flux d'événements -------------------------------------------------------


def test_le_flux_rejoue_depuis_le_debut_puis_se_termine(client):
    """Un client qui arrive en retard doit voir où l'on en est, pas le silence."""
    soumis = _lancer(client, "Une phrase pour la progression.")

    événements = []
    with client.stream("GET", f"/jobs/{soumis['id']}/events") as flux:
        assert flux.headers["content-type"].startswith("text/event-stream")
        courant: dict = {}
        for ligne in flux.iter_lines():
            if ligne.startswith("event: "):
                courant = {"event": ligne[7:]}
            elif ligne.startswith("data: "):
                courant["data"] = json.loads(ligne[6:])
                événements.append(courant)
                if courant["event"] == "end":
                    break

    types = [e["event"] for e in événements]
    assert types[0] == "state", "le premier événement dit que le job existe"
    assert types[-1] == "end", "sans fin explicite, EventSource rouvrirait la connexion"
    assert "progress" in types, "la progression du worker doit traverser jusqu'au client"

    états = [e["data"]["state"] for e in événements if e["event"] != "end"]
    assert états[0] == "queued"
    assert états[-1] == "done"
    dernier = événements[-2]["data"]
    assert dernier["files"]["audio"].endswith("audio.wav")
    # Le flux se suffit à lui-même : un client qui n'écoute que lui doit pouvoir
    # afficher la sortie sans relire le job ni son manifeste.
    assert dernier["output"] == {"audio": "audio.wav"}


def test_le_flux_d_un_job_deja_fini_rend_son_histoire_entiere(client):
    """Le journal n'est pas consommé : deux abonnés successifs lisent la même chose."""
    fini = _attendre(client, _lancer(client)["id"])

    with client.stream("GET", f"/jobs/{fini['id']}/events") as flux:
        corps = "".join(flux.iter_text())

    assert corps.count("event: state") >= 3  # queued, running, done
    assert corps.rstrip().endswith("}")
    assert "event: end" in corps


def test_le_flux_d_un_job_inconnu_est_404(client):
    assert client.get("/jobs/20260821-120000-abcdef/events").status_code == 404


def test_un_job_qui_finit_entre_deux_lectures_ne_perd_pas_son_dernier_evenement():
    """La course que seul un client suivant le flux fait payer.

    Le flux lisait le journal, puis demandait si le job était terminé. Entre les
    deux, le fil du job pouvait exécuter `finish()` en entier — dernier événement
    compris —, si bien que la lecture rendait « rien de neuf » et que le test de
    terminaison concluait « plus rien à venir ». Le `end` partait sans que l'état
    final soit passé : le client restait sur l'avant-dernier, en cours à jamais,
    barre figée et sortie jamais montrée.

    La course se joue en quelques bytecodes et ne s'attrape pas en lançant de
    vrais jobs. On la provoque donc à l'endroit exact où elle se produit : une
    lecture qui laisse le job finir avant de rendre sa liste vide.
    """
    import asyncio

    from ecurie_api.routers.jobs import _flux

    class JobQuiFinitPendantLaLecture(Job):
        def events_since(self, index: int) -> list:
            vus = super().events_since(index)
            if not vus and not self.terminal:
                with self._lock:
                    self.state = "done"
                    self.progress = 100
                    self.outputs = {"audio": "audio.wav"}
                    self._emit("state")
            return vus

    job = JobQuiFinitPendantLaLecture(
        id="20260821-120000-abcdef",
        ref="tts-test@essai",
        model="tts-test",
        variant="essai",
        capability="text-to-speech",
        input={"text": "x"},
    )

    class RequeteConnectee:
        async def is_disconnected(self) -> bool:
            return False

    async def collecter() -> list[str]:
        return [trame async for trame in _flux(job, RequeteConnectee())]

    trames = asyncio.run(collecter())

    assert trames[-1].startswith("event: end"), "le flux doit finir par un end explicite"
    états = [t for t in trames if t.startswith("event: state")]
    assert json.loads(états[-1].split("data: ", 1)[1])["state"] == "done", (
        "le dernier état passé au client doit être l'état final, jamais l'avant-dernier"
    )


# --- ce qui est refusé avant de créer un job ------------------------------------


def test_une_entree_refusee_par_le_contrat_ne_cree_pas_de_job(client):
    """Créer un job voué à l'échec pour le voir échouer trois secondes plus tard
    n'apprend rien de plus et laisse une trace inutile."""
    réponse = client.post("/jobs", json={"ref": "tts-test", "input": {"speed": 99}})

    assert réponse.status_code == 422
    assert "text" in réponse.json()["detail"]
    assert client.ecurie.jobs.all() == []


def test_un_parametre_hors_contrat_est_refuse_avec_la_liste_des_bons(client):
    réponse = client.post(
        "/jobs", json={"ref": "tts-test", "input": {"text": "x", "temperature": 0.7}}
    )

    assert réponse.status_code == 422
    détail = réponse.json()["detail"]
    assert "temperature" in détail and "text" in détail


def test_un_variant_non_pret_est_refuse_avec_ce_qui_manque(client_factory, depot):
    """Un job sur un variant sans profil échouerait à l'admission : autant le dire."""
    depot.capability("text-to-speech").env("mlx-audio").model("sans-profil", peak_bytes=None)
    client = client_factory(depot)

    réponse = client.post("/jobs", json={"ref": "sans-profil", "input": {"text": "x"}})

    assert réponse.status_code == 409
    détail = réponse.json()["detail"]
    assert détail["ref"] == "sans-profil@essai"
    assert any("ecurie bench" in b for b in détail["blockers"])


def test_un_modele_inconnu_est_404(client):
    réponse = client.post("/jobs", json={"ref": "nexistepas", "input": {}})

    assert réponse.status_code == 404
    assert "modèle inconnu" in réponse.json()["detail"]


def test_au_dela_de_la_limite_les_jobs_sont_refuses(client, monkeypatch):
    """Un garde-fou contre une boucle côté client, pas une politique de parc."""
    from ecurie_api import jobs as module_jobs

    monkeypatch.setattr(module_jobs, "MAX_JOBS_ACTIFS", 1)
    premier = _lancer(client, "Un job qui occupe la place.")

    réponse = client.post("/jobs", json={"ref": "tts-test", "input": {"text": "Deux."}})

    assert réponse.status_code == 429
    assert "maximum" in réponse.json()["detail"]
    _attendre(client, premier["id"])


# --- ce qui ne peut être refusé qu'après ----------------------------------------


def test_une_admission_refusee_fait_un_job_qui_echoue_lisiblement(client_factory, depot):
    """L'admission ne se tranche qu'au moment de charger : d'ici là, la place a pu se libérer.

    Le job existe donc, et il échoue avec la phrase que le contrôle d'admission
    compose — celle que le §4 du plan exige, en gigaoctets et non en octets bruts.
    """
    depot.capability("text-to-speech").env("mlx-audio").model("enorme", peak_bytes=64 * 1024**3)
    client = client_factory(depot)

    soumis = client.post("/jobs", json={"ref": "enorme", "input": {"text": "x"}})
    assert soumis.status_code == 202, soumis.text

    fini = _attendre(client, soumis.json()["id"])
    assert fini["state"] == "failed"
    assert fini["error"].startswith("admission refusée : "), (
        "un refus est une décision, pas une panne : pas de nom de classe en tête"
    )
    assert "Gio" in fini["error"], "un refus se lit en gigaoctets"
    assert "budget entier" in fini["error"]


# --- les fichiers ----------------------------------------------------------------


def test_un_chemin_qui_sort_du_dossier_du_job_est_404(client, ecurie_home):
    """La question « ce fichier existe-t-il ailleurs » n'a pas à recevoir de réponse."""
    fini = _attendre(client, _lancer(client)["id"])
    secret = ecurie_home / "secret.txt"
    secret.write_text("rien à voir ici")

    réponse = client.get(f"/jobs/{fini['id']}/files/..%2Fsecret.txt")

    assert réponse.status_code == 404
    assert "rien à voir ici" not in réponse.text


def test_un_identifiant_de_job_invalide_est_404(client):
    """Un identifiant compose un chemin : la forme se vérifie avant d'y toucher.

    `%2e%2e` plutôt que `..` : les clients HTTP normalisent le second avant de
    l'envoyer, si bien qu'il n'atteindrait jamais la route qu'on veut éprouver.
    """
    for mauvais in ("%2e%2e", "sans-forme", "20260821-120000-XYZ", "20260821-120000-abcdef-x"):
        réponse = client.get(f"/jobs/{mauvais}")
        assert réponse.status_code == 404, f"{mauvais} → {réponse.status_code}"
        assert "invalide" in réponse.json()["detail"] or "inconnu" in réponse.json()["detail"]


def test_un_fichier_absent_est_404(client):
    fini = _attendre(client, _lancer(client)["id"])
    assert client.get(f"/jobs/{fini['id']}/files/inexistant.wav").status_code == 404


def test_l_entree_copiee_est_servie_comme_la_sortie(client, ecurie_home):
    """Le dossier du job est servi en entier : l'entrée copiée en fait partie.

    C'est ce qui permettra de rejouer un artefact de trois semaines sans dépendre
    d'un fichier d'origine qu'on aura renommé entre-temps.
    """
    fini = _attendre(client, _lancer(client)["id"])

    réponse = client.get(f"/jobs/{fini['id']}/files/manifest.json")

    assert réponse.status_code == 200
    assert réponse.json()["job_id"] == fini["id"]


# --- la mémoire longue ------------------------------------------------------------


def test_un_job_d_une_session_precedente_se_relit_sur_le_disque(client, ecurie_home):
    """La table des jobs ne survit pas au redémarrage ; le dossier du job, si."""
    job_id = "20260101-101010-abc123"
    dossier = ecurie_home / "outputs" / job_id
    dossier.mkdir(parents=True)
    (dossier / "audio.wav").write_bytes(b"RIFF....WAVE")
    (dossier / "manifest.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "ok": True,
                "started_at": "2026-01-01T10:10:10+00:00",
                "capability": "text-to-speech",
                "model": "tts-test",
                "variant": "essai",
                "input": {"text": "d'avant le redémarrage"},
                "output": {"audio": "audio.wav", "duration_seconds": 1.83},
                "metrics": {"rtf": 0.05},
                "contract": {"id": "text-to-speech", "outputs": {"audio": "audio/wav"}},
            },
            ensure_ascii=False,
        )
    )

    corps = client.get(f"/jobs/{job_id}").json()

    assert corps["state"] == "done"
    assert corps["input"]["text"] == "d'avant le redémarrage"
    # Deux clés dans la réponse du worker, une seule qui soit un fichier. Les
    # confondre ferait disparaître de l'écran tout ce qui n'est pas un fichier —
    # une langue détectée, un nombre de pages, une liste d'appels d'outils.
    assert corps["output"] == {"audio": "audio.wav", "duration_seconds": 1.83}
    assert corps["outputs"] == {"audio": "audio.wav"}
    assert corps["files"]["audio"] == f"/jobs/{job_id}/files/audio.wav"
    fichier = client.get(corps["files"]["audio"])
    assert fichier.status_code == 200
    assert fichier.headers["content-type"].startswith("audio/")


def test_un_job_jamais_execute_est_404(client):
    assert client.get("/jobs/20260101-101010-abc123").status_code == 404
