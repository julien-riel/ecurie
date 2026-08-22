"""Adaptateur diffusers/MPS, chemin **transformation d'image entière**.

Troisième emploi des octets de SDXL, après la génération et la retouche masquée.
C'est le plus simple des trois, et sa simplicité mérite d'être dite : il n'y a ni
masque à fabriquer, ni UNet à neuf canaux à regretter, ni raccord de bord à
surveiller. `AutoPipelineForImage2Image` bruite l'image reçue à hauteur de
`strength` et redébruite depuis là — la transformation porte sur toute la
surface, donc le défaut nommé de `diffusers_inpaint` (« le raccord au bord de la
zone repeinte se voit ») n'a pas d'objet ici.

Deux choses valent d'être écrites, parce qu'elles surprennent au premier job.

**`strength` décide du nombre de pas réellement exécutés.** `diffusers` calcule
`int(steps × strength)` : à 0,6, un job de 30 pas n'en calcule que 18. C'est le
seul adaptateur du parc dont la durée dépend d'un paramètre autre que `steps`, et
c'est aussi pourquoi une valeur trop basse ne rend pas une image « à peine
transformée » mais une image **à peine débruitée** — en dessous d'une poignée de
pas, il reste du grain. Le plancher à un pas est posé ici plutôt que laissé au
pipeline, qui accepte zéro et rend l'image d'entrée telle quelle sans rien dire.

**`max_side` est une borne mémoire, pas un confort.** Le pic d'un modèle de
diffusion suit la surface traitée ; `sdxl-base@fp16` a trois pics mesurés selon
la résolution — 10,07 Gio au plus petit format, près de 16 au plus grand. Une
photo de téléphone donnée telle quelle emporterait le budget de la machine avant
d'avoir produit un pixel. Le contrat l'expose donc, comme celui de la retouche.
"""

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
)
from ecurie_runtime.workers.diffusers_mps import (
    ENV_NAME,
    MESSAGE_ENV,
    detect_variant,
    draw_seed,
    torch_dtype_name,
)

IMAGE_NAME = "image.png"

# Le multiple auquel SDXL travaille : une entrée qui n'y est pas alignée est
# recadrée par le VAE, et la sortie n'a plus la taille qu'on croit lui avoir
# donnée. La même constante que dans `diffusers_inpaint`, et pour la même raison.
PAS = 8


def _import_diffusers() -> tuple[Any, Any]:
    try:
        torch = importlib.import_module("torch")
        diffusers = importlib.import_module("diffusers")
    except ImportError as exc:
        raise WorkerError(f"{MESSAGE_ENV} ({exc})") from exc
    return torch, diffusers.AutoPipelineForImage2Image


