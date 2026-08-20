"""Le job complet : entrée typée, artefact, manifeste rejouable, télémétrie."""

import json

import pytest
from ecurie_core.config import Config
from ecurie_runtime.runner import (
    InputError,
    parse_assignments,
    resolve_input,
    run_job,
)
from ecurie_runtime.supervisor import parse_ref
from ecurie_store.db import StateDB

GIB = 1 << 30


def _pieces(superviseur, ref="tts-test"):
    model, variant, _ = parse_ref(superviseur.registry, ref)
    return model, variant, superviseur.registry.capabilities[model.capability]


# --- résolution de l'entrée --------------------------------------------------


def test_les_assignations_acceptent_un_egal_dans_la_valeur():
    assert parse_assignments(["text=a=b", "speed=1.5"]) == {"text": "a=b", "speed": "1.5"}


def test_assignation_mal_formee():
    with pytest.raises(InputError, match="clé=valeur"):
        parse_assignments(["texte-sans-egal"])


def test_les_types_du_contrat_sont_appliques(parc, supervisor_factory):
    parc.capability().model()
    superviseur = supervisor_factory(parc)
    model, variant, contract = _pieces(superviseur)

    résolu = resolve_input(contract, variant, {"text": "bonjour", "speed": "1.5"})
    assert résolu.values["speed"] == 1.5  # et non la chaîne "1.5"
    assert isinstance(résolu.values["speed"], float)


def test_une_valeur_hors_bornes_est_refusee_avant_toute_execution(parc, supervisor_factory):
    parc.capability().model()
    superviseur = supervisor_factory(parc)
    _, variant, contract = _pieces(superviseur)

    with pytest.raises(InputError) as exc:
        resolve_input(contract, variant, {"text": "bonjour", "speed": "9"})
    assert "speed" in str(exc.value)


def test_un_parametre_inconnu_liste_ceux_qui_existent(parc, supervisor_factory):
    parc.capability().model()
    superviseur = supervisor_factory(parc)
    _, variant, contract = _pieces(superviseur)

    with pytest.raises(InputError) as exc:
        resolve_input(contract, variant, {"text": "x", "vitesse": "1"})
    message = str(exc.value)
    assert "vitesse" in message and "speed" in message and "text" in message


def test_l_ordre_des_defauts_contrat_variant_utilisateur(parc, supervisor_factory):
    parc.capability().model(defaults={"speed": 1.25})
    superviseur = supervisor_factory(parc)
    _, variant, contract = _pieces(superviseur)

    # Le contrat dit 1.0, le variant 1.25 : sans saisie, c'est le variant qui parle.
    assert resolve_input(contract, variant, {"text": "x"}).values["speed"] == 1.25
    # L'utilisateur tranche toujours en dernier.
    assert resolve_input(contract, variant, {"text": "x", "speed": "0.8"}).values["speed"] == 0.8


def test_une_option_de_runtime_est_surchargeable_mais_pas_inventable(parc, supervisor_factory):
    parc.capability().model(options={"language": "auto"})
    superviseur = supervisor_factory(parc)
    _, variant, contract = _pieces(superviseur)

    résolu = resolve_input(contract, variant, {"text": "x", "language": "french"})
    assert résolu.params == {"language": "french"}
    assert "language" not in résolu.values  # ce n'est pas un champ du contrat

    with pytest.raises(InputError):
        resolve_input(contract, variant, {"text": "x", "langue": "french"})


def test_l_empreinte_d_entree_ignore_l_ordre_mais_pas_les_valeurs(parc, supervisor_factory):
    parc.capability().model()
    superviseur = supervisor_factory(parc)
    _, variant, contract = _pieces(superviseur)

    a = resolve_input(contract, variant, {"text": "bonjour", "speed": "1.0"})
    b = resolve_input(contract, variant, {"speed": "1.0", "text": "bonjour"})
    c = resolve_input(contract, variant, {"text": "bonsoir", "speed": "1.0"})
    assert a.hash() == b.hash()
    assert a.hash() != c.hash()


# --- exécution ---------------------------------------------------------------


def test_job_complet_ecrit_artefact_manifeste_et_telemetrie(
    parc, supervisor_factory, config: Config
):
    parc.capability().model(incumbent=True)
    superviseur = supervisor_factory(parc)
    model, variant, contract = _pieces(superviseur)
    db = StateDB(config.state_db)

    try:
        outcome = run_job(
            superviseur, model, variant, contract,
            {"text": "Bonjour le parc."}, seed=42, db=db,
        )
        assert outcome.ok, outcome.error
        wav = outcome.job_dir / "audio.wav"
        assert wav.is_file() and wav.stat().st_size > 0

        manifeste = json.loads((outcome.job_dir / "manifest.json").read_text())
        assert manifeste["model"] == "tts-test"
        assert manifeste["variant"] == "essai"
        assert manifeste["capability"] == "text-to-speech"
        assert manifeste["seed"] == 42
        assert manifeste["input"]["text"] == "Bonjour le parc."
        assert manifeste["input"]["speed"] == 1.0, "les défauts résolus sont écrits, pas supposés"
        assert manifeste["input_hash"]
        assert manifeste["output"] == {"audio": "audio.wav"}
        assert manifeste["versions"]["runtime"] == "mlx-audio"
        assert manifeste["contract"]["outputs"] == {"audio": "audio/wav"}

        assert db.runs_count() == 1
        assert db.last_run_by_variant() == {"tts-test@essai": manifeste["started_at"]}
    finally:
        db.close()
        superviseur.unload_all(force=True)


