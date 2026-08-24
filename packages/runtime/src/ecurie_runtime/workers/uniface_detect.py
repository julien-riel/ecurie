"""Adaptateur `uniface`, chemin **détection de visages**.

Le seul des six adaptateurs de la famille dont le détecteur *est* la tâche : les
cinq autres en chargent un en amont du leur, celui-ci n'a rien devant lui. C'est
pourquoi son variant nomme ses poids par `options.weights` comme les autres, mais
n'a pas de `options.detector` — il n'y a rien à mettre devant.

Ce qu'il rend et que la détection d'objets ne rend pas : cinq points d'ancrage
par visage — les deux yeux, le nez, les deux coins de la bouche. Ce sont eux qui
définissent la transformation qui redresse un visage, et sans eux ni l'empreinte
d'identité ni le découpage en régions ne sont calculables. Un détecteur qui n'en
rend pas peut servir cette capacité, mais pas alimenter les autres : le worker le
signale plutôt que d'écrire une liste vide là où l'on attendait des coordonnées.
"""

from typing import Any

from ecurie_runtime.workers.base import InferRequest, InferResult, ProgressFn, main
from ecurie_runtime.workers.uniface_base import UnifaceWorker, ecrire_json

SORTIE_JSON = "faces.json"
SORTIE_OVERLAY = "overlay.png"


class UnifaceDetectWorker(UnifaceWorker):
    """Boîtes et points d'ancrage : le socle de toute la famille visage."""

    name = "uniface-detect"
    tache_separee = False

    def traiter(
        self, image: Any, visages: list[Any], request: InferRequest, progress: ProgressFn
    ) -> InferResult:
        np = self.np
        hauteur, largeur = image.shape[:2]

        progress(70, "mise en forme")
        liste = []
        for visage in visages:
            points = np.asarray(visage.landmarks, dtype=float)
            liste.append(
                {
                    "bbox": self.boite(visage),
                    "confidence": round(float(visage.confidence), 4),
                    "landmarks": [[round(float(x), 2), round(float(y), 2)] for x, y in points]
                    if points.size
                    else [],
                }
            )

        ecrire_json(
            request.output_dir / SORTIE_JSON,
            {
                "image": {"width": int(largeur), "height": int(hauteur)},
                "detector": self.options.get("weights") or self.options.get("detector"),
                # Cinq points sont la convention d'alignement du parc. Un
                # détecteur qui en rend six — BlazeFace — ou zéro reste utile
                # ici, mais ne peut pas alimenter `face-embed` : le dire dans la
                # sortie évite d'avoir à le redécouvrir au job suivant.
                "landmarks_per_face": int(len(liste[0]["landmarks"])) if liste else 0,
                "faces": liste,
            },
        )

        sortie: dict[str, Any] = {"faces": SORTIE_JSON, "count": len(liste)}

        if bool(self.reglage(request, "overlay", True)):
            progress(85, "image annotée")
            from uniface.draw import draw_detections

            vue = image.copy()
            draw_detections(image=vue, faces=visages, vis_threshold=0.0, draw_score=True)
            self.ecrire_image(vue, request.output_dir / SORTIE_OVERLAY)
            sortie["overlay"] = SORTIE_OVERLAY

        return InferResult(
            output=sortie,
            metrics={
                "faces": len(liste),
                "confidence_max": round(max((f["confidence"] for f in liste), default=0.0), 4),
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )


if __name__ == "__main__":
    raise SystemExit(main(UnifaceDetectWorker))
