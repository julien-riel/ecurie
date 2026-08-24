"""Adaptateurs LTX-Video — ce qui se vérifie sans Apple Silicon.

Le point de testabilité est `plan_generation` et les deux alignements qu'il
applique : la grille de 32 pixels et les groupes de 8 images du VAE 3D. Ce sont
eux qui décident de ce que le modèle reçoit vraiment, et eux qui doivent laisser
une trace dans le manifeste du job — une vidéo de 97 images là où on en a
demandé 100 ne se devine pas en comptant les images du fichier.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from ecurie_runtime.envs import WORKER_MODULES, WORKER_MODULES_BY_CAPABILITY, worker_module
from ecurie_runtime.workers.base import InferRequest, WorkerError
from ecurie_runtime.workers.ltx_video import (
    CONTRACT_DEFAULTS,
    GRILLE_PIXELS,
    GROUPE_IMAGES,
    VIDEO_NAME,
    LtxVideoWorker,
    aligner_images,
    aligner_pixels,
    plan_generation,
    step_progress,
)
from ecurie_runtime.workers.ltx_video_i2v import LtxImageToVideoWorker, dimensions_pour

REPO_ROOT = Path(__file__).parents[3]
CONTRAT_T2V = REPO_ROOT / "registry" / "capabilities" / "text-to-video.json"
CONTRAT_I2V = REPO_ROOT / "registry" / "capabilities" / "image-to-video.json"


def demande(**champs) -> InferRequest:
    """Une requête d'inférence telle que le superviseur la transmet."""
    return InferRequest(
        job_id="j1",
        input=champs.pop("input", {"prompt": "un cheval qui traverse le cadre"}),
        params=champs.pop("params", {}),
        output_dir=champs.pop("output_dir", Path(".")),
        seed=champs.pop("seed", None),
    )


# --- imports paresseux -------------------------------------------------------


def test_modules_importables_sans_torch():
    """L'invariant qui rend la CI possible : rien du runtime au niveau du module."""
    code = (
        "import sys, ecurie_runtime.workers.ltx_video_i2v as m;"
        "print(m.LtxImageToVideoWorker.name, 'torch' in sys.modules, 'diffusers' in sys.modules)"
    )
    résultat = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert résultat.returncode == 0, résultat.stderr
    assert résultat.stdout.split() == ["ltx-video-i2v", "False", "False"]


# --- choix de l'adaptateur ---------------------------------------------------


def test_les_deux_capacites_video_ont_leur_adaptateur():
    """Sans ces entrées, le worker générique du runtime reçoit LTX et échoue au
    chargement — `AutoPipeline` ne connaît aucun pipeline vidéo."""
    assert worker_module("diffusers-mps", "text-to-video").endswith("ltx_video")
    assert worker_module("diffusers-mps", "image-to-video").endswith("ltx_video_i2v")
    # Le repli générique reste celui de l'image : c'est ce qu'on ne veut plus voir
    # servir une capacité vidéo.
    assert worker_module("diffusers-mps", None) == WORKER_MODULES["diffusers-mps"]
    assert ("diffusers-mps", "text-to-video") in WORKER_MODULES_BY_CAPABILITY


# --- fidélité au contrat de capacité -----------------------------------------


def test_defauts_du_worker_alignes_sur_le_contrat():
    """La duplication des défauts est assumée ; sa dérive ne l'est pas."""
    contrat = json.loads(CONTRAT_T2V.read_text())["input"]["properties"]
    attendus = {clé: champ["default"] for clé, champ in contrat.items() if "default" in champ}
    assert CONTRACT_DEFAULTS == attendus


def test_sorties_declarees_par_les_contrats():
    for chemin in (CONTRAT_T2V, CONTRAT_I2V):
        contrat = json.loads(chemin.read_text())["output"]
        assert contrat["required"] == ["video"]
        assert contrat["properties"]["video"]["contentMediaType"] == "video/mp4"
    assert VIDEO_NAME.endswith(".mp4")


# --- alignements sur la grille du modèle -------------------------------------


@pytest.mark.parametrize(
    ("demandé", "attendu"),
    [(512, 512), (832, 832), (500, 512), (480, 480), (300, 288), (16, 32)],
)
def test_les_dimensions_tombent_sur_la_grille(demandé, attendu):
    aligné, _ = aligner_pixels(demandé, "width")
    assert aligné == attendu
    assert aligné % GRILLE_PIXELS == 0


def test_une_dimension_ajustee_laisse_une_trace():
    aligné, note = aligner_pixels(500, "width")
    assert aligné == 512
    assert "500" in note and "512" in note
    assert aligner_pixels(512, "width")[1] is None


