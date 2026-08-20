"""Adaptateur mlx-audio, chemin musique : une description, des paroles, une chanson.

Même runtime que `mlx_audio.py`, autre bibliothèque à l'intérieur :
`mlx_audio.music` ne partage avec `mlx_audio.tts` ni le chargement (`load` rend
un modèle seul, pas un couple modèle/processeur), ni l'appel (`generate` exige
des paroles et rend un itérateur), ni la sortie (stéréo 44,1 kHz contre mono
24 kHz). Les fondre dans un seul adaptateur donnerait un fichier qui commence par
un aiguillage — d'où deux modules, choisis par la capacité déclarée au manifeste
(voir `envs.worker_module`).

Rien de mlx n'est importé au niveau du module (voir `workers/__init__.py`).

Écrit contre le support MiniMax Music 3 mergé dans mlx-audio (PR #888), épinglé
au commit `784b29e` par `runtimes/mlx-audio-music/pyproject.toml` : aucune version
publiée sur PyPI ne l'embarque encore.
"""

import gc
import math
import sys
import time
from collections.abc import Callable, Mapping
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
REPAIR = "ecurie env sync mlx-audio-music"

# Le checkpoint exige des paroles. `[instrumental]` est la façon documentée de
# demander une pièce sans voix — un champ vide serait refusé par le modèle, et
# traduire nous-mêmes le silence en cette marque évite à l'utilisateur d'avoir à
# connaître une convention de MiniMax pour obtenir de l'instrumental.
INSTRUMENTAL = "[instrumental]"

# Pas de débruitage du transformeur de flot. Le contrat de capacité n'expose pas
# ce réglage — il n'a rien d'un dénominateur commun de la génération musicale —
# donc il vit dans les `options:` du variant, et cette valeur n'est que le repli.
DEFAULT_STEPS = 30
SAMPLE_RATE_ATTENDU = 44_100


@dataclass(frozen=True)
class Runtime:
    """Les deux points d'entrée de mlx-audio dont cet adaptateur dépend."""

    mx: Any
    load: Callable[..., Any]
    write: Callable[..., Any]


@dataclass(frozen=True)
class GenerationPlan:
    """Les arguments prêts pour `generate`, et ce qui n'a pas pu être honoré."""

    kwargs: dict[str, Any]
    warnings: tuple[str, ...] = ()


def import_runtime() -> Runtime:
    """Importe mlx-audio, ou explique comment réparer l'environnement."""
    try:
        import mlx.core as mx
        from mlx_audio.audio_io import write
        from mlx_audio.music import load
    except ImportError as exc:
        raise WorkerError(
            f"runtime mlx-audio (chemin musique) indisponible ({exc}) — "
            f"le reconstruire avec `{REPAIR}`. Le support MiniMax Music 3 n'est "
            "dans aucune version publiée sur PyPI : l'env épingle un commit."
        ) from exc
    return Runtime(mx=mx, load=load, write=write)


