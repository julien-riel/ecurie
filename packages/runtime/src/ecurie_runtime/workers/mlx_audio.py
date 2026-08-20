"""Adaptateur mlx-audio : synthèse vocale sur Apple Silicon (CONCEPTION.md §5.2).

Sert aujourd'hui `qwen3-tts-1.7b@8bit-mlx`, un dépôt Qwen3-TTS de famille
« CustomVoice » dont la synthèse passe par `generate_custom_voice(speaker=…)` et
non par le `generate()` générique. L'adaptateur ne présume pourtant pas cette
famille : la méthode est choisie au chargement d'après le `config.json` livré
avec les poids, pour qu'un futur variant `base` ou `voice_design` n'oblige pas à
le réécrire.

Rien de mlx n'est importé au niveau du module. C'est ce qui permet à la CI
d'importer ce fichier sur une machine sans Apple Silicon, et à un environnement
mal synchronisé d'échouer sur un message qui nomme sa réparation plutôt que sur
un `ModuleNotFoundError` au démarrage du worker.

Ce qui suit a été établi en lisant le source de mlx-audio 0.5.0, jamais en
l'exécutant. Les points restés incertains sont signalés là où ils portent à
conséquence : la vitesse d'élocution, la découverte des timbres, la
compatibilité d'un dépôt converti par une version antérieure de mlx-audio.
"""

import gc
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecurie_runtime.workers.base import (
    InferRequest,
    InferResult,
    ProgressFn,
    Worker,
    WorkerError,
    main,
    peak_rss_bytes,
)

OUTPUT_NAME = "audio.wav"
AUTO_LANGUAGE = "auto"

# `ModelConfig.sample_rate` de Qwen3-TTS vaut 24 kHz et le config.json du dépôt
# ne porte pas la clé : le modèle chargé reste la source, ceci n'est qu'un filet.
DEFAULT_SAMPLE_RATE = 24_000

METHOD_CUSTOM_VOICE = "generate_custom_voice"
METHOD_GENERIC = "generate"

# Réglages d'échantillonnage portant le même nom dans les deux méthodes de
# Qwen3-TTS. Ils ne figurent dans aucun contrat de capacité : ils n'arrivent que
# si un manifeste ou un job les pose explicitement, et sinon on laisse les
# valeurs de mlx-audio, qui sont celles avec lesquelles le modèle a été réglé.
SAMPLING_KEYS = ("temperature", "max_tokens", "top_k", "top_p", "repetition_penalty")

# Dernier recours quand ni le modèle ni sa config ne savent se décrire : les neuf
# timbres de `talker_config.spk_id` du config.json de
# mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit, relevés le 2026-08-20. Une
# reconversion du dépôt ou un autre variant Qwen3-TTS périme cette liste — d'où
# le repli en dernier, et jamais pour un modèle d'une autre famille. Ne pas se
# fier aux README de mlx-audio, qui citent « Chelsie » et « Ethan », absents d'ici.
FALLBACK_VOICES = (
    "serena",
    "vivian",
    "uncle_fu",
    "ryan",
    "aiden",
    "ono_anna",
    "sohee",
    "eric",
    "dylan",
)

REPAIR = "ecurie env sync mlx-audio"


@dataclass(frozen=True)
class Runtime:
    """Les trois seuls points d'entrée de mlx-audio dont l'adaptateur dépend."""

    mx: Any
    load_model: Callable[..., Any]
    write: Callable[..., Any]


@dataclass(frozen=True)
class GenerationPlan:
    """Les arguments prêts pour la méthode de synthèse, et ce qui n'a pas pu être honoré.

    Un avertissement porte une clé en plus de son texte : le texte cite la valeur
    demandée et change donc d'un job à l'autre, la clé nomme le sujet et permet
    de ne le signaler qu'une fois par worker.
    """

    method: str
    kwargs: dict[str, Any]
    speed: float = 1.0
    warnings: tuple[tuple[str, str], ...] = ()

    @property
    def messages(self) -> list[str]:
        return [texte for _, texte in self.warnings]


@dataclass(frozen=True)
class MergedAudio:
    audio: Any
    segments: int = 0
    token_count: int = 0
    samples: int = 0


