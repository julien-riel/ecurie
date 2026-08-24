"""Adaptateur `uniface`, chemin **régions du visage**.

BiSeNet découpe un visage **cadré** : il reçoit un rectangle, il rend une carte
de la taille de ce rectangle. Or le contrat promet une carte aux dimensions de
l'image fournie, et ce n'est pas un caprice — un masque qu'il faudrait recoller
soi-même, en connaissant la boîte, n'est pas utilisable par un compositeur, et
sur une image à deux visages il y en aurait deux sans dire où les poser. Le
recollage est donc fait ici, une fois, à l'endroit qui connaît les boîtes.

Le nom des régions vient de la table d'uniface et n'est pas recopié : dix-neuf
libellés dupliqués dériveraient au premier changement d'amont, et une carte dont
la légende ment est pire qu'une carte sans légende.

Deux visages qui se chevauchent posent une question que le contrat doit trancher
plutôt que subir : le second écrase-t-il le premier ? Ici, non — les visages sont
traités du plus grand au plus petit et **un pixel déjà étiqueté n'est pas
réécrit**. Le visage au premier plan, qui est presque toujours le plus grand,
garde donc ses contours entiers.
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

SORTIE_MASQUE = "mask.png"
SORTIE_JSON = "regions.json"
SORTIE_OVERLAY = "overlay.png"

# La marge ajoutée autour de la boîte du détecteur avant de découper. BiSeNet est
# entraîné sur des visages cadrés large, front et menton compris : lui donner la
# boîte nue fait étiqueter en « peau » ce qui est du cheveu.
MARGE = 0.25


class UnifaceParseWorker(UnifaceWorker):
    """Découpage du visage en régions nommées, recollé aux dimensions de l'entrée."""

    name = "uniface-parse"

    def traiter(
        self, image: Any, visages: list[Any], request: InferRequest, progress: ProgressFn
    ) -> InferResult:
        np = self.np
        from uniface.draw import FACE_PARSING_LABELS

        hauteur, largeur = image.shape[:2]
        carte = np.zeros((hauteur, largeur), dtype=np.uint8)
        relevé = []

        for rang, visage in enumerate(visages):
            progress(45 + int(35 * rang / max(1, len(visages))), f"visage {rang + 1}")
            boite = self._boite_marge(image, visage)
            x1, y1, x2, y2 = boite
            try:
                masque = self.modele.parse(image[y1:y2, x1:x2])
            except Exception as exc:  # noqa: BLE001 — remonte avec le contexte
                raise WorkerError(
                    f"découpage impossible : {type(exc).__name__}: {exc}"
                ) from exc
            masque = np.asarray(masque, dtype=np.uint8)

            # Le visage au premier plan garde ses contours : on n'écrit que là où
            # rien n'a encore été écrit.
            fenêtre = carte[y1:y2, x1:x2]
            libre = fenêtre == 0
            fenêtre[libre] = masque[libre]

            présentes = [int(v) for v in np.unique(masque) if int(v) != 0]
            relevé.append(
                {
                    "bbox": self.boite(visage),
                    "crop": [int(v) for v in boite],
                    "confidence": round(float(visage.confidence), 4),
                    "regions": [
                        {
                            "id": indice,
                            "name": FACE_PARSING_LABELS[indice]
                            if indice < len(FACE_PARSING_LABELS)
                            else f"classe-{indice}",
                            "pixels": int((masque == indice).sum()),
                        }
                        for indice in présentes
                    ],
                }
            )

        self.ecrire_image(carte, request.output_dir / SORTIE_MASQUE)
        ecrire_json(
            request.output_dir / SORTIE_JSON,
            {
                "image": {"width": int(largeur), "height": int(hauteur)},
                "model": self.options.get("weights"),
                "scheme": "celebamask-hq-19",
                "labels": list(FACE_PARSING_LABELS),
                "faces": relevé,
            },
        )

        sortie: dict[str, Any] = {
            "mask": SORTIE_MASQUE,
            "regions": SORTIE_JSON,
            "count": len(relevé),
            "scheme": "celebamask-hq-19",
        }

        if bool(self.reglage(request, "overlay", True)):
            progress(85, "image annotée")
            from uniface.draw import vis_parsing_maps

            self.ecrire_image(
                vis_parsing_maps(image, carte), request.output_dir / SORTIE_OVERLAY
            )
            sortie["overlay"] = SORTIE_OVERLAY

        return InferResult(
            output=sortie,
            metrics={
                "faces": len(relevé),
                "regions_max": max((len(f["regions"]) for f in relevé), default=0),
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def _boite_marge(self, image: Any, visage: Any) -> tuple[int, int, int, int]:
        hauteur, largeur = image.shape[:2]
        x1, y1, x2, y2 = (float(v) for v in visage.bbox)
        dx, dy = (x2 - x1) * MARGE, (y2 - y1) * MARGE
        return (
            max(0, int(x1 - dx)),
            max(0, int(y1 - dy)),
            min(largeur, int(x2 + dx)),
            min(hauteur, int(y2 + dy)),
        )


if __name__ == "__main__":
    raise SystemExit(main(UnifaceParseWorker))
