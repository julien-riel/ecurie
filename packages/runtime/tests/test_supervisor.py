"""Le superviseur : résidence, réutilisation, éviction, mode mesure, tour de rôle.

Ces tests lancent de vrais sous-processus détachés qui écoutent de vrais sockets
Unix. C'est voulu : le mécanisme qu'on éprouve ici est précisément celui qui fait
qu'un modèle survit entre deux commandes, et il ne se simule pas.

Deux superviseurs construits sur le même `ecurie_home` tiennent lieu de deux
processus : c'est ce que sont un `ecurie serve` et un `ecurie run` lancés en même
temps. Ils ne partagent que `residents.json`, ce qui est exactement le sujet
depuis la tâche 4.6 — le miroir dit ce que la mémoire d'en face sait.
"""

import os
import threading
import time

import pytest
from ecurie_runtime.admission import Resident
from ecurie_runtime.supervisor import (
    AdmissionRefused,
    QueueTimeout,
    ReentrantJob,
    RefError,
    parse_ref,
)
from ecurie_runtime.worker import Timeouts, pid_alive

GIB = 1 << 30


def _acquire(supervisor, registry, ref="tts-test", **kwargs):
    model, variant, _ = parse_ref(registry, ref)
    return supervisor.acquire(model, variant, **kwargs)


def _en_fil(cible):
    """Lance `cible` dans un fil et rend de quoi l'attendre et le juger."""
    résultat: dict = {}

    def travail() -> None:
        try:
            résultat["valeur"] = cible()
        except BaseException as exc:  # noqa: BLE001 — le test juge ce qui a été levé
            résultat["erreur"] = exc

    fil = threading.Thread(target=travail, daemon=True)
    fil.start()
    return fil, résultat


def test_un_worker_reste_resident_entre_deux_prises(parc, supervisor_factory):
    parc.capability().model()
    superviseur = supervisor_factory(parc)

    premier = _acquire(superviseur, superviseur.registry)
    pid = premier.entry.pid
    premier.release()

    assert pid_alive(pid), "le worker doit survivre à la commande qui l'a lancé"
    assert [e.ref for e in superviseur.residents()] == ["tts-test@essai"]

    second = _acquire(superviseur, superviseur.registry)
    try:
        assert second.reused, "le second job doit retrouver le worker chaud"
        assert second.entry.pid == pid
    finally:
        second.release()
        superviseur.unload_all(force=True)
    assert not pid_alive(pid)


def test_le_deuxieme_modele_lourd_evince_le_premier(parc, supervisor_factory):
    parc.capability().model("lourd-a", peak_bytes=7 * GIB)
    parc.model("lourd-b", peak_bytes=7 * GIB)
    superviseur = supervisor_factory(parc)

    a = _acquire(superviseur, superviseur.registry, "lourd-a")
    pid_a = a.entry.pid
    a.release()

    b = _acquire(superviseur, superviseur.registry, "lourd-b")
    try:
        # La règle du parc (un seul lourd) ET le budget concourent ici : les deux
        # ensemble feraient 14 Gio pour un budget de 8.
        assert b.evicted == ("lourd-a@essai",)
        assert [e.ref for e in superviseur.residents()] == ["lourd-b@essai"]
        assert not pid_alive(pid_a), "l'évincé doit être réellement mort, pas seulement oublié"
    finally:
        b.release()
        superviseur.unload_all(force=True)


def test_deux_modeles_legers_restent_chauds_ensemble(parc, supervisor_factory):
    parc.capability().model("leger-a", peak_bytes=1 * GIB)
    parc.model("leger-b", peak_bytes=1 * GIB)
    superviseur = supervisor_factory(parc)

    a = _acquire(superviseur, superviseur.registry, "leger-a")
    a.release()
    b = _acquire(superviseur, superviseur.registry, "leger-b")
    try:
        assert b.evicted == ()
        assert {e.ref for e in superviseur.residents()} == {"leger-a@essai", "leger-b@essai"}
    finally:
        b.release()
        superviseur.unload_all(force=True)


