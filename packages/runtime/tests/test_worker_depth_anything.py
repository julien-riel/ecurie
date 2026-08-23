"""Depth Anything 3 — ce qui se vérifie sans torch.

L'essentiel de cet adaptateur est du calcul sur des tableaux : normaliser une
carte, la coloriser, refuser une entrée. Tout cela s'éprouve sans charger 411
millions de paramètres, et doit l'être — c'est là que vivent les défauts
silencieux d'une carte de profondeur, qui a l'air plausible même quand elle est
fausse.
"""

import pytest
from ecurie_runtime.envs import worker_module
from ecurie_runtime.workers.base import InferRequest, WorkerError
from ecurie_runtime.workers.depth_anything import (
    DepthAnythingWorker,
    _coloriser,
    normaliser,
)


@pytest.fixture
def np():
    """numpy vit dans l'env du runtime, pas dans celui d'Écurie.

    Les trois premiers tests n'en ont pas besoin et doivent tourner partout :
    l'aiguillage et le refus d'un worker non chargé sont ce qui casse le plus
    souvent, et un fichier entier sauté les emporterait avec le reste.
    """
    return pytest.importorskip("numpy", reason="numpy n'est pas dans l'env d'Écurie")


def test_le_module_s_importe_sans_torch():
    assert DepthAnythingWorker.name == "depth-anything"


def test_la_capacite_choisit_cet_adaptateur():
    assert worker_module("depth-anything", "depth-estimation").endswith("depth_anything")


def test_un_worker_non_charge_le_dit():
    with pytest.raises(WorkerError, match="non chargé"):
        DepthAnythingWorker().infer(
            InferRequest(job_id="j", input={}, params={}, output_dir=None),
            lambda pct, note="": None,
        )


# --- normalisation ----------------------------------------------------------------


def test_une_carte_est_ramenee_entre_zero_et_un(np):
    plan = np.array([[2.0, 4.0], [6.0, 10.0]], dtype="float32")

    échelle = normaliser(np, plan, 2.0, 10.0)

    assert échelle.min() == 0.0
    assert échelle.max() == 1.0


def test_une_carte_plate_rend_zero_plutot_que_des_nan(np):
    """`far - near` vaut zéro sur un mur ou un fond uni. La division rendrait des
    NaN, qu'un PNG écrit en noir — le symptôme aurait alors la même tête qu'une
    estimation ratée."""
    plan = np.full((4, 4), 3.0, dtype="float32")

    échelle = normaliser(np, plan, 3.0, 3.0)

    assert not np.isnan(échelle).any()
    assert (échelle == 0).all()


def test_des_bornes_inversees_ne_produisent_pas_de_nan(np):
    plan = np.array([[1.0, 2.0]], dtype="float32")

    échelle = normaliser(np, plan, 5.0, 1.0)

    assert not np.isnan(échelle).any()


# --- colorisation -----------------------------------------------------------------


def test_la_colorisation_rend_trois_canaux_en_octets(np):
    échelle = np.linspace(0, 1, 16, dtype="float32").reshape(4, 4)

    couleurs = _coloriser(np, échelle, "turbo")

    assert couleurs.shape == (4, 4, 3)
    assert couleurs.dtype == np.uint8


def test_les_deux_extremes_ne_se_ressemblent_pas(np):
    """Un aperçu dont le proche et le lointain ont la même couleur ne sert à rien."""
    couleurs = _coloriser(np, np.array([[0.0, 1.0]], dtype="float32"), "turbo")

    assert not np.array_equal(couleurs[0][0], couleurs[0][1])


def test_une_palette_inconnue_retombe_sur_turbo_sans_lever(np):
    milieu = np.array([[0.5]], dtype="float32")

    assert np.array_equal(
        _coloriser(np, milieu, "inexistante"), _coloriser(np, milieu, "turbo")
    )


def test_les_valeurs_hors_bornes_sont_ecretees(np):
    """Une carte déjà normalisée ailleurs peut déborder de quelques millièmes ;
    l'indexation de la palette lèverait sur un indice hors limites."""
    couleurs = _coloriser(np, np.array([[-0.4, 1.7]], dtype="float32"), "magma")

    assert couleurs.shape == (1, 2, 3)


# --- SeedVR2 : la traduction des conventions de résolution -------------------------


def test_le_facteur_est_applique_au_petit_cote():
    """Le contrat parle d'un facteur, mflux attend une résolution absolue sur le
    petit côté. La traduction est tout ce que l'adaptateur ajoute ici."""
    from ecurie_runtime.workers.seedvr2 import resolution_cible

    assert resolution_cible(512, 512, 2, 4096) == 1024
    assert resolution_cible(800, 600, 3, 4096) == 1800


def test_le_plafond_mord_sur_le_grand_cote():
    """`max_side` borne le grand côté du résultat. Sur une image 16/9, appliquer
    le plafond au petit côté donnerait près du double de ce qui est demandé."""
    from ecurie_runtime.workers.seedvr2 import resolution_cible

    # 1920 × 2 = 3840 > 2048, donc le facteur retombe à 2048/1920.
    assert resolution_cible(1920, 1080, 2, 2048) == 1152


def test_une_image_portrait_prend_bien_sa_largeur_comme_petit_cote():
    from ecurie_runtime.workers.seedvr2 import resolution_cible

    assert resolution_cible(600, 800, 2, 4096) == 1200


def test_un_facteur_invalide_est_refuse():
    from ecurie_runtime.workers.base import WorkerError
    from ecurie_runtime.workers.seedvr2 import resolution_cible

    with pytest.raises(WorkerError, match="facteur"):
        resolution_cible(512, 512, -1, 4096)


def test_une_image_de_taille_nulle_est_refusee():
    from ecurie_runtime.workers.base import WorkerError
    from ecurie_runtime.workers.seedvr2 import resolution_cible

    with pytest.raises(WorkerError, match="taille nulle"):
        resolution_cible(0, 100, 2, 4096)


def test_la_capacite_upscale_a_deux_adaptateurs():
    """swin2sr interpole, SeedVR2 régénère : deux façons d'agrandir qui ne se
    remplacent pas."""
    assert worker_module("mflux", "image-upscale").endswith("seedvr2")
    assert worker_module("torch-vision", "image-upscale").endswith("swin2sr")
