"""Adaptateur mlx-audio, chemin **compréhension audio** : ce qu'on entend, pas ce qui est dit.

Quatrième adaptateur du runtime `mlx-audio`. Il est à la transcription ce que la
description d'image est à la lecture de document — et la comparaison n'est pas
une image : c'est **le même modèle** qui ferait les deux, et c'est la consigne
qui tranche.

**Sans consigne, ces réseaux transcrivent.** Leur défaut d'usine est « Please
transcribe the speech », et un adaptateur qui laisserait passer un `prompt` vide
rendrait une transcription sous une capacité qui promet une description. La
consigne par défaut est donc écrite ici, en français, et elle demande
explicitement de décrire plutôt que de retranscrire.

**La mémoire suit la durée écoutée.** Ces modèles encodent l'audio en jetons :
une heure d'enregistrement donnée telle quelle ne tient pas dans le budget de la
machine. `max_seconds` est une borne mémoire, et la sortie `listened_seconds` dit
ce qui a réellement été entendu — une réponse sur les deux premières minutes
d'une réunion d'une heure ne dit pas ce qu'elle a l'air de dire.
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
OUTPUT_TEXT = "text.txt"
SAMPLE_RATE = 16_000

DESCRIPTION = (
    "Décris ce qu'on entend dans cet extrait : la nature des sons, les voix, la "
    "musique, l'ambiance. Ne retranscris pas les paroles."
)
QUESTION = (
    "Réponds à la question suivante en te fondant uniquement sur ce que tu "
    "entends. Si l'extrait ne permet pas de répondre, dis-le plutôt que de deviner."
)


def build_prompt(question: str | None, langue: str | None) -> str:
    """La consigne envoyée au modèle, composée depuis les champs du contrat."""
    demande = (question or "").strip()
    consigne = f"{QUESTION}\n\nQuestion : {demande}" if demande else DESCRIPTION
    if langue and str(langue).strip() and str(langue).strip().lower() not in ("auto", ""):
        consigne += f" Rédige ta réponse en {str(langue).strip()}."
    return consigne


def _import_runtime() -> tuple[Any, Any, Any]:
    try:
        import mlx.core as mx
        from mlx_audio.stt.utils import load_audio, load_model
    except ImportError as exc:
        raise WorkerError(
            f"runtime mlx-audio indisponible dans cet environnement ({exc}) — "
            f"le reconstruire avec `{REPAIR}`"
        ) from exc
    return mx, load_model, load_audio


class Qwen2AudioWorker(Worker):
    """Compréhension audio : décrire, analyser, interroger un enregistrement."""

    name = "qwen2-audio"

    def __init__(self) -> None:
        self.mx: Any = None
        self.load_audio: Any = None
        self.model: Any = None
        self.defaults: dict[str, Any] = {}
        self._peak_load = 0

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        mx, load_model, load_audio = _import_runtime()
        self.mx = mx
        self.load_audio = load_audio
        self.defaults = dict(variant.get("defaults") or {})

        brut = str(variant.get("weights_path") or "").strip()
        ref = variant.get("ref") or "<ref>"
        if not brut or not Path(brut).is_dir():
            raise WorkerError(
                f"poids absents : {brut or '(chemin vide)'} n'est pas un dossier — "
                f"télécharger avec : ecurie pull {ref}"
            )
        try:
            self.model = load_model(brut)
        except Exception as exc:  # noqa: BLE001 — remonte avec la réparation
            raise WorkerError(f"chargement impossible : {type(exc).__name__}: {exc}") from exc
        self._peak_load = self._pic() or 0
        # Liste ouverte, comme pour les VLM : ce modèle répond dans les langues
        # qu'il connaît, et une liste fermée en refuserait qu'il maîtrise.
        return {"languages": [], "versions": self._versions()}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.model is None:
            raise WorkerError("infer avant load — aucun modèle en mémoire")

        audio_path = self._fichier(request, "audio")
        question = self._reglage(request, "question", None)
        langue = self._reglage(request, "language", None)
        max_seconds = float(self._reglage(request, "max_seconds", 120))
        max_tokens = int(self._reglage(request, "max_tokens", 512))
        température = float(self._reglage(request, "temperature", 0.2))

        progress(8, "lecture de l'enregistrement")
        signal = self.load_audio(str(audio_path), sr=SAMPLE_RATE)
        échantillons = int(max_seconds * SAMPLE_RATE)
        tronqué = signal.shape[0] > échantillons
        if tronqué:
            signal = signal[:échantillons]
        écoutée = float(signal.shape[0]) / SAMPLE_RATE

        self.mx.reset_peak_memory()
        if request.seed is not None:
            self.mx.random.seed(int(request.seed))

        progress(20, f"écoute en cours ({écoutée:.0f} s)")
        début = time.monotonic()
        try:
            résultat = self.model.generate(
                signal,
                prompt=build_prompt(question, langue),
                max_tokens=max_tokens,
                temperature=température,
            )
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte
            raise WorkerError(f"écoute impossible : {type(exc).__name__}: {exc}") from exc
        calcul = time.monotonic() - début

        texte = str(getattr(résultat, "text", résultat) or "").strip()
        jetons = int(getattr(résultat, "generation_tokens", 0) or 0)

        progress(92, "écriture")
        (request.output_dir / OUTPUT_TEXT).write_text(texte, encoding="utf-8")

        return InferResult(
            output={
                "text": OUTPUT_TEXT,
                "listened_seconds": round(écoutée, 3),
                "tokens_generated": jetons,
                "finish_reason": "length" if jetons >= max_tokens else "stop",
            },
            metrics={
                "characters": len(texte),
                "generation_tokens": jetons,
                "tokens_per_second": round(jetons / calcul, 2) if calcul > 0 else None,
                "truncated": tronqué,
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
    raise SystemExit(main(Qwen2AudioWorker))
