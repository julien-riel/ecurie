"""Adaptateur `uniface`, chemin **empreinte d'identité**.

Le seul des six qui désigne quelqu'un plutôt que de mesurer quelque chose, et le
contrat le déclare : `human_subject: identifies`. Ce n'est pas une précaution
oratoire — c'est ce qui permet à l'UI, au registre et à qui relit un manifeste de
distinguer cette capacité de ses cinq voisines, qui décrivent un visage sans
jamais le nommer.

L'alignement n'est pas un détail d'implémentation, c'est la capacité elle-même.
Le réseau reçoit un carré de 112 pixels où les cinq points d'ancrage ont été
amenés sur une position canonique par une similitude ; c'est cette normalisation
qui fait qu'une même personne de trois quarts et de face donne deux vecteurs
proches. Un recadrage sur la boîte, sans alignement, donnerait un vecteur qui
mesure surtout la pose. `uniface.face_alignment` s'en charge à partir des cinq
points du détecteur — d'où l'exigence, ici et nulle part ailleurs dans la
famille, d'un détecteur qui les rende.

`compare_to` est la seule façon de vérifier cette capacité sans index : un
vecteur de 512 nombres ne se relit pas, un cosinus se lit. Le contrat ne fixe
aucun seuil de décision, et c'est délibéré — le seuil au-delà duquel on affirme
que deux photos montrent la même personne dépend du modèle et du risque qu'on
accepte de prendre, et ce n'est pas à un adaptateur d'en décider.
"""

from typing import Any

from ecurie_runtime.workers.base import (
    InferRequest,
    InferResult,
    ProgressFn,
    WorkerError,
    main,
)
from ecurie_runtime.workers.uniface_base import (
    UnifaceWorker,
    ecrire_json,
    resolve_image,
)

SORTIE_JSON = "embeddings.json"

# Nombre de points d'ancrage qu'exige l'alignement canonique. BlazeFace en rend
# six et les détecteurs sans points zéro : les uns comme les autres servent
# `face-detect`, aucun ne peut alimenter celle-ci.
POINTS_ALIGNEMENT = 5


class UnifaceEmbedWorker(UnifaceWorker):
    """Visage redressé vers un vecteur — la reconnaissance faciale du parc."""

    name = "uniface-embed"

    def traiter(
        self, image: Any, visages: list[Any], request: InferRequest, progress: ProgressFn
    ) -> InferResult:
        normaliser = bool(self.reglage(request, "normalize", True))
        vecteurs = []
        for rang, visage in enumerate(visages):
            progress(45 + int(30 * rang / max(1, len(visages))), f"visage {rang + 1}")
            vecteur = self._encoder(image, visage, normaliser)
            vecteurs.append(
                {
                    "bbox": self.boite(visage),
                    "confidence": round(float(visage.confidence), 4),
                    "embedding": [round(float(v), 6) for v in vecteur],
                }
            )

        dimensions = len(vecteurs[0]["embedding"]) if vecteurs else 0
        similarité: float | None = None
        comparaison = self.reglage(request, "compare_to", None)
        if comparaison:
            progress(80, "seconde image")
            similarité = self._comparer(vecteurs, comparaison, request, normaliser)

        ecrire_json(
            request.output_dir / SORTIE_JSON,
            {
                # Le modèle est écrit dans le document, et ce n'est pas une
                # commodité : deux modèles de cette capacité rendent des vecteurs
                # de même longueur qui n'appartiennent pas au même espace. Un
                # cosinus entre les deux est un nombre qui ne veut rien dire, et
                # rien d'autre que cette ligne ne l'empêcherait d'être calculé.
                "model": self.options.get("weights"),
                "detector": self.options.get("detector"),
                "normalized": normaliser,
                "dimensions": dimensions,
                "similarity": similarité,
                "faces": vecteurs,
            },
        )

        sortie: dict[str, Any] = {
            "embeddings": SORTIE_JSON,
            "count": len(vecteurs),
            "dimensions": dimensions,
        }
        if similarité is not None:
            sortie["similarity"] = similarité

        return InferResult(
            output=sortie,
            metrics={
                "faces": len(vecteurs),
                "dimensions": dimensions,
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    # --- détails -------------------------------------------------------------

    def _encoder(self, image: Any, visage: Any, normaliser: bool) -> Any:
        np = self.np
        points = np.asarray(visage.landmarks, dtype=float)
        if points.shape[0] != POINTS_ALIGNEMENT:
            raise WorkerError(
                f"ce détecteur rend {points.shape[0]} point(s) d'ancrage, il en faut "
                f"{POINTS_ALIGNEMENT} pour redresser un visage — choisir un détecteur "
                "qui les produit (`options.detector`), par exemple retinaface_mnet_v2"
            )
        try:
            if normaliser:
                vecteur = self.modele.get_normalized_embedding(image, points)
            else:
                vecteur = self.modele.get_embedding(image, points)
        except Exception as exc:  # noqa: BLE001 — remonte avec le contexte
            raise WorkerError(f"encodage impossible : {type(exc).__name__}: {exc}") from exc
        return np.asarray(vecteur, dtype=float).reshape(-1)

    def _comparer(
        self,
        vecteurs: list[dict[str, Any]],
        chemin: Any,
        request: InferRequest,
        normaliser: bool,
    ) -> float | None:
        """Le cosinus entre le plus grand visage de chaque image, ou None.

        Rendre None plutôt qu'échouer quand l'une des deux n'a pas de visage :
        l'absence est un résultat, et le job a produit ses vecteurs. C'est le
        `count` de la sortie qui dit ce qui a été trouvé.
        """
        np = self.np
        seconde = self._lire(resolve_image(chemin, request.output_dir, "compare_to"))
        autres = self.detecter(seconde, request)
        if not autres or not vecteurs:
            return None
        b = self._encoder(seconde, autres[0], normaliser)
        a = np.asarray(vecteurs[0]["embedding"], dtype=float)
        dénominateur = float(np.linalg.norm(a) * np.linalg.norm(b))
        if dénominateur <= 0:
            return None
        return round(float(a @ b) / dénominateur, 4)


if __name__ == "__main__":
    raise SystemExit(main(UnifaceEmbedWorker))
