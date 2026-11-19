"""Adaptateur `mflux` pour `text-to-image` : FLUX.2 Klein, Qwen-Image, Z-Image.

Second adaptateur de `text-to-image`, après `workers.diffusers_mps` qui sert
SDXL sur PyTorch. Les modèles récents de génération d'image — FLUX.2, la
famille Qwen, Z-Image — n'ont pas de chemin diffusers praticable sur Apple
Silicon ; leurs conversions MLX sont publiées pour `mflux`, qui a son propre
format de dépôt (un dossier par composant).

Un seul adaptateur pour les trois familles, aiguillé par l'option
`mflux_model` du manifeste. Ce qui change d'un modèle à l'autre se résume à
une classe et une configuration de mflux : la signature de `generate_image`
est la même, au `negative_prompt` près, que seuls certains acceptent.

La résolution des paramètres est celle de `diffusers_mps.plan_generation` —
job, puis défauts du variant, puis contrat — reprise telle quelle : deux
adaptateurs d'une même capacité ne doivent pas lire différemment la même
requête.

**Les poids sont passés par chemin.** Contrairement à `workers.seedvr2`, où
mflux résout lui-même un nom de dépôt, ces modèles acceptent `model_path` :
on lui donne le dossier que le superviseur a vérifié, et la quantification
est lue dans les poids, pas redemandée.

Rien de mlx n'est importé au niveau du module (voir `workers/__init__.py`).
"""

import gc
import inspect
import time
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
from ecurie_runtime.workers.diffusers_mps import plan_generation

ENV_NAME = "mflux"
REPAIR = f"ecurie env sync {ENV_NAME}"
IMAGE_NAME = "image.png"


@dataclass(frozen=True)
class Famille:
    """Ce qu'il faut à mflux pour construire un modèle : un module, une classe, une config."""

    module: str
    classe: str
    config: str


# La clé est l'option `mflux_model` du manifeste, et reprend l'alias de mflux.
FAMILLES: dict[str, Famille] = {
    "flux2-klein-4b": Famille("mflux.models.flux2.variants", "Flux2Klein", "flux2_klein_4b"),
    "qwen-image": Famille(
        "mflux.models.qwen.variants.txt2img.qwen_image", "QwenImage", "qwen_image"
    ),
    "z-image-turbo": Famille("mflux.models.z_image.variants", "ZImage", "z_image_turbo"),
}


def famille_de(options: dict[str, Any]) -> Famille:
    """La famille désignée par le manifeste, ou une erreur qui dit quoi écrire."""
    nom = str(options.get("mflux_model") or "").strip()
    if nom not in FAMILLES:
        connues = ", ".join(sorted(FAMILLES))
        raise WorkerError(
            f"options.mflux_model = {nom!r} : famille inconnue de l'adaptateur — "
            f"valeurs admises : {connues}"
        )
    return FAMILLES[nom]


class _Progression:
    """Callback mflux qui remonte l'avancement du débruitage, pas à pas."""

    def __init__(self, progress: ProgressFn, pas: int) -> None:
        self._progress = progress
        self._pas = max(1, pas)
        self._fait = 0

    def call_in_loop(self, t, seed, prompt, latents, config, time_steps) -> None:  # noqa: ANN001
        self._fait += 1
        self._progress(10 + int(80 * self._fait / self._pas), f"pas {self._fait}/{self._pas}")


class _EvictionEncodeur:
    """Callback qui rend la mémoire de l'encodeur de texte dès le prompt encodé.

    Il n'est plus utile pendant le débruitage, et sur Qwen-Image il pèse
    autant que le transformer. Le prix : le job suivant recharge le modèle.
    """

    def __init__(self, worker: "MfluxImageWorker") -> None:
        self._worker = worker

    def call_before_loop(self, *args: Any, **kwargs: Any) -> None:
        self._worker._evincer_encodeur()


