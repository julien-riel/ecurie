"""Adaptateur mlx-audio, chemin **synthèse à voix de référence**.

Cinquième adaptateur du runtime `mlx-audio`, et le second à faire parler — mais
pas la même capacité que `mlx_audio`, qui sert `text-to-speech`. La distinction
tient en une phrase : le titulaire de la synthèse choisit parmi les voix qu'il
embarque, celui-ci imite une voix qu'on lui donne. Un modèle ne peut pas
remplacer l'autre derrière le même contrat, donc deux contrats.

**La référence est bornée par le modèle, pas par nous.** `ref_audio_max_duration_s`
vaut dix secondes en amont : au-delà, l'échantillon est tronqué sans qu'un mot
soit dit. La sortie `reference_seconds` rapporte ce qui a réellement servi —
c'est la première chose à regarder quand une imitation déçoit.

**La transcription de l'échantillon n'est pas décorative.** Ces modèles alignent
le texte de référence sur son audio pour en tirer la prosodie ; une transcription
approximative déforme plus sûrement qu'une absence de transcription, et le
contrat le dit. Vide, le modèle se débrouille.

**La sortie arrive par morceaux.** `generate` rend un générateur de segments, un
par phrase environ : les concaténer est le travail de l'adaptateur. La durée se
compte alors en échantillons — le champ `audio_duration` des segments est un
horodatage formaté, et le croire flottant a fait échouer les trois cas du
premier banc.
"""

import gc
import time
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

ENV_NAME = "mlx-audio"
REPAIR = f"ecurie env sync {ENV_NAME}"
OUTPUT_AUDIO = "audio.wav"


def _import_runtime() -> tuple[Any, Any, Any]:
    try:
        import mlx.core as mx
        from mlx_audio.audio_io import write
        from mlx_audio.tts.utils import load_model
    except ImportError as exc:
        raise WorkerError(
            f"runtime mlx-audio indisponible dans cet environnement ({exc}) — "
            f"le reconstruire avec `{REPAIR}`"
        ) from exc
    return mx, load_model, write