def test_un_variant_sans_profil_est_refuse_hors_mode_mesure(parc, supervisor_factory):
    parc.capability().model("sans-profil", peak_bytes=None)
    superviseur = supervisor_factory(parc)

    with pytest.raises(AdmissionRefused) as exc:
        _acquire(superviseur, superviseur.registry, "sans-profil")
    assert "ecurie bench" in str(exc.value)
    assert superviseur.residents() == []


def test_le_mode_mesure_vide_le_parc_y_compris_les_epingles(parc, supervisor_factory):
    parc.capability().model("resident", peak_bytes=1 * GIB)
    parc.model("a-mesurer", peak_bytes=None)
    superviseur = supervisor_factory(parc)

    déjà = _acquire(superviseur, superviseur.registry, "resident", pin=True)
    pid = déjà.entry.pid
    déjà.release()

    mesure = _acquire(superviseur, superviseur.registry, "a-mesurer", measure=True)
    try:
        assert mesure.admission.measure_mode
        assert mesure.evicted == ("resident@essai",)
        assert not pid_alive(pid)
        # Le worker de mesure n'est pas un résident : il ne doit pas peser sur le
        # budget des jobs suivants.
        assert superviseur.residents() == []
    finally:
        mesure.release()


def test_un_epingle_bloque_l_admission_plutot_que_de_partir(parc, supervisor_factory):
    parc.capability().model("epingle", peak_bytes=5 * GIB)
    parc.model("gros", peak_bytes=5 * GIB)
    superviseur = supervisor_factory(parc)

    a = _acquire(superviseur, superviseur.registry, "epingle", pin=True)
    a.release()
    try:
        with pytest.raises(AdmissionRefused) as exc:
            _acquire(superviseur, superviseur.registry, "gros")
        assert "(épinglé)" in str(exc.value)
        assert "epingle@essai" in str(exc.value)
    finally:
        superviseur.unload_all(force=True)


def test_un_worker_mort_ne_grave_plus_le_budget(parc, supervisor_factory):
    parc.capability().model()
    superviseur = supervisor_factory(parc)

    bail = _acquire(superviseur, superviseur.registry)
    pid = bail.entry.pid
    bail.release()

    # Le worker meurt sans que personne ne le décharge : c'est le cas d'un
    # `kill -9`, d'un plantage, ou d'une session fermée brutalement.
    import os
    import signal

    os.kill(pid, signal.SIGKILL)
    for _ in range(100):
        if not pid_alive(pid):
            break
        time.sleep(0.02)

    assert superviseur.residents() == [], "une entrée fantôme réserverait de la mémoire pour rien"
    assert [e.ref for e in superviseur.registry_file.stale()] == ["tts-test@essai"]


def test_reprise_apres_socket_disparu(parc, supervisor_factory):
    """Un socket effacé sous les pieds du superviseur : il relance, il n'échoue pas."""
    parc.capability().model()
    superviseur = supervisor_factory(parc)

    bail = _acquire(superviseur, superviseur.registry)
    ancien_pid = bail.entry.pid
    bail.release()

    from pathlib import Path

    Path(superviseur.registry_file.read()["tts-test@essai"].socket).unlink()

    repris = _acquire(superviseur, superviseur.registry)
    try:
        assert repris.entry.pid != ancien_pid
        assert not repris.reused
    finally:
        repris.release()
        superviseur.unload_all(force=True)


def test_un_manifeste_qui_a_change_ne_reutilise_pas_le_worker_chaud(parc, supervisor_factory):
    """Le worker garde en mémoire ce qu'il a chargé, pas ce que dit le manifeste.

    Réutiliser sans comparer ferait écrire au manifeste du job une révision qui
    n'est pas celle qui a produit la sortie — la reproductibilité annoncée
    deviendrait un mensonge silencieux.
    """
    parc.capability().model()
    superviseur = supervisor_factory(parc)

    premier = _acquire(superviseur, superviseur.registry)
    ancien_pid = premier.entry.pid
    premier.release()

    # Le manifeste change sous nos pieds : d'autres poids pour le même variant.
    autres = parc.weights.parent / "poids-2"
    autres.mkdir()
    (autres / "model.safetensors").write_bytes(b"X" * 2048)
    superviseur.registry.models["tts-test"].variants[0].source.path = str(autres)

    second = _acquire(superviseur, superviseur.registry)
    try:
        assert not second.reused, "un manifeste modifié doit relancer le worker"
        assert second.entry.pid != ancien_pid
        assert not pid_alive(ancien_pid)
    finally:
        second.release()
        superviseur.unload_all(force=True)


