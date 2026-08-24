"""Adaptateur `diffusers-mps`, chemin **texte → vidéo** (LTX-Video).

Quatrième emploi du runtime `diffusers-mps`, et le premier qui ne rend pas une
image. Il existe parce que le worker générique de ce runtime charge par
`AutoPipelineForText2Image`, et qu'`AutoPipeline` ne connaît aucun pipeline
vidéo : sur LTX il échoue au chargement, en deux secondes, par
`ValueError: AutoPipeline can't find a pipeline linked to LTXPipeline for None`.
Rien de mémoire, rien de modèle — juste une porte d'entrée qui n'était pas la
bonne.

**Trois contraintes du modèle que le contrat ne peut pas exprimer.** LTX
travaille sur une grille de 32 pixels et par groupes de 8 images ; le contrat,
lui, sert toute la capacité et n'impose que le multiple de 16. Un job hors
grille échoue au fond du VAE 3D, sur une forme de tenseur que personne ne relie
à sa demande. L'adaptateur ajuste donc, et **inscrit dans les métriques ce qu'il
a changé** : une vidéo de 97 images là où on en demandait 100 doit se voir dans
le manifeste du job, pas se deviner en comptant les images du fichier.

1. `width` et `height` sont ramenés au multiple de 32 le plus proche ;
2. `num_frames` est ramené à `8k+1` (le VAE compresse le temps par 8, plus une
   image de référence) ;
3. `fps` sert deux fois : le modèle est **conditionné** par la cadence
   (`frame_rate`), et le fichier écrit la porte. Les dissocier donnerait une
   vidéo dont le mouvement ne correspond pas à sa vitesse de lecture.

**L'écriture du mp4 passe par `imageio-ffmpeg`.** C'est ce qu'`export_to_video`
de diffusers utilise, et la seule dépendance que cette capacité ajoute à l'env.
Le binaire ffmpeg est embarqué dans la roue : rien à installer sur la machine.
"""

import gc
import importlib
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
from ecurie_runtime.workers.diffusers_mps import (
    ENV_NAME,
    detect_variant,
    draw_seed,
    torch_dtype_name,
)

VIDEO_NAME = "video.mp4"

# Miroir des `default` de registry/capabilities/text-to-video.json. La
# duplication est assumée : un worker ne lit pas le registre
# (workers/__init__.py, règle 2). Un test confronte les deux fichiers.
CONTRACT_DEFAULTS: dict[str, Any] = {
    "width": 832,
    "height": 480,
    "num_frames": 81,
    "fps": 16,
    "steps": 30,
    "guidance_scale": 5.0,
}

# La grille spatiale du VAE 3D de LTX. Le contrat impose 16 ; le modèle exige 32.
GRILLE_PIXELS = 32

# Compression temporelle du VAE : les images se comptent par groupes de 8, plus
# la première. 8 images demandées donnent donc 9, et 100 en donnent 97.
GROUPE_IMAGES = 8

MESSAGE_ENV = (
    "diffusers, torch ou imageio-ffmpeg sont absents de cet environnement — "
    f"reconstruire avec : ecurie env sync {ENV_NAME}"
)


# --- préparation d'un job (pur, sans torch) ----------------------------------


def aligner_pixels(valeur: int, nom: str) -> tuple[int, str | None]:
    """Ramène une dimension sur la grille de 32 px. Rend (valeur, note)."""
    if valeur <= 0:
        raise WorkerError(f"{nom} = {valeur} : valeur strictement positive attendue")
    aligné = max(GRILLE_PIXELS, round(valeur / GRILLE_PIXELS) * GRILLE_PIXELS)
    if aligné == valeur:
        return valeur, None
    return aligné, f"{nom} ramené de {valeur} à {aligné} px (grille de {GRILLE_PIXELS} du VAE 3D)"