class OmniVoiceWorker(Worker):
    """Synthèse vocale guidée par un échantillon de voix."""

    name = "omnivoice"

    def __init__(self) -> None:
        self.mx: Any = None
        self.write: Any = None
        self.model: Any = None
        self.defaults: dict[str, Any] = {}
        self._peak_load = 0

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        mx, load_model, write = _import_runtime()
        self.mx = mx
        self.write = write
        self.defaults = dict(variant.get("defaults") or {})

        brut = str(variant.get("weights_path") or "").strip()
        ref = variant.get("ref") or "<ref>"
        if not brut or not Path(brut).is_dir():
            raise WorkerError(
                f"poids absents : {brut or '(chemin vide)'} n'est pas un dossier — "
                f"télécharger avec : ecurie pull {ref}"
            )
        try:
            self.model = load_model(Path(brut))
        except Exception as exc:  # noqa: BLE001 — remonte avec la réparation
            raise WorkerError(f"chargement impossible : {type(exc).__name__}: {exc}") from exc
        self._peak_load = self._pic() or 0
        # Pas de `voices` ici, et c'est le point : cette capacité n'en a pas de
        # préenregistrée. La liste des langues reste ouverte, comme ailleurs.
        return {"languages": [], "versions": self._versions()}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.model is None:
            raise WorkerError("infer avant load — aucun modèle en mémoire")

        texte = str(self._reglage(request, "text", "") or "").strip()
        if not texte:
            raise WorkerError("« text » est obligatoire : le contrat le déclare requis")
        référence = self._fichier(request, "reference_audio")
        ref_texte = str(self._reglage(request, "reference_text", "") or "").strip() or None
        langue = str(self._reglage(request, "language", "") or "").strip() or "None"
        vitesse = float(self._reglage(request, "speed", 1.0))

        self.mx.reset_peak_memory()
        if request.seed is not None:
            self.mx.random.seed(int(request.seed))

        progress(15, "synthèse en cours")
        début = time.monotonic()
        try:
            segments = list(
                self.model.generate(
                    text=texte,
                    ref_audio=str(référence),
                    ref_text=ref_texte,
                    language=langue,
                    speed=vitesse,
                )
            )
        except TypeError:
            # `speed` n'est pas accepté par toutes les versions du modèle : on
            # réessaie sans lui plutôt que de faire échouer un job pour un
            # réglage de confort, et le caveat du manifeste le dit.
            segments = list(
                self.model.generate(
                    text=texte,
                    ref_audio=str(référence),
                    ref_text=ref_texte,
                    language=langue,
                )
            )
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte
            raise WorkerError(f"synthèse impossible : {type(exc).__name__}: {exc}") from exc
        calcul = time.monotonic() - début

        if not segments:
            raise WorkerError("le modèle n'a produit aucun segment audio")

        progress(88, "assemblage")
        fréquence = int(getattr(segments[0], "sample_rate", 24_000) or 24_000)
        morceaux = [s.audio for s in segments if getattr(s, "audio", None) is not None]
        if not morceaux:
            raise WorkerError("les segments produits ne portent aucun signal")
        signal = morceaux[0] if len(morceaux) == 1 else self.mx.concatenate(morceaux, axis=0)
        # La durée se compte en échantillons, pas en lisant `audio_duration` :
        # ce champ est un horodatage formaté (« 00:00:02.759 »), et le premier
        # essai est mort dessus sur les trois cas de la charge type. Le signal,
        # lui, ne ment pas et ne change pas de format d'une version à l'autre.
        durée = float(signal.shape[0]) / fréquence

        self.write(str(request.output_dir / OUTPUT_AUDIO), signal, fréquence, format="wav")

        return InferResult(
            output={
                "audio": OUTPUT_AUDIO,
                "duration_seconds": round(durée, 3),
                "reference_seconds": round(self._duree_reference(référence), 3),
            },
            metrics={
                "segments": len(segments),
                "sample_rate": fréquence,
                "characters": len(texte),
                "rtf": round(calcul / durée, 4) if durée > 0 else None,
                "infer_ms": int(calcul * 1000),
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def unload(self) -> None:
        self.model = None
        self._peak_load = 0
        gc.collect()
        if self.mx is not None:
            self.mx.clear_cache()

    def peak_memory_bytes(self) -> int | None:
        pic = self._pic()
        if pic is None:
            return peak_rss_bytes()
        return max(pic, self._peak_load)

    # --- détails -------------------------------------------------------------

    def _duree_reference(self, chemin: Path) -> float:
        """Ce que le modèle a pu entendre de l'échantillon, borné comme lui.

        La borne amont est de dix secondes. On rapporte le minimum des deux
        plutôt que la durée du fichier : dire « référence de 40 s » quand le
        modèle n'en a écouté que dix expliquerait mal une imitation qui déçoit.
        """
        try:
            import wave

            with wave.open(str(chemin)) as fichier:
                brute = fichier.getnframes() / float(fichier.getframerate())
        except Exception:  # noqa: BLE001 — un format que `wave` ne lit pas n'est pas une panne
            return 0.0
        return min(brute, 10.0)

    def _fichier(self, request: InferRequest, nom: str) -> Path:
        brut = str(self._reglage(request, nom, "") or "").strip()
        if not brut:
            raise WorkerError(f"« {nom} » est obligatoire")
        chemin = Path(brut).expanduser()
        if not chemin.is_absolute():
            chemin = request.output_dir / chemin
        if not chemin.is_file():
            raise WorkerError(f"{nom} introuvable : {chemin}")
        return chemin

    def _reglage(self, request: InferRequest, nom: str, defaut: Any) -> Any:
        valeur = request.get(nom)
        if valeur is not None:
            return valeur
        if self.defaults.get(nom) is not None:
            return self.defaults[nom]
        return defaut

    def _pic(self) -> int | None:
        if self.mx is None:
            return None
        try:
            return int(self.mx.get_peak_memory())
        except Exception:  # noqa: BLE001 — une mesure ratée ne fait pas échouer un job
            return None

    def _versions(self) -> dict[str, str]:
        versions = {}
        for nom, module in (("mlx", "mlx.core"), ("mlx-audio", "mlx_audio")):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


if __name__ == "__main__":
    raise SystemExit(main(OmniVoiceWorker))