def test_un_job_en_cours_n_est_pas_evince_par_le_job_suivant(parc, supervisor_factory):
    """Le cas qui coûte le plus cher : deux commandes lancées en même temps.

    Le résident occupé est le moins récemment utilisé, donc la victime LRU
    naturelle. L'évincer ne libérerait pourtant rien tout de suite : le worker
    meurt au milieu de son inférence, la sortie est perdue, et la commande qui a
    provoqué l'éviction ne sait même pas qu'elle vient de casser un travail.
    """
    parc.capability().model("occupe", peak_bytes=5 * GIB)
    parc.model("nouveau", peak_bytes=5 * GIB)
    superviseur = supervisor_factory(parc)

    # Le bail n'est pas relâché : le job est réputé en cours, comme dans un autre
    # terminal.
    occupé = _acquire(superviseur, superviseur.registry, "occupe")
    pid_occupé = occupé.entry.pid
    try:
        assert superviseur.registry_file.read()["occupe@essai"].busy

        with pytest.raises(AdmissionRefused) as exc:
            _acquire(superviseur, superviseur.registry, "nouveau")
        assert "en cours de job" in str(exc.value)
        assert pid_alive(pid_occupé), "le worker du job en cours doit être intact"

        # Le job se termine : la place se libère d'elle-même.
        occupé.release()
        suivant = _acquire(superviseur, superviseur.registry, "nouveau")
        assert suivant.evicted == ("occupe@essai",)
        suivant.release()
    finally:
        superviseur.unload_all(force=True)


def test_une_occupation_orpheline_ne_bloque_pas_le_parc(parc, supervisor_factory):
    """Un processus tué en plein job ne rend pas son bail.

    Le cas ne se pose plus qu'entre processus : chez nous, l'occupation vit en
    mémoire et disparaît avec le fil qui la portait. Ce que le miroir transporte,
    lui, survit à qui l'a écrit — d'où le pid, qui se vérifie, plutôt qu'un
    drapeau qui rendrait le résident inévinçable jusqu'au prochain redémarrage.
    """
    parc.capability().model("occupe", peak_bytes=5 * GIB)
    parc.model("nouveau", peak_bytes=5 * GIB)
    superviseur = supervisor_factory(parc)
    autre = supervisor_factory(parc)  # l'autre processus, qui ne lit que le miroir

    bail = _acquire(superviseur, superviseur.registry, "occupe")
    try:
        assert autre.residents()[0].busy, "l'autre processus doit voir le job en cours"

        with autre.registry_file.locked() as entries:
            # Un pid qui n'existe pas : le processus détenteur a disparu sans
            # rendre son bail. Le superviseur qui l'a écrit ne repassera plus.
            entries["occupe@essai"].busy_by = 2**22
        assert not autre.residents()[0].busy

        suivant = _acquire(autre, autre.registry, "nouveau")
        assert suivant.evicted == ("occupe@essai",)
        suivant.release()
    finally:
        bail.release()
        superviseur.unload_all(force=True)
        autre.unload_all(force=True)


# --- tour de rôle : un modèle sert un job à la fois ---------------------------


