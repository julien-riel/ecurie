"""Adaptateur mlx-audio, éprouvé sans mlx (CONCEPTION.md §5.2).

Ces tests tournent en CI sur une machine sans Apple Silicon, sans mlx et sans
poids. Ils ne prouvent donc rien de la qualité de la synthèse — ils prouvent
tout le reste, qui est précisément ce qui casse en silence : la paresse des
imports, la traduction d'une demande du protocole en arguments de génération, le
recollage des segments, et le message qu'on lit quand l'environnement manque.

Le modèle, le module `mlx.core` et l'écriture WAV sont des doublures. Elles
n'imitent que ce que la reconnaissance a relevé dans le source de mlx-audio
0.5.0 : un générateur de `GenerationResult`, `write(fichier, audio, sr, format=)`,
`get_peak_memory()` en octets.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from ecurie_runtime.workers import mlx_audio
from ecurie_runtime.workers.base import InferRequest, WorkerError
from ecurie_runtime.workers.mlx_audio import (
    AUTO_LANGUAGE,
    FALLBACK_VOICES,
    METHOD_CUSTOM_VOICE,
    METHOD_GENERIC,
    OUTPUT_NAME,
    MlxAudioWorker,
    Runtime,
    announced_languages,
    announced_voices,
    generation_method,
    import_runtime,
    merge_segments,
    plan_generation,
    read_config,
)

VOIX = ["serena", "vivian", "ryan"]

# Extrait fidèle du config.json de mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit :
# c'est ce document, livré avec les poids, qui décide de la famille et des timbres.
CONFIG_CUSTOM_VOICE = {
    "model_type": "qwen3_tts",
    "tts_model_type": "custom_voice",
    "talker_config": {
        "spk_id": {"serena": 3066, "vivian": 3065, "ryan": 3061},
        "codec_language_id": {
            "french": 2061,
            "english": 2050,
            "beijing_dialect": 2074,
        },
    },
}


# --- doublures ---------------------------------------------------------------


class FauxSegment:
    """Ce qu'un `GenerationResult` de mlx-audio porte et dont l'adaptateur se sert."""

    def __init__(self, audio: str, samples: int = 24_000, token_count: int = 40) -> None:
        self.audio = audio
        self.samples = samples
        self.token_count = token_count
        self.sample_rate = 24_000
        # mlx-audio expose aussi `real_time_factor`, en convention inverse de
        # celle d'Écurie. Il est ici pour que le test échoue si l'adaptateur se
        # met un jour à le recopier au lieu de calculer le sien.
        self.real_time_factor = 1.67


class FauxAleatoire:
    def __init__(self) -> None:
        self.graines: list[int] = []

    def seed(self, valeur: int) -> None:
        self.graines.append(valeur)


class FauxMx:
    """`mlx.core` réduit aux quatre appels de l'adaptateur."""

    def __init__(self, pic: int = 4_000_000_000) -> None:
        self.pic = pic
        self.remises_a_zero = 0
        self.caches_vides = 0
        self.random = FauxAleatoire()

    def reset_peak_memory(self) -> None:
        self.remises_a_zero += 1

    def get_peak_memory(self) -> int:
        return self.pic

    def clear_cache(self) -> None:
        self.caches_vides += 1

    def concatenate(self, parts: list[Any], axis: int = 0) -> str:
        return "+".join(parts)


class FauxModele:
    def __init__(self, *, voix: list[str] | None = None, segments: int = 1) -> None:
        self.sample_rate = 24_000
        self._voix = voix
        self._segments = segments
        self.appels: list[dict[str, Any]] = []

    def get_supported_speakers(self) -> list[str]:
        if self._voix is None:
            raise RuntimeError("config partielle")
        return list(self._voix)

    def generate_custom_voice(self, **kwargs: Any):
        self.appels.append(kwargs)
        for i in range(self._segments):
            yield FauxSegment(f"pcm{i}")


class FauxEcriture:
    def __init__(self) -> None:
        self.appels: list[tuple] = []

    def __call__(self, fichier, audio, samplerate, format=None) -> None:
        self.appels.append((fichier, audio, samplerate, format))
        Path(fichier).write_bytes(b"RIFF")


@dataclass
class Banc:
    """Un worker et les doublures que son runtime lui a fournies."""

    worker: MlxAudioWorker
    modele: FauxModele
    mx: FauxMx
    ecriture: FauxEcriture


@pytest.fixture
def poids(tmp_path: Path) -> Path:
    dossier = tmp_path / "snapshot"
    dossier.mkdir()
    (dossier / "config.json").write_text(json.dumps(CONFIG_CUSTOM_VOICE), encoding="utf-8")
    return dossier