def plan_generation(
    *,
    entree: Mapping[str, Any],
    params: Mapping[str, Any],
    defaults: Mapping[str, Any],
) -> GenerationPlan:
    """Traduit une demande du protocole en arguments de `generate`.

    Fonction pure, et c'est là que se joue tout ce qui se vérifie sans Apple
    Silicon : la priorité des trois couches de réglages, la traduction de
    « pas de paroles » en `[instrumental]`, et le sort des paramètres que le
    contrat déclare mais que ce modèle n'expose pas.
    """
    couches = (entree, params, defaults)

    description = _reglage("prompt", *couches)
    description = str(description).strip() if description is not None else ""
    if not description:
        raise WorkerError("aucune description musicale : le champ `prompt` est vide")

    paroles = _reglage("lyrics", *couches)
    paroles = str(paroles).strip() if paroles is not None else ""
    if not paroles:
        paroles = INSTRUMENTAL

    kwargs: dict[str, Any] = {"text": description, "lyrics": paroles}

    durée = _reglage("duration_seconds", *couches)
    if durée is not None:
        kwargs["duration"] = float(durée)

    steps = _reglage("steps", *couches)
    kwargs["steps"] = int(steps) if steps is not None else DEFAULT_STEPS

    graine = _reglage("seed", *couches)
    if graine is not None:
        kwargs["seed"] = int(graine)

    avertissements = []
    for nom in ("guidance_scale", "temperature"):
        # Le contrat les déclare parce que la plupart des modèles musicaux les
        # exposent ; celui-ci non. Les passer à `generate` les ferait absorber
        # par son `**_` sans le moindre effet — le pire des deux mondes, puisque
        # l'utilisateur croirait avoir réglé quelque chose.
        if _reglage(nom, *couches) is not None:
            avertissements.append(
                f"{nom} ignoré : MiniMax Music 3 n'expose pas ce réglage "
                "(voir les caveats du manifeste)"
            )

    return GenerationPlan(kwargs=kwargs, warnings=tuple(avertissements))


def _reglage(nom: str, *couches: Mapping[str, Any]) -> Any:
    """Première valeur définie, de la plus prioritaire à la moins : entrée, job, manifeste."""
    for couche in couches:
        valeur = couche.get(nom)
        if valeur is not None:
            return valeur
    return None


def merge_segments(segments: list[Any], concat: Callable[[list[Any]], Any]) -> Any:
    """Recolle les morceaux rendus par le générateur.

    `generate` rend un itérateur : un seul élément aujourd'hui, mais rien ne le
    garantit pour une pièce longue. Le cas à un segment court-circuite la
    concaténation, qui copierait tout l'audio pour rien.
    """
    if not segments:
        raise WorkerError("le modèle n'a produit aucun segment audio")
    if len(segments) == 1:
        return segments[0].audio
    return concat([s.audio for s in segments])


