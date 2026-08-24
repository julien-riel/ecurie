"""Adaptateur mlx-audio, chemin **séparation de pistes** : un mixage, des pistes.

Septième emploi du runtime `mlx-audio`, et le troisième à ne rien partager avec
les autres à l'intérieur : `mlx_audiogen` réimplémente HTDemucs contre MLX, et
n'a de commun avec `mlx_audio` que le nom du runtime déclaré au manifeste. D'où
l'environnement dédié `mlx-audiogen` — celui que le variant nomme par
`runtime_env` — et ce module plutôt qu'un aiguillage dans `mlx_audio.py`.

**Le réseau produit toujours quatre pistes.** `stems: 2` du contrat n'est pas un
mode du modèle : c'est la voix d'un côté, et la somme des trois autres de
l'autre. Le manifeste le dit déjà en caveat ; l'adaptateur le fait, et l'inscrit
dans les métriques pour qu'aucun lecteur du job ne croie avoir économisé du
calcul.

**`shifts` n'est pas honoré.** Le contrat le déclare parce que la référence
PyTorch de Demucs l'expose ; `DemucsPipeline.separate` n'en a pas le paramètre.
Le passer sous un autre nom serait deviner, et l'ignorer en silence ferait croire
à une passe moyennée qui n'a pas eu lieu — d'où l'avertissement remonté au job.
"""

import gc
import sys
import time
from collections.abc import Mapping
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

ENV_NAME = "mlx-audiogen"
REPAIR = f"ecurie env sync {ENV_NAME}"

# Les quatre sorties de HTDemucs, dans l'ordre où le modèle les déclare. Les
# noms sont ceux du contrat de capacité — c'est une coïncidence heureuse pour
# trois d'entre eux, pas une garantie : `_piste_contrat` fait la traduction.
SOURCES_ATTENDUES = ("drums", "bass", "other", "vocals")

# Ce que le contrat appelle « tout sauf la voix ».
ACCOMPAGNEMENT = "accompaniment"

# 44,1 kHz natif. Un fichier à une autre fréquence est rééchantillonné par le
# pipeline avant traitement, puis restitué à la fréquence d'origine — c'est lui
# qui s'en charge, pas cet adaptateur (voir `DemucsPipeline.separate`).
SAMPLE_RATE_NATIF = 44_100

# Recouvrement des tranches, en fraction de tranche. Ce n'est pas un paramètre du
# contrat : il ne décrit pas la séparation demandée mais la façon de la calculer
# par morceaux. La valeur est celle du pipeline amont.
OVERLAP = 0.25


@dataclass(frozen=True)
class Demande:
    """Ce qui a été demandé, résolu, et ce qui n'a pas pu l'être."""

    stems: int
    segment_seconds: float
    warnings: tuple[str, ...] = ()


def plan_separation(
    *,
    entree: Mapping[str, Any],
    params: Mapping[str, Any],
    defaults: Mapping[str, Any],
) -> Demande:
    """Traduit une demande du protocole en réglages de séparation.

    Fonction pure, sans MLX : c'est tout ce qui se vérifie sans Apple Silicon —
    la priorité des trois couches, le refus d'un nombre de pistes que le modèle
    ne sait pas rendre, et le sort de `shifts`.
    """
    couches = (entree, params, defaults)

    stems = _reglage("stems", *couches)
    stems = int(stems) if stems is not None else 2
    if stems not in (2, 4):
        raise WorkerError(f"stems = {stems} : le contrat n'accepte que 2 ou 4 pistes")

    segment = _reglage("segment_seconds", *couches)
    segment = float(segment) if segment is not None else 10.0
    if segment <= 0:
        raise WorkerError(f"segment_seconds = {segment} : durée strictement positive attendue")

    avertissements = []
    shifts = _reglage("shifts", *couches)
    if shifts is not None and int(shifts) > 1:
        avertissements.append(
            f"shifts = {int(shifts)} ignoré : mlx-audiogen n'expose pas les passes "
            "décalées de la référence PyTorch — une seule passe a été calculée"
        )

    return Demande(stems=stems, segment_seconds=segment, warnings=tuple(avertissements))


def _reglage(nom: str, *couches: Mapping[str, Any]) -> Any:
    """Première valeur définie, de la plus prioritaire à la moins : entrée, job, manifeste."""
    for couche in couches:
        valeur = couche.get(nom)
        if valeur is not None:
            return valeur
    return None


def pistes_du_contrat(sources: dict[str, Any], stems: int, somme: Any) -> dict[str, Any]:
    """Les quatre sorties du réseau → ce que le contrat déclare.

    À deux pistes, l'accompagnement est la somme des trois qui ne sont pas la
    voix. La somme est passée en argument plutôt que calculée ici pour garder la
    fonction vérifiable sans numpy.
    """
    if "vocals" not in sources:
        raise WorkerError(
            "le modèle n'a pas rendu de piste « vocals » — sources reçues : "
            + ", ".join(sorted(sources))
        )
    if stems == 4:
        return dict(sources)
    accompagnement = somme([piste for nom, piste in sources.items() if nom != "vocals"])
    return {"vocals": sources["vocals"], ACCOMPAGNEMENT: accompagnement}