@pytest.fixture
def banc(monkeypatch: pytest.MonkeyPatch) -> Banc:
    """Un worker dont le runtime mlx-audio est une doublure, montée avant `load`."""
    modele = FauxModele(voix=VOIX)
    mx = FauxMx()
    ecriture = FauxEcriture()
    monkeypatch.setattr(
        mlx_audio,
        "import_runtime",
        lambda: Runtime(mx=mx, load_model=lambda chemin: modele, write=ecriture),
    )
    return Banc(worker=MlxAudioWorker(), modele=modele, mx=mx, ecriture=ecriture)


def _requete(tmp_path: Path, **entree: Any) -> InferRequest:
    sortie = tmp_path / "job"
    sortie.mkdir(exist_ok=True)
    return InferRequest(job_id="j1", input=entree, params={}, output_dir=sortie)


# --- paresse des imports -----------------------------------------------------


def test_le_module_s_importe_sans_mlx() -> None:
    """La CI importe cet adaptateur sur une machine qui n'a ni mlx ni Apple Silicon.

    Un `import mlx` remonté au niveau du module casserait la collecte de toute la
    suite de tests, et pas seulement de ce fichier.
    """
    assert "mlx" not in sys.modules
    assert "mlx_audio" not in sys.modules


def test_env_absent_nomme_sa_reparation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le message doit porter la commande, pas seulement le nom du module manquant."""
    for nom in ("mlx", "mlx.core", "mlx_audio", "mlx_audio.tts", "mlx_audio.audio_io"):
        monkeypatch.setitem(sys.modules, nom, None)

    with pytest.raises(WorkerError) as capture:
        import_runtime()
    assert "ecurie env sync mlx-audio" in str(capture.value)


# --- choix de la méthode de génération ---------------------------------------


def test_custom_voice_prend_la_methode_dediee() -> None:
    assert generation_method(FauxModele(), CONFIG_CUSTOM_VOICE) == METHOD_CUSTOM_VOICE


def test_une_autre_famille_passe_par_generate() -> None:
    """`generate_custom_voice` existe sur la classe Qwen3-TTS même pour un dépôt `base`.

    Si l'adaptateur la détectait par `hasattr`, ce cas partirait sur un chemin
    qui exige un timbre que le dépôt n'a pas.
    """
    config = {**CONFIG_CUSTOM_VOICE, "tts_model_type": "base"}
    assert generation_method(FauxModele(), config) == METHOD_GENERIC


def test_modele_sans_methode_dediee_passe_par_generate() -> None:
    class Kokoro:
        pass

    assert generation_method(Kokoro(), {}) == METHOD_GENERIC


# --- découverte des voix et des langues --------------------------------------


def test_le_modele_prime_sur_le_config() -> None:
    assert announced_voices(FauxModele(voix=["ryan"]), CONFIG_CUSTOM_VOICE) == ["ryan"]


def test_le_config_prend_le_relais_si_le_modele_se_tait() -> None:
    """`get_supported_speakers` lève quand la config est partielle : ce n'est pas fatal."""
    assert announced_voices(FauxModele(voix=None), CONFIG_CUSTOM_VOICE) == VOIX


def test_le_repli_code_en_dur_ne_sert_qu_a_qwen3() -> None:
    qwen = {"model_type": "qwen3_tts"}
    assert announced_voices(FauxModele(voix=None), qwen) == list(FALLBACK_VOICES)
    # Servir neuf timbres Qwen3 à une autre famille remplirait l'UI de choix
    # qui échoueraient tous.
    assert announced_voices(FauxModele(voix=None), {"model_type": "kokoro"}) == []


def test_les_dialectes_sont_ecartes_des_langues() -> None:
    langues = announced_languages(FauxModele(voix=None), CONFIG_CUSTOM_VOICE)
    assert langues == [AUTO_LANGUAGE, "french", "english"]


