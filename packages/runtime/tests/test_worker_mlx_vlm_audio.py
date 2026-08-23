"""Écouter avec le moteur de mlx-vlm — ce qui se vérifie sans poids.

L'adaptateur ouvre `audio-to-text` aux modèles omni, ceux dont un seul jeu de
poids voit et entend. Ce qui lui est propre et tient dans du code pur : la
composition de la consigne, le refus d'un format qu'aucun décodeur n'ouvrira, et
le fait qu'il hérite bien du socle plutôt que de le recopier.
"""

from pathlib import Path

import pytest
from ecurie_runtime.envs import worker_module
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.mlx_lm import MlxLmBase
from ecurie_runtime.workers.mlx_vlm_audio import (
    DESCRIPTION,
    MlxVlmAudioWorker,
    _duree,
    build_prompt,
    resolve_audio,
)
from ecurie_runtime.workers.mlx_vlm_lm import SurMlxVlm


def test_le_module_s_importe_sans_mlx():
    assert MlxVlmAudioWorker.name == "mlx-vlm-audio"


def test_la_capacite_garde_ses_deux_chemins():
    """Le nouvel adaptateur n'évince pas l'ancien : `mlx-audio` sert toujours
    `audio-to-text` sur d'autres poids. Deux runtimes, un seul contrat."""
    assert worker_module("mlx-vlm", "audio-to-text").endswith("mlx_vlm_audio")
    assert worker_module("mlx-audio", "audio-to-text").endswith("qwen2_audio")


def test_l_adaptateur_herite_du_socle_plutot_que_de_le_recopier():
    assert issubclass(MlxVlmAudioWorker, SurMlxVlm)
    assert issubclass(MlxVlmAudioWorker, MlxLmBase)
    # `SurMlxVlm` doit passer avant, sinon ses surcharges seraient masquées et le
    # worker chargerait mlx-lm — qui ne connaît aucune de ces architectures.
    mro = MlxVlmAudioWorker.__mro__
    assert mro.index(SurMlxVlm) < mro.index(MlxLmBase)


# --- la consigne ------------------------------------------------------------------


def test_sans_question_la_consigne_demande_une_description():
    invite = build_prompt(None, None)
    assert invite == DESCRIPTION
    assert "Question" not in invite


def test_avec_question_la_consigne_l_encadre_et_interdit_de_deviner():
    invite = build_prompt("Combien de voix ?", None)
    assert "Question : Combien de voix ?" in invite
    assert "dis-le plutôt que de deviner" in invite


def test_la_langue_est_demandee_en_dernier():
    """Une consigne de langue placée avant la tâche se fait oublier au bout de
    quelques dizaines de jetons."""
    invite = build_prompt("What is this?", "français")
    assert invite.rstrip().endswith("Réponds en français.")


@pytest.mark.parametrize("valeur", [None, "", "  ", "auto", "AUTO"])
def test_une_langue_automatique_n_ajoute_rien(valeur):
    assert "Réponds en" not in build_prompt(None, valeur)


# --- l'enregistrement -------------------------------------------------------------


def test_un_enregistrement_absent_est_refuse_avant_tout_chargement():
    with pytest.raises(WorkerError, match="aucun enregistrement"):
        resolve_audio(None, Path("/tmp"))


def test_un_fichier_introuvable_le_dit_avec_son_chemin(tmp_path):
    with pytest.raises(WorkerError, match="introuvable"):
        resolve_audio("absent.wav", tmp_path)


def test_un_format_non_gere_est_refuse_avec_la_liste_des_formats(tmp_path):
    """Un `.opus` refusé vaut mieux qu'un décodage qui rend zéro échantillon et
    une erreur venue de trois couches plus bas."""
    fichier = tmp_path / "voix.opus"
    fichier.write_bytes(b"")

    with pytest.raises(WorkerError) as échec:
        resolve_audio("voix.opus", tmp_path)

    assert ".wav" in str(échec.value)


def test_un_chemin_relatif_est_resolu_dans_le_dossier_du_job(tmp_path):
    fichier = tmp_path / "voix.wav"
    fichier.write_bytes(b"")

    assert resolve_audio("voix.wav", tmp_path) == fichier


# --- la durée ---------------------------------------------------------------------


def test_la_duree_d_un_wav_se_lit_dans_son_en_tete(tmp_path):
    import wave

    chemin = tmp_path / "essai.wav"
    with wave.open(str(chemin), "wb") as fichier:
        fichier.setnchannels(1)
        fichier.setsampwidth(2)
        fichier.setframerate(16000)
        fichier.writeframes(b"\x00\x00" * 16000)

    assert _duree(chemin) == 1.0


def test_un_conteneur_sans_en_tete_lisible_rend_none_plutot_qu_un_chiffre_faux(tmp_path):
    """Le contrat déclare la durée écoutée, pas la durée devinée."""
    chemin = tmp_path / "voix.mp3"
    chemin.write_bytes(b"pas un mp3")

    assert _duree(chemin) is None


def test_un_wav_illisible_ne_fait_pas_echouer_le_job(tmp_path):
    chemin = tmp_path / "casse.wav"
    chemin.write_bytes(b"RIFF....pas vraiment")

    assert _duree(chemin) is None
