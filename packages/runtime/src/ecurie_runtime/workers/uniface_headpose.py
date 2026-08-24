"""Adaptateur `uniface`, chemin **orientation de la tête**.

Trois angles par visage, obtenus d'un réseau qui prédit une matrice de rotation
puis la convertit en angles d'Euler. Le contrat fixe le signe des trois — menton
qui monte, sujet qui tourne vers sa gauche, tête qui penche vers son épaule
droite — parce qu'aucune convention ne s'impose dans ce domaine et que deux
modèles de la même capacité doivent rendre le même signe pour la même pose. Si
un futur variant sortait la convention inverse, c'est ici qu'il faudrait la
retourner, et non chez l'appelant.

Le cadrage compte : ces réseaux sont entraînés sur une vignette un peu plus large
que la boîte du détecteur. Lui donner la boîte nue fait dériver l'angle sans que
rien ne le signale — le job réussit, les nombres sont faux.
"""

from typing import Any

from ecurie_runtime.workers.base import (
    InferRequest,
    InferResult,
    ProgressFn,
    WorkerError,
    main,
)
from ecurie_runtime.workers.uniface_base import UnifaceWorker, ecrire_json

SORTIE_JSON = "poses.json"
SORTIE_OVERLAY = "overlay.png"

MARGE = 0.15


class UnifaceHeadposeWorker(UnifaceWorker):
    """Tangage, lacet, roulis — où la tête pointe, non où l'œil regarde."""

    name = "uniface-headpose"

    def traiter(
        self, image: Any, visages: list[Any], request: InferRequest, progress: ProgressFn
    ) -> InferResult:
        hauteur, largeur = image.shape[:2]
        relevé = []
        for rang, visage in enumerate(visages):
            progress(45 + int(35 * rang / max(1, len(visages))), f"visage {rang + 1}")
            try:
                pose = self.modele.estimate(self.recadrer(image, visage, MARGE))
            except Exception as exc:  # noqa: BLE001 — remonte avec le contexte
                raise WorkerError(
                    f"orientation impossible : {type(exc).__name__}: {exc}"
                ) from exc
            relevé.append(
                {
                    "bbox": self.boite(visage),
                    "confidence": round(float(visage.confidence), 4),
                    "pitch": round(float(pose.pitch), 2),
                    "yaw": round(float(pose.yaw), 2),
                    "roll": round(float(pose.roll), 2),
                }
            )

        ecrire_json(
            request.output_dir / SORTIE_JSON,
            {
                "image": {"width": int(largeur), "height": int(hauteur)},
                "model": self.options.get("weights"),
                "units": "degrés",
                "convention": (
                    "tangage positif menton levé ; lacet positif tête tournée vers la "
                    "gauche du sujet ; roulis positif tête penchée vers son épaule droite"
                ),
                "faces": relevé,
            },
        )

        sortie: dict[str, Any] = {"poses": SORTIE_JSON, "count": len(relevé)}

        if bool(self.reglage(request, "overlay", True)):
            progress(85, "image annotée")
            from uniface.draw import draw_head_pose

            vue = image.copy()
            for visage in relevé:
                draw_head_pose(
                    vue,
                    visage["bbox"],
                    visage["pitch"],
                    visage["yaw"],
                    visage["roll"],
                    draw_type="axis",
                    draw_bbox=True,
                )
            self.ecrire_image(vue, request.output_dir / SORTIE_OVERLAY)
            sortie["overlay"] = SORTIE_OVERLAY

        return InferResult(
            output=sortie,
            metrics={"faces": len(relevé), "peak_memory_bytes": self.peak_memory_bytes()},
        )


if __name__ == "__main__":
    raise SystemExit(main(UnifaceHeadposeWorker))
