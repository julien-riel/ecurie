"""Adaptateur rtmlib, chemin **trajectoires articulaires 3D**.

Premier worker du parc qui ne passe ni par MLX ni par torch : rtmlib sert des
modèles ONNX, et onnxruntime est un troisième moteur d'inférence. Trois
conséquences, et aucune n'est cosmétique.

**Le pic se mesure au RSS, et c'est le seul cas du parc où il dit vrai.**
Partout ailleurs, le RSS ignore la mémoire Metal — le v0.3 l'a payé d'un facteur
38. Ici il n'y a pas de mémoire Metal : onnxruntime tourne sur CoreML ou sur le
CPU, et tout ce qu'il alloue est dans le RSS du processus.

**Le worker ne télécharge pas, et rtmlib voudrait le faire.** Ses deux poids —
le détecteur de personnes et le réseau de pose — sont désignés par des URL
câblées en dur, qu'il rapatrie au premier chargement dans `~/.cache/rtmlib`.
L'adaptateur passe donc les deux chemins **locaux** et refuse de démarrer s'ils
manquent, avec la commande qui les obtient. C'est une exception assumée au
principe « `ecurie pull` est le seul chemin vers le réseau » : ces artefacts ne
sont pas sur Hugging Face sous une forme que `pull` sache prendre, et le
manifeste la consigne comme une dette.

**Ce module ne produit pas de BVH.** Le contrat le déclare facultatif et faux par
défaut, et l'adaptateur refuse net qu'on le demande : aucun modèle de cette
chaîne ne rend de rotations, seulement des positions. Fabriquer une hiérarchie
d'os et un quaternion de swing par articulation est du code maison qu'aucune
mesure ne validerait — et une capacité qui rendrait un fichier d'animation dont
la torsion est nulle par construction survendrait ce qu'elle sait faire.
"""

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

ENV_NAME = "rtmlib"
REPAIR = f"ecurie env sync {ENV_NAME}"
MOTION_NAME = "motion.json"
PREVIEW_NAME = "preview.mp4"

VIDEOS = {".mp4", ".mov", ".m4v", ".avi", ".webm"}

# Les deux artefacts, dans le cache que rtmlib se donne. Nommés ici parce que
# l'adaptateur les passe explicitement : laisser rtmlib les résoudre lui-même
# reviendrait à laisser un worker atteindre le réseau.
CACHE = Path.home() / ".cache" / "rtmlib" / "hub" / "checkpoints"
DETECTEUR = "yolox_m_8xb8-300e_humanart-c2c7a14a.onnx"
POSE = "rtmw3d-x_8xb64_cocktail14-384x288-b0a0eab7_20240626.onnx"

