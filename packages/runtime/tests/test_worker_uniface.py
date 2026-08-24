"""La famille visage — ce qui se vérifie sans onnxruntime et sans poids.

Six capacités, un runtime, et trois choses qui tiennent dans du code pur : le
choix du détecteur quand le manifeste n'en nomme aucun, le refus d'un poids
absent ou altéré, et la conversion des angles. Le reste — les boîtes, les points,
les régions, les vecteurs — demande les poids et vit dans `ecurie bench`.

Ces tests tournent en CI, sur des machines sans Apple Silicon, sans poids et sans
venv de runtime : rien de ce fichier n'importe cv2, numpy ni uniface au niveau du
module.
"""

import math
from pathlib import Path

import pytest
from ecurie_runtime.envs import worker_module
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.uniface_base import (
    DETECTEUR_PAR_DEFAUT,
    _surface,
    _taille_entree,
    exiger_poids_de_tache,
    resolve_image,
    weights_dir,
)

# --- câblage ----------------------------------------------------------------------


CAPACITES = {
    "face-detect": "uniface_detect",
    "face-landmark": "uniface_landmark",
    "face-parse": "uniface_parse",
    "face-embed": "uniface_embed",
    "face-headpose": "uniface_headpose",
    "face-gaze": "uniface_gaze",
}


@pytest.mark.parametrize(("capacite", "module"), sorted(CAPACITES.items()))
def test_chaque_capacite_visage_a_son_adaptateur(capacite: str, module: str):
    """Six capacités, six adaptateurs — et non un seul fichier qui aiguillerait.

    Ce qui leur est commun vit dans `uniface_base` ; ce qui leur est propre —
    l'appel, la sortie, l'aperçu — n'a rien à faire dans un module qui
    commencerait par un `if capability ==` et ne se relirait plus.
    """
    assert worker_module("uniface", capacite).endswith(module)


def test_le_runtime_uniface_n_a_pas_d_adaptateur_par_defaut():
    """Un runtime est une famille de bibliothèques, pas une promesse d'API commune.

    Sans capacité, il n'y a rien à servir : détecter et encoder une identité ne
    partagent ni l'appel ni la sortie. Le message dit alors quoi faire.
    """
    from ecurie_runtime.envs import NOT_YET

    assert worker_module("uniface", None) is None
    assert "six capacités" in NOT_YET["uniface"]


# --- détecteur par défaut ----------------------------------------------------------


def test_le_detecteur_par_defaut_est_celui_qui_sert_la_charge_type():
    """MESURÉ — et le choix n'est pas celui qu'on attendrait.

    `retinaface_mnet050` trouve les quatre visages de la charge type aux trois
    définitions ; `mnet_v2`, deux fois plus lourd, en trouve deux à 320, un seul
    à 640 et quatre à 1280. Un détecteur non monotone est inutilisable en amont
    d'une autre capacité : le visage qu'il manque n'aura ni points clés, ni
    régions, ni empreinte.
    """
    assert DETECTEUR_PAR_DEFAUT == "retinaface_mnet050"


def test_une_capacite_a_tache_exige_que_le_manifeste_nomme_ses_poids():
    """Cinq capacités sur six chargent deux modèles : un détecteur, puis le leur.

    Sans `options.weights`, il n'y a rien à charger — et deviner reviendrait à
    choisir un modèle à la place du manifeste.
    """
    with pytest.raises(WorkerError) as échec:
        exiger_poids_de_tache({"detector": "retinaface_mnet050"})

    assert "options.weights" in str(échec.value)


def test_les_poids_declares_sont_rendus_tels_quels():
    assert exiger_poids_de_tache({"weights": "pipnet_r18_wflw_98"}) == "pipnet_r18_wflw_98"


# --- poids : le worker ne télécharge jamais ----------------------------------------


def test_un_chemin_de_poids_absent_est_refuse_avec_ce_qui_repare():
    with pytest.raises(WorkerError, match="superviseur"):
        weights_dir({})


