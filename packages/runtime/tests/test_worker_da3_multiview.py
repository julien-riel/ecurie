"""Les multi-vues — ce qui se vérifie sans poids, sans MPS et sans venv de runtime.

Cinq pièces tiennent dans du code pur : le câblage (runtime, capacité) →
adaptateur, la résolution d'un champ **tableau de fichiers** — le premier du parc,
et cette capacité en est le premier employeur —, la lecture des extrinsèques
world-to-camera, le cadrage du contrat revérifié dans l'adaptateur, et le
contrôle des fichiers laissés dans le dossier du job.

**Les deux tests qui comptent le plus sont ceux des extrinsèques et des traces.**
Le premier parce que confondre `t` et `-Rᵀt` produit un arc de caméras plausible
et faux, que rien ne signale : le job réussit, l'aperçu montre une jolie courbe,
et les poses sont ailleurs. Le second parce que l'export d'amont, appelé par son
chemin habituel, dépose un `depth_vis/` d'un JPEG par vue et un `scene.jpg` qui
n'est que la première vue — deux choses que le contrat ne déclare pas et que
`job.files` ne verrait jamais.

Rien ici n'importe torch, numpy, trimesh ni PIL, pas plus que l'adaptateur
lui-même au niveau de son module.
"""

import json
from pathlib import Path

import pytest
from ecurie_runtime.envs import NOT_YET, worker_module
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.da3_multiview import (
    APERCU,
    CAMERAS,
    NUAGE,
    POINTS_MAX,
    RES_DEFAUT,
    VUES_MAX,
    VUES_MIN,
    DA3MultiviewWorker,
    _traces,
    centres_cameras,
    couleur_vue,
    plan_multivue,
    resolve_vues,
    weights_dir,
)


def vues(dossier: Path, combien: int, suffixe: str = ".png") -> list[str]:
    """`combien` fichiers d'image plausibles, dont le contenu n'importe pas ici."""
    créés = []
    for rang in range(combien):
        chemin = dossier / f"{rang:03d}-vue{suffixe}"
        chemin.write_bytes(b"\x89PNG\r\n\x1a\n")
        créés.append(str(chemin))
    return créés


# --- câblage ------------------------------------------------------------------


def test_la_capacite_est_servie_par_son_adaptateur():
    assert worker_module("depth-anything", "multiview-to-3d").endswith("da3_multiview")


def test_le_runtime_sert_aussi_la_profondeur_monoculaire():
    """Deux capacités sur le même env, et deux adaptateurs distincts.

    Elles partagent la pile et la famille de poids, pas leur sortie : l'une rend
    une carte par image, l'autre relie les images entre elles. Le runtime n'a
    donc aucun adaptateur « par défaut » — c'est le §5.2 : un runtime est une
    famille de bibliothèques, pas une promesse d'API commune.

    Sans capacité, il n'y a donc rien à servir — et le message le dit en nommant
    les deux, plutôt que de rendre « adaptateur non livré ». L'entrée de `NOT_YET`
    a manqué pendant les quatre jours où ce runtime n'en servait qu'une, et
    l'oubli ne se voyait pas : c'est la seconde capacité qui l'a révélé.
    """
    assert worker_module("depth-anything", "depth-estimation").endswith("depth_anything")
    assert worker_module("depth-anything", "multiview-to-3d").endswith("da3_multiview")
    assert worker_module("depth-anything", None) is None
    assert "depth-estimation" in NOT_YET["depth-anything"]
    assert "multiview-to-3d" in NOT_YET["depth-anything"]


# --- le champ tableau de fichiers ---------------------------------------------


def test_une_liste_de_chemins_relatifs_est_resolue_depuis_le_dossier_du_job(tmp_path):
    """Ce que le superviseur transmet : `inputs/NNN-nom`, dans l'ordre reçu."""
    entrées = tmp_path / "inputs"
    entrées.mkdir()
    vues(entrées, 3)
    résolus = resolve_vues(["inputs/000-vue.png", "inputs/001-vue.png", "inputs/002-vue.png"],
                           tmp_path)
    assert [c.name for c in résolus] == ["000-vue.png", "001-vue.png", "002-vue.png"]
    assert all(c.is_absolute() for c in résolus)


def test_une_liste_json_du_terminal_est_acceptee(tmp_path):
    """`-p images=[…]` arrive en chaîne : la refuser obligerait à l'écrire deux fois."""
    chemins = vues(tmp_path, 2)
    assert resolve_vues(json.dumps(chemins), tmp_path) == [Path(c) for c in chemins]


def test_une_image_unique_est_refusee_en_nommant_la_capacite_qui_convient(tmp_path):
    """La faute la plus probable : croire que cette capacité remplace `depth-estimation`."""
    chemin = vues(tmp_path, 1)[0]
    with pytest.raises(WorkerError, match="depth-estimation"):
        resolve_vues(chemin, tmp_path)


def test_une_seule_vue_dans_la_liste_est_refusee(tmp_path):
    with pytest.raises(WorkerError, match="inter-vues"):
        resolve_vues(vues(tmp_path, 1), tmp_path)
    assert VUES_MIN == 2


def test_au_dela_du_plafond_la_liste_est_refusee(tmp_path):
    """Le plafond n'est pas une prudence : c'est le dernier N dont le pic est mesuré."""
    with pytest.raises(WorkerError, match=str(VUES_MAX)):
        resolve_vues(vues(tmp_path, VUES_MAX + 1), tmp_path)