def import_runtime() -> Runtime:
    """Importe mlx-audio, ou explique comment réparer l'environnement.

    Volontairement à l'écart de `mlx_audio.tts.generate` : ce module tire le
    lecteur audio, donc `sounddevice` et PortAudio, dont un worker sans carte
    son n'a que faire, et sa fonction `generate_audio` nomme les fichiers
    elle-même — alors que le protocole exige des noms relatifs choisis ici.
    """
    try:
        import mlx.core as mx
        from mlx_audio.audio_io import write
        from mlx_audio.tts.utils import load_model
    except ImportError as exc:
        raise WorkerError(
            f"runtime mlx-audio indisponible dans cet environnement ({exc}) — "
            f"le reconstruire avec `{REPAIR}`"
        ) from exc
    return Runtime(mx=mx, load_model=load_model, write=write)


# --- découverte du modèle ----------------------------------------------------


def read_config(weights_path: Path) -> dict[str, Any]:
    """Le `config.json` livré avec les poids, ou un dict vide.

    C'est la seule description du modèle qui voyage avec lui, donc la seule qui
    ne mente pas sur ce dépôt-ci : la carte HF de ce variant est auto-générée et
    son exemple d'usage est faux pour la famille CustomVoice.
    """
    try:
        with open(weights_path / "config.json", encoding="utf-8") as fichier:
            config = json.load(fichier)
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def generation_method(model: Any, config: Mapping[str, Any]) -> str:
    """Choisit la méthode de synthèse d'après la famille déclarée par le dépôt.

    `generate_custom_voice` est définie sur la classe Qwen3-TTS quelle que soit
    la famille du dépôt : la détecter par `hasattr` ferait passer un modèle
    `base` ou `voice_design` par un chemin qui n'est pas le sien, avec un timbre
    qu'il n'a pas. C'est `tts_model_type` qui tranche, et `generate` — qui route
    lui-même selon la famille — sert de repli pour tout le reste de mlx-audio.
    """
    famille = str(config.get("tts_model_type") or "").lower()
    if famille == "custom_voice" and callable(getattr(model, METHOD_CUSTOM_VOICE, None)):
        return METHOD_CUSTOM_VOICE
    return METHOD_GENERIC


def announced_voices(model: Any, config: Mapping[str, Any]) -> list[str]:
    """Les timbres réellement disponibles, pour le `x-options-from: runtime.voices` du contrat."""
    voix = _liste_annoncee(model, "get_supported_speakers")
    if voix:
        return voix
    voix = [str(nom) for nom in _sous_config(config, "spk_id")]
    if voix:
        return voix
    # Le repli n'est légitime que pour la famille dont il vient : servir neuf
    # noms chinois à un Kokoro produirait une liste de voix inexistantes, donc
    # une UI qui propose des choix qui échoueront tous.
    if str(config.get("model_type") or "").lower() == "qwen3_tts":
        return list(FALLBACK_VOICES)
    return []


def announced_languages(model: Any, config: Mapping[str, Any]) -> list[str]:
    """Les langues acceptées, `auto` compris. Vide si le modèle n'en distingue pas."""
    langues = _liste_annoncee(model, "get_supported_languages")
    if langues:
        return langues
    codes = [str(nom) for nom in _sous_config(config, "codec_language_id") if "dialect" not in nom]
    return [AUTO_LANGUAGE, *codes] if codes else []


def _sous_config(config: Mapping[str, Any], clé: str) -> list[str]:
    talker = config.get("talker_config")
    table = talker.get(clé) if isinstance(talker, Mapping) else None
    return [str(nom) for nom in table] if isinstance(table, Mapping) else []


def _liste_annoncee(objet: Any, nom: str) -> list[str]:
    """Interroge une méthode de découverte du modèle sans en dépendre.

    Ces méthodes n'existent que sur certaines familles mlx-audio et lisent une
    config qui peut être partielle : leur échec dégrade la liste d'options, il
    ne doit jamais faire échouer un chargement par ailleurs valide.
    """
    methode = getattr(objet, nom, None)
    if not callable(methode):
        return []
    try:
        valeurs = methode()
    except Exception:  # noqa: BLE001 — source d'information, pas dépendance
        return []
    if not isinstance(valeurs, (list, tuple)):
        return []
    return [str(valeur) for valeur in valeurs if valeur]