def aligner_images(valeur: int) -> tuple[int, str | None]:
    """Ramène un nombre d'images à `8k+1`. Rend (valeur, note).

    On arrondit vers le bas plutôt que vers le haut : une image de plus coûte du
    calcul et de la mémoire que l'utilisateur n'a pas demandés, alors qu'une de
    moins ne change rien à ce qu'il voit. Le plancher est 9 — 1 seul groupe.
    """
    if valeur <= 0:
        raise WorkerError(f"num_frames = {valeur} : valeur strictement positive attendue")
    groupes = max(1, (valeur - 1) // GROUPE_IMAGES)
    aligné = groupes * GROUPE_IMAGES + 1
    if aligné == valeur:
        return valeur, None
    return aligné, f"num_frames ramené de {valeur} à {aligné} (le VAE compresse le temps par 8)"


class PlanVideo:
    """Ce qui sera demandé au pipeline, entièrement résolu et reproductible."""

    def __init__(
        self,
        *,
        prompt: str,
        negative_prompt: str | None,
        width: int,
        height: int,
        num_frames: int,
        fps: int,
        steps: int,
        guidance_scale: float,
        seed: int,
        notes: tuple[str, ...] = (),
    ) -> None:
        self.prompt = prompt
        self.negative_prompt = negative_prompt
        self.width = width
        self.height = height
        self.num_frames = num_frames
        self.fps = fps
        self.steps = steps
        self.guidance_scale = guidance_scale
        self.seed = seed
        self.notes = notes

    def pipeline_kwargs(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "width": self.width,
            "height": self.height,
            "num_frames": self.num_frames,
            # Le modèle est conditionné par la cadence : une même consigne à 8 et
            # à 24 images par seconde ne produit pas le même mouvement.
            "frame_rate": self.fps,
            "num_inference_steps": self.steps,
            "guidance_scale": self.guidance_scale,
        }

    def as_metrics(self) -> dict[str, Any]:
        métriques: dict[str, Any] = {
            "seed": self.seed,
            "steps": self.steps,
            "guidance_scale": self.guidance_scale,
            "width": self.width,
            "height": self.height,
            "num_frames": self.num_frames,
            "fps": self.fps,
            "duration_seconds": round(self.num_frames / max(1, self.fps), 3),
        }
        if self.notes:
            métriques["adjustments"] = list(self.notes)
        return métriques


def plan_generation(
    request: InferRequest,
    defaults: dict[str, Any] | None = None,
    *,
    exige_prompt: bool = True,
) -> PlanVideo:
    """Résout les paramètres d'un job : job > défauts du variant > contrat.

    Fonction pure, sans torch : c'est elle qui porte toute la logique testable de
    l'adaptateur — priorité des couches, alignements sur la grille du modèle,
    graine reproductible.
    """
    réglages = defaults or {}

    def valeur(clé: str) -> Any:
        for source in (request.get(clé), réglages.get(clé), CONTRACT_DEFAULTS.get(clé)):
            if source is not None:
                return source
        return None

    prompt = str(valeur("prompt") or "").strip()
    if exige_prompt and not prompt:
        raise WorkerError("prompt vide — le contrat text-to-video exige une description")

    négatif = valeur("negative_prompt")
    négatif = str(négatif).strip() or None if négatif is not None else None

    # `request.seed` est le champ de protocole, rempli par le superviseur ;
    # `seed` dans l'entrée est ce que l'utilisateur a tapé. Le protocole gagne,
    # sinon un rejeu par graine imposée serait silencieusement écrasé.
    graine = request.seed if request.seed is not None else valeur("seed")
    graine = draw_seed() if graine is None else int(graine)
    if graine < 0:
        raise WorkerError(f"graine négative : {graine}")

    largeur, note_l = aligner_pixels(_entier(valeur("width"), "width"), "width")
    hauteur, note_h = aligner_pixels(_entier(valeur("height"), "height"), "height")
    images, note_i = aligner_images(_entier(valeur("num_frames"), "num_frames"))
    notes = tuple(n for n in (note_l, note_h, note_i) if n)

    return PlanVideo(
        prompt=prompt,
        negative_prompt=négatif,
        width=largeur,
        height=hauteur,
        num_frames=images,
        fps=_entier(valeur("fps"), "fps"),
        steps=_entier(valeur("steps"), "steps"),
        guidance_scale=float(valeur("guidance_scale")),
        seed=graine,
        notes=notes,
    )


def _entier(valeur: Any, nom: str) -> int:
    try:
        entier = int(valeur)
    except (TypeError, ValueError) as exc:
        raise WorkerError(f"{nom} : entier attendu, reçu {valeur!r}") from exc
    if entier <= 0:
        raise WorkerError(f"{nom} = {entier} : valeur strictement positive attendue")
    return entier


def step_progress(step: int, total: int) -> int:
    """Pas de débruitage → pourcentage. Bornes réservées au reste du job."""
    return 5 + int(80 * min(step, total) / max(1, total))


# --- worker ------------------------------------------------------------------


class LtxVideoWorker(Worker):
    """Texte → vidéo par LTX-Video sur PyTorch/MPS."""

    name = "ltx-video"
    pipeline_attr = "LTXPipeline"
    capability = "text-to-video"
    generator_device = "cpu"

    def __init__(self) -> None:
        self.torch: Any = None
        self.pipe: Any = None
        self.export: Any = None
        self.defaults: dict[str, Any] = {}
        self.dtype_name = "bfloat16"
        self.caveats: list[str] = []
        # Plus haut `driver_allocated_memory` vu depuis le démarrage : c'est lui
        # qui fait le profil, pas le RSS (voir `diffusers_mps.peak_memory_bytes`).
        self._peak_driver = 0

    # --- chargement ----------------------------------------------------------

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        torch, pipeline_class, export = _import_runtime(self.pipeline_attr)
        self.torch = torch
        self.export = export
        self.defaults = dict(variant.get("defaults") or {})
        self.caveats = []

        if not torch.backends.mps.is_available():
            raise WorkerError(
                "backend MPS indisponible (torch.backends.mps.is_available() est faux) — "
                f"cet adaptateur ne sert que sur Apple Silicon ; vérifier que "
                f"runtimes/{ENV_NAME}/.venv utilise un Python arm64"
            )

        brut = str(variant.get("weights_path") or "").strip()
        ref = variant.get("ref") or "<ref>"
        if not brut or not Path(brut).is_dir():
            raise WorkerError(
                f"poids absents : {brut or '(chemin vide)'} n'est pas un dossier — "
                f"télécharger avec : ecurie pull {ref}"
            )
        chemin = Path(brut)

        self.dtype_name = torch_dtype_name(variant.get("quantization"))
        variante = detect_variant(chemin)

        try:
            pipe = pipeline_class.from_pretrained(
                str(chemin),
                variant=variante,
                torch_dtype=getattr(torch, self.dtype_name),
                use_safetensors=True,
                local_files_only=True,
                # Le VAE de LTX-Video 0.9.1 ne porte pas
                # `decoder.timestep_scale_multiplier`, que la classe
                # `AutoencoderKLLTXVideo` de diffusers attend depuis la 0.9.5.
                # Avec le chargement économe — le défaut quand accelerate est là —
                # ce paramètre reste un tenseur **meta**, sans données, et le
                # `.to("mps")` qui suit échoue par « Cannot copy out of meta
                # tensor ». Le chargement matérialisé l'initialise à sa valeur par
                # défaut, qui est celle qu'attend un checkpoint 0.9.1. Le prix est
                # un pic de mémoire hôte pendant le chargement, pas à l'exécution.
                low_cpu_mem_usage=False,
            )
        except Exception as exc:
            raise WorkerError(
                f"chargement impossible depuis {chemin} : {type(exc).__name__}: {exc}"
            ) from exc

        pipe.set_progress_bar_config(disable=True)
        pipe = pipe.to("mps")
        self.pipe = pipe

        vae = getattr(pipe, "vae", None)
        if vae is not None and hasattr(vae, "enable_tiling"):
            # Contrairement au chemin image, le pavage n'est pas ici un réglage
            # de manifeste laissé à la mesure : le VAE 3D décode toutes les
            # images d'un coup, et c'est le seul moment du job où le pic dépend
            # de la longueur de la séquence plutôt que de sa résolution.
            vae.enable_tiling()
            self.caveats.append("pavage du VAE 3D activé : le décodage vidéo se fait par tuiles")

        self.caveats.append(
            "peak_memory_bytes est le plus haut driver_allocated_memory relevé : "
            "torch.mps n'expose aucun pic, et le RSS ne compte pas la mémoire Metal"
        )
        self.caveats.append(
            "decoder.timestep_scale_multiplier absent du checkpoint 0.9.1 : diffusers "
            "l'initialise à sa valeur par défaut, celle que cette version attend"
        )
        return self._options()

    def _options(self) -> dict[str, Any]:
        """Ce que l'UI peut proposer, lu sur le pipeline réel plutôt que supposé."""
        pipe = self.pipe
        options: dict[str, Any] = {
            "pipeline": type(pipe).__name__,
            "device": "mps",
            "dtype": self.dtype_name,
            "scheduler": type(pipe.scheduler).__name__,
            "components": sorted(pipe.components),
            "pixel_grid": GRILLE_PIXELS,
            "frame_group": GROUPE_IMAGES,
            "caveats": self.caveats,
        }
        budget = self._mps_counters().get("mps_recommended_max_bytes")
        if budget:
            options["recommended_max_memory_bytes"] = budget
        return options

    # --- exécution -----------------------------------------------------------

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.pipe is None:
            raise WorkerError("infer avant load — aucun pipeline en mémoire")
        plan = plan_generation(
            request, self.defaults, exige_prompt=self.capability != "image-to-video"
        )
        progress(2, "préparation")

        appel = self._appel(request, plan, progress)

        départ = time.monotonic()
        try:
            sortie = self.pipe(**appel)
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte utile
            raise WorkerError(f"génération impossible : {type(exc).__name__}: {exc}") from exc
        # MPS est asynchrone : sans cette barrière, la durée mesurée est celle de
        # la mise en file d'attente, pas celle du calcul.
        self.torch.mps.synchronize()
        génération_ms = int((time.monotonic() - départ) * 1000)

        progress(90, "encodage mp4")
        images = sortie.frames[0]
        chemin = request.output_dir / VIDEO_NAME
        try:
            self.export(images, str(chemin), fps=plan.fps)
        except Exception as exc:  # noqa: BLE001 — l'écriture est le seul point qui dépend d'ffmpeg
            raise WorkerError(
                f"écriture du mp4 impossible ({type(exc).__name__}: {exc}) — "
                f"imageio-ffmpeg est-il dans runtimes/{ENV_NAME} ? `ecurie env sync {ENV_NAME}`"
            ) from exc

        compteurs = self._mps_counters()
        self.torch.mps.empty_cache()
        return InferResult(
            output={"video": VIDEO_NAME},
            metrics={
                **plan.as_metrics(),
                "frames_written": len(images),
                "dtype": self.dtype_name,
                "generate_ms": génération_ms,
                "ms_per_step": round(génération_ms / max(1, plan.steps), 1),
                "ms_per_frame": round(génération_ms / max(1, plan.num_frames), 1),
                **compteurs,
            },
        )

    def _appel(
        self, request: InferRequest, plan: PlanVideo, progress: ProgressFn
    ) -> dict[str, Any]:
        """Les arguments du pipeline. Le chemin image→vidéo y ajoute son image."""
        # Le device du générateur n'est pas un détail de style : le chemin
        # texte→vidéo accepte un générateur CPU — le chemin nominal et silencieux
        # sur MPS —, tandis que le chemin image→vidéo échantillonne le latent de
        # l'image sur l'appareil et refuse un générateur qui n'y est pas
        # (« Expected a 'mps' device type for generator but found 'cpu' »). D'où
        # un attribut de classe plutôt qu'une valeur en dur.
        générateur = self.torch.Generator(device=self.generator_device).manual_seed(plan.seed)
        return {
            **plan.pipeline_kwargs(),
            "generator": générateur,
            "num_videos_per_prompt": 1,
            "output_type": "np",
            "return_dict": True,
            "callback_on_step_end": _step_reporter(progress, plan.steps),
        }

    def unload(self) -> None:
        """Rend la mémoire au budget, pas seulement à Python."""
        self.pipe = None
        if self.torch is not None:
            gc.collect()
            try:
                self.torch.mps.empty_cache()
            except (AttributeError, RuntimeError):
                pass

    # --- mesures -------------------------------------------------------------

    def peak_memory_bytes(self) -> int | None:
        """Le plus haut relevé de `driver_allocated_memory`, et non le pic RSS.

        Même raison que pour le chemin image : le RSS ne voit pas la mémoire
        Metal, et `driver_allocated_memory` redescend — d'où un maximum tenu à
        chaque relevé plutôt qu'une lecture unique à la fin.
        """
        self._mps_counters()
        return max(self._peak_driver, peak_rss_bytes() or 0) or None

    def _mps_counters(self) -> dict[str, int]:
        mps = getattr(self.torch, "mps", None)
        if mps is None:
            return {}
        try:
            compteurs = {
                "mps_current_allocated_bytes": int(mps.current_allocated_memory()),
                "mps_driver_allocated_bytes": int(mps.driver_allocated_memory()),
                "mps_recommended_max_bytes": int(mps.recommended_max_memory()),
            }
        except (AttributeError, RuntimeError):
            return {}
        self._peak_driver = max(self._peak_driver, compteurs["mps_driver_allocated_bytes"])
        return compteurs


# --- accès au runtime --------------------------------------------------------


def _import_runtime(pipeline_attr: str) -> tuple[Any, Any, Any]:
    """Importe torch, le pipeline LTX et l'export vidéo, ou explique la réparation."""
    try:
        torch = importlib.import_module("torch")
        diffusers = importlib.import_module("diffusers")
        export_to_video = importlib.import_module("diffusers.utils").export_to_video
    except ImportError as exc:
        raise WorkerError(f"{MESSAGE_ENV} ({exc})") from exc
    pipeline_class = getattr(diffusers, pipeline_attr, None)
    if pipeline_class is None:
        raise WorkerError(
            f"{pipeline_attr} absent de diffusers {getattr(diffusers, '__version__', '?')} — "
            "LTX-Video demande diffusers >= 0.32 ; vérifier le pyproject de "
            f"runtimes/{ENV_NAME}"
        )
    return torch, pipeline_class, export_to_video


def _step_reporter(progress: ProgressFn, total: int) -> Any:
    """Callback de diffusers branché sur la progression du protocole.

    Un job vidéo dure des minutes : sans ces messages, l'Atelier n'aurait rien à
    afficher entre l'envoi et le fichier. Le contrat de diffusers impose de
    retourner le dictionnaire reçu — ne pas le faire vide les tenseurs que le
    pipeline réinjecte au pas suivant.
    """

    def au_pas(_pipe: Any, step: int, _timestep: Any, kwargs: dict) -> dict:
        fait = step + 1
        progress(step_progress(fait, total), f"débruitage {fait}/{total}")
        return kwargs

    return au_pas


if __name__ == "__main__":
    raise SystemExit(main(LtxVideoWorker))