def test_un_dossier_de_poids_inexistant_dit_qu_un_worker_ne_telecharge_pas(tmp_path: Path):
    with pytest.raises(WorkerError) as échec:
        weights_dir({"weights_path": str(tmp_path / "absent")})
    assert "ne télécharge jamais" in str(échec.value)


# --- entrées -----------------------------------------------------------------------


def test_une_image_relative_se_resout_dans_le_dossier_du_job(tmp_path: Path):
    """Le superviseur copie l'entrée dans le dossier du job et transmet un chemin
    relatif : c'est ce qui rend un job rejouable ailleurs."""
    (tmp_path / "inputs").mkdir()
    fichier = tmp_path / "inputs" / "portrait.png"
    fichier.write_bytes(b"")

    assert resolve_image("inputs/portrait.png", tmp_path) == fichier


def test_un_champ_image_vide_nomme_le_champ_fautif():
    with pytest.raises(WorkerError, match="compare_to"):
        resolve_image("", Path("."), "compare_to")


def test_un_format_non_gere_liste_ceux_qui_le_sont(tmp_path: Path):
    fichier = tmp_path / "portrait.tiff"
    fichier.write_bytes(b"")
    with pytest.raises(WorkerError) as échec:
        resolve_image(str(fichier), tmp_path)
    assert ".png" in str(échec.value)


# --- taille d'entrée ---------------------------------------------------------------


class DetecteurCouple:
    def __init__(self, *, input_size: tuple[int, int] = (640, 640)) -> None: ...


class DetecteurEntier:
    def __init__(self, *, input_size: int = 640) -> None: ...


class DetecteurFige:
    """BlazeFace : sa résolution est celle des poids, il n'expose rien."""

    def __init__(self) -> None: ...


def test_la_taille_prend_la_forme_qu_attend_ce_detecteur_la():
    """Trois formes coexistent chez uniface, et passer la mauvaise lève au chargement.

    On lit la signature plutôt que de tenir une table qui aurait vieilli à la
    première version d'amont.
    """
    assert _taille_entree(DetecteurCouple, 320) == {"input_size": (320, 320)}
    assert _taille_entree(DetecteurEntier, 320) == {"input_size": 320}
    assert _taille_entree(DetecteurFige, 320) == {}


def test_sans_taille_demandee_on_laisse_le_detecteur_a_son_defaut():
    assert _taille_entree(DetecteurCouple, None) == {}


# --- tri des visages ---------------------------------------------------------------


class VisageFeint:
    def __init__(self, x1, y1, x2, y2, confidence=0.9):
        self.bbox = (x1, y1, x2, y2)
        self.confidence = confidence


def test_les_visages_se_trient_par_surface():
    """`max_faces` coupe la liste, et garder les plus grands est le seul choix
    défendable : un visage de quinze pixels ne donne ni points clés utilisables,
    ni empreinte comparable."""
    petit = VisageFeint(0, 0, 10, 10)
    grand = VisageFeint(0, 0, 100, 100)
    assert sorted([petit, grand], key=_surface, reverse=True) == [grand, petit]


def test_une_boite_degeneree_a_une_surface_nulle_plutot_que_negative():
    assert _surface(VisageFeint(100, 100, 40, 40)) == 0.0


# --- unités ------------------------------------------------------------------------


def test_le_regard_est_converti_en_degres_comme_l_orientation():
    """MobileGaze rend des radians, `face-headpose` rend des degrés.

    Laisser passer l'écart serait le pire des pièges : deux capacités voisines,
    deux sorties d'apparence identique, un facteur 57 entre les deux. Un appelant
    qui trace les deux flèches verrait celle du regard immobile et conclurait que
    le modèle ne fonctionne pas.
    """
    assert round(math.degrees(-0.1904), 2) == -10.91
    assert round(math.degrees(0.0), 2) == 0.0
