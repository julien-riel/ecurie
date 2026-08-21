"""Intégration réelle, hors CI : un job sur le vrai parc (CONCEPTION.md §12).

Ces tests sont exclus par défaut (`addopts = -m 'not real'`). Ils demandent une
machine Apple Silicon, un environnement de runtime synchronisé et les poids
téléchargés — et c'est justement ce qu'ils vérifient :

    ecurie env sync mlx-audio
    ecurie pull qwen3-tts-1.7b@8bit-mlx
    pytest -m real

Ils sautent avec un message qui dit quelle commande manque plutôt que d'échouer :
un test rouge parce qu'un modèle de deux gigaoctets n'est pas téléchargé
n'apprend rien à personne.

C'est le dernier maillon du critère de sortie du v0.3 : le reste de la suite
éprouve la mécanique avec un worker d'essai, ceci éprouve qu'un vrai modèle
tourne derrière la même mécanique.
"""

import threading
import wave
from pathlib import Path

import pytest
from ecurie_core.config import load_config
from ecurie_core.registry import load_registry
from ecurie_runtime.envs import EnvError, spec_for_variant
from ecurie_runtime.runner import run_job
from ecurie_runtime.supervisor import Supervisor, parse_ref
from ecurie_store.weights import WeightsMissing, resolve_weights

pytestmark = pytest.mark.real

REPO = Path(__file__).parents[3]
REF_TTS = "qwen3-tts-1.7b@8bit-mlx"


def _pret(ref: str):
    """Registre, variant et superviseur — ou un saut motivé, jamais un échec."""
    registre = load_registry(REPO)
    if registre.errors:
        pytest.skip(f"registre en erreur : {registre.errors[0].message}")
    try:
        model, variant, résolu = parse_ref(registre, ref)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(str(exc))

    config = load_config()
    try:
        spec_for_variant(REPO, variant, ref=résolu)
    except EnvError as exc:
        pytest.skip(str(exc))
    try:
        resolve_weights(config, variant, ref=résolu)
    except WeightsMissing as exc:
        pytest.skip(str(exc))

    contract = registre.capabilities[model.capability]
    return Supervisor(REPO, registre, config), model, variant, contract


def test_un_job_tts_reel_produit_un_wav_audible():
    superviseur, model, variant, contract = _pret(REF_TTS)
    try:
        outcome = run_job(
            superviseur,
            model,
            variant,
            contract,
            {"text": "Bonjour. L'écurie est prête, et le parc tient dans le budget."},
            seed=1,
        )
        assert outcome.ok, outcome.error
        wav = outcome.job_dir / "audio.wav"
        assert wav.is_file()
        with wave.open(str(wav)) as fichier:
            durée = fichier.getnframes() / fichier.getframerate()
        assert durée > 0.5, "un wav d'une demi-seconde ne contient pas cette phrase"
        assert outcome.manifest["revision"], "la révision épinglée doit être écrite au manifeste"
    finally:
        superviseur.unload_all(force=True)


def test_le_second_job_ne_repaie_pas_le_warmup():
    """La résidence est la raison d'être du protocole : elle doit se voir."""
    superviseur, model, variant, contract = _pret(REF_TTS)
    try:
        premier = run_job(superviseur, model, variant, contract, {"text": "Un."}, seed=1)
        second = run_job(superviseur, model, variant, contract, {"text": "Deux."}, seed=1)
        assert premier.ok and second.ok
        assert not premier.reused and second.reused
        assert second.duration_ms < premier.duration_ms
    finally:
        superviseur.unload_all(force=True)


def test_deux_jobs_concurrents_se_partagent_un_seul_worker():
    """La situation du serveur : un superviseur, deux jobs, un modèle (tâche 4.6).

    Le reste de la suite éprouve le tour de rôle avec un worker d'essai. Ici,
    c'est un vrai modèle MLX qui reçoit deux inférences venues de deux fils :
    le point est qu'il n'y en ait qu'un en mémoire, chargé une seule fois, et que
    les deux jobs aboutissent — un seul des deux paie le warmup.
    """
    superviseur, model, variant, contract = _pret(REF_TTS)
    résultats: dict[str, object] = {}

    def job(nom: str, texte: str) -> None:
        résultats[nom] = run_job(superviseur, model, variant, contract, {"text": texte}, seed=1)

    fils = [
        threading.Thread(target=job, args=(nom, texte))
        for nom, texte in (("a", "Premier job."), ("b", "Deuxième job, en même temps."))
    ]
    try:
        for fil in fils:
            fil.start()
        for fil in fils:
            fil.join(600)
        assert set(résultats) == {"a", "b"}, "les deux jobs doivent avoir rendu la main"
        for nom, outcome in résultats.items():
            assert outcome.ok, f"{nom} : {outcome.error}"
            assert (outcome.job_dir / "audio.wav").is_file()
        assert [e.ref for e in superviseur.residents()] == [REF_TTS]
        assert sum(1 for o in résultats.values() if o.reused) == 1, (
            "un seul des deux jobs paie le warmup ; l'autre retrouve le worker chaud"
        )
    finally:
        superviseur.unload_all(force=True)


def test_un_job_lourd_decharge_le_tts_sans_swap():
    """Le critère de sortie du v0.3, tel quel : le second modèle chasse le premier.

    Saute tant qu'un second variant mesuré n'est pas installé — l'éviction ne se
    vérifie qu'avec deux modèles qui ne tiennent pas ensemble dans le budget.

    Encore faut-il que le chasseur tienne **seul** dans le budget : depuis que le
    parc compte un profil paramétré, le premier lourd venu peut être une musique
    de trente secondes à 23,94 Gio, que rien n'admet jamais. Le prendre pour
    victime ferait échouer ce test sur un refus parfaitement correct.
    """
    superviseur, model, variant, contract = _pret(REF_TTS)
    lourds = [
        (m, v, pic)
        for m in superviseur.registry.models.values()
        for v in m.variants
        if (pic := superviseur.peak_bytes(v)) is not None
        and superviseur.config.heavy_threshold_bytes < pic <= superviseur.budget.bytes
        and f"{m.id}@{v.id}" != REF_TTS
    ]
    if not lourds:
        pytest.skip(
            "aucun second variant lourd mesuré qui tienne seul dans le budget — "
            "rien qui puisse faire chasser le TTS"
        )

    try:
        run_job(superviseur, model, variant, contract, {"text": "Trois."}, seed=1)
        assert [e.ref for e in superviseur.residents()] == [REF_TTS]

        autre_model, autre_variant, pic = lourds[0]
        décision = superviseur.simulate(f"{autre_model.id}@{autre_variant.id}", pic)
        assert décision.admitted, décision.reason
        assert REF_TTS in décision.evict
    finally:
        superviseur.unload_all(force=True)