def test_deux_jobs_sur_le_meme_worker_attendent_leur_tour(parc, supervisor_factory):
    """Le second job attend que le premier ait rendu la main, pas une connexion.

    Un worker résident écoute une connexion à la fois (`listen(1)`) : sans tour
    de rôle, le second job ouvrait un socket qui restait dans le backlog, sans
    que rien ne dise pourquoi, pendant que son délai d'inférence courait déjà.
    """
    parc.capability().model()
    superviseur = supervisor_factory(parc)

    premier = _acquire(superviseur, superviseur.registry, job_id="job-1")
    entré = threading.Event()

    def second():
        bail = _acquire(superviseur, superviseur.registry, job_id="job-2")
        entré.set()
        bail.release()
        return bail

    fil, résultat = _en_fil(second)
    try:
        assert not entré.wait(0.4), "le second job n'entre pas tant que le premier tient le worker"
        assert superviseur.residents()[0].busy_by == os.getpid()
        premier.release()
        assert entré.wait(5), "le tour passe dès que le premier a rendu la main"
        fil.join(5)
        assert "erreur" not in résultat, résultat.get("erreur")
        assert résultat["valeur"].reused, "le second retrouve le worker chaud"
    finally:
        superviseur.unload_all(force=True)


def test_la_fin_d_un_job_ne_libere_pas_un_worker_qu_un_autre_occupe(parc, supervisor_factory):
    """Le défaut que le déménagement corrige : deux jobs, un seul pid.

    L'occupation était le pid du processus détenteur, écrit dans le fichier des
    résidents. Deux jobs du même processus y inscrivaient le même chiffre, et le
    premier à finir l'effaçait : le worker redevenait évinçable alors qu'une
    inférence tournait dessus. Une commande ne tenait qu'un job à la fois et ne
    l'a jamais rencontré ; un serveur en tient plusieurs, et le rencontre au
    premier usage à deux fenêtres.
    """
    parc.capability().model("occupe", peak_bytes=5 * GIB)
    parc.model("nouveau", peak_bytes=5 * GIB)
    superviseur = supervisor_factory(parc)

    premier = _acquire(superviseur, superviseur.registry, "occupe", job_id="job-1")
    pris = threading.Event()
    rendre = threading.Event()

    def second():
        bail = _acquire(superviseur, superviseur.registry, "occupe", job_id="job-2")
        pris.set()
        rendre.wait(10)
        bail.release()

    fil, résultat = _en_fil(second)
    try:
        time.sleep(0.2)  # le second est en file derrière le premier
        premier.release()
        assert pris.wait(5), "le second job doit avoir pris le relais"

        # Le premier job est fini ; le second tourne. Le worker n'est pas libre.
        with pytest.raises(AdmissionRefused) as exc:
            _acquire(superviseur, superviseur.registry, "nouveau")
        assert "en cours de job" in str(exc.value)
        assert superviseur.residents()[0].busy
    finally:
        rendre.set()
        fil.join(5)
        assert "erreur" not in résultat, résultat.get("erreur")
        superviseur.unload_all(force=True)


def test_l_attente_se_dit_a_qui_la_subit(parc, supervisor_factory):
    """Une attente muette est indiscernable d'un blocage.

    C'est le cas d'un `ecurie run` lancé pendant qu'un job de l'Atelier occupe le
    même modèle : sans un mot, la commande paraît avoir cessé de répondre.
    """
    parc.capability().model()
    superviseur = supervisor_factory(parc)

    annonces: list[str] = []
    libre = _acquire(superviseur, superviseur.registry, job_id="job-1", on_wait=annonces.append)
    assert annonces == [], "un tour libre ne s'annonce pas"

    entré = threading.Event()

    def second():
        bail = _acquire(
            superviseur, superviseur.registry, job_id="job-2", on_wait=annonces.append
        )
        entré.set()
        bail.release()

    fil, résultat = _en_fil(second)
    try:
        time.sleep(0.3)
        assert annonces == ["job-1"], "le job qui précède se nomme, il ne se compte pas"
        libre.release()
        assert entré.wait(5)
        fil.join(5)
        assert "erreur" not in résultat, résultat.get("erreur")
    finally:
        superviseur.unload_all(force=True)