# --- préparation d'une synthèse ----------------------------------------------


def plan_generation(
    *,
    entree: Mapping[str, Any],
    params: Mapping[str, Any],
    defaults: Mapping[str, Any],
    method: str,
    voices: Sequence[str] = (),
    language: str = AUTO_LANGUAGE,
) -> GenerationPlan:
    """Traduit une demande du protocole en arguments pour la méthode de synthèse.

    Fonction pure, et c'est délibéré : c'est ici que se joue tout ce qui peut
    être vérifié sans Apple Silicon — la priorité entre les trois couches de
    réglages, le renommage `voice` → `speaker` propre à CustomVoice, le refus
    d'un timbre inconnu avant d'avoir payé une seconde de calcul.

    `InferRequest.get` ne suffit pas : il fusionne l'entrée typée et les
    réglages du job, mais ignore les `defaults:` du manifeste, qui forment une
    troisième couche, la moins prioritaire.
    """
    couches = (entree, params, defaults)

    texte = _reglage("text", *couches)
    texte = str(texte).strip() if texte is not None else ""
    if not texte:
        raise WorkerError("aucun texte à synthétiser : le champ `text` est vide")

    voix = _reglage("voice", *couches)
    catalogue = {nom.lower(): nom for nom in voices}
    if voix is None and voices:
        # Le contrat de capacité ne rend pas `voice` obligatoire, mais un modèle
        # CustomVoice refuse de synthétiser sans timbre. Choisir le premier
        # annoncé vaut mieux que remonter la ValueError de la bibliothèque.
        voix = voices[0]
    if voix is not None and catalogue and str(voix).lower() not in catalogue:
        disponibles = ", ".join(voices)
        raise WorkerError(f"voix inconnue : {voix!r} — disponibles : {disponibles}")

    langue = _reglage("language", *couches) or language or AUTO_LANGUAGE

    avertissements: list[tuple[str, str]] = []
    vitesse = _vitesse(_reglage("speed", *couches))
    if abs(vitesse - 1.0) > 1e-6:
        # Vérifié dans le source de mlx-audio : `generate_custom_voice` n'a
        # aucun paramètre de vitesse, et `generate` documente le sien comme
        # « not directly supported yet » pour Qwen3-TTS. Rééchantillonner nous-
        # mêmes donnerait une voix accélérée au prix du timbre — ce n'est pas ce
        # qui a été demandé, et ça se remarquerait davantage que le refus.
        avertissements.append(
            (
                "speed",
                f"speed={vitesse:g} ignoré : Qwen3-TTS n'expose aucun contrôle de vitesse "
                "et Écurie ne rééchantillonne pas — l'audio sort à la vitesse nominale",
            )
        )

    if method == METHOD_CUSTOM_VOICE:
        if not voix:
            # Ne survient que si le modèle et sa config se taisent tous les deux.
            # Laisser passer donnerait la même panne trois secondes plus tard,
            # sous la forme d'une ValueError de mlx-audio qui ne dit pas d'où
            # elle vient.
            raise WorkerError(
                "aucun timbre : ce modèle CustomVoice en exige un, et aucun n'a été annoncé "
                "au chargement — vérifier le `config.json` livré avec les poids"
            )
        kwargs: dict[str, Any] = {"text": texte, "speaker": voix, "language": langue}
    else:
        # `generate` renomme les deux mêmes notions : `voice` et `lang_code`.
        kwargs = {"text": texte, "voice": voix, "lang_code": langue}

    instruct = _reglage("instruct", *couches)
    if instruct:
        kwargs["instruct"] = str(instruct)
    for nom in SAMPLING_KEYS:
        valeur = _reglage(nom, *couches)
        if valeur is not None:
            kwargs[nom] = valeur

    return GenerationPlan(
        method=method,
        kwargs=kwargs,
        speed=vitesse,
        warnings=tuple(avertissements),
    )


def _reglage(nom: str, *couches: Mapping[str, Any]) -> Any:
    """Première valeur définie, de la plus prioritaire à la moins : entrée, job, manifeste."""
    for couche in couches:
        valeur = couche.get(nom)
        if valeur is not None:
            return valeur
    return None


