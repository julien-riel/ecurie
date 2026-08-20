"""Le superviseur : résidence, réutilisation, éviction, mode mesure.

Ces tests lancent de vrais sous-processus détachés qui écoutent de vrais sockets
Unix. C'est voulu : le mécanisme qu'on éprouve ici est précisément celui qui fait
qu'un modèle survit entre deux commandes, et il ne se simule pas.
"""

import time

import pytest
from ecurie_runtime.admission import Resident
from ecurie_runtime.supervisor import AdmissionRefused, RefError, parse_ref
from ecurie_runtime.worker import pid_alive

GIB = 1 << 30


def _acquire(supervisor, registry, ref="tts-test", **kwargs):
    model, variant, _ = parse_ref(registry, ref)
    return supervisor.acquire(model, variant, **kwargs)


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
    """Une commande tuée en plein job ne rend pas son bail.

    Si l'occupation était un simple drapeau, le résident deviendrait
    inévinçable jusqu'au prochain redémarrage. On retient le pid du détenteur :
    un détenteur mort ne retient plus rien.
    """
    parc.capability().model("occupe", peak_bytes=5 * GIB)
    parc.model("nouveau", peak_bytes=5 * GIB)
    superviseur = supervisor_factory(parc)

    bail = _acquire(superviseur, superviseur.registry, "occupe")
    try:
        with superviseur.registry_file.locked() as entries:
            # Un pid qui n'existe pas : le processus détenteur a disparu.
            entries["occupe@essai"].busy_by = 2**22
        assert not superviseur.registry_file.read()["occupe@essai"].busy

        suivant = _acquire(superviseur, superviseur.registry, "nouveau")
        assert suivant.evicted == ("occupe@essai",)
        suivant.release()
    finally:
        bail.release()
        superviseur.unload_all(force=True)


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
