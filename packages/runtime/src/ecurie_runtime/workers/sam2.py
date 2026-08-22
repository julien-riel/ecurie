"""Adaptateur torch-vision, chemin **segmentation guidée** : suivre ce qu'on montre.

Troisième adaptateur du runtime `torch-vision`, à côté de `birefnet` (détourage)
et `swin2sr` (agrandissement) — et la démonstration, une fois de plus, qu'un
runtime est une famille de bibliothèques et non une promesse d'API commune : les
trois ne partagent ni le chargement, ni l'appel, ni la sortie.

**Détourer et segmenter ne sont pas la même chose.** BiRefNet décide seul de ce
qui est au premier plan ; SAM 2.1 ne décide rien, il suit le point ou la boîte
qu'on lui donne. Les deux rendent un masque, et c'est tout ce qu'ils ont en
commun — d'où deux capacités, et non un paramètre de plus sur la première.

**Le modèle rend trois masques, et son propre score choisit.** L'objet, une de
ses parties, une sous-partie : c'est ce que `multimask_output` produit, avec un
recouvrement prédit par le réseau pour chacun. Ce score est une sortie
**entraînée**, pas une confiance déclarée après coup comme celle qu'un modèle de
langue improviserait — il vaut d'être lu, et les deux masques écartés restent
nommés dans `candidates` parce que le second est parfois celui qu'on voulait.

**Les coordonnées restent celles de l'image fournie.** Le processeur travaille
sur une image redimensionnée ; l'utilisateur, lui, lit des pixels sur son écran.
La mise à l'échelle est faite ici, dans les deux sens — les points entrent en
pixels d'origine, les masques ressortent à la taille d'origine par
`post_process_masks`.
"""

import importlib
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

ENV_NAME = "torch-vision"
MASK_NAME = "mask.png"
OVERLAY_NAME = "overlay.png"
CANDIDATES_NAME = "candidates.json"
REPAIR = f"ecurie env sync {ENV_NAME}"


def _import_runtime() -> tuple[Any, Any, Any]:
    """torch, `Sam2Model`, `Sam2Processor` — ou la commande qui répare l'env."""
    try:
        torch = importlib.import_module("torch")
        transformers = importlib.import_module("transformers")
        modèle = transformers.Sam2Model
        processeur = transformers.Sam2Processor
    except (ImportError, AttributeError) as exc:
        raise WorkerError(
            f"SAM 2 indisponible dans cet environnement ({exc}) — il demande "
            f"transformers ≥ 4.57 ; reconstruire l'env avec `{REPAIR}`"
        ) from exc
    return torch, modèle, processeur


def _device(torch: Any) -> str:
    return "mps" if torch.backends.mps.is_available() else "cpu"