def test_la_graine_rend_la_sortie_reproductible(parc, supervisor_factory):
    parc.capability().model()
    superviseur = supervisor_factory(parc)
    model, variant, contract = _pieces(superviseur)
    try:
        a = run_job(superviseur, model, variant, contract, {"text": "test"}, seed=7)
        b = run_job(superviseur, model, variant, contract, {"text": "test"}, seed=7)
        c = run_job(superviseur, model, variant, contract, {"text": "test"}, seed=8)
        assert (a.job_dir / "audio.wav").read_bytes() == (b.job_dir / "audio.wav").read_bytes()
        assert (a.job_dir / "audio.wav").read_bytes() != (c.job_dir / "audio.wav").read_bytes()
        assert a.manifest["input_hash"] == b.manifest["input_hash"] != c.manifest["input_hash"]
    finally:
        superviseur.unload_all(force=True)


def test_un_fichier_d_entree_est_copie_dans_le_job(parc, supervisor_factory, tmp_path):
    image = tmp_path / "objet.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    parc.capability("image-to-mesh").model("mesh-test", capability="image-to-mesh")
    superviseur = supervisor_factory(
        parc, env_vars={"ECURIE_FAKE_OUTPUT_KEY": "mesh", "ECURIE_FAKE_OUTPUT_NAME": "mesh.glb"}
    )
    model, variant, contract = _pieces(superviseur, "mesh-test")

    try:
        outcome = run_job(superviseur, model, variant, contract, {"image": str(image)})
        assert outcome.ok, outcome.error
        copie = outcome.job_dir / "inputs" / "objet.png"
        assert copie.read_bytes() == image.read_bytes()
        # Le manifeste pointe la copie, pas l'original : l'original peut disparaître.
        assert outcome.manifest["input"]["image"] == "inputs/objet.png"
        assert outcome.manifest["input_files"]["image"]
    finally:
        superviseur.unload_all(force=True)


def test_un_fichier_d_entree_absent_est_refuse_avant_le_worker(parc, supervisor_factory):
    parc.capability("image-to-mesh").model("mesh-test", capability="image-to-mesh")
    superviseur = supervisor_factory(parc)
    model, variant, contract = _pieces(superviseur, "mesh-test")

    with pytest.raises(InputError, match="introuvable"):
        run_job(superviseur, model, variant, contract, {"image": "/inexistant/objet.png"})
    assert superviseur.residents() == [], "rien ne doit être chargé pour une entrée invalide"


def test_une_panne_d_adaptateur_reste_un_job_trace(parc, supervisor_factory, config: Config):
    parc.capability().model()
    superviseur = supervisor_factory(parc, env_vars={"ECURIE_FAKE_FAIL": "infer"})
    model, variant, contract = _pieces(superviseur)
    db = StateDB(config.state_db)
    try:
        outcome = run_job(superviseur, model, variant, contract, {"text": "x"}, db=db)
        assert not outcome.ok
        assert "panne demandée" in outcome.error
        # Le manifeste est écrit malgré l'échec : c'est lui qui dit ce qui a été tenté.
        manifeste = json.loads((outcome.job_dir / "manifest.json").read_text())
        assert manifeste["ok"] is False and manifeste["error"]
        ligne = db.conn.execute("SELECT ok FROM runs WHERE id = ?", (outcome.job_id,)).fetchone()
        assert ligne == (0,)
    finally:
        db.close()
        superviseur.unload_all(force=True)


def test_un_job_refuse_avant_le_worker_n_est_pas_un_usage(parc, supervisor_factory, config: Config):
    """Sans profil mesuré, l'admission refuse et rien ne tourne.

    La ligne de télémétrie ne doit pas être écrite : elle atteste d'un usage, et
    c'est elle qui décide plus tard si un variant peut être proposé à la
    récupération d'espace. L'inscrire ici rendrait ce variant définitivement
    « utilisé » sans qu'il ait jamais tourné.
    """
    parc.capability().model("sans-profil", peak_bytes=None)
    superviseur = supervisor_factory(parc)
    model, variant, contract = _pieces(superviseur, "sans-profil")
    db = StateDB(config.state_db)
    try:
        outcome = run_job(superviseur, model, variant, contract, {"text": "x"}, db=db)
        assert not outcome.ok and "ecurie bench" in outcome.error
        assert db.runs_count() == 0
        assert db.last_run_by_variant() == {}
    finally:
        db.close()


def test_une_sortie_exigee_mais_absente_fait_echouer_le_job(parc, supervisor_factory):
    """Le worker rend un résultat, mais pas la sortie que le contrat exige.

    Le contrat fait loi jusqu'au bout : compter ce job « ok » mettrait un
    artefact vide dans la Bibliothèque et un faux succès dans la télémétrie.
    """
    parc.capability().model()
    superviseur = supervisor_factory(
        parc, env_vars={"ECURIE_FAKE_OUTPUT_NAME": "audio.wav", "ECURIE_FAKE_OUTPUT_KEY": "autre"}
    )
    model, variant, contract = _pieces(superviseur)
    try:
        outcome = run_job(superviseur, model, variant, contract, {"text": "x"})
        assert not outcome.ok
        assert "'audio'" in outcome.error and "exigée par le contrat" in outcome.error
        assert outcome.manifest["ok"] is False
    finally:
        superviseur.unload_all(force=True)


def test_le_second_job_retrouve_le_modele_chaud(parc, supervisor_factory):
    parc.capability().model()
    superviseur = supervisor_factory(parc, env_vars={"ECURIE_FAKE_WARMUP_MS": "300"})
    model, variant, contract = _pieces(superviseur)
    try:
        premier = run_job(superviseur, model, variant, contract, {"text": "x"})
        second = run_job(superviseur, model, variant, contract, {"text": "y"})
        assert not premier.reused and second.reused
        assert second.duration_ms < premier.duration_ms, "le warmup ne doit pas être repayé"
    finally:
        superviseur.unload_all(force=True)
