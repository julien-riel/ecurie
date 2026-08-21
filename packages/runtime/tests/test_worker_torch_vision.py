"""Adaptateurs torch-vision — ce qui se vérifie sans torch ni Apple Silicon.

Détourage et agrandissement partagent une pile et rien d'autre. Ce qui est
testable ici est ce qui casse en silence : un chemin résolu au mauvais endroit,
un format refusé trop tard, une couverture calculée sans diviser par la
profondeur du canal.

Le reste — la justesse d'un masque, la fidélité d'un agrandissement — relève du
golden set, qui a pour ces deux capacités une vérité terrain exacte : le masque
qui a servi à composer la scène, et l'image dont l'entrée est la réduction.
"""

import json

import pytest
from ecurie_runtime.envs import worker_module
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.birefnet import MEAN, STD, BiRefNetWorker, _couverture
from ecurie_runtime.workers.swin2sr import FENETRE, Swin2srWorker
from ecurie_runtime.workers.torch_vision import (
    REPAIR,
    TorchVisionWorker,
    import_torch,
    resolve_image,
    weights_dir,
)


def test_les_modules_s_importent_sans_torch():
    assert BiRefNetWorker.name == "birefnet"
    assert Swin2srWorker.name == "swin2sr"


def test_l_absence_du_runtime_nomme_la_reparation(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "torch", None)
    with pytest.raises(WorkerError) as échec:
        import_torch()
    assert REPAIR in str(échec.value)


def test_chaque_capacite_a_son_adaptateur_et_le_runtime_n_en_a_aucun_par_defaut():
    """Un runtime est une famille de bibliothèques, pas une promesse d'API
    commune : détourer et agrandir n'ont aucun appel en partage."""
    assert worker_module("torch-vision", "image-matting").endswith("birefnet")
    assert worker_module("torch-vision", "image-upscale").endswith("swin2sr")
    assert worker_module("torch-vision", None) is None


# --- résolution des entrées ------------------------------------------------------


def test_un_chemin_relatif_se_resout_dans_le_dossier_du_job(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "photo.jpg").write_bytes(b"x")
    assert resolve_image("inputs/photo.jpg", tmp_path) == tmp_path / "inputs" / "photo.jpg"


def test_un_chemin_absolu_reste_accepte(tmp_path):
    """Le banc d'essai en passe : ses charges types vivent dans le dépôt, pas
    dans le dossier du job."""
    image = tmp_path / "charge.png"
    image.write_bytes(b"x")
    assert resolve_image(str(image), tmp_path) == image


def test_une_image_vide_ou_absente_est_refusee(tmp_path):
    with pytest.raises(WorkerError, match="vide"):
        resolve_image("", tmp_path)
    with pytest.raises(WorkerError, match="introuvable"):
        resolve_image("absente.png", tmp_path)


def test_un_format_non_gere_liste_ceux_qui_le_sont(tmp_path):
    (tmp_path / "modele.svg").write_bytes(b"<svg/>")
    with pytest.raises(WorkerError) as échec:
        resolve_image("modele.svg", tmp_path)
    assert ".png" in str(échec.value)


def test_un_chemin_de_poids_vide_ne_devient_pas_le_dossier_courant(tmp_path):
    """`Path("")` vaut `Path(".")`, qui est bien un dossier : sans ce contrôle,
    `from_pretrained` chargerait le répertoire de travail."""
    with pytest.raises(WorkerError, match="aucun chemin"):
        weights_dir({"weights_path": ""})
    with pytest.raises(WorkerError, match="introuvable"):
        weights_dir({"weights_path": str(tmp_path / "absent")})


# --- ordre des réglages ----------------------------------------------------------


class _Requete:
    def __init__(self, **valeurs):
        self.valeurs = valeurs

    def get(self, clé, defaut=None):
        return self.valeurs.get(clé, defaut)


def test_l_ordre_des_reglages_entree_options_defauts():
    worker = TorchVisionWorker()
    worker.defaults = {"max_side": 1024}
    worker.options = {"max_side": 2048}

    assert worker.reglage(_Requete(max_side=512), "max_side", 256) == 512
    assert worker.reglage(_Requete(), "max_side", 256) == 2048
    worker.options = {}
    assert worker.reglage(_Requete(), "max_side", 256) == 1024
    worker.defaults = {}
    assert worker.reglage(_Requete(), "max_side", 256) == 256


# --- couverture du masque ---------------------------------------------------------


class _Alpha:
    """Un masque réduit à ce que `_couverture` en lit : son histogramme."""

    def __init__(self, histogramme):
        self._histogramme = histogramme

    def histogram(self):
        return self._histogramme


def _uniforme(valeur: int, pixels: int = 100) -> _Alpha:
    histogramme = [0] * 256
    histogramme[valeur] = pixels
    return _Alpha(histogramme)


def test_un_masque_entierement_opaque_couvre_tout():
    """Le bogue à ne pas écrire : oublier de diviser par 255 rend 255 au lieu
    de 1, et le chiffre passe pour un nombre de pixels."""
    assert _couverture(_uniforme(255)) == 1.0


def test_un_masque_vide_ne_couvre_rien():
    assert _couverture(_uniforme(0)) == 0.0


def test_une_transparence_partielle_compte_pour_sa_part():
    assert _couverture(_uniforme(128)) == pytest.approx(128 / 255, abs=1e-4)


def test_un_masque_sans_pixel_ne_divise_pas_par_zero():
    assert _couverture(_Alpha([0] * 256)) == 0.0


# --- constantes qui doivent rester d'accord avec l'amont ---------------------------


def test_la_normalisation_est_celle_d_imagenet():
    """Écrite ici parce que le dépôt de BiRefNet ne publie pas de processeur.
    Deviner une normalisation donne un masque plausible et faux."""
    assert MEAN == (0.485, 0.456, 0.406)
    assert STD == (0.229, 0.224, 0.225)


def test_la_fenetre_de_swin2sr_est_une_puissance_de_deux():
    assert FENETRE == 8


def test_les_contrats_declarent_les_sorties_que_les_adaptateurs_ecrivent(repo_root):
    """Une sortie exigée par le contrat et jamais écrite fait échouer le job
    après l'inférence — c'est-à-dire après avoir payé."""
    for capacité, attendues in (
        ("image-matting", {"image", "mask"}),
        ("image-upscale", {"image"}),
    ):
        contrat = json.loads(
            (repo_root / "registry" / "capabilities" / f"{capacité}.json").read_text()
        )
        assert set(contrat["output"]["required"]) == attendues
