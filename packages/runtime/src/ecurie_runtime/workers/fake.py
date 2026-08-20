"""Worker d'essai : parle le protocole, ne charge aucun modèle (CONCEPTION.md §12).

C'est lui qui rend testables en CI — sur des machines sans Apple Silicon, sans
poids, sans venv de runtime — la supervision, le ping, le timeout, l'escalade
SIGTERM/SIGKILL, le contrôle d'admission et le job de bout en bout.

Les pannes se demandent par variables d'environnement, pour qu'un test puisse
provoquer exactement le comportement qu'il vérifie :

    ECURIE_FAKE_PEAK_BYTES     pic mémoire annoncé (défaut : 1 GiB)
    ECURIE_FAKE_WARMUP_MS      attente au chargement
    ECURIE_FAKE_HANG           load | infer | ping — ne répond plus
    ECURIE_FAKE_FAIL           load | infer — lève une exception
    ECURIE_FAKE_EXIT           load | infer — le processus meurt en pleine opération
    ECURIE_FAKE_IGNORE_SIGTERM ignore SIGTERM (pour éprouver l'escalade SIGKILL)
    ECURIE_FAKE_NOISE          écrit une ligne hors protocole sur le canal
    ECURIE_FAKE_OUTPUT_KEY     clé de sortie (défaut : audio)
    ECURIE_FAKE_OUTPUT_NAME    nom du fichier produit (défaut : audio.wav)
"""

import math
import os
import signal
import struct
import sys
import time
import wave

from ecurie_runtime.workers.base import (
    InferRequest,
    InferResult,
    ProgressFn,
    Worker,
    WorkerError,
    main,
)

VOICES = ["ethan", "chelsie", "aiden", "nofish"]
SAMPLE_RATE = 24_000


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value and value.lstrip("-").isdigit() else default


class FakeWorker(Worker):
    name = "fake"

    def __init__(self) -> None:
        self.variant: dict | None = None
        self.peak = _env_int("ECURIE_FAKE_PEAK_BYTES", 1 << 30)
        if os.environ.get("ECURIE_FAKE_IGNORE_SIGTERM"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if os.environ.get("ECURIE_FAKE_NOISE"):
            # Avant que `serve_stdio` ne mette stdout à l'abri : la ligne part donc
            # bien sur le canal, ce qui est tout l'objet de l'essai.
            print("ceci n'est pas du JSON", flush=True)

    def load(self, variant: dict) -> dict:
        self._maybe_break("load")
        time.sleep(_env_int("ECURIE_FAKE_WARMUP_MS", 0) / 1000)
        self.variant = variant
        return {"voices": VOICES}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        self._maybe_break("infer")
        texte = str(request.get("text", ""))
        progress(10, "préparation")
        secondes = max(0.05, min(len(texte) / 15.0, 10.0))
        nom = os.environ.get("ECURIE_FAKE_OUTPUT_NAME", "audio.wav")
        chemin = request.output_dir / nom
        _write_wav(chemin, secondes, seed=request.seed)
        progress(90, "écriture")
        return InferResult(
            output={os.environ.get("ECURIE_FAKE_OUTPUT_KEY", "audio"): nom},
            metrics={"audio_seconds": round(secondes, 3), "rtf": 0.05},
        )

    def unload(self) -> None:
        self.variant = None

    def peak_memory_bytes(self) -> int | None:
        return self.peak

    def rss_bytes(self) -> int | None:
        return self.peak // 2

    def ping(self) -> int | None:
        self._maybe_break("ping")
        return self.rss_bytes()

    def _maybe_break(self, phase: str) -> None:
        if os.environ.get("ECURIE_FAKE_HANG") == phase:
            time.sleep(3600)
        if os.environ.get("ECURIE_FAKE_EXIT") == phase:
            sys.stderr.write(f"sortie brutale demandée en {phase}\n")
            os._exit(9)
        if os.environ.get("ECURIE_FAKE_FAIL") == phase:
            raise WorkerError(f"panne demandée en {phase}")


def _write_wav(path, seconds: float, *, seed: int | None) -> None:
    """Un vrai fichier WAV — le job de bout en bout produit un artefact lisible.

    La graine change la note : deux exécutions de même graine donnent le même
    fichier, ce qui permet d'éprouver la reproductibilité sans modèle réel.
    """
    frequence = 220.0 + (seed or 0) % 220
    frames = int(SAMPLE_RATE * seconds)
    with wave.open(str(path), "wb") as sortie:
        sortie.setnchannels(1)
        sortie.setsampwidth(2)
        sortie.setframerate(SAMPLE_RATE)
        sortie.writeframes(
            b"".join(
                struct.pack("<h", int(12000 * math.sin(2 * math.pi * frequence * i / SAMPLE_RATE)))
                for i in range(frames)
            )
        )


if __name__ == "__main__":
    raise SystemExit(main(FakeWorker))