def _aligner(valeur: int) -> int:
    return max(PAS, int(valeur) // PAS * PAS)


def preparer(image_path: Path, max_side: int) -> Any:
    """Ouvre l'image, la borne, et l'aligne sur la grille du modèle.

    Le rapport de forme est conservé : recadrer pour tomber sur un format carré
    choisirait à la place de l'utilisateur ce qui reste de sa photo, et rien dans
    le contrat ne le demande.
    """
    from PIL import Image

    with Image.open(image_path) as ouverte:
        image = ouverte.convert("RGB")

    plus_grand = max(image.size)
    if plus_grand > max_side:
        facteur = max_side / plus_grand
        taille = (_aligner(image.width * facteur), _aligner(image.height * facteur))
    else:
        taille = (_aligner(image.width), _aligner(image.height))
    return image.resize(taille, Image.LANCZOS) if taille != image.size else image


def pas_effectifs(steps: int, strength: float) -> int:
    """Ce que `diffusers` exécutera réellement — au moins un.

    Le pipeline accepte `int(steps × strength) == 0` et rend alors l'image
    d'entrée inchangée, sans avertissement : un job qui a l'air d'avoir réussi et
    qui n'a rien fait. Un pas au minimum transforme peu, mais transforme.
    """
    return max(1, int(steps * max(0.0, min(1.0, strength))))


class DiffusersImg2ImgWorker(Worker):
    """Transformation d'une image entière par un modèle de diffusion."""

    name = "diffusers-img2img"

    def __init__(self) -> None:
        self.torch: Any = None
        self.pipe: Any = None
        self.defaults: dict[str, Any] = {}
        self.dtype_name = "float16"

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        torch, auto_pipeline = _import_diffusers()
        self.torch = torch
        self.defaults = dict(variant.get("defaults") or {})

        if not torch.backends.mps.is_available():
            raise WorkerError(
                "backend MPS indisponible — cet adaptateur ne sert que sur Apple "
                f"Silicon ; vérifier que runtimes/{ENV_NAME}/.venv utilise un Python arm64"
            )

        brut = str(variant.get("weights_path") or "").strip()
        ref = variant.get("ref") or "<ref>"
        if not brut or not Path(brut).is_dir():
            raise WorkerError(
                f"poids absents : {brut or '(chemin vide)'} n'est pas un dossier — "
                f"télécharger avec : ecurie pull {ref}"
            )

        self.dtype_name = torch_dtype_name(variant.get("quantization"))
        # Les `allow_patterns` du manifeste ne rapatrient que `*fp16.safetensors`,
        # et `from_pretrained` cherche le fichier sans suffixe tant qu'on ne lui
        # nomme pas la variante. Les deux adaptateurs voisins ont payé ce défaut
        # avant celui-ci ; leur détection est réemployée telle quelle.
        variante = detect_variant(Path(brut))
        try:
            self.pipe = auto_pipeline.from_pretrained(
                str(brut),
                variant=variante,
                torch_dtype=getattr(torch, self.dtype_name),
                use_safetensors=True,
                local_files_only=True,
                safety_checker=None,
                requires_safety_checker=False,
            )
            self.pipe.set_progress_bar_config(disable=True)
            self.pipe = self.pipe.to("mps")
        except Exception as exc:  # noqa: BLE001 — remonte avec la réparation
            raise WorkerError(f"chargement impossible : {type(exc).__name__}: {exc}") from exc

        return {
            "variant": variante or "(sans suffixe)",
            "versions": self._versions(),
        }

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.pipe is None:
            raise WorkerError("infer avant load — aucun pipeline en mémoire")

        image_path = self._fichier(request, "image")
        prompt = str(self._reglage(request, "prompt", "") or "").strip()
        if not prompt:
            raise WorkerError("« prompt » est obligatoire : le contrat le déclare requis")

        max_side = int(self._reglage(request, "max_side", 1024))
        steps = int(self._reglage(request, "steps", 25))
        strength = float(self._reglage(request, "strength", 0.6))
        guidance = float(self._reglage(request, "guidance_scale", 7.0))
        graine = request.seed if request.seed is not None else draw_seed()

        progress(5, "lecture de l'image de départ")
        image = preparer(image_path, max_side)
        exécutés = pas_effectifs(steps, strength)

        générateur = self.torch.Generator(device="cpu").manual_seed(int(graine))
        progress(15, f"transformation en cours ({image.width}×{image.height}, {exécutés} pas)")
        départ = time.monotonic()
        try:
            sortie = self.pipe(
                prompt=prompt,
                negative_prompt=str(self._reglage(request, "negative_prompt", "") or "") or None,
                image=image,
                num_inference_steps=steps,
                strength=strength,
                guidance_scale=guidance,
                generator=générateur,
                num_images_per_prompt=1,
                output_type="pil",
                return_dict=True,
            )
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte
            raise WorkerError(f"transformation impossible : {type(exc).__name__}: {exc}") from exc
        self.torch.mps.synchronize()
        génération_ms = int((time.monotonic() - départ) * 1000)

        progress(92, "encodage PNG")
        produite = sortie.images[0]
        produite.save(request.output_dir / IMAGE_NAME, format="PNG")
        self.torch.mps.empty_cache()

        return InferResult(
            output={
                "image": IMAGE_NAME,
                "width": produite.width,
                "height": produite.height,
            },
            metrics={
                "steps": steps,
                # Ce que le pipeline a réellement calculé. L'écart avec `steps`
                # est la première question qu'on se pose devant une durée qui ne
                # correspond pas à ce qu'on a demandé.
                "steps_effectifs": exécutés,
                "strength": strength,
                "guidance_scale": guidance,
                "seed": int(graine),
                "generate_ms": génération_ms,
                "ms_per_step": round(génération_ms / max(1, exécutés), 1),
                "dtype": self.dtype_name,
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def unload(self) -> None:
        self.pipe = None
        if self.torch is not None:
            self.torch.mps.empty_cache()

    def peak_memory_bytes(self) -> int | None:
        if self.torch is None:
            return None
        try:
            return int(self.torch.mps.driver_allocated_memory())
        except Exception:  # noqa: BLE001 — une mesure ratée ne fait pas échouer un job
            return None

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

    def _versions(self) -> dict[str, str]:
        versions = {}
        for nom, module in (("torch", "torch"), ("diffusers", "diffusers")):
            try:
                importé = importlib.import_module(module)
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


if __name__ == "__main__":
    raise SystemExit(main(DiffusersImg2ImgWorker))