class MfluxImageWorker(Worker):
    """Génération d'image sur mflux, pour les familles déclarées dans `FAMILLES`."""

    name = "mflux-image"

    def __init__(self) -> None:
        self._mx: Any = None
        self._model: Any = None
        self._famille: Famille | None = None
        self._chemin: Path | None = None
        self._defaults: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._encodeur_evince = False
        self._rechargements = 0

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        chemin = Path(str(variant.get("weights_path") or ""))
        if not chemin.is_dir():
            raise WorkerError(
                f"poids introuvables : {chemin} — le superviseur transmet un chemin local "
                f"déjà vérifié, un worker ne télécharge jamais ({REPAIR} si l'env est en cause)"
            )
        self._defaults = dict(variant.get("defaults") or {})
        self._options = dict(variant.get("options") or {})
        self._famille = famille_de(self._options)
        self._chemin = chemin
        try:
            import mlx.core as mx
        except ImportError as exc:
            raise WorkerError(
                f"runtime mflux indisponible dans cet environnement ({exc}) — `{REPAIR}`"
            ) from exc
        self._mx = mx
        self._construire()
        return {"versions": self._versions(), "mflux_model": self._options["mflux_model"]}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self._model is None:
            raise WorkerError("infer avant load — aucun modèle en mémoire")
        plan = plan_generation(request, self._defaults)

        if self._encodeur_evince:
            progress(2, "rechargement du modèle (encodeur de texte rendu au job précédent)")
            self._construire()
            self._rechargements += 1

        appel: dict[str, Any] = {
            "seed": plan.seed,
            "prompt": plan.prompt,
            "num_inference_steps": plan.steps,
            "width": plan.width,
            "height": plan.height,
            "guidance": plan.guidance_scale,
        }
        négatif_ignoré = False
        if plan.negative_prompt:
            if "negative_prompt" in inspect.signature(self._model.generate_image).parameters:
                appel["negative_prompt"] = plan.negative_prompt
            else:
                négatif_ignoré = True

        progression = _Progression(progress, plan.steps)
        self._model.callbacks.register(progression)
        self._mx.reset_peak_memory()
        progress(5, "encodage du prompt")
        départ = time.monotonic()
        try:
            produite = self._model.generate_image(**appel)
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte utile
            raise WorkerError(f"génération impossible : {type(exc).__name__}: {exc}") from exc
        finally:
            _desinscrire(self._model, progression)
        génération_ms = int((time.monotonic() - départ) * 1000)

        progress(92, "encodage PNG")
        produite.image.save(request.output_dir / IMAGE_NAME, format="PNG")
        pic = self.peak_memory_bytes()
        gc.collect()
        self._mx.clear_cache()

        return InferResult(
            output={"image": IMAGE_NAME},
            metrics={
                **plan.as_metrics(),
                "mflux_model": self._options.get("mflux_model"),
                "generate_ms": génération_ms,
                "ms_per_step": round(génération_ms / max(1, plan.steps), 1),
                # Dit qu'un `negative_prompt` n'a pas atteint le modèle : le taire
                # laisserait croire qu'il a été pris en compte.
                "negative_prompt_ignored": négatif_ignoré,
                "text_encoder_evicted": self._encodeur_evince,
                "peak_memory_bytes": pic,
            },
        )

    def unload(self) -> None:
        self._model = None
        gc.collect()
        if self._mx is not None:
            self._mx.clear_cache()

    def peak_memory_bytes(self) -> int | None:
        if self._mx is None:
            return peak_rss_bytes()
        try:
            return max(int(self._mx.get_peak_memory()), peak_rss_bytes() or 0)
        except Exception:  # noqa: BLE001 — une mesure ratée ne fait pas échouer un job
            return peak_rss_bytes()

    # --- détails -------------------------------------------------------------

    def _construire(self) -> None:
        """Instancie le modèle depuis le dossier local des poids."""
        assert self._famille is not None and self._chemin is not None
        self._model = None
        gc.collect()
        if self._mx is not None:
            self._mx.clear_cache()
        try:
            import importlib

            from mflux.models.common.config import ModelConfig

            classe = getattr(importlib.import_module(self._famille.module), self._famille.classe)
            config = getattr(ModelConfig, self._famille.config)()
        except (ImportError, AttributeError) as exc:
            raise WorkerError(
                f"mflux ne fournit pas {self._famille.classe} ({exc}) — `{REPAIR}`"
            ) from exc
        self._model = classe(model_path=str(self._chemin), model_config=config)
        self._encodeur_evince = False
        if self._options.get("evict_text_encoder"):
            self._model.callbacks.register(_EvictionEncodeur(self))

    def _evincer_encodeur(self) -> None:
        if self._model is None:
            return
        self._model.text_encoder = None
        self._encodeur_evince = True
        gc.collect()
        self._mx.clear_cache()

    def _versions(self) -> dict[str, str]:
        versions = {}
        for nom, module in (("mlx", "mlx.core"), ("mflux", "mflux")):
            try:
                importé = __import__(module, fromlist=["__version__"])
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


def _desinscrire(model: Any, callback: Any) -> None:
    """Retire un callback de job, pour qu'il ne s'accumule pas d'un job à l'autre."""
    registre = getattr(model, "callbacks", None)
    liste = getattr(registre, "in_loop", None)
    if isinstance(liste, list) and callback in liste:
        liste.remove(callback)


if __name__ == "__main__":
    raise SystemExit(main(MfluxImageWorker))
