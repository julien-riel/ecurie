"""Adaptateur mlx-audio, chemin **qui parle quand**.

Troisième adaptateur du runtime `mlx-audio`, après la synthèse vocale et la
chanson. Il ne transcrit pas : le modèle en est capable — il rend même le texte
avec ses locuteurs —, mais ce texte appartient à `speech-to-text`, qui a son
contrat, et mêler les deux ferait un contrat qu'aucun autre modèle de
diarisation ne pourrait remplir.

**Deux des paramètres du contrat ne s'appliquent pas à ce modèle**, et il vaut
mieux l'écrire que le laisser deviner. `num_speakers` et `threshold` supposent un
réseau qui décide trame par trame — c'est le cas de Sortformer, pas de celui-ci,
qui produit ses tours de parole en même temps que son texte. Ils restent au
contrat parce que le contrat sert plusieurs modèles ; ce variant les déclare
inopérants dans ses caveats plutôt que de faire croire qu'il les honore.

`min_segment_seconds` et `max_seconds`, eux, s'appliquent ici : le premier
recolle après coup ce que le modèle a coupé trop fin, le second borne ce qu'on
lui donne à entendre. Ce sont des décisions de l'adaptateur, pas du réseau, et
elles valent pour tout modèle de cette capacité.

**Le RTTM n'est pas une commodité.** C'est le format que lisent les outils
d'évaluation du domaine, et le seul par lequel un score de diarisation se calcule
sans réécrire un analyseur — donc le seul par lequel le golden set de cette
capacité pourra se noter au v0.5.
"""

import gc
import json
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
SEGMENTS_NAME = "segments.json"
RTTM_NAME = "segments.rttm"
SAMPLE_RATE = 16_000


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


def fusionner(segments: list[dict], minimum: float) -> list[dict]:
    """Absorbe les tours plus courts que `minimum` dans leur voisin.

    Sans elle, une réunion produit des centaines de segments de deux dixièmes de
    seconde, qu'aucun usage ne demande. Le segment court est rattaché au
    précédent quand il vient du même locuteur, et supprimé sinon — le fusionner
    avec un locuteur différent inventerait une prise de parole.
    """
    if minimum <= 0:
        return segments
    gardés: list[dict] = []
    for segment in segments:
        durée = float(segment["end"]) - float(segment["start"])
        if durée >= minimum:
            gardés.append(dict(segment))
            continue
        if gardés and gardés[-1]["speaker"] == segment["speaker"]:
            gardés[-1]["end"] = segment["end"]
    return gardés


def en_rttm(segments: list[dict], nom: str) -> str:
    """Le format du domaine. Une ligne par tour, dix champs, `<NA>` pour le reste."""
    lignes = []
    for segment in segments:
        début = float(segment["start"])
        durée = max(0.0, float(segment["end"]) - début)
        lignes.append(
            f"SPEAKER {nom} 1 {début:.3f} {durée:.3f} <NA> <NA> {segment['speaker']} <NA> <NA>"
        )
    return "\n".join(lignes) + ("\n" if lignes else "")


class MossDiarizeWorker(Worker):
    """Diarisation : découpe un enregistrement en tours de parole."""

    name = "moss-diarize"

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
        return {"versions": self._versions()}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.model is None:
            raise WorkerError("infer avant load — aucun modèle en mémoire")

        audio_path = self._fichier(request, "audio")
        max_seconds = float(self._reglage(request, "max_seconds", 1800))
        minimum = float(self._reglage(request, "min_segment_seconds", 0.5))

        progress(8, "lecture de l'enregistrement")
        signal = self.load_audio(str(audio_path), sr=SAMPLE_RATE)
        échantillons = int(max_seconds * SAMPLE_RATE)
        tronqué = signal.shape[0] > échantillons
        if tronqué:
            signal = signal[:échantillons]
        durée = float(signal.shape[0]) / SAMPLE_RATE

        self.mx.reset_peak_memory()
        progress(20, f"diarisation en cours ({durée:.0f} s)")
        début = time.monotonic()
        try:
            résultat = self.model.generate(signal, max_tokens=2048, temperature=0.0)
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte
            raise WorkerError(f"diarisation impossible : {type(exc).__name__}: {exc}") from exc
        calcul = time.monotonic() - début

        bruts = getattr(résultat, "segments", None) or []
        segments = [
            {
                "start": round(float(s["start"]), 3),
                "end": round(float(s["end"]), 3),
                "speaker": str(s.get("speaker_id") or s.get("speaker") or "S00"),
            }
            for s in bruts
            if s.get("start") is not None and s.get("end") is not None
        ]
        segments = fusionner(segments, minimum)

        progress(90, "écriture")
        (request.output_dir / SEGMENTS_NAME).write_text(
            json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (request.output_dir / RTTM_NAME).write_text(
            en_rttm(segments, audio_path.stem), encoding="utf-8"
        )

        locuteurs = sorted({s["speaker"] for s in segments})
        parole = sum(s["end"] - s["start"] for s in segments)

        return InferResult(
            output={
                "segments": SEGMENTS_NAME,
                "rttm": RTTM_NAME,
                "speakers": len(locuteurs),
                "speech_seconds": round(parole, 3),
                "duration_seconds": round(durée, 3),
            },
            metrics={
                "segments": len(segments),
                "merged_away": max(0, len(bruts) - len(segments)),
                "truncated": tronqué,
                "speech_ratio": round(parole / durée, 3) if durée > 0 else 0.0,
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
    raise SystemExit(main(MossDiarizeWorker))