@pytest.mark.parametrize(
    ("demandé", "attendu"), [(81, 81), (97, 97), (25, 25), (100, 97), (8, 9), (129, 129)]
)
def test_les_images_tombent_sur_un_groupe_plus_une(demandé, attendu):
    aligné, _ = aligner_images(demandé)
    assert aligné == attendu
    assert (aligné - 1) % GROUPE_IMAGES == 0


def test_une_valeur_non_positive_est_refusee():
    for fonction, argument in ((aligner_pixels, ("width",)), (aligner_images, ())):
        with pytest.raises(WorkerError):
            fonction(0, *argument)


# --- résolution d'un job -----------------------------------------------------


def test_les_defauts_du_contrat_s_appliquent():
    plan = plan_generation(demande())
    assert (plan.width, plan.height) == (CONTRACT_DEFAULTS["width"], CONTRACT_DEFAULTS["height"])
    assert plan.num_frames == CONTRACT_DEFAULTS["num_frames"]
    assert plan.fps == CONTRACT_DEFAULTS["fps"]


def test_le_variant_corrige_le_contrat_et_le_job_tranche():
    plan = plan_generation(
        demande(input={"prompt": "p", "steps": 12}), {"steps": 30, "num_frames": 97}
    )
    assert plan.steps == 12  # le job
    assert plan.num_frames == 97  # le variant


def test_la_graine_du_protocole_gagne_sur_celle_de_l_entree():
    plan = plan_generation(demande(input={"prompt": "p", "seed": 7}, seed=42))
    assert plan.seed == 42


def test_un_prompt_vide_est_refuse_en_texte_vers_video():
    with pytest.raises(WorkerError) as échec:
        plan_generation(demande(input={"prompt": "   "}))
    assert "text-to-video" in str(échec.value)


def test_le_prompt_reste_facultatif_en_image_vers_video():
    """Le contrat `image-to-video` ne l'exige pas ; le manifeste avertit, il ne refuse pas."""
    plan = plan_generation(demande(input={}), exige_prompt=False)
    assert plan.prompt == ""


def test_la_cadence_conditionne_le_modele_et_le_fichier():
    """Dissocier les deux donnerait une vidéo dont le mouvement ne correspond pas
    à sa vitesse de lecture."""
    plan = plan_generation(demande(input={"prompt": "p", "fps": 24}))
    assert plan.pipeline_kwargs()["frame_rate"] == 24
    assert plan.as_metrics()["fps"] == 24


def test_les_ajustements_sont_rapportes_au_manifeste():
    plan = plan_generation(demande(input={"prompt": "p", "width": 500, "num_frames": 100}))
    ajustements = plan.as_metrics()["adjustments"]
    assert any("width" in note for note in ajustements)
    assert any("num_frames" in note for note in ajustements)


def test_la_duree_annoncee_suit_les_images_reellement_produites():
    plan = plan_generation(demande(input={"prompt": "p", "num_frames": 100, "fps": 25}))
    assert plan.num_frames == 97
    assert plan.as_metrics()["duration_seconds"] == pytest.approx(97 / 25, rel=1e-3)


# --- image → vidéo -----------------------------------------------------------


def test_la_taille_de_sortie_vient_de_l_image():
    largeur, hauteur, notes = dimensions_pour(1024, 576)
    assert (largeur, hauteur) == (1024, 576)
    assert notes == ()


def test_une_image_hors_grille_est_ajustee_et_le_dit():
    largeur, hauteur, notes = dimensions_pour(1000, 500)
    assert (largeur % GRILLE_PIXELS, hauteur % GRILLE_PIXELS) == (0, 0)
    assert notes and all("image d'entrée" in note for note in notes)


def test_les_deux_workers_different_par_leur_pipeline_et_leur_generateur():
    """Le chemin image→vidéo échantillonne sur l'appareil : un générateur CPU y
    est refusé par le pipeline, alors qu'il est le chemin nominal en texte→vidéo."""
    assert LtxVideoWorker.pipeline_attr == "LTXPipeline"
    assert LtxImageToVideoWorker.pipeline_attr == "LTXImageToVideoPipeline"
    assert LtxVideoWorker.generator_device == "cpu"
    assert LtxImageToVideoWorker.generator_device == "mps"


# --- progression -------------------------------------------------------------


def test_la_progression_reste_bornee():
    assert step_progress(0, 30) >= 5
    assert step_progress(30, 30) <= 90
    assert step_progress(60, 30) <= 90  # un pas de trop ne dépasse pas
