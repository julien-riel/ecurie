"""Transformation d'image — ce qui se vérifie sans Apple Silicon ni torch.

Comme pour les deux adaptateurs voisins, ces tests tournent dans le venv
d'Écurie, qui n'a ni torch ni diffusers : c'est la situation de la CI, et c'est
ce qui leur donne leur valeur. Ce qui est propre à celui-ci tient en une ligne de
`diffusers` que rien ne signale : **`strength` décide du nombre de pas réellement
exécutés**, et un produit qui tombe à zéro rend l'image d'entrée inchangée sans
lever, sans avertir, et avec un job marqué réussi.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from ecurie_runtime.envs import worker_module
from ecurie_runtime.workers.base import InferRequest, WorkerError
from ecurie_runtime.workers.diffusers_img2img import (
    ENV_NAME,
    IMAGE_NAME,
    DiffusersImg2ImgWorker,
    pas_effectifs,
    preparer,
)

REPO_ROOT = Path(__file__).parents[3]
CONTRAT = REPO_ROOT / "registry" / "capabilities" / "image-to-image.json"

TORCH_PRÉSENT = importlib.util.find_spec("torch") is not None
PIL_PRÉSENT = importlib.util.find_spec("PIL") is not None


def demande(**champs) -> InferRequest:
    return InferRequest(
        job_id="j1",
        input=champs.pop("input", {}),
        params=champs.pop("params", {}),
        output_dir=champs.pop("output_dir", Path(".")),
        seed=champs.pop("seed", None),
    )


# --- imports paresseux -------------------------------------------------------


def test_module_importable_sans_torch():
    code = (
        "import sys, ecurie_runtime.workers.diffusers_img2img as m;"
        "print(m.ENV_NAME, 'torch' in sys.modules, 'diffusers' in sys.modules)"
    )
    résultat = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert résultat.returncode == 0, résultat.stderr
    assert résultat.stdout.split() == [ENV_NAME, "False", "False"]


@pytest.mark.skipif(TORCH_PRÉSENT, reason="torch installé : le chemin d'échec ne s'y produit pas")
def test_load_sans_diffusers_nomme_la_reparation():
    with pytest.raises(WorkerError) as échec:
        DiffusersImg2ImgWorker().load({"weights_path": "/inexistant"})
    assert f"ecurie env sync {ENV_NAME}" in str(échec.value)


# --- aiguillage --------------------------------------------------------------


def test_la_capacite_choisit_cet_adaptateur_et_non_la_generation():
    """Trois capacités, un runtime, trois adaptateurs sur les mêmes octets."""
    assert worker_module("diffusers-mps", "image-to-image").endswith("diffusers_img2img")
    assert worker_module("diffusers-mps", "image-inpaint").endswith("diffusers_inpaint")
    assert worker_module("diffusers-mps", "text-to-image").endswith("diffusers_mps")


# --- fidélité au contrat -----------------------------------------------------


def test_le_contrat_borne_l_entree_comme_l_adaptateur_l_attend():
    """`max_side` est une borne mémoire, et le contrat doit l'exposer.

    Sans lui, une photo de téléphone arriverait telle quelle et le pic suivrait
    la surface — le contrôle d'admission, lui, aurait chiffré le job sur le pic
    du variant.
    """
    entrée = json.loads(CONTRAT.read_text())["input"]["properties"]
    assert "max_side" in entrée
    assert entrée["max_side"]["default"] == 1024
    assert entrée["strength"]["default"] == 0.6


def test_sortie_declaree_par_le_contrat():
    contrat = json.loads(CONTRAT.read_text())["output"]
    assert contrat["required"] == ["image"]
    assert IMAGE_NAME.endswith(".png")


# --- les pas réellement exécutés ---------------------------------------------


@pytest.mark.parametrize(
    ("steps", "strength", "attendu"),
    [
        (30, 1.0, 30),
        (30, 0.6, 18),
        (25, 0.6, 15),
        (30, 0.5, 15),
    ],
)
def test_strength_multiplie_les_pas(steps, strength, attendu):
    assert pas_effectifs(steps, strength) == attendu


def test_un_pas_au_minimum_meme_a_force_nulle():
    """`diffusers` accepte zéro pas et rend l'image d'entrée sans rien dire.

    Un job qui a l'air d'avoir réussi et qui n'a rien fait est le pire des deux
    échecs possibles : il ne se voit qu'à l'œil, sur la sortie.
    """
    assert pas_effectifs(30, 0.0) == 1
    assert pas_effectifs(1, 0.01) == 1


def test_une_force_hors_bornes_est_ramenee_dans_l_intervalle():
    assert pas_effectifs(20, 1.5) == 20
    assert pas_effectifs(20, -1) == 1


# --- préparation de l'image --------------------------------------------------


@pytest.mark.skipif(not PIL_PRÉSENT, reason="Pillow absent de ce venv")
def test_l_image_est_bornee_et_alignee_sur_la_grille(tmp_path):
    from PIL import Image

    chemin = tmp_path / "grande.png"
    Image.new("RGB", (2000, 1500), "white").save(chemin)

    préparée = preparer(chemin, max_side=1024)

    assert max(préparée.size) <= 1024
    assert préparée.width % 8 == 0 and préparée.height % 8 == 0
    # Le rapport de forme est conservé : recadrer choisirait à la place de
    # l'utilisateur ce qui reste de sa photo.
    assert abs(préparée.width / préparée.height - 2000 / 1500) < 0.02


@pytest.mark.skipif(not PIL_PRÉSENT, reason="Pillow absent de ce venv")
def test_une_petite_image_n_est_pas_agrandie(tmp_path):
    from PIL import Image

    chemin = tmp_path / "petite.png"
    Image.new("RGB", (320, 240), "white").save(chemin)

    préparée = preparer(chemin, max_side=1024)

    assert préparée.size == (320, 240)


@pytest.mark.skipif(not PIL_PRÉSENT, reason="Pillow absent de ce venv")
def test_une_taille_non_alignee_est_ramenee_sur_la_grille(tmp_path):
    from PIL import Image

    chemin = tmp_path / "impaire.png"
    Image.new("RGB", (517, 301), "white").save(chemin)

    préparée = preparer(chemin, max_side=1024)

    # Le VAE recadre ce qui n'est pas aligné : la sortie n'aurait plus la taille
    # qu'on croit lui avoir donnée.
    assert préparée.size == (512, 296)


# --- refus lisibles ----------------------------------------------------------


def test_infer_avant_load_le_dit():
    with pytest.raises(WorkerError, match="infer avant load"):
        DiffusersImg2ImgWorker().infer(demande(), lambda *_: None)


def test_une_image_manquante_nomme_le_champ():
    worker = DiffusersImg2ImgWorker()
    worker.pipe = object()
    with pytest.raises(WorkerError, match="« image » est obligatoire"):
        worker.infer(demande(input={"prompt": "un cheval"}), lambda *_: None)


def test_un_prompt_vide_est_refuse(tmp_path):
    worker = DiffusersImg2ImgWorker()
    worker.pipe = object()
    fichier = tmp_path / "a.png"
    fichier.write_bytes(b"x")
    with pytest.raises(WorkerError, match="prompt"):
        worker.infer(
            demande(input={"image": str(fichier), "prompt": "   "}, output_dir=tmp_path),
            lambda *_: None,
        )