def test_un_fichier_manquant_nomme_son_rang(tmp_path):
    """Sur trente-deux vues, « fichier introuvable » sans rang n'aide personne."""
    présents = vues(tmp_path, 2)
    with pytest.raises(WorkerError, match=r"images\[2\]"):
        resolve_vues([*présents, str(tmp_path / "absente.png")], tmp_path)


def test_un_format_non_geré_nomme_son_rang_et_les_extensions(tmp_path):
    présents = vues(tmp_path, 2)
    (tmp_path / "notes.txt").write_text("pas une image")
    with pytest.raises(WorkerError, match=r"images\[2\].*\.png"):
        resolve_vues([*présents, str(tmp_path / "notes.txt")], tmp_path)


# --- les réglages -------------------------------------------------------------


def test_les_trois_couches_se_superposent_dans_l_ordre():
    """Entrée du job, puis options du variant, puis défauts du manifeste."""
    plan = plan_multivue(
        entree={"process_res": 700},
        params={"process_res": 800, "max_points": 120000},
        defaults={"process_res": 504, "max_points": 999999, "seed": 7},
    )
    assert plan.process_res == 700
    assert plan.max_points == 120000
    assert plan.seed == 7


def test_les_defauts_du_manifeste_suffisent():
    plan = plan_multivue(entree={}, params={}, defaults={})
    assert (plan.process_res, plan.seed) == (RES_DEFAUT, 0)


def test_une_valeur_hors_bornes_est_refusee_avant_l_inference():
    """Trente secondes d'inférence ne doivent pas précéder le refus."""
    with pytest.raises(WorkerError, match=r"\[50000 ; 2000000\]"):
        plan_multivue(entree={"max_points": POINTS_MAX + 1}, params={}, defaults={})
    with pytest.raises(WorkerError, match=r"\[0.0 ; 90.0\]"):
        plan_multivue(entree={"conf_thresh_percentile": 95}, params={}, defaults={})


def test_une_resolution_hors_grille_de_patchs_est_signalee():
    """L'encodeur travaille par patchs de 14 : 500 devient autre chose, en silence."""
    plan = plan_multivue(entree={"process_res": 500}, params={}, defaults={})
    assert plan.process_res == 500
    assert any("multiple de 14" in a for a in plan.warnings)
    assert plan_multivue(entree={"process_res": 504}, params={}, defaults={}).warnings == ()


# --- la géométrie -------------------------------------------------------------


def test_le_centre_d_une_camera_se_lit_en_world_to_camera():
    """`-Rᵀt`, jamais `t`, et c'est le genre d'erreur qui ne se voit pas.

    On construit ici la pose d'une caméra dont le centre est connu, on l'écrit
    en w2c comme le modèle le fait, et on vérifie que la lecture rend bien le
    centre de départ. Prendre `t` rendrait un arc plausible et faux.
    """
    np = pytest.importorskip("numpy", reason="numpy n'est pas dans l'env d'Écurie")

    centre = np.array([1.5, -0.5, 3.0])
    angle = 0.7
    R = np.array([
        [np.cos(angle), 0.0, np.sin(angle)],
        [0.0, 1.0, 0.0],
        [-np.sin(angle), 0.0, np.cos(angle)],
    ])
    w2c = np.zeros((1, 3, 4))
    w2c[0, :3, :3] = R
    w2c[0, :3, 3] = -R @ centre

    assert np.allclose(centres_cameras(np, w2c)[0], centre)
    assert not np.allclose(w2c[0, :3, 3], centre)


def test_deux_vues_donnent_deux_couleurs_distinctes_et_stables():
    """La vignette d'une vue et sa caméra dans le GLB portent la même teinte.

    La fonction est réécrite depuis l'export d'amont plutôt qu'importée : ce
    test est ce qui garantit qu'elle en reste la copie — teintes vives, bien
    séparées, et identiques d'une exécution à l'autre.
    """
    couleurs = [couleur_vue(rang, 8) for rang in range(8)]
    assert len(set(couleurs)) == 8
    assert couleurs == [couleur_vue(rang, 8) for rang in range(8)]
    assert all(max(c) > 200 and min(c) < 80 for c in couleurs)


# --- ce que le job a le droit de contenir -------------------------------------


def test_les_trois_sorties_declarees_ne_comptent_pas_pour_des_traces():
    assert _traces({"inputs"}, {"inputs", NUAGE, APERCU, CAMERAS}) == []


def test_un_depth_vis_oublie_serait_remonte_en_avertissement():
    """Le contrôle existe pour cette panne-là, et pour aucune autre.

    `inference(export_dir=…)` dépose un `depth_vis/` et un `scene.jpg` que le
    contrat ne déclare pas. L'adaptateur passe `export_depth_vis=False` ; ce test
    dit ce qui arriverait si quelqu'un revenait au chemin d'amont.
    """
    (avertissement,) = _traces(
        {"inputs"}, {"inputs", NUAGE, APERCU, CAMERAS, "depth_vis", "scene.jpg"}
    )
    assert "depth_vis" in avertissement and "scene.jpg" in avertissement


# --- le refus de charger ------------------------------------------------------


def test_des_poids_absents_sont_refuses_sans_tentative_de_telechargement(tmp_path):
    with pytest.raises(WorkerError, match="ne télécharge jamais"):
        weights_dir({"weights_path": str(tmp_path / "nulle-part")})
    with pytest.raises(WorkerError, match="poids introuvables"):
        weights_dir({})


def test_l_adaptateur_s_instancie_sans_torch():
    """Ce que fait `--self-test`, et ce que la CI vérifie sur toutes les machines."""
    assert DA3MultiviewWorker().name == "da3-multiview"