def _vitesse(brut: Any) -> float:
    if brut is None:
        return 1.0
    try:
        return float(brut)
    except (TypeError, ValueError) as exc:
        raise WorkerError(f"speed doit être un nombre, reçu {brut!r}") from exc


def merge_segments(
    segments: Sequence[Any], concat: Callable[[list[Any]], Any]
) -> MergedAudio:
    """Recolle les `GenerationResult` en un seul signal, et additionne leurs compteurs.

    Le générateur ne dit pas à l'avance combien de segments il rendra : un texte
    court en donne un, un texte à plusieurs lignes peut en donner autant. Le cas
    à un segment court-circuite la concaténation, qui coûterait une copie de
    tout l'audio pour rien.
    """
    if not segments:
        raise WorkerError("le modèle n'a produit aucun segment audio")
    audio = segments[0].audio if len(segments) == 1 else concat([s.audio for s in segments])
    return MergedAudio(
        audio=audio,
        segments=len(segments),
        token_count=sum(_entier(s, "token_count") for s in segments),
        samples=sum(_entier(s, "samples") for s in segments),
    )


def _entier(segment: Any, nom: str) -> int:
    valeur = getattr(segment, nom, 0)
    try:
        return int(valeur or 0)
    except (TypeError, ValueError):
        return 0


# --- le worker ---------------------------------------------------------------


