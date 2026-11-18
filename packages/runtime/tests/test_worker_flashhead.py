"""Tête parlante — ce qui se vérifie sans mlx, sans poids et sans Apple Vision.

La justesse de la tête se regarde dans la sortie d'`ecurie bench`, pas ici.
"""

import json
from pathlib import Path

import pytest
from ecurie_runtime.envs import worker_module
from ecurie_runtime.workers.base import InferRequest, WorkerError
from ecurie_runtime.workers.flashhead import (
    MENTION,
    TAILLE,
    FlashHeadWorker,
    bas_du_carre,
    boite_bouche,
    candidats_vendor,
    carre_autour_du_visage,
    carre_central,
    commande_remux,
)

CONTRAT = Path(__file__).parents[3] / "registry" / "capabilities" / "talking-head.json"


def test_le_carre_suit_le_cadrage_de_l_amont():
    """Deux largeurs de visage, le centre du visage à 55 % de la hauteur."""
    x1, y1, x2, y2 = carre_autour_du_visage((400, 300, 600, 500), 1920, 1080)
    assert x2 - x1 == y2 - y1 == 400
    assert (x1 + x2) / 2 == 500
    assert y1 == round(400 - 400 * 0.55)


def test_le_carre_est_decale_plutot_que_rogne():
    """L'amont rogne au bord, et le pipeline recentre ensuite : la tête se déplace."""
    x1, y1, x2, y2 = carre_autour_du_visage((0, 0, 200, 200), 1000, 1000)
    assert (x1, y1) == (0, 0)
    assert x2 - x1 == y2 - y1 == 400


def test_le_carre_ne_depasse_jamais_l_image():
    x1, y1, x2, y2 = carre_autour_du_visage((100, 100, 700, 700), 768, 1024)
    assert x2 - x1 == y2 - y1 == 768
    assert 0 <= x1 and x2 <= 768 and 0 <= y1 and y2 <= 1024


def test_un_visage_degenere_est_refuse():
    with pytest.raises(ValueError, match="dégénérée"):
        carre_autour_du_visage((300, 300, 300, 400), 1000, 1000)


def test_le_carre_central_sans_visage():
    assert carre_central(1920, 1080) == (420, 0, 1500, 1080)
    assert carre_central(768, 1024) == (0, 128, 768, 896)


def test_la_boite_de_la_bouche_entoure_les_levres_et_reste_dans_le_carre():
    lèvres = [(200.0, 330.0), (256.0, 320.0), (312.0, 330.0), (256.0, 350.0)]
    x1, y1, x2, y2 = boite_bouche(lèvres, TAILLE)
    assert x1 < 200 and x2 > 312 and y1 < 320 and y2 > 350
    assert boite_bouche([(10.0, 500.0), (60.0, 505.0)], TAILLE)[3] == TAILLE


def test_des_levres_degenerees_sont_refusees():
    with pytest.raises(ValueError, match="dégénéré"):
        boite_bouche([(100.0, 100.0), (100.0, 120.0)], TAILLE)


def test_le_repli_de_la_bouche_est_dans_le_bas_du_carre():
    x1, y1, x2, y2 = bas_du_carre(TAILLE)
    assert y1 >= TAILLE // 2 and y2 <= TAILLE and x1 < TAILLE // 2 < x2


def test_le_remux_copie_les_images_et_porte_la_mention(tmp_path):
    v, a, o = tmp_path / "v.mp4", tmp_path / "a.wav", tmp_path / "o.mp4"
    commande = commande_remux("ffmpeg", v, a, 4.0, o)
    assert commande[commande.index("-c:v") + 1] == "copy"
    assert f"comment={MENTION}" in commande
    assert commande[commande.index("-t") + 1] == "4.000"


def test_le_vendor_se_cherche_en_remontant_jusqu_a_runtimes(tmp_path):
    racine = tmp_path / "depot"
    (racine / "runtimes" / "mlx-flashhead").mkdir(parents=True)
    module = racine / "packages" / "runtime" / "workers" / "flashhead.py"
    module.parent.mkdir(parents=True)
    candidats = candidats_vendor(module, tmp_path)
    assert candidats[0] == racine / "runtimes" / "mlx-flashhead" / "vendor"
    assert candidats_vendor(module, tmp_path, "/ailleurs") == [Path("/ailleurs")]


def test_le_runtime_mene_a_cet_adaptateur():
    assert worker_module("mlx-flashhead", "talking-head") == "ecurie_runtime.workers.flashhead"


def test_le_contrat_annonce_ce_que_l_adaptateur_rend():
    contrat = json.loads(CONTRAT.read_text())
    assert contrat["human_subject"] == "synthesizes"
    assert set(contrat["input"]["required"]) == {"portrait", "audio"}
    assert set(contrat["output"]["required"]) <= {
        "video", "fps", "width", "height", "frames", "duration_seconds",
        "crop_box", "mouth_motion",
    }  # fmt: skip


def test_infer_refuse_avant_load(tmp_path):
    requête = InferRequest(job_id="j", input={}, params={}, output_dir=tmp_path)
    with pytest.raises(WorkerError, match="avant load"):
        FlashHeadWorker().infer(requête, lambda *_: None)
