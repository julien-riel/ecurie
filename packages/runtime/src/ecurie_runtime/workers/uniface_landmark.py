"""Adaptateur `uniface`, chemin **points clés du visage**.

Deux familles de poids servent ce contrat par deux appels qui ne se ressemblent
pas, et l'adaptateur absorbe l'écart plutôt que de le laisser remonter :

- **PIPNet** décrit un visage à partir de sa boîte, et rend des points 2D en
  pixels de l'image — 98 points sur la convention WFLW, 68 sur 300W ;
- **Face Mesh** travaille à partir d'une région orientée déduite des cinq points
  d'ancrage, et rend un maillage 3D de 468 ou 478 points, dont la troisième
  coordonnée est une profondeur relative sans unité.

Le contrat rend donc `points_per_face` et `scheme`, et c'est délibéré : les
indices n'ont pas le même sens d'une convention à l'autre. Le point 33 est un
coin d'œil sur WFLW et un point de mâchoire sur 300W ; comparer deux sorties
sans lire leur convention est une faute que le contrat rend impossible à faire
sans le savoir.
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

SORTIE_JSON = "landmarks.json"
SORTIE_OVERLAY = "overlay.png"

# La convention d'annotation de chaque jeu de poids. Écrite ici parce qu'elle ne
# se déduit d'aucune sortie du réseau : c'est une propriété du jeu d'entraînement.
CONVENTIONS = {
    "pipnet_r18_wflw_98": "wflw-98",
    "pipnet_r18_300w_celeba_68": "300w-68",
    "2d_106": "insightface-106",
    "face_mesh": "mediapipe-468",
    "face_landmarker": "mediapipe-478",
}


class UnifaceLandmarkWorker(UnifaceWorker):
    """Géométrie fine du visage — plusieurs dizaines à plusieurs centaines de points."""

    name = "uniface-landmark"

    def traiter(
        self, image: Any, visages: list[Any], request: InferRequest, progress: ProgressFn
    ) -> InferResult:
        hauteur, largeur = image.shape[:2]
        cle = str(self.options.get("weights") or "")
        maillage = cle in ("face_mesh", "face_landmarker")

        liste = []
        for rang, visage in enumerate(visages):
            progress(45 + int(35 * rang / max(1, len(visages))), f"visage {rang + 1}")
            points = self._points(image, visage, maillage)
            liste.append(
                {
                    "bbox": self.boite(visage),
                    "confidence": round(float(visage.confidence), 4),
                    "points": [[round(float(v), 2) for v in point] for point in points],
                }
            )

        par_visage = len(liste[0]["points"]) if liste else 0
        ecrire_json(
            request.output_dir / SORTIE_JSON,
            {
                "image": {"width": int(largeur), "height": int(hauteur)},
                "model": cle,
                "scheme": CONVENTIONS.get(cle, cle),
                "points_per_face": par_visage,
                # Une troisième coordonnée est une profondeur relative, sans
                # unité et sans origine : le dire évite qu'on la prenne pour des
                # pixels ou pour des millimètres.
                "dimensions": 3 if maillage else 2,
                "faces": liste,
            },
        )

        sortie: dict[str, Any] = {
            "landmarks": SORTIE_JSON,
            "count": len(liste),
            "points_per_face": par_visage,
            "scheme": CONVENTIONS.get(cle, cle),
        }

        if bool(self.reglage(request, "overlay", True)):
            progress(85, "image annotée")
            vue = image.copy()
            rayon = _rayon(largeur, par_visage)
            for visage in liste:
                for point in visage["points"]:
                    x, y = int(point[0]), int(point[1])
                    if 0 <= x < largeur and 0 <= y < hauteur:
                        self.cv2.circle(vue, (x, y), rayon, (40, 220, 255), -1)
                x1, y1, x2, y2 = visage["bbox"]
                self.cv2.rectangle(vue, (x1, y1), (x2, y2), (60, 200, 60), 2)
            self.ecrire_image(vue, request.output_dir / SORTIE_OVERLAY)
            sortie["overlay"] = SORTIE_OVERLAY

        return InferResult(
            output=sortie,
            metrics={
                "faces": len(liste),
                "points_per_face": par_visage,
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def _points(self, image: Any, visage: Any, maillage: bool) -> Any:
        np = self.np
        try:
            if maillage:
                résultats = self.modele.predict(image, [visage])
                if not résultats:
                    return np.zeros((0, 3))
                return np.asarray(résultats[0].landmarks, dtype=float)
            return np.asarray(self.modele.get_landmarks(image, visage.bbox), dtype=float)
        except Exception as exc:  # noqa: BLE001 — remonte avec le contexte
            raise WorkerError(f"points clés impossibles : {type(exc).__name__}: {exc}") from exc


def _rayon(largeur: int, points: int) -> int:
    """Un point de 468 ne se trace pas comme un point de 68.

    À rayon constant, un maillage dense couvre le visage d'une tache uniforme et
    l'aperçu ne dit plus rien de ce qu'on voulait vérifier.
    """
    base = max(1, largeur // 400)
    return base if points > 200 else base + 1


if __name__ == "__main__":
    raise SystemExit(main(UnifaceLandmarkWorker))