class MlxAudioWorker(Worker):
    """Synthèse vocale mlx-audio : un modèle chargé, une méthode, un wav par job."""

    name = "mlx-audio"

    def __init__(self) -> None:
        self._runtime: Runtime | None = None
        self._model: Any = None
        self._method = METHOD_GENERIC
        self._voices: list[str] = []
        self._language = AUTO_LANGUAGE
        self._sample_rate = DEFAULT_SAMPLE_RATE
        self._defaults: dict[str, Any] = {}
        self._peak_load = 0
        self._warned: set[str] = set()

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        runtime = import_runtime()
        chemin = Path(str(variant.get("weights_path") or ""))
        if not chemin.is_dir():
            raise WorkerError(
                f"poids introuvables : {chemin} — le superviseur transmet un chemin local "
                "déjà vérifié, un worker ne télécharge jamais"
            )

        self._defaults = dict(variant.get("defaults") or {})
        config = read_config(chemin)

        # `load_model` ne va sur le réseau que si le chemin n'existe pas ; on lui
        # en donne un qui existe, et `HF_HUB_OFFLINE=1` ferme la porte de toute
        # façon (envs.py). Les deux protections se recouvrent volontairement.
        model = runtime.load_model(chemin)

        self._runtime = runtime
        self._model = model
        self._method = generation_method(model, config)
        self._voices = announced_voices(model, config)
        self._sample_rate = int(getattr(model, "sample_rate", None) or DEFAULT_SAMPLE_RATE)
        # Le contrat text-to-speech n'a pas de champ de langue, et il n'en aura
        # pas : toutes les synthèses vocales ne distinguent pas les langues, or un
        # contrat ne déclare que le dénominateur commun d'une capacité. C'est le
        # rôle d'`options:` au manifeste (réglages propres au runtime, transmis
        # tels quels en `params`), que `defaults:` ne peut pas tenir puisqu'il est
        # croisé avec le contrat. Les deux sont lus, `options` d'abord.
        options = dict(variant.get("options") or {})
        self._language = str(options.get("language") or self._defaults.get("language")
                             or AUTO_LANGUAGE)
        self._peak_load = self._pic_mlx() or 0

        if not self._voices:
            self._avertir(
                "voix",
                "aucun timbre annoncé par le modèle : l'UI n'aura pas de liste de voix",
            )
        return {
            "voices": self._voices,
            "languages": announced_languages(model, config),
            "language": self._language,
            "sample_rate": self._sample_rate,
        }

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._runtime is None or self._model is None:
            raise WorkerError("modèle non chargé")
        runtime = self._runtime

        plan = plan_generation(
            entree=request.input,
            params=request.params,
            defaults=self._defaults,
            method=self._method,
            voices=self._voices,
            language=self._language,
        )
        for clé, message in plan.warnings:
            self._avertir(clé, message)
        progress(5, "préparation")

        # Remis à zéro par job : `mx.get_peak_memory()` compte depuis le début du
        # processus, sinon le pic du chargement masquerait celui de la synthèse.
        runtime.mx.reset_peak_memory()
        if request.seed is not None:
            # La génération n'expose pas de graine ; semer le générateur MLX est
            # le seul levier, et il ne couvre que l'échantillonnage des tokens.
            runtime.mx.random.seed(int(request.seed))

        progress(15, "synthèse")
        debut = time.monotonic()
        flux = getattr(self._model, plan.method)(**plan.kwargs)
        # Qwen3-TTS rend un générateur ; une autre famille mlx-audio pourrait
        # rendre un résultat seul. Un `hasattr` évite un TypeError illisible au
        # premier variant qui ne sera pas celui-ci.
        if hasattr(flux, "audio"):
            flux = [flux]
        segments = []
        for segment in flux:
            segments.append(segment)
            progress(min(85, 15 + 15 * len(segments)), f"segment {len(segments)}")
        fusion = merge_segments(segments, lambda parts: runtime.mx.concatenate(parts, axis=0))
        calcul = time.monotonic() - debut

        progress(90, "écriture du wav")
        chemin = request.output_dir / OUTPUT_NAME
        runtime.write(str(chemin), fusion.audio, self._sample_rate, format="wav")

        return InferResult(
            output={"audio": OUTPUT_NAME},
            metrics=self._metriques(fusion, calcul, plan),
        )

    def unload(self) -> None:
        self._model = None
        self._peak_load = 0  # plus de modèle résident : plus de plancher à défendre
        # L'ordre compte : tant qu'une référence Python tient les tableaux, leurs
        # buffers ne sont que « cachés » et `clear_cache` ne rend rien au système.
        gc.collect()
        if self._runtime is not None:
            self._runtime.mx.clear_cache()

    def peak_memory_bytes(self) -> int | None:
        """Pic MLX en octets — la mesure juste ici, bien plus que le RSS du processus.

        Le plancher du chargement est conservé parce que `reset_peak_memory()`
        est appelé à chaque job : sans lui, un job très court rapporterait un pic
        inférieur au poids résident du modèle, et le contrôle d'admission
        laisserait entrer un second résident que la mémoire ne peut pas tenir.
        """
        pic = self._pic_mlx()
        if pic is None:
            return peak_rss_bytes()
        return max(pic, self._peak_load)

    def _pic_mlx(self) -> int | None:
        if self._runtime is None:
            return None
        try:
            return int(self._runtime.mx.get_peak_memory())
        except Exception:  # noqa: BLE001 — une mesure ratée ne doit pas faire échouer un job
            return None

    def _metriques(
        self, fusion: MergedAudio, calcul: float, plan: GenerationPlan
    ) -> dict[str, Any]:
        secondes = fusion.samples / self._sample_rate if fusion.samples else None
        metriques: dict[str, Any] = {
            "sample_rate": self._sample_rate,
            "token_count": fusion.token_count,
            "segments": fusion.segments,
        }
        if secondes:
            metriques["audio_seconds"] = round(secondes, 3)
            # Calculé ici plutôt que repris du `real_time_factor` de mlx-audio :
            # leur convention est l'inverse de celle d'Écurie (ils annoncent
            # « 1,67× temps réel », on note le temps de calcul par seconde
            # produite). Deux conventions dans le même champ rendraient tout
            # profil de banc d'essai ininterprétable.
            metriques["rtf"] = round(calcul / secondes, 4)
        if abs(plan.speed - 1.0) > 1e-6:
            metriques["speed_requested"] = plan.speed
            metriques["speed_applied"] = 1.0
        if plan.warnings:
            metriques["warnings"] = plan.messages
        return metriques

    def _avertir(self, clé: str, message: str) -> None:
        """Une fois par worker et par sujet : répété à chaque job, un avertissement
        devient du bruit et cesse d'être lu."""
        if clé in self._warned:
            return
        self._warned.add(clé)
        print(f"[mlx-audio] {message}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main(MlxAudioWorker))