def test_le_meme_fil_ne_peut_pas_prendre_deux_bails_sur_un_worker(parc, supervisor_factory):
    """Un fil qui s'oublie s'attendrait lui-même, et rien ne le dirait."""
    parc.capability().model()
    superviseur = supervisor_factory(parc)

    bail = _acquire(superviseur, superviseur.registry, job_id="job-1")
    try:
        with pytest.raises(ReentrantJob) as exc:
            _acquire(superviseur, superviseur.registry, job_id="job-2")
        assert "job-1" in str(exc.value)
    finally:
        bail.release()
        superviseur.unload_all(force=True)


def test_un_bail_rendu_deux_fois_ne_bloque_pas_le_variant(parc, supervisor_factory):
    """Rendre deux fois n'est pas rendre à quelqu'un d'autre.

    Un `release()` en trop relâcherait le tour du job suivant, qui se croirait
    seul sur un worker occupé — la panne la plus difficile à lire de toutes.
    """
    parc.capability().model()
    superviseur = supervisor_factory(parc)

    bail = _acquire(superviseur, superviseur.registry, job_id="job-1")
    bail.release()
    bail.release()
    try:
        suivant = _acquire(superviseur, superviseur.registry, job_id="job-2")
        assert suivant.reused
        suivant.release()
    finally:
        superviseur.unload_all(force=True)


def test_un_tour_qui_ne_vient_jamais_se_dit(parc, supervisor_factory):
    """Attendre est normal ; attendre plus longtemps qu'un job entier ne l'est pas."""
    parc.capability().model()
    superviseur = supervisor_factory(
        parc, timeouts=Timeouts(load_s=30, infer_s=30, ping_s=5, grace_s=2, queue_s=0.2)
    )

    bail = _acquire(superviseur, superviseur.registry, job_id="job-1")
    fil, résultat = _en_fil(lambda: _acquire(superviseur, superviseur.registry, job_id="job-2"))
    try:
        fil.join(5)
        assert isinstance(résultat.get("erreur"), QueueTimeout), résultat
        assert "ecurie ps --ping" in str(résultat["erreur"])
    finally:
        bail.release()
        superviseur.unload_all(force=True)


def test_health_ne_derange_pas_un_worker_que_nous_occupons(parc, supervisor_factory, monkeypatch):
    """Le pinger reviendrait à faire la queue derrière notre propre job.

    C'est la limite que la docstring de `health` annonçait pour le v0.4 : depuis
    une CLI, un worker occupé et un worker bloqué se ressemblent ; depuis le
    processus qui tient le job, non.
    """
    parc.capability().model()
    superviseur = supervisor_factory(parc)
    bail = _acquire(superviseur, superviseur.registry, job_id="job-1")

    connexions: list[object] = []
    vrai_connect = superviseur._connect

    def compter(*args, **kwargs):
        connexions.append(args)
        return vrai_connect(*args, **kwargs)

    monkeypatch.setattr(superviseur, "_connect", compter)
    try:
        assert superviseur.health() == {"tts-test@essai": True}
        assert connexions == [], "un worker que nous occupons n'a pas à être interrogé"
    finally:
        monkeypatch.undo()
        bail.release()
        superviseur.unload_all(force=True)


# --- le miroir : ce que les autres processus en lisent ------------------------


def test_le_miroir_publie_l_occupation_pour_les_autres_processus(parc, supervisor_factory):
    parc.capability().model()
    superviseur = supervisor_factory(parc)
    autre = supervisor_factory(parc)

    bail = _acquire(superviseur, superviseur.registry, job_id="job-1")
    try:
        vu = autre.residents()[0]
        assert vu.busy and vu.busy_by == os.getpid()
        assert vu.busy_since > 0
    finally:
        bail.release()
    try:
        assert not autre.residents()[0].busy, "le miroir suit la fin du job, sans attendre"
    finally:
        superviseur.unload_all(force=True)


def test_un_superviseur_n_efface_pas_les_workers_d_un_autre(parc, supervisor_factory):
    """Publier sa vue ne doit pas revenir à publier que les autres n'existent pas."""
    parc.capability().model("un", peak_bytes=1 * GIB)
    parc.model("deux", peak_bytes=1 * GIB)
    a = supervisor_factory(parc)
    b = supervisor_factory(parc)

    _acquire(a, a.registry, "un").release()
    _acquire(b, b.registry, "deux").release()
    try:
        assert {e.ref for e in a.residents()} == {"un@essai", "deux@essai"}
        assert {e.ref for e in b.residents()} == {"un@essai", "deux@essai"}
    finally:
        a.unload_all(force=True)