class Sam2Worker(Worker):
    """Segmentation guidée par point ou par boîte."""

    name = "sam2"

    def __init__(self) -> None:
        self.torch: Any = None
        self.model: Any = None
        self.processor: Any = None
        self.defaults: dict[str, Any] = {}
        self.device = "cpu"

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        torch, Sam2Model, Sam2Processor = _import_runtime()
        self.torch = torch
        self.defaults = dict(variant.get("defaults") or {})

        brut = str(variant.get("weights_path") or "").strip()
        ref = variant.get("ref") or "<ref>"
        if not brut or not Path(brut).is_dir():
            raise WorkerError(
                f"poids absents : {brut or '(chemin vide)'} n'est pas un dossier — "
                f"télécharger avec : ecurie pull {ref}"
            )

        self.device = _device(torch)
        try:
            self.model = Sam2Model.from_pretrained(brut, local_files_only=True).to(self.device)
            self.model.eval()
            self.processor = Sam2Processor.from_pretrained(brut, local_files_only=True)
        except Exception as exc:  # noqa: BLE001 — remonte avec la réparation
            raise WorkerError(f"chargement impossible : {type(exc).__name__}: {exc}") from exc

        return {"device": self.device, "versions": self._versions()}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.model is None or self.processor is None:
            raise WorkerError("infer avant load — aucun modèle en mémoire")
        from PIL import Image

        torch = self.torch
        image_path = self._fichier(request, "image")
        max_side = int(self._reglage(request, "max_side", 2048))

        with Image.open(image_path) as ouverte:
            image = ouverte.convert("RGB")
        origine = image.size
        if max(origine) > max_side:
            facteur = max_side / max(origine)
            image = image.resize(
                (round(image.width * facteur), round(image.height * facteur)), Image.LANCZOS
            )

        points, étiquettes, boîte = self._invite(request, origine, image.size)
        if points is None and boîte is None:
            raise WorkerError(
                "aucune invite : donner au moins un point ou une boîte — sans elle, "
                "le modèle n'a rien à suivre (c'est le détourage qu'il faut, "
                "capacité image-matting)"
            )

        progress(15, "segmentation en cours")
        début = time.monotonic()
        entrées = self.processor(
            images=image,
            input_points=points,
            input_labels=étiquettes,
            input_boxes=boîte,
            return_tensors="pt",
        ).to(self.device)
        try:
            with torch.no_grad():
                sortie = self.model(**entrées, multimask_output=True)
        except Exception as exc:  # noqa: BLE001 — remonte en ev:error avec le contexte
            raise WorkerError(f"segmentation impossible : {type(exc).__name__}: {exc}") from exc
        if self.device == "mps":
            torch.mps.synchronize()
        calcul = time.monotonic() - début

        progress(80, "mise à l'échelle des masques")
        # `post_process_masks` rend une liste par image, et chaque élément a la
        # forme (invites, masques, H, W). Les deux premières dimensions se
        # ressemblent assez pour qu'on prenne l'une pour l'autre : le premier
        # essai indexait les invites en croyant indexer les masques, et échouait
        # sur « index 1 is out of bounds for dimension 0 with size 1 ».
        masques = self.processor.post_process_masks(
            sortie.pred_masks, [[origine[1], origine[0]]]
        )[0][0]
        scores = sortie.iou_scores.squeeze().tolist()
        scores = [scores] if isinstance(scores, float) else list(scores)

        # Le modèle propose l'objet, une partie, une sous-partie. On garde le
        # mieux noté et l'on nomme les autres : ils sont sur le disque, et le
        # second est parfois celui qu'on voulait.
        ordre = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        meilleur = ordre[0]
        retenu = self._ecrire_masque(masques[meilleur], request.output_dir / MASK_NAME)
        candidats = []
        for rang, index in enumerate(ordre[1:], start=1):
            nom = f"candidate-{rang}.png"
            self._ecrire_masque(masques[index], request.output_dir / nom)
            candidats.append({"mask": nom, "score": round(float(scores[index]), 4)})
        (request.output_dir / CANDIDATES_NAME).write_text(
            json.dumps(candidats, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        progress(92, "surimpression")
        self._overlay(image_path, request.output_dir / MASK_NAME, request.output_dir / OVERLAY_NAME)

        return InferResult(
            output={
                "mask": MASK_NAME,
                "overlay": OVERLAY_NAME,
                "candidates": CANDIDATES_NAME,
                "score": round(float(scores[meilleur]), 4),
                "coverage": round(retenu, 4),
            },
            metrics={
                "masks_proposed": len(scores),
                "device": self.device,
                "infer_ms": int(calcul * 1000),
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def unload(self) -> None:
        self.model = None
        self.processor = None
        if self.torch is not None and self.device == "mps":
            self.torch.mps.empty_cache()

    def peak_memory_bytes(self) -> int | None:
        if self.torch is not None and self.device == "mps":
            try:
                return int(self.torch.mps.driver_allocated_memory())
            except Exception:  # noqa: BLE001 — une mesure ratée ne fait pas échouer un job
                pass
        return peak_rss_bytes()

    # --- détails -------------------------------------------------------------

    def _invite(
        self, request: InferRequest, origine: tuple[int, int], travail: tuple[int, int]
    ) -> tuple[Any, Any, Any]:
        """Points et boîte de l'utilisateur, mis à l'échelle de l'image traitée.

        L'utilisateur donne des pixels de **son** image ; le modèle voit une
        image redimensionnée. Faire porter cette conversion au client
        l'obligerait à connaître `max_side`, qui est un réglage de mémoire.
        """
        fx = travail[0] / origine[0]
        fy = travail[1] / origine[1]

        bruts = self._reglage(request, "points", None) or []
        points, étiquettes = [], []
        for p in bruts:
            points.append([round(float(p["x"]) * fx), round(float(p["y"]) * fy)])
            étiquettes.append(1 if p.get("include", True) else 0)

        brute = self._reglage(request, "box", None)
        boîte = None
        if brute:
            boîte = [[[
                round(float(brute["x1"]) * fx),
                round(float(brute["y1"]) * fy),
                round(float(brute["x2"]) * fx),
                round(float(brute["y2"]) * fy),
            ]]]

        return (
            [[points]] if points else None,
            [[étiquettes]] if étiquettes else None,
            boîte,
        )

    def _ecrire_masque(self, masque: Any, cible: Path) -> float:
        """Écrit un masque binaire et rend la part de l'image qu'il couvre."""
        from PIL import Image

        plan = masque.squeeze()
        if plan.ndim > 2:
            plan = plan[0]
        binaire = (plan > 0).to("cpu").numpy()
        Image.fromarray((binaire * 255).astype("uint8"), mode="L").save(cible)
        return float(binaire.mean())

    def _overlay(self, image_path: Path, masque_path: Path, cible: Path) -> None:
        """Le masque teinté sur l'image : un contour qui a mordu ne se voit que là."""
        from PIL import Image

        with Image.open(image_path) as ouverte:
            fond = ouverte.convert("RGB")
        with Image.open(masque_path) as ouvert:
            masque = ouvert.convert("L").resize(fond.size, Image.NEAREST)
        teinte = Image.new("RGB", fond.size, (220, 40, 40))
        Image.composite(Image.blend(fond, teinte, 0.45), fond, masque).save(cible)

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
        for nom, module in (("torch", "torch"), ("transformers", "transformers")):
            try:
                importé = importlib.import_module(module)
            except ImportError:
                continue
            version = getattr(importé, "__version__", None)
            if version:
                versions[nom] = str(version)
        return versions


if __name__ == "__main__":
    raise SystemExit(main(Sam2Worker))