class MlxAudioMusicWorker(Worker):
    """Génération de chanson : une description, des paroles, un wav stéréo."""

    name = "mlx-audio-music"

    def __init__(self) -> None:
        self._runtime: Runtime | None = None
        self._model: Any = None
        self._defaults: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._sample_rate = SAMPLE_RATE_ATTENDU
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
        self._options = dict(variant.get("options") or {})
        self._model = runtime.load(chemin)
        self._runtime = runtime
        self._sample_rate = int(getattr(self._model, "sample_rate", SAMPLE_RATE_ATTENDU))
        self._peak_load = self._pic_mlx() or 0

        return {
            "sample_rate": self._sample_rate,
            "lyrics_required": True,
            "instrumental_marker": INSTRUMENTAL,
            "versions": self._versions(),
        }

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._runtime is None or self._model is None:
            raise WorkerError("modèle non chargé")
        runtime = self._runtime

        plan = plan_generation(
            entree=request.input,
            params=request.params,
            defaults=self._defaults,
        )
        for message in plan.warnings:
            self._avertir(message)
        if request.seed is not None:
            plan.kwargs.setdefault("seed", int(request.seed))

        progress(5, "préparation")
        runtime.mx.reset_peak_memory()

        # La progression ne peut pas être plus fine : `generate` ne rend la main
        # qu'une fois le morceau produit. Annoncer un pourcentage qui avance
        # pendant ce temps serait une animation, pas une mesure.
        progress(15, f"génération ({plan.kwargs.get('steps')} pas)")
        début = time.monotonic()
        try:
            segments = list(self._model.generate(**plan.kwargs))
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte utile
            raise WorkerError(f"génération impossible : {type(exc).__name__}: {exc}") from exc
        audio = merge_segments(segments, lambda parts: runtime.mx.concatenate(parts, axis=0))
        calcul = time.monotonic() - début

        progress(92, "écriture du wav")
        chemin = request.output_dir / OUTPUT_NAME
        runtime.write(str(chemin), audio, self._sample_rate, format="wav")

        return InferResult(
            output={"audio": OUTPUT_NAME},
            metrics=self._metriques(audio, segments, calcul, plan),
        )

    def unload(self) -> None:
        self._model = None
        self._peak_load = 0
        # L'ordre compte : tant qu'une référence Python tient les tableaux, leurs
        # buffers ne sont que « cachés » et `clear_cache` ne rend rien au système.
        gc.collect()
        if self._runtime is not None:
            self._runtime.mx.clear_cache()

    def peak_memory_bytes(self) -> int | None:
        """Pic MLX en octets, avec le poids résident pour plancher.

        `reset_peak_memory()` est appelé à chaque job : sans ce plancher, une
        pièce très courte rapporterait un pic inférieur au modèle chargé, et le
        contrôle d'admission laisserait entrer un résident de plus que la
        mémoire ne peut tenir.
        """
        pic = self._pic_mlx()
        if pic is None:
            return peak_rss_bytes()
        return max(pic, self._peak_load)

    # --- détails -------------------------------------------------------------

    def _metriques(
        self, audio: Any, segments: list[Any], calcul: float, plan: GenerationPlan
    ) -> dict[str, Any]:
        échantillons = _longueur(audio)
        secondes = échantillons / self._sample_rate if échantillons else None
        métriques: dict[str, Any] = {
            "sample_rate": self._sample_rate,
            "segments": len(segments),
            "steps": plan.kwargs.get("steps"),
            "channels": _canaux(audio),
            "lyrics_characters": len(plan.kwargs.get("lyrics", "")),
            "instrumental": plan.kwargs.get("lyrics") == INSTRUMENTAL,
            "peak_memory_bytes": self.peak_memory_bytes(),
        }
        if secondes:
            métriques["audio_seconds"] = round(secondes, 3)
            # Convention d'Écurie : temps de calcul par seconde produite, pour
            # que `rtf` et `throughput` restent inverses l'un de l'autre.
            métriques["rtf"] = round(calcul / secondes, 4)
            demandée = plan.kwargs.get("duration")
            if demandée and secondes < float(demandée) * 0.9:
                # La durée est une borne haute : l'étage autorégressif peut
                # s'arrêter tôt. Le dire évite de croire à une troncature.
                métriques["duration_note"] = (
                    f"{secondes:.1f} s produites pour {float(demandée):.0f} demandées — "
                    "le modèle a émis sa fin plus tôt"
                )
        if plan.warnings:
            métriques["warnings"] = list(plan.warnings)
        return métriques

    def _pic_mlx(self) -> int | None:
        if self._runtime is None:
            return None
        try:
            return int(self._runtime.mx.get_peak_memory())
        except Exception:  # noqa: BLE001 — une mesure ratée ne doit pas faire échouer un job
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

    def _avertir(self, message: str) -> None:
        """Une fois par worker et par sujet : répété à chaque job, un avertissement
        devient du bruit et cesse d'être lu."""
        if message in self._warned:
            return
        self._warned.add(message)
        print(f"[mlx-audio-music] {message}", file=sys.stderr, flush=True)


def _longueur(audio: Any) -> int:
    forme = getattr(audio, "shape", None)
    if not forme:
        return 0
    # Stéréo : (échantillons, canaux) ou (canaux, échantillons) selon la version.
    # On prend la plus grande dimension — un morceau a toujours plus
    # d'échantillons que de canaux.
    return int(max(forme))


def _canaux(audio: Any) -> int:
    forme = getattr(audio, "shape", None)
    if not forme:
        return 0
    return int(min(forme)) if len(forme) > 1 else 1


def secondes_vers_pas(secondes: float, pas_par_seconde: float = 1.0) -> int:
    """Pas de débruitage suggérés pour une durée. Utilitaire, non appelé par défaut."""
    return max(1, math.ceil(secondes * pas_par_seconde))


if __name__ == "__main__":
    raise SystemExit(main(MlxAudioMusicWorker))
