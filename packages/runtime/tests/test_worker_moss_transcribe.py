"""Transcription MOSS — ce qui se vérifie sans mlx ni poids.

Le voisin `test_worker_moss_diarize.py` couvre le même modèle par l'autre bout ;
ce qui est propre à celui-ci est ce que le contrat promet et que ce réseau ne
tient pas. Trois paramètres sont inopérants et un quatrième est refusé, et la
différence entre les deux traitements est tout le sujet : un réglage sans effet
laisse une transcription juste, une traduction demandée et non faite rend une
sortie qui n'est pas celle qu'on a demandée.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from ecurie_runtime.envs import worker_module
from ecurie_runtime.workers.base import InferRequest, WorkerError
from ecurie_runtime.workers.moss_transcribe import (
    ENV_NAME,
    INOPERANTS,
    SEGMENTS_NAME,
    TEXTE_NAME,
    MossTranscribeWorker,
    reproches,
    sans_marqueurs,
    texte_des_segments,
)

REPO_ROOT = Path(__file__).parents[3]
CONTRAT = REPO_ROOT / "registry" / "capabilities" / "speech-to-text.json"


def demande(**champs) -> InferRequest:
    return InferRequest(
        job_id="j1",
        input=champs.pop("input", {}),
        params=champs.pop("params", {}),
        output_dir=champs.pop("output_dir", Path(".")),
        seed=champs.pop("seed", None),
    )


# --- imports paresseux -------------------------------------------------------


def test_module_importable_sans_mlx():
    code = (
        "import sys, ecurie_runtime.workers.moss_transcribe as m;"
        "print(m.ENV_NAME, 'mlx' in sys.modules, 'mlx_audio' in sys.modules)"
    )
    résultat = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert résultat.returncode == 0, résultat.stderr
    assert résultat.stdout.split() == [ENV_NAME, "False", "False"]


# --- aiguillage --------------------------------------------------------------


def test_transcrire_et_diariser_sont_deux_adaptateurs():
    """Mêmes octets, deux lectures du même résultat, deux contrats."""
    assert worker_module("mlx-audio", "speech-to-text").endswith("moss_transcribe")
    assert worker_module("mlx-audio", "speaker-diarization").endswith("moss_diarize")


# --- fidélité au contrat -----------------------------------------------------


def test_les_sorties_ecrites_sont_celles_que_le_contrat_exige():
    import json

    contrat = json.loads(CONTRAT.read_text())["output"]
    assert contrat["required"] == ["text"]
    assert TEXTE_NAME.endswith(".txt")
    assert SEGMENTS_NAME.endswith(".json")


def test_les_parametres_declares_inoperants_existent_bien_au_contrat():
    """Une liste d'inopérants qui nommerait un champ absent serait du bruit."""
    import json

    entrée = json.loads(CONTRAT.read_text())["input"]["properties"]
    for nom, (neutre, _) in INOPERANTS.items():
        assert nom in entrée, f"{nom} n'est pas au contrat"
        # La valeur « neutre » doit être celle du contrat : signaler un réglage
        # laissé à son défaut ferait un avertissement à chaque job.
        assert entrée[nom].get("default") == neutre


# --- ce que le variant n'honore pas ------------------------------------------


def test_un_reglage_laisse_a_son_defaut_ne_se_signale_pas():
    assert reproches({"beam_size": 5, "temperature": 0.0, "word_timestamps": False}) == []


def test_un_reglage_demande_est_signale_avec_sa_raison():
    dits = reproches({"beam_size": 8})
    assert len(dits) == 1
    assert "beam_size" in dits[0]
    assert "faisceau" in dits[0]


def test_un_reglage_absent_ne_se_signale_pas():
    """`None` veut dire « non demandé », et non « demandé à zéro »."""
    assert reproches({"beam_size": None, "temperature": None}) == []


def test_les_trois_inoperants_se_cumulent():
    dits = reproches({"beam_size": 1, "temperature": 0.5, "word_timestamps": True})
    assert len(dits) == 3


def test_traduire_est_refuse_et_non_signale():
    """La différence avec les trois autres : ce n'est pas la même sortie.

    Un `beam_size` ignoré laisse une transcription juste. Une traduction demandée
    et non faite rend un texte dans la mauvaise langue, sous un job réussi.
    """
    worker = MossTranscribeWorker()
    worker.model = object()
    with pytest.raises(WorkerError) as échec:
        worker.infer(demande(input={"audio": "a.wav", "task": "translate"}), lambda *_: None)
    assert "translate" in str(échec.value)
    # Le refus porte la sortie de secours : la capacité translation existe.
    assert "translation" in str(échec.value)


# --- recomposition du texte --------------------------------------------------


def test_le_texte_se_recompose_des_segments():
    segments = [{"text": "bonjour"}, {"text": " tout le monde "}, {"text": ""}]
    assert texte_des_segments(segments) == "bonjour tout le monde"


def test_aucun_segment_donne_un_texte_vide():
    assert texte_des_segments([]) == ""


def test_l_identifiant_de_locuteur_ne_passe_pas_dans_la_transcription():
    """La frontière entre les deux capacités que ces poids servent.

    Trouvé au premier job réel, et pas avant : le banc d'essai vérifie qu'un
    fichier de sortie existe, pas ce qu'il contient. Le contrat de
    `speech-to-text` ne déclare aucun locuteur — c'est `speaker-diarization` qui
    les porte —, et les laisser dans le texte livrerait une sortie qu'aucun autre
    modèle de transcription ne produirait.
    """
    assert sans_marqueurs("[S01] Je peux faire glisser.") == "Je peux faire glisser."
    assert texte_des_segments([{"text": "[S01] Un."}, {"text": "[S02] Deux."}]) == "Un. Deux."


def test_les_bornes_intercalees_par_le_modele_sont_retirees():
    """Le texte global du modèle est un flux de diarisation aplati."""
    brut = "[1.28][S01] Je peux faire glisser.[9.21][10.48][S01] Encore.[17.84]"
    assert sans_marqueurs(brut) == "Je peux faire glisser.Encore."


def test_un_crochet_qui_n_est_pas_un_marqueur_survit():
    # La reconnaissance est précise à dessein : `[S\\d+]` et un nombre entre
    # crochets, pas « tout ce qui ressemble à un crochet ».
    assert sans_marqueurs("il a dit [sic] non") == "il a dit [sic] non"


# --- refus lisibles ----------------------------------------------------------


def test_infer_avant_load_le_dit():
    with pytest.raises(WorkerError, match="infer avant load"):
        MossTranscribeWorker().infer(demande(), lambda *_: None)


def test_un_audio_manquant_nomme_le_champ():
    worker = MossTranscribeWorker()
    worker.model = object()
    with pytest.raises(WorkerError, match="« audio » est obligatoire"):
        worker.infer(demande(input={}), lambda *_: None)


def test_un_audio_introuvable_donne_le_chemin(tmp_path):
    worker = MossTranscribeWorker()
    worker.model = object()
    with pytest.raises(WorkerError, match="audio introuvable"):
        worker.infer(
            demande(input={"audio": "absent.wav"}, output_dir=tmp_path),
            lambda *_: None,
        )


def test_la_borne_de_duree_vient_des_options_du_variant():
    """`speech-to-text` ne déclare pas `max_seconds` : elle est hors contrat.

    `InferRequest.get` lit l'entrée typée puis les `options:` du variant, ce qui
    est exactement ce que le manifeste prévoit pour un réglage propre au runtime.
    """
    requête = demande(input={"audio": "a.wav"}, params={"max_seconds": 42})
    assert requête.get("max_seconds") == 42