def _import_runtime() -> tuple[Any, Any, Any]:
    """Importe mlx-audiogen, ou explique comment réparer l'environnement."""
    try:
        import mlx.core as mx
        import numpy as np
        from mlx_audiogen.models.demucs.pipeline import DemucsPipeline
    except ImportError as exc:
        raise WorkerError(
            f"runtime mlx-audiogen indisponible dans cet environnement ({exc}) — "
            f"le reconstruire avec `{REPAIR}`. Cet adaptateur ne tourne PAS dans "
            "l'env `mlx-audio` : le variant le dit par `runtime_env: mlx-audiogen`."
        ) from exc
    return mx, np, DemucsPipeline


class DemucsSeparateWorker(Worker):
    """Séparation de pistes : un mixage en entrée, deux ou quatre wav en sortie."""

    name = "demucs-separate"

    def __init__(self) -> None:
        self._mx: Any = None
        self._np: Any = None
        self._pipeline: Any = None
        self._defaults: dict[str, Any] = {}
        self._peak_load = 0
        self._warned: set[str] = set()

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        mx, np, DemucsPipeline = _import_runtime()
        chemin = Path(str(variant.get("weights_path") or ""))
        if not chemin.is_dir():
            raise WorkerError(
                f"poids introuvables : {chemin} — le superviseur transmet un chemin local "
                "déjà vérifié, un worker ne télécharge jamais"
            )

        self._defaults = dict(variant.get("defaults") or {})
        try:
            self._pipeline = DemucsPipeline.from_pretrained(str(chemin))
        except Exception as exc:  # noqa: BLE001 — remonte avec le chemin en clair
            raise WorkerError(
                f"chargement impossible depuis {chemin} : {type(exc).__name__}: {exc}"
            ) from exc
        self._mx, self._np = mx, np
        self._peak_load = self._pic_mlx() or 0

        sources = list(getattr(self._pipeline.config, "sources", SOURCES_ATTENDUES))
        if sorted(sources) != sorted(SOURCES_ATTENDUES):
            # Non bloquant : un checkpoint à trois pistes resterait utilisable
            # pour ce qu'il rend. Mais le dire évite de chercher longtemps
            # pourquoi `stems: 4` ne remplit pas les quatre clés du contrat.
            self._avertir(
                f"sources inattendues au checkpoint : {sources} — le contrat en attend "
                f"{list(SOURCES_ATTENDUES)}"
            )

        return {
            "sources": sources,
            "sample_rate": int(getattr(self._pipeline.config, "samplerate", SAMPLE_RATE_NATIF)),
            "native_sample_rate": SAMPLE_RATE_NATIF,
            "always_four_stems": True,
            "versions": self._versions(),
        }

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._pipeline is None or self._np is None:
            raise WorkerError("modèle non chargé")
        np = self._np

        plan = plan_separation(
            entree=request.input, params=request.params, defaults=self._defaults
        )
        for message in plan.warnings:
            self._avertir(message)

        chemin_entree = self._chemin_entree(request)
        progress(5, "lecture de l'entrée")
        audio, sample_rate = self._lire(chemin_entree)

        # La tranche est le seul levier mémoire de cette capacité : le réseau
        # traite un segment à la fois, et le pic suit sa longueur, pas celle du
        # morceau. Le contrat l'expose pour cette raison.
        self._pipeline.config.segment = plan.segment_seconds

        self._mx.reset_peak_memory()
        progress(15, f"séparation ({plan.stems} pistes)")
        début = time.monotonic()
        try:
            sources = self._pipeline.separate(
                audio,
                sample_rate=sample_rate,
                overlap=OVERLAP,
                progress_callback=lambda fraction: progress(
                    15 + int(70 * max(0.0, min(1.0, float(fraction)))), "séparation"
                ),
            )
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte utile
            raise WorkerError(f"séparation impossible : {type(exc).__name__}: {exc}") from exc
        calcul = time.monotonic() - début

        pistes = pistes_du_contrat(sources, plan.stems, lambda parts: sum(parts[1:], parts[0]))

        progress(90, "écriture des pistes")
        écrites: dict[str, str] = {}
        écrêtage = 0.0
        for nom, piste in pistes.items():
            fichier = f"{nom}.wav"
            écrêtage = max(écrêtage, self._ecrire(request.output_dir / fichier, piste, sample_rate))
            écrites[nom] = fichier
        if écrêtage > 0.001:
            # Hors du domaine d'entraînement — une voix seule, un enregistrement
            # de terrain —, les pistes dépassent l'échelle du mixage et le PCM 16
            # bits les replie. Le dire : une piste écrêtée s'entend, et rien dans
            # le fichier ne dit d'où vient la distorsion.
            self._avertir(
                f"{écrêtage:.1%} des échantillons écrêtés à l'écriture : les pistes "
                "dépassent l'échelle du mixage, ce qui arrive sur une entrée éloignée "
                "de MUSDB18 (voix seule, enregistrement de terrain — caveat du manifeste)"
            )

        secondes = float(np.asarray(audio).shape[-1]) / max(1, sample_rate)
        return InferResult(
            output={"tracks": écrites},
            metrics=self._metriques(plan, secondes, calcul, sample_rate, sources),
        )

    def unload(self) -> None:
        self._pipeline = None
        self._peak_load = 0
        # L'ordre compte : tant qu'une référence Python tient les tableaux, leurs
        # buffers ne sont que « cachés » et `clear_cache` ne rend rien au système.
        gc.collect()
        if self._mx is not None:
            self._mx.clear_cache()

    def peak_memory_bytes(self) -> int | None:
        """Pic MLX, avec le poids résident pour plancher — même règle que la musique."""
        pic = self._pic_mlx()
        if pic is None:
            return peak_rss_bytes()
        return max(pic, self._peak_load)

    # --- détails -------------------------------------------------------------

    def _chemin_entree(self, request: InferRequest) -> Path:
        brut = request.get("audio")
        if not brut:
            raise WorkerError("aucun fichier : le contrat audio-separation exige `audio`")
        chemin = Path(str(brut))
        if not chemin.is_absolute():
            # Le superviseur copie l'entrée dans le dossier du job et transmet un
            # chemin relatif à ce dossier — c'est ce qui rend un job rejouable.
            chemin = request.output_dir / chemin
        if not chemin.is_file():
            raise WorkerError(f"fichier d'entrée introuvable : {chemin}")
        return chemin

    def _lire(self, chemin: Path) -> tuple[Any, int]:
        """Lit le mixage en (canaux, échantillons), à sa fréquence d'origine.

        `soundfile` est déclaré par l'env pour cette lecture : `DemucsPipeline`
        reçoit et rend des tableaux numpy, et rien dans mlx-audiogen ne lit un
        fichier. La forme rendue par soundfile est (échantillons, canaux) ; le
        pipeline sait retourner l'une ou l'autre, mais il le déduit d'une
        comparaison de dimensions qui se trompe sur un extrait très court —
        d'où la transposition explicite ici.
        """
        try:
            import soundfile
        except ImportError as exc:
            raise WorkerError(
                f"soundfile absent de l'environnement {ENV_NAME} ({exc}) — `{REPAIR}`"
            ) from exc
        try:
            données, fréquence = soundfile.read(str(chemin), dtype="float32", always_2d=True)
        except Exception as exc:  # noqa: BLE001 — fichier illisible : le dire avec son chemin
            raise WorkerError(f"lecture impossible de {chemin} : {exc}") from exc
        return données.T, int(fréquence)

    def _ecrire(self, chemin: Path, piste: Any, sample_rate: int) -> float:
        """Écrit une piste et rend la fraction d'échantillons qui a été écrêtée."""
        import soundfile

        np = self._np
        données = np.asarray(piste, dtype="float32")
        if données.ndim == 2:
            # (canaux, échantillons) → (échantillons, canaux), ce que soundfile écrit.
            données = données.T
        débordent = float(np.mean(np.abs(données) > 1.0))
        soundfile.write(str(chemin), np.clip(données, -1.0, 1.0), sample_rate, subtype="PCM_16")
        return débordent

    def _metriques(
        self,
        plan: Demande,
        secondes: float,
        calcul: float,
        sample_rate: int,
        sources: dict[str, Any],
    ) -> dict[str, Any]:
        métriques: dict[str, Any] = {
            "stems": plan.stems,
            "sources_computed": len(sources),
            "segment_seconds": plan.segment_seconds,
            "overlap": OVERLAP,
            "sample_rate": sample_rate,
            "resampled": sample_rate != SAMPLE_RATE_NATIF,
            "peak_memory_bytes": self.peak_memory_bytes(),
        }
        if secondes > 0:
            métriques["audio_seconds"] = round(secondes, 3)
            # Convention d'Écurie : temps de calcul par seconde traitée.
            métriques["rtf"] = round(calcul / secondes, 4)
        if plan.warnings:
            métriques["warnings"] = list(plan.warnings)
        return métriques

    def _pic_mlx(self) -> int | None:
        if self._mx is None:
            return None
        try:
            return int(self._mx.get_peak_memory())
        except Exception:  # noqa: BLE001 — une mesure ratée ne doit pas faire échouer un job
            return None

    def _versions(self) -> dict[str, str]:
        versions = {}
        for nom, module in (("mlx", "mlx.core"), ("mlx-audiogen", "mlx_audiogen")):
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
        print(f"[{self.name}] {message}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main(DemucsSeparateWorker))
