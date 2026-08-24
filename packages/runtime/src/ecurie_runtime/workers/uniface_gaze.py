"""Adaptateur `uniface`, chemin **direction du regard**.

Deux angles par visage : où les yeux pointent, ce qui n'est pas où la tête
pointe. Une tête de face avec l'œil en coin donne un lacet de tête nul et un
lacet de regard franc, et c'est précisément pour cela que le parc en fait deux
capacités plutôt qu'un contrat à cinq angles.

**MobileGaze rend des radians, `face-headpose` rend des degrés.** L'écart vient
d'amont, et le laisser passer serait le pire des pièges : deux capacités
voisines, deux sorties d'apparence identique, un facteur 57 entre les deux. Un
appelant qui trace les deux flèches verrait celle du regard immobile et
conclurait que le modèle ne fonctionne pas. La conversion est donc faite ici, une
fois, et le document produit porte son unité en toutes lettres.

Le dessin, lui, veut les radians d'origine : `draw_gaze` les reprojette
lui-même. On lui rend donc ce qu'il attend plutôt que de reconvertir en sens
inverse une valeur déjà arrondie.
"""

import math
from typing import Any

from ecurie_runtime.workers.base import (
    InferRequest,
    InferResult,
    ProgressFn,
    WorkerError,
    main,
)
from ecurie_runtime.workers.uniface_base import UnifaceWorker, ecrire_json

SORTIE_JSON = "gazes.json"
SORTIE_OVERLAY = "overlay.png"

MARGE = 0.15


class UnifaceGazeWorker(UnifaceWorker):
    """Direction du regard, en degrés — où l'œil va, non où la tête pointe."""

    name = "uniface-gaze"

    def traiter(
        self, image: Any, visages: list[Any], request: InferRequest, progress: ProgressFn
    ) -> InferResult:
        hauteur, largeur = image.shape[:2]
        relevé = []
        radians = []
        for rang, visage in enumerate(visages):
            progress(45 + int(35 * rang / max(1, len(visages))), f"visage {rang + 1}")
            try:
                regard = self.modele.estimate(self.recadrer(image, visage, MARGE))
            except Exception as exc:  # noqa: BLE001 — remonte avec le contexte
                raise WorkerError(f"regard impossible : {type(exc).__name__}: {exc}") from exc
            radians.append((float(regard.pitch), float(regard.yaw)))
            relevé.append(
                {
                    "bbox": self.boite(visage),
                    "confidence": round(float(visage.confidence), 4),
                    "pitch": round(math.degrees(float(regard.pitch)), 2),
                    "yaw": round(math.degrees(float(regard.yaw)), 2),
                }
            )

        ecrire_json(
            request.output_dir / SORTIE_JSON,
            {
                "image": {"width": int(largeur), "height": int(hauteur)},
                "model": self.options.get("weights"),
                "units": "degrés",
                "convention": (
                    "tangage positif regard vers le haut ; lacet positif regard vers la "
                    "gauche du sujet — les mêmes signes que `face-headpose`, pour que les "
                    "deux mesures s'additionnent au lieu de se contredire"
                ),
                "faces": relevé,
            },
        )

        sortie: dict[str, Any] = {"gazes": SORTIE_JSON, "count": len(relevé)}

        if bool(self.reglage(request, "overlay", True)):
            progress(85, "image annotée")
            from uniface.draw import draw_gaze

            vue = image.copy()
            for visage, (tangage, lacet) in zip(relevé, radians, strict=True):
                draw_gaze(vue, visage["bbox"], tangage, lacet, draw_bbox=True)
            self.ecrire_image(vue, request.output_dir / SORTIE_OVERLAY)
            sortie["overlay"] = SORTIE_OVERLAY

        return InferResult(
            output=sortie,
            metrics={"faces": len(relevé), "peak_memory_bytes": self.peak_memory_bytes()},
        )


if __name__ == "__main__":
    raise SystemExit(main(UnifaceGazeWorker))