def test_config_illisible_ne_fait_pas_echouer(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{ pas du json", encoding="utf-8")
    assert read_config(tmp_path) == {}
    assert read_config(tmp_path / "nulle-part") == {}


# --- préparation des arguments de génération ---------------------------------


def _plan(method: str = METHOD_CUSTOM_VOICE, **couches: Any):
    return plan_generation(
        entree=couches.get("entree", {}),
        params=couches.get("params", {}),
        defaults=couches.get("defaults", {}),
        method=method,
        voices=couches.get("voices", VOIX),
        language=couches.get("language", AUTO_LANGUAGE),
    )


def test_custom_voice_renomme_voice_en_speaker() -> None:
    plan = _plan(entree={"text": "Bonjour", "voice": "Vivian"})
    assert plan.kwargs == {"text": "Bonjour", "speaker": "Vivian", "language": AUTO_LANGUAGE}


def test_generate_utilise_voice_et_lang_code() -> None:
    plan = _plan(METHOD_GENERIC, entree={"text": "Bonjour"}, language="french")
    assert plan.kwargs == {"text": "Bonjour", "voice": "serena", "lang_code": "french"}


def test_l_entree_prime_sur_le_job_qui_prime_sur_le_manifeste() -> None:
    plan = _plan(
        entree={"text": "Bonjour", "voice": "ryan"},
        params={"voice": "vivian"},
        defaults={"voice": "serena"},
    )
    assert plan.kwargs["speaker"] == "ryan"

    plan = _plan(
        entree={"text": "Bonjour"}, params={"voice": "vivian"}, defaults={"voice": "serena"}
    )
    assert plan.kwargs["speaker"] == "vivian"

    plan = _plan(entree={"text": "Bonjour"}, defaults={"voice": "serena"})
    assert plan.kwargs["speaker"] == "serena"


def test_sans_voix_demandee_le_premier_timbre_annonce() -> None:
    """Le contrat ne rend pas `voice` obligatoire ; un CustomVoice, si."""
    assert _plan(entree={"text": "Bonjour"}).kwargs["speaker"] == "serena"


def test_custom_voice_sans_aucun_timbre_echoue_avant_de_calculer() -> None:
    """La même panne, mais trois secondes plus tôt et en nommant sa cause."""
    with pytest.raises(WorkerError) as capture:
        _plan(entree={"text": "Bonjour"}, voices=[])
    assert "config.json" in str(capture.value)


def test_voix_inconnue_refusee_avec_la_liste() -> None:
    with pytest.raises(WorkerError) as capture:
        _plan(entree={"text": "Bonjour", "voice": "chelsie"})
    message = str(capture.value)
    assert "chelsie" in message
    assert "serena, vivian, ryan" in message


def test_la_casse_du_timbre_est_indifferente() -> None:
    """mlx-audio compare en minuscules ; refuser « Serena » serait un refus inventé."""
    assert _plan(entree={"text": "Bonjour", "voice": "SERENA"}).kwargs["speaker"] == "SERENA"


def test_langue_du_manifeste_sinon_auto() -> None:
    assert _plan(entree={"text": "Bonjour"}, defaults={"language": "french"}).kwargs[
        "language"
    ] == "french"
    assert _plan(entree={"text": "Bonjour"}).kwargs["language"] == AUTO_LANGUAGE


def test_texte_vide_refuse() -> None:
    with pytest.raises(WorkerError):
        _plan(entree={"text": "   "})
    with pytest.raises(WorkerError):
        _plan(entree={})


def test_les_reglages_d_echantillonnage_ne_passent_que_s_ils_sont_poses() -> None:
    """Sans consigne, on laisse les valeurs avec lesquelles le modèle a été réglé."""
    assert "temperature" not in _plan(entree={"text": "Bonjour"}).kwargs
    plan = _plan(entree={"text": "Bonjour"}, params={"temperature": 0.6, "max_tokens": 512})
    assert plan.kwargs["temperature"] == 0.6
    assert plan.kwargs["max_tokens"] == 512


def test_instruct_transmis_quand_il_est_demande() -> None:
    plan = _plan(entree={"text": "Bonjour"}, params={"instruct": "Très joyeux."})
    assert plan.kwargs["instruct"] == "Très joyeux."


def test_speed_est_signale_jamais_fabrique() -> None:
    """Qwen3-TTS n'a pas de contrôle de vitesse : ni kwarg inventé, ni rééchantillonnage."""
    plan = _plan(entree={"text": "Bonjour", "speed": 1.5})
    assert "speed" not in plan.kwargs
    assert plan.speed == 1.5
    assert [clé for clé, _ in plan.warnings] == ["speed"]
    assert "vitesse" in plan.messages[0]


def test_speed_nominale_ne_declenche_rien() -> None:
    assert _plan(entree={"text": "Bonjour"}, defaults={"speed": 1.0}).warnings == ()


def test_speed_non_numerique_refusee() -> None:
    with pytest.raises(WorkerError):
        _plan(entree={"text": "Bonjour", "speed": "vite"})


# --- recollage des segments --------------------------------------------------


def test_un_seul_segment_ne_declenche_aucune_copie() -> None:
    def interdit(_parts):
        raise AssertionError("concaténation inutile pour un segment unique")

    fusion = merge_segments([FauxSegment("pcm0")], interdit)
    assert fusion.audio == "pcm0"
    assert fusion.segments == 1


def test_plusieurs_segments_sont_concatenes_et_leurs_compteurs_additionnes() -> None:
    segments = [
        FauxSegment("a", samples=12_000, token_count=10),
        FauxSegment("b", samples=24_000, token_count=20),
        FauxSegment("c", samples=6_000, token_count=5),
    ]
    fusion = merge_segments(segments, lambda parts: "+".join(parts))
    assert fusion.audio == "a+b+c"
    assert fusion.segments == 3
    assert fusion.samples == 42_000
    assert fusion.token_count == 35


def test_aucun_segment_est_une_erreur_explicite() -> None:
    with pytest.raises(WorkerError):
        merge_segments([], lambda parts: parts)


# --- le worker de bout en bout, sur doublures --------------------------------


def test_load_annonce_les_voix_du_modele(banc: Banc, poids: Path) -> None:
    options = banc.worker.load({"weights_path": str(poids), "defaults": {"language": "french"}})
    assert options["voices"] == VOIX
    assert options["languages"] == [AUTO_LANGUAGE, "french", "english"]
    assert options["language"] == "french"
    assert options["sample_rate"] == 24_000


def test_load_refuse_un_chemin_de_poids_absent(banc: Banc, tmp_path: Path) -> None:
    """Le superviseur transmet un chemin déjà vérifié ; un worker ne télécharge rien."""
    with pytest.raises(WorkerError) as capture:
        banc.worker.load({"weights_path": str(tmp_path / "nulle-part")})
    assert "nulle-part" in str(capture.value)


def test_infer_ecrit_un_wav_et_rend_un_nom_relatif(
    banc: Banc, poids: Path, tmp_path: Path
) -> None:
    banc.worker.load({"weights_path": str(poids)})
    étapes: list[tuple[int, str]] = []

    requête = _requete(tmp_path, text="Bonjour", voice="ryan", speed=1.5)
    requête.seed = 42
    résultat = banc.worker.infer(requête, lambda pct, note="": étapes.append((pct, note)))

    assert résultat.output == {"audio": OUTPUT_NAME}
    assert (requête.output_dir / OUTPUT_NAME).exists()
    fichier, audio, samplerate, format_ = banc.ecriture.appels[0]
    assert Path(fichier).name == OUTPUT_NAME
    assert (audio, samplerate, format_) == ("pcm0", 24_000, "wav")
    assert banc.modele.appels[0]["speaker"] == "ryan"
    assert banc.mx.remises_a_zero == 1
    assert banc.mx.random.graines == [42]
    assert étapes and étapes[-1][0] == 90


def test_les_metriques_portent_ce_que_le_banc_d_essai_lira(
    banc: Banc, poids: Path, tmp_path: Path
) -> None:
    banc.worker.load({"weights_path": str(poids)})
    requête = _requete(tmp_path, text="Bonjour", speed=1.5)
    métriques = banc.worker.infer(requête, lambda *a: None).metrics

    assert métriques["audio_seconds"] == 1.0  # 24 000 échantillons à 24 kHz
    assert métriques["token_count"] == 40
    assert métriques["sample_rate"] == 24_000
    # Convention d'Écurie : temps de calcul par seconde produite, donc très
    # au-dessous de 1 pour du TTS — surtout pas le « 1,67× » de mlx-audio.
    assert 0 <= métriques["rtf"] < 1
    assert métriques["speed_requested"] == 1.5
    assert métriques["speed_applied"] == 1.0
    assert métriques["warnings"]


def test_le_pic_memoire_ne_descend_jamais_sous_le_poids_du_modele(
    banc: Banc, poids: Path
) -> None:
    """`reset_peak_memory()` par job ne doit pas faire oublier le modèle résident.

    Sans ce plancher, le contrôle d'admission croirait le variant bien plus léger
    qu'il n'est et laisserait entrer un second résident.
    """
    banc.mx.pic = 4_000_000_000
    banc.worker.load({"weights_path": str(poids)})

    banc.mx.pic = 100_000_000
    assert banc.worker.peak_memory_bytes() == 4_000_000_000
    banc.mx.pic = 5_000_000_000
    assert banc.worker.peak_memory_bytes() == 5_000_000_000


def test_unload_rend_le_cache_mlx(banc: Banc, poids: Path) -> None:
    banc.worker.load({"weights_path": str(poids)})
    banc.worker.unload()
    assert banc.mx.caches_vides == 1


def test_infer_avant_load_est_une_erreur_lisible(tmp_path: Path) -> None:
    with pytest.raises(WorkerError):
        MlxAudioWorker().infer(_requete(tmp_path, text="Bonjour"), lambda *a: None)
