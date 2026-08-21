"""Adaptateur mlx-vlm, chemin description — ce qui se vérifie sans mlx.

Cette capacité est la moins chère que le parc ait ajoutée : mêmes poids que la
lecture de document, même environnement, un contrat de plus. Ce qui lui est
propre tient dans la composition de l'invite et le refus d'un PDF, et les deux
sont du code pur.
"""

import json

import pytest
from ecurie_runtime.envs import worker_module
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.mlx_vlm_describe import (
    DETAILS,
    MlxVlmDescribeWorker,
    _fin,
    build_prompt,
    resolve_image,
)


def test_le_module_s_importe_sans_mlx():
    assert MlxVlmDescribeWorker.name == "mlx-vlm-describe"


def test_la_capacite_choisit_cet_adaptateur_et_pas_celui_de_la_transcription():
    """C'est le couple (runtime, capacité) qui aiguille : sans cette entrée, une
    description partirait dans le lecteur de document et rendrait une
    transcription de l'image."""
    assert worker_module("mlx-vlm", "image-to-text").endswith("mlx_vlm_describe")
    assert worker_module("mlx-vlm", "document-to-text").endswith("mlx_vlm")


# --- composition de l'invite ---------------------------------------------------


def test_sans_question_la_consigne_demande_une_description():
    invite = build_prompt(None, "normal", None)
    assert "Décris cette image" in invite
    assert "Question" not in invite


def test_avec_question_la_consigne_l_encadre_et_interdit_de_deviner():
    invite = build_prompt("Combien d'objets ?", "bref", None)
    assert "Question : Combien d'objets ?" in invite
    assert "dis-le plutôt que de deviner" in invite


@pytest.mark.parametrize("detail", sorted(DETAILS))
def test_chaque_niveau_de_detail_a_sa_consigne(detail):
    assert DETAILS[detail] in build_prompt(None, detail, None)


def test_un_detail_inconnu_retombe_sur_normal():
    assert DETAILS["normal"] in build_prompt(None, "inexistant", None)


def test_la_langue_est_demandee_en_dernier():
    """Une consigne de langue placée avant la tâche se fait oublier au bout de
    quelques dizaines de jetons, et le modèle répond dans la langue de la
    question."""
    invite = build_prompt("What is this?", "normal", "français")
    assert invite.rstrip().endswith("Rédige ta réponse en français.")


def test_une_langue_automatique_n_ajoute_rien():
    for valeur in (None, "", "  ", "auto"):
        assert "Rédige" not in build_prompt(None, "normal", valeur)


def test_les_niveaux_de_detail_couvrent_l_enum_du_contrat(repo_root):
    """Le contrat déclare trois niveaux : un adaptateur qui n'en connaîtrait que
    deux traiterait le troisième comme « normal », en silence."""
    contrat = json.loads(
        (repo_root / "registry" / "capabilities" / "image-to-text.json").read_text()
    )
    assert set(DETAILS) == set(contrat["input"]["properties"]["detail"]["enum"])


# --- résolution de l'image -----------------------------------------------------


def test_un_chemin_relatif_se_resout_dans_le_dossier_du_job(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "scene.png").write_bytes(b"x")
    assert resolve_image("inputs/scene.png", tmp_path) == tmp_path / "inputs" / "scene.png"


def test_un_pdf_renvoie_vers_la_capacite_qui_sait_le_lire(tmp_path):
    """Un PDF est un document, pas une image : échouer dans mlx-vlm sur un
    fichier qu'il n'ouvrira pas n'apprendrait rien."""
    (tmp_path / "rapport.pdf").write_bytes(b"%PDF")
    with pytest.raises(WorkerError) as échec:
        resolve_image("rapport.pdf", tmp_path)
    assert "document-to-text" in str(échec.value)


def test_une_image_absente_est_nommee(tmp_path):
    with pytest.raises(WorkerError) as échec:
        resolve_image("absente.png", tmp_path)
    assert "introuvable" in str(échec.value)


# --- cause de l'arrêt ----------------------------------------------------------


class _Resultat:
    def __init__(self, raison=None):
        self.finish_reason = raison


def test_un_plafond_atteint_se_dit_length():
    """Une description coupée au plafond ne doit pas être notée comme une
    description ratée."""
    assert _fin(_Resultat(), jetons=512, max_tokens=512) == "length"
    assert _fin(_Resultat("length"), jetons=10, max_tokens=512) == "length"


def test_une_fin_naturelle_se_dit_stop():
    assert _fin(_Resultat("stop"), jetons=42, max_tokens=512) == "stop"
    assert _fin(_Resultat(), jetons=42, max_tokens=512) == "stop"
