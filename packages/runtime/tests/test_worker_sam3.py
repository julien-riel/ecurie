"""SAM 3 — ce qui se vérifie sans mlx.

Deux choses lui sont propres et tiennent dans du code pur : le refus d'un job
sans concept, et la réduction de l'image. Le reste — l'encodage du texte, les
masques — demande les poids et vit dans `ecurie bench`.
"""

import pytest
from ecurie_runtime.envs import worker_module
from ecurie_runtime.workers.base import InferRequest, WorkerError
from ecurie_runtime.workers.sam3 import Sam3Worker, _reduire


def test_le_module_s_importe_sans_mlx():
    assert Sam3Worker.name == "sam3"


def test_la_capacite_a_deux_adaptateurs_selon_le_runtime():
    """Deux façons de désigner, deux runtimes, un seul contrat : SAM 2 suit un
    clic, SAM 3 suit un mot."""
    assert worker_module("mlx-vlm", "image-segment").endswith("sam3")
    assert worker_module("torch-vision", "image-segment").endswith("sam2")


def test_un_job_sans_concept_est_refuse_avec_la_capacite_qui_le_sert():
    """Servir un `prompt` générique rendrait un masque plausible que personne
    n'a demandé — le pire des deux, puisque rien ne le signalerait."""
    worker = Sam3Worker()
    worker._predictor = object()  # noqa: SLF001 — on n'éprouve que la garde d'entrée

    with pytest.raises(WorkerError) as échec:
        worker.infer(
            InferRequest(job_id="j", input={"image": "x.png"}, params={}, output_dir=None),
            lambda pct, note="": None,
        )

    assert "prompt" in str(échec.value)
    assert "sam2-hiera-small" in str(échec.value)


def test_un_worker_non_charge_le_dit_avant_de_regarder_l_entree():
    with pytest.raises(WorkerError, match="non chargé"):
        Sam3Worker().infer(
            InferRequest(job_id="j", input={}, params={}, output_dir=None),
            lambda pct, note="": None,
        )


# --- réduction de l'image ---------------------------------------------------------


# PIL n'est pas dans l'environnement d'Écurie, seulement dans celui du runtime :
# le filtre se passe donc en paramètre, et sa valeur n'a aucune importance ici.
FILTRE = object()


class ImageFeinte:
    """Le strict nécessaire de PIL pour `_reduire` : une taille, un redimensionnement."""

    def __init__(self, largeur: int, hauteur: int) -> None:
        self.size = (largeur, hauteur)
        self.width = largeur
        self.height = hauteur

    def resize(self, taille, _filtre):
        return ImageFeinte(*taille)


def test_une_image_deja_petite_traverse_intacte():
    image = ImageFeinte(800, 600)
    assert _reduire(image, 1024, FILTRE) is image


def test_le_plus_grand_cote_est_ramene_sous_la_borne():
    """Le coût suit la surface : doubler le côté quadruple le travail."""
    réduite = _reduire(ImageFeinte(2048, 1024), 1024, FILTRE)
    assert réduite.size == (1024, 512)


def test_les_proportions_sont_gardees_sur_un_format_portrait():
    réduite = _reduire(ImageFeinte(600, 3000), 1000, FILTRE)
    assert réduite.size == (200, 1000)


def test_une_borne_nulle_ou_negative_ne_reduit_rien():
    image = ImageFeinte(4000, 4000)
    assert _reduire(image, 0, FILTRE) is image


def test_la_reduction_ne_produit_jamais_un_cote_nul():
    """Une image très allongée arrondirait son petit côté à zéro, et PIL lève."""
    réduite = _reduire(ImageFeinte(5000, 3), 100, FILTRE)
    assert min(réduite.size) >= 1