# Les segments du squelette COCO-WholeBody qu'on trace sur la vidéo de contrôle.
# Le corps seul : les 68 points du visage et les 42 des mains feraient une tache
# là où l'on veut vérifier qu'une personne a été suivie.
OS = [
    (5, 7), (7, 9), (6, 8), (8, 10), (5, 6), (5, 11), (6, 12),
    (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
]


def _import_runtime() -> tuple[Any, Any, Any]:
    try:
        import cv2
        import numpy as np
        from rtmlib import Wholebody3d
    except ImportError as exc:
        raise WorkerError(
            f"runtime rtmlib indisponible dans cet environnement ({exc}) — "
            f"le reconstruire avec `{REPAIR}`"
        ) from exc
    return cv2, np, Wholebody3d


def lisser(pistes: list[list[list[float]]], facteur: float) -> list[list[list[float]]]:
    """Moyenne glissante exponentielle sur les positions, image après image.

    À zéro, la trajectoire tremble d'une image à l'autre — le réseau travaille
    par image et ne sait rien du temps. Trop haut, un geste rapide s'aplatit.
    """
    if facteur <= 0 or len(pistes) < 2:
        return pistes
    alpha = 1.0 - min(0.95, facteur)
    lissées = [pistes[0]]
    for image in pistes[1:]:
        précédente = lissées[-1]
        lissées.append(
            [
                [alpha * v + (1 - alpha) * p for v, p in zip(point, avant, strict=True)]
                for point, avant in zip(image, précédente, strict=True)
            ]
        )
    return lissées


class Rtmw3dWorker(Worker):
    """Trajectoires articulaires 3D depuis une vidéo, squelette COCO-WholeBody."""

    name = "rtmw3d"

    def __init__(self) -> None:
        self.cv2: Any = None
        self.np: Any = None
        self.model: Any = None
        self.defaults: dict[str, Any] = {}

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        cv2, np, Wholebody3d = _import_runtime()
        self.cv2, self.np = cv2, np
        self.defaults = dict(variant.get("defaults") or {})

        manquants = [n for n in (DETECTEUR, POSE) if not (CACHE / n).is_file()]
        if manquants:
            raise WorkerError(
                f"poids absents du cache rtmlib : {', '.join(manquants)} — ce runtime "
                f"n'est pas servi par `ecurie pull` (voir le manifeste) ; les obtenir "
                f"une fois par : uv run --project runtimes/{ENV_NAME} python -c "
                f"\"from rtmlib import Wholebody3d; Wholebody3d(mode='balanced')\""
            )

        try:
            self.model = Wholebody3d(
                det=str(CACHE / DETECTEUR),
                pose=str(CACHE / POSE),
                backend="onnxruntime",
                device="cpu",
            )
        except Exception as exc:  # noqa: BLE001 — remonte avec la réparation
            raise WorkerError(f"chargement impossible : {type(exc).__name__}: {exc}") from exc

        import onnxruntime

        return {
            "joints": 133,
            "providers": list(onnxruntime.get_available_providers()),
            "versions": {"onnxruntime": onnxruntime.__version__},
        }

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.model is None:
            raise WorkerError("infer avant load — aucun modèle en mémoire")
        cv2, np = self.cv2, self.np

        if bool(self._reglage(request, "bvh", False)):
            raise WorkerError(
                "cette chaîne ne produit pas de BVH : aucun de ses modèles ne rend "
                "de rotations, seulement des positions. Le contrat le déclare "
                "facultatif et faux par défaut ; un autre variant pourra l'honorer"
            )

        vidéo = self._fichier(request, "video")
        fps_voulu = float(self._reglage(request, "fps", 24))
        max_seconds = float(self._reglage(request, "max_seconds", 20))
        lissage = float(self._reglage(request, "smoothing", 0.3))
        aperçu = bool(self._reglage(request, "preview", True))

        progress(5, "ouverture de la vidéo")
        capture = cv2.VideoCapture(str(vidéo))
        if not capture.isOpened():
            raise WorkerError(f"vidéo illisible : {vidéo.name}")
        fps_source = capture.get(cv2.CAP_PROP_FPS) or 1.0
        pas = max(1, round(fps_source / max(0.1, fps_voulu)))
        plafond = int(max_seconds * fps_source)

        images, brutes = [], []
        index = 0
        while True:
            ok, image = capture.read()
            if not ok or index >= plafond:
                break
            if index % pas == 0:
                images.append(image)
            index += 1
        capture.release()
        if not images:
            raise WorkerError(f"aucune image décodée depuis {vidéo.name}")

        progress(20, f"suivi de {len(images)} image(s)")
        début = time.monotonic()
        trouvées = 0
        confiances: list[float] = []
        for rang, image in enumerate(images):
            try:
                points, scores, _, _ = self.model(image)
            except Exception as exc:  # noqa: BLE001 — remonte avec le contexte
                raise WorkerError(f"suivi impossible : {type(exc).__name__}: {exc}") from exc
            tableau = np.asarray(points)
            if tableau.size == 0:
                brutes.append([[0.0, 0.0, 0.0]] * 133)
                continue
            trouvées += 1
            confiances.append(float(np.asarray(scores)[0].mean()))
            brutes.append([[float(v) for v in point] for point in tableau[0]])
            if rang % 4 == 0:
                progress(20 + int(60 * rang / len(images)), f"image {rang + 1}/{len(images)}")
        calcul = time.monotonic() - début

        pistes = lisser(brutes, lissage)

        progress(85, "écriture des trajectoires")
        (request.output_dir / MOTION_NAME).write_text(
            json.dumps(
                {
                    "fps": round(fps_source / pas, 3),
                    "joints": 133,
                    "skeleton": "coco-wholebody",
                    "frames": [
                        {"index": i, "points": [[round(v, 4) for v in p] for p in image]}
                        for i, image in enumerate(pistes)
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        sortie: dict[str, Any] = {
            "motion": MOTION_NAME,
            "frames": len(images),
            "detected_frames": trouvées,
            "joints": 133,
            "confidence_mean": round(sum(confiances) / len(confiances), 4) if confiances else 0.0,
        }
        if aperçu:
            progress(92, "vidéo de contrôle")
            self._preview(images, pistes, request.output_dir / PREVIEW_NAME, fps_source / pas)
            sortie["preview"] = PREVIEW_NAME

        return InferResult(
            output=sortie,
            metrics={
                "frames": len(images),
                "detected_frames": trouvées,
                "sampled_fps": round(fps_source / pas, 3),
                "ms_per_frame": round(calcul * 1000 / len(images), 1),
                "infer_ms": int(calcul * 1000),
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def unload(self) -> None:
        self.model = None

    def peak_memory_bytes(self) -> int | None:
        # Le seul worker du parc dont le RSS dit la vérité : pas de mémoire Metal
        # à ignorer, onnxruntime allouant sur CoreML ou sur le CPU.
        return peak_rss_bytes()

    # --- détails -------------------------------------------------------------

    def _preview(self, images: list, pistes: list, cible: Path, fps: float) -> None:
        """Le squelette incrusté sur les images d'origine.

        C'est la seule sortie qu'un humain sait juger d'un coup d'œil : un JSON
        de 133 points par image ne dit pas si l'on a suivi la bonne personne, ni
        si l'on a suivi quoi que ce soit.
        """
        cv2 = self.cv2
        hauteur, largeur = images[0].shape[:2]
        codec = cv2.VideoWriter_fourcc(*"mp4v")
        écrivain = cv2.VideoWriter(str(cible), codec, max(1.0, fps), (largeur, hauteur))
        for image, points in zip(images, pistes, strict=True):
            vue = image.copy()
            for a, b in OS:
                pa, pb = points[a], points[b]
                if pa[0] or pa[1]:
                    cv2.line(
                        vue,
                        (int(pa[0]), int(pa[1])),
                        (int(pb[0]), int(pb[1])),
                        (40, 40, 220),
                        2,
                    )
            for point in points[:17]:
                if point[0] or point[1]:
                    cv2.circle(vue, (int(point[0]), int(point[1])), 3, (40, 220, 220), -1)
            écrivain.write(vue)
        écrivain.release()

    def _fichier(self, request: InferRequest, nom: str) -> Path:
        brut = str(self._reglage(request, nom, "") or "").strip()
        if not brut:
            raise WorkerError(f"« {nom} » est obligatoire")
        chemin = Path(brut).expanduser()
        if not chemin.is_absolute():
            chemin = request.output_dir / chemin
        if not chemin.is_file():
            raise WorkerError(f"{nom} introuvable : {chemin}")
        if chemin.suffix.lower() not in VIDEOS:
            raise WorkerError(
                f"format non géré : {chemin.suffix or '(sans extension)'} — "
                f"formats acceptés : {', '.join(sorted(VIDEOS))}"
            )
        return chemin

    def _reglage(self, request: InferRequest, nom: str, defaut: Any) -> Any:
        valeur = request.get(nom)
        if valeur is not None:
            return valeur
        if self.defaults.get(nom) is not None:
            return self.defaults[nom]
        return defaut


if __name__ == "__main__":
    raise SystemExit(main(Rtmw3dWorker))
