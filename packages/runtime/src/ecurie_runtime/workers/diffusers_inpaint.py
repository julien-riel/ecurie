"""Adaptateur diffusers/MPS, chemin **retouche par zone masquée**.

Mêmes poids que `workers.diffusers_mps`, qui sert la génération d'image : le
dépôt SDXL déjà sur le disque, à la même révision. Ce que ce module ajoute n'est
pas un modèle, c'est un **masque** — et tout ce qui suit en découle.

**Le checkpoint de base sait retoucher, à un prix.** `AutoPipelineForInpainting`
accepte un UNet à quatre canaux et bascule sur un chemin où le masque est appliqué
au bruit latent plutôt que donné au réseau. Un checkpoint d'inpainting dédié en a
neuf, dont cinq entraînés pour cette tâche. Le nôtre n'en a que quatre : la
retouche marche, elle raccorde moins bien sur les grandes zones. C'est le
compromis assumé d'une capacité qui ne coûte aucun octet — un second variant, à
télécharger, reste la porte de sortie si la qualité ne suffit pas.

**Deux façons de désigner la zone, et une seule à la fois.** Un masque fourni
retouche un intérieur ; `expand` étend la toile et fabrique le masque de bordure.
Les accepter ensemble donnerait un job dont personne ne saurait dire ce qu'il a
repeint. Le contrat les déclare tous deux facultatifs parce que l'un OU l'autre
suffit ; l'adaptateur refuse les deux ensemble, et refuse aussi l'absence des
deux.

**`max_side` est une borne mémoire, pas un confort.** Le pic d'un modèle de
diffusion suit la surface traitée, et `sdxl-base@fp16` n'a pas un pic mais trois
selon la résolution — 10,07 Gio au plus petit format mesuré, près de 16 au plus
grand. Une photo de téléphone donnée telle quelle emporterait le budget de la
machine avant d'avoir produit un pixel.
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
MASK_NAME = "mask_used.png"

# Le multiple auquel SDXL travaille. Une entrée qui n'y est pas alignée est
# recadrée par le VAE, et le masque et l'image ne se superposent plus au pixel
# près — c'est le genre de décalage qu'on ne voit qu'au bord de la zone repeinte.
PAS = 8


def _import_diffusers() -> tuple[Any, Any]:
    try:
        torch = importlib.import_module("torch")
        diffusers = importlib.import_module("diffusers")
    except ImportError as exc:
        raise WorkerError(f"{MESSAGE_ENV} ({exc})") from exc
    return torch, diffusers.AutoPipelineForInpainting


def _aligner(valeur: int) -> int:
    return max(PAS, int(valeur) // PAS * PAS)


def preparer(
    image_path: Path,
    mask_path: Path | None,
    expand: dict[str, int] | None,
    max_side: int,
) -> tuple[Any, Any]:
    """Rend le couple (image, masque) prêt pour le pipeline, aligné et borné.

    L'ordre est imposé par la géométrie : on étend d'abord la toile, on borne
    ensuite. L'inverse rétrécirait une image pour l'agrandir aussitôt, et la
    zone ajoutée n'aurait pas la définition demandée.
    """
    from PIL import Image, ImageOps

    with Image.open(image_path) as ouverte:
        image = ouverte.convert("RGB")

    if expand:
        haut, bas, gauche, droite = (
            int(expand.get("top", 0)),
            int(expand.get("bottom", 0)),
            int(expand.get("left", 0)),
            int(expand.get("right", 0)),
        )
        if haut == bas == gauche == droite == 0:
            raise WorkerError(
                "« expand » ne demande aucune extension — donner au moins un côté, "
                "ou fournir un masque"
            )
        image = ImageOps.expand(image, border=(gauche, haut, droite, bas), fill=(127, 127, 127))
        # Le masque de bordure est fabriqué ici, et il est exact par construction :
        # blanc partout sauf sur le rectangle d'origine.
        masque = Image.new("L", image.size, 255)
        masque.paste(0, (gauche, haut, image.width - droite, image.height - bas))
    else:
        assert mask_path is not None
        with Image.open(mask_path) as ouvert:
            masque = ouvert.convert("L")
        if masque.size != image.size:
            masque = masque.resize(image.size, Image.NEAREST)

    plus_grand = max(image.size)
    if plus_grand > max_side:
        facteur = max_side / plus_grand
        taille = (_aligner(image.width * facteur), _aligner(image.height * facteur))
        image = image.resize(taille, Image.LANCZOS)
        masque = masque.resize(taille, Image.NEAREST)
    else:
        taille = (_aligner(image.width), _aligner(image.height))
        if taille != image.size:
            image = image.resize(taille, Image.LANCZOS)
            masque = masque.resize(taille, Image.NEAREST)

    return image, masque


class DiffusersInpaintWorker(Worker):
    """Retouche par zone masquée et extension de toile, par modèle de diffusion."""

    name = "diffusers-inpaint"

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
        # Le premier lancement a échoué là-dessus, et c'était prévisible : les
        # `allow_patterns` du manifeste ne rapatrient que `*fp16.safetensors`,
        # et `from_pretrained` cherche le fichier sans suffixe tant qu'on ne lui
        # nomme pas la variante. Le voisin `diffusers_mps` avait déjà payé ce
        # défaut ; sa fonction de détection est réemployée telle quelle.
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

        canaux = int(self.pipe.unet.config.in_channels)
        return {
            "unet_in_channels": canaux,
            "variant": variante or "(sans suffixe)",
            # Dit en clair ce que la prose du module explique : ce checkpoint
            # n'est pas entraîné pour la retouche, il est détourné pour elle.
            "dedicated_inpaint": canaux == 9,
            "versions": self._versions(),
        }

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.pipe is None:
            raise WorkerError("infer avant load — aucun pipeline en mémoire")

        image_path = self._fichier(request, "image", obligatoire=True)
        mask_path = self._fichier(request, "mask", obligatoire=False)
        expand = self._reglage(request, "expand", None)
        if mask_path is not None and expand and any(int(v or 0) for v in expand.values()):
            raise WorkerError(
                "« mask » et « expand » sont exclusifs : on retouche un intérieur, ou "
                "on étend un bord, jamais les deux dans le même job"
            )
        if mask_path is None and not expand:
            raise WorkerError(
                "aucune zone à repeindre : fournir « mask », ou « expand » avec au "
                "moins un côté"
            )

        prompt = str(self._reglage(request, "prompt", "") or "").strip()
        if not prompt:
            raise WorkerError("« prompt » est obligatoire : le contrat le déclare requis")
        max_side = int(self._reglage(request, "max_side", 1024))
        steps = int(self._reglage(request, "steps", 25))
        strength = float(self._reglage(request, "strength", 0.99))
        guidance = float(self._reglage(request, "guidance_scale", 7.5))
        graine = request.seed if request.seed is not None else draw_seed()

        progress(5, "préparation du masque")
        image, masque = preparer(image_path, mask_path, expand, max_side)

        générateur = self.torch.Generator(device="cpu").manual_seed(int(graine))
        progress(15, f"retouche en cours ({image.width}×{image.height})")
        départ = time.monotonic()
        try:
            sortie = self.pipe(
                prompt=prompt,
                negative_prompt=str(self._reglage(request, "negative_prompt", "") or "") or None,
                image=image,
                mask_image=masque,
                num_inference_steps=steps,
                strength=strength,
                guidance_scale=guidance,
                generator=générateur,
                num_images_per_prompt=1,
                output_type="pil",
                return_dict=True,
            )
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte
            raise WorkerError(f"retouche impossible : {type(exc).__name__}: {exc}") from exc
        self.torch.mps.synchronize()
        génération_ms = int((time.monotonic() - départ) * 1000)

        progress(92, "encodage PNG")
        produite = sortie.images[0]
        produite.save(request.output_dir / IMAGE_NAME, format="PNG")
        masque.save(request.output_dir / MASK_NAME, format="PNG")
        self.torch.mps.empty_cache()

        return InferResult(
            output={
                "image": IMAGE_NAME,
                "mask_used": MASK_NAME,
                "width": produite.width,
                "height": produite.height,
            },
            metrics={
                "steps": steps,
                "strength": strength,
                "guidance_scale": guidance,
                "seed": int(graine),
                "mode": "expand" if expand and mask_path is None else "mask",
                "generate_ms": génération_ms,
                "ms_per_step": round(génération_ms / max(1, steps), 1),
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

    def _fichier(self, request: InferRequest, nom: str, *, obligatoire: bool) -> Path | None:
        brut = str(self._reglage(request, nom, "") or "").strip()
        if not brut:
            if obligatoire:
                raise WorkerError(f"« {nom} » est obligatoire")
            return None
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
    raise SystemExit(main(DiffusersInpaintWorker))
