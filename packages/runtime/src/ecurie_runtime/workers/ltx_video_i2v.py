"""Adaptateur `diffusers-mps`, chemin **image → vidéo** (LTX-Video).

Mêmes poids que `ltx_video`, autre pipeline : `LTXImageToVideoPipeline` encode
l'image reçue comme première trame conditionnante, là où `LTXPipeline` part du
seul texte. C'est la même distinction qu'entre `diffusers_mps` et
`diffusers_img2img`, et elle se règle de la même façon — un module par capacité,
choisi par ce que le manifeste déclare (voir `envs.worker_module`).

Deux différences avec le chemin texte, et elles viennent du contrat :

**Le `prompt` est facultatif ici, et pourtant nécessaire.** Le contrat
`image-to-video` ne l'exige pas — certains modèles animent une image sans
consigne. Le caveat du manifeste de `ltx-video-2b-i2v` dit l'inverse pour
celui-ci : « sans prompt, la séquence produite est quasi fixe ». L'adaptateur ne
refuse donc pas, il avertit : refuser contredirait le contrat, se taire
laisserait croire à une panne devant une vidéo immobile.

**La taille de sortie vient de l'image, pas du contrat.** `image-to-video` ne
déclare ni `width` ni `height` : l'image d'entrée les porte. On l'ajuste sur la
grille de 32 px du VAE 3D en conservant le rapport de forme — ce que le caveat
du manifeste annonce déjà.
"""

from pathlib import Path
from typing import Any

from ecurie_runtime.workers.base import InferRequest, ProgressFn, WorkerError, main
from ecurie_runtime.workers.ltx_video import (
    LtxVideoWorker,
    PlanVideo,
    aligner_pixels,
)

# Ce que le caveat du manifeste annonce, et que l'adaptateur ne peut pas
# corriger : sans consigne de mouvement, ce modèle rend une séquence quasi fixe.
SANS_PROMPT = (
    "aucun prompt : LTX-Video anime d'après une consigne de mouvement, et la "
    "séquence produite sans elle est quasi fixe (caveat du manifeste)"
)


def dimensions_pour(largeur: int, hauteur: int) -> tuple[int, int, tuple[str, ...]]:
    """Taille de sortie déduite de l'image d'entrée, alignée sur la grille.

    Le rapport de forme est conservé au pixel de grille près : c'est ce que le
    caveat du manifeste annonce, et ce qu'un utilisateur constate en comparant
    son image et sa vidéo.
    """
    if largeur <= 0 or hauteur <= 0:
        raise WorkerError(f"image de taille invalide : {largeur}×{hauteur}")
    l_alignée, note_l = aligner_pixels(largeur, "width")
    h_alignée, note_h = aligner_pixels(hauteur, "height")
    notes = tuple(
        n.replace("(grille", "d'après l'image d'entrée (grille")
        for n in (note_l, note_h)
        if n
    )
    return l_alignée, h_alignée, notes


class LtxImageToVideoWorker(LtxVideoWorker):
    """Image → vidéo par LTX-Video sur PyTorch/MPS."""

    name = "ltx-video-i2v"
    pipeline_attr = "LTXImageToVideoPipeline"
    capability = "image-to-video"
    # Ce pipeline échantillonne le latent de l'image de départ sur l'appareil, et
    # refuse un générateur qui n'y est pas. La reproductibilité par graine reste
    # entière : c'est `manual_seed` qui la porte, pas le device.
    generator_device = "mps"

    def _appel(
        self, request: InferRequest, plan: PlanVideo, progress: ProgressFn
    ) -> dict[str, Any]:
        """Les arguments du pipeline, avec l'image de départ et sa taille."""
        image = self._charger_image(request)
        largeur, hauteur, notes = dimensions_pour(*image.size)
        if not plan.prompt:
            notes = notes + (SANS_PROMPT,)
        if notes:
            # `plan.notes` est ce que le manifeste du job rapportera : l'ajuster
            # ici plutôt que de laisser croire que la taille demandée a été tenue.
            plan.notes = tuple(plan.notes) + notes
        plan.width, plan.height = largeur, hauteur

        appel = super()._appel(request, plan, progress)
        appel["image"] = image
        return appel

    def _charger_image(self, request: InferRequest) -> Any:
        """L'image de départ, lue depuis le dossier du job.

        Le superviseur copie l'entrée dans le dossier du job et transmet un
        chemin relatif à ce dossier : c'est ce qui rend un job rejouable trois
        mois plus tard, quand le fichier d'origine a été renommé.
        """
        brut = request.get("image")
        if not brut:
            raise WorkerError("aucune image : le contrat image-to-video exige `image`")
        chemin = Path(str(brut))
        if not chemin.is_absolute():
            chemin = request.output_dir / chemin
        if not chemin.is_file():
            raise WorkerError(f"image d'entrée introuvable : {chemin}")

        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover — pillow est une dépendance de l'env
            raise WorkerError(f"pillow absent de l'environnement ({exc})") from exc
        try:
            return Image.open(chemin).convert("RGB")
        except Exception as exc:  # noqa: BLE001 — fichier illisible : le dire avec son chemin
            raise WorkerError(f"image illisible : {chemin} ({exc})") from exc


if __name__ == "__main__":
    raise SystemExit(main(LtxImageToVideoWorker))