def test_un_worker_evince_par_un_autre_processus_sort_de_notre_memoire(parc, supervisor_factory):
    parc.capability().model("un", peak_bytes=5 * GIB)
    parc.model("deux", peak_bytes=5 * GIB)
    a = supervisor_factory(parc)
    b = supervisor_factory(parc)

    _acquire(a, a.registry, "un").release()
    évinceur = _acquire(b, b.registry, "deux")
    évinceur.release()
    try:
        assert évinceur.evicted == ("un@essai",)
        assert [e.ref for e in a.residents()] == ["deux@essai"]
        # A relance son modèle sans croire qu'il est encore chaud — et il prend à
        # son tour la place de celui de B, les deux étant lourds.
        repris = _acquire(a, a.registry, "un")
        assert not repris.reused
        assert repris.evicted == ("deux@essai",)
        repris.release()
    finally:
        a.unload_all(force=True)
        b.unload_all(force=True)


def test_unload_refuse_un_worker_en_plein_job(parc, supervisor_factory):
    """`--force` reste possible : ce qui manquait, c'est de savoir ce qu'on casse."""
    parc.capability().model()
    superviseur = supervisor_factory(parc)
    bail = _acquire(superviseur, superviseur.registry, job_id="job-1")
    try:
        with pytest.raises(AdmissionRefused) as exc:
            superviseur.unload("tts-test@essai")
        assert "un job est en cours" in str(exc.value)
        assert superviseur.unload("tts-test@essai", force=True)
    finally:
        bail.release()
        superviseur.unload_all(force=True)


def test_le_superviseur_ne_se_tue_pas_lui_meme(parc, supervisor_factory):
    """Notre pid dans le registre des résidents ne peut être qu'une entrée fausse.

    Un fichier corrompu, un pid recyclé, une entrée fabriquée : dans les trois
    cas, l'éviction tuerait le processus qui la décide. Un serveur disparaîtrait
    en voulant faire de la place.
    """
    from ecurie_runtime.residents import ResidentEntry

    parc.capability().model()
    superviseur = supervisor_factory(parc)
    faux_socket = parc.root / "pas-un-socket"
    faux_socket.write_text("")

    with superviseur.registry_file.locked() as entries:
        entries["fantome@essai"] = ResidentEntry(
            ref="fantome@essai",
            pid=os.getpid(),
            socket=str(faux_socket),
            peak_bytes=GIB,
            last_used=time.time(),
        )

    assert superviseur.unload("fantome@essai", force=True)
    assert superviseur.residents() == []
    assert pid_alive(os.getpid()), "nous sommes encore là"


def test_close_retire_l_occupation_sans_tuer_les_workers(parc, supervisor_factory):
    """Un serveur qui s'arrête en plein job laisse un worker chaud, pas un job fantôme."""
    parc.capability().model()
    superviseur = supervisor_factory(parc)
    bail = _acquire(superviseur, superviseur.registry, job_id="job-1")
    pid = bail.entry.pid

    superviseur.close()
    autre = supervisor_factory(parc)
    try:
        assert pid_alive(pid), "le résident survit au processus qui l'a chargé"
        assert not autre.residents()[0].busy
    finally:
        autre.unload_all(force=True)


def test_health_rapporte_sans_tuer(parc, supervisor_factory):
    parc.capability().model()
    superviseur = supervisor_factory(parc)
    bail = _acquire(superviseur, superviseur.registry)
    pid = bail.entry.pid
    bail.release()
    try:
        assert superviseur.health() == {"tts-test@essai": True}
        assert pid_alive(pid)
    finally:
        superviseur.unload_all(force=True)


def test_prune_tue_un_worker_devenu_injoignable(parc, supervisor_factory):
    """Socket effacé, processus vivant : le retirer sans le tuer perdrait sa mémoire.

    Plus personne ne saurait que ce worker existe, et le budget compterait comme
    libre une place qui ne l'est pas.
    """
    from pathlib import Path

    parc.capability().model()
    superviseur = supervisor_factory(parc)
    bail = _acquire(superviseur, superviseur.registry)
    pid = bail.entry.pid
    bail.release()

    Path(superviseur.registry_file.read()["tts-test@essai"].socket).unlink()
    assert pid_alive(pid)

    assert superviseur.prune() == ["tts-test@essai"]
    for _ in range(100):
        if not pid_alive(pid):
            break
        time.sleep(0.02)
    assert not pid_alive(pid)
    assert superviseur.registry_file.stale() == []


def test_simulate_ne_charge_rien(parc, supervisor_factory):
    parc.capability().model(peak_bytes=2 * GIB)
    superviseur = supervisor_factory(parc)
    décision = superviseur.simulate("tts-test@essai", 2 * GIB)
    assert décision.admitted
    assert superviseur.residents() == []


def test_parse_ref_refuse_de_deviner_entre_deux_variants(parc):
    parc.capability().model()
    registry = parc.load()
    document = registry.models["tts-test"]
    document.variants.append(document.variants[0].model_copy(update={"id": "autre"}))

    with pytest.raises(RefError) as exc:
        parse_ref(registry, "tts-test")
    assert "plusieurs variants" in str(exc.value)
    assert parse_ref(registry, "tts-test@autre")[2] == "tts-test@autre"


def test_unload_refuse_un_epingle_sans_force(parc, supervisor_factory):
    parc.capability().model()
    superviseur = supervisor_factory(parc)
    bail = _acquire(superviseur, superviseur.registry, pin=True)
    bail.release()
    try:
        with pytest.raises(AdmissionRefused):
            superviseur.unload("tts-test@essai")
        assert superviseur.unload("tts-test@essai", force=True)
    finally:
        superviseur.unload_all(force=True)


def test_residents_ordonnes_du_plus_recent(parc, supervisor_factory):
    parc.capability().model("un", peak_bytes=1 * GIB)
    parc.model("deux", peak_bytes=1 * GIB)
    superviseur = supervisor_factory(parc)
    _acquire(superviseur, superviseur.registry, "un").release()
    time.sleep(0.01)
    _acquire(superviseur, superviseur.registry, "deux").release()
    try:
        assert [e.ref for e in superviseur.residents()] == ["deux@essai", "un@essai"]
    finally:
        superviseur.unload_all(force=True)


def test_le_pic_rapporte_l_emporte_sur_un_profil_sous_estime(parc, supervisor_factory):
    """Le manifeste annonce 1 Gio, le worker en rapporte 3 : c'est le worker qui a raison.

    Le budget doit refléter ce qui est en mémoire maintenant, pas ce que le
    manifeste espérait — sinon un profil périmé fait accepter un second modèle
    qui ne tient pas.
    """
    parc.capability().model(peak_bytes=1 * GIB)
    superviseur = supervisor_factory(parc, env_vars={"ECURIE_FAKE_PEAK_BYTES": str(3 * GIB)})
    bail = _acquire(superviseur, superviseur.registry)
    try:
        assert bail.entry.peak_bytes == 3 * GIB
        assert any("supérieur de plus de 15 %" in a for a in bail.warnings)
    finally:
        bail.release()
        superviseur.unload_all(force=True)


def test_admission_utilise_les_residents_reels(parc, supervisor_factory):
    parc.capability().model("un", peak_bytes=5 * GIB)
    parc.model("deux", peak_bytes=5 * GIB)
    superviseur = supervisor_factory(parc)
    _acquire(superviseur, superviseur.registry, "un").release()
    try:
        décision = superviseur.simulate("deux@essai", 5 * GIB)
        assert décision.admitted
        assert décision.evict == ("un@essai",)
        assert Resident("un@essai", 5 * GIB, 0.0) is not None
    finally:
        superviseur.unload_all(force=True)
