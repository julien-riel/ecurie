"""Adaptateur mlx-audio (musique) — ce qui se vérifie sans Apple Silicon.

Le point de testabilité est `plan_generation` : trois couches de réglages, la
traduction du silence en `[instrumental]`, et le sort des paramètres que le
contrat déclare mais que ce modèle n'expose pas. Tout le reste demande le vrai
modèle et relève du banc d'essai.
"""

import json

import pytest
from ecurie_runtime.envs import (
    WORKER_MODULES,
    WORKER_MODULES_BY_CAPABILITY,
    worker_module,
)
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.mlx_audio_music import (
    DEFAULT_STEPS,
    INSTRUMENTAL,
    MlxAudioMusicWorker,
    import_runtime,
    merge_segments,
    plan_generation,
)


def test_le_module_s_importe_sans_mlx():
    assert MlxAudioMusicWorker.name == "mlx-audio-music"


def test_l_absence_du_runtime_nomme_la_reparation(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "mlx_audio", None)
    with pytest.raises(WorkerError) as exc:
        import_runtime()
    assert "ecurie env sync mlx-audio-music" in str(exc.value)
    # Le message doit dire *pourquoi* cet env est à part, sinon la réparation
    # ressemble à une redite de celle du TTS.
    assert "PyPI" in str(exc.value)


# --- choix de l'adaptateur ------------------------------------------------------------


def test_la_capacite_choisit_l_adaptateur_au_sein_d_un_meme_runtime():
    """`mlx-audio` sert six capacités par des API qui n'ont rien en commun."""
    assert worker_module("mlx-audio", "text-to-speech") == WORKER_MODULES["mlx-audio"]
    assert (
        worker_module("mlx-audio", "text-to-music")
        == WORKER_MODULES_BY_CAPABILITY[("mlx-audio", "text-to-music")]
    )
    # Sans capacité connue, on retombe sur l'adaptateur du runtime.
    assert worker_module("mlx-audio", None) == WORKER_MODULES["mlx-audio"]
    # `speech-to-text` a eu son adaptateur en même temps que son modèle : le
    # même MOSS que la diarisation, sur les mêmes octets, par une autre lecture
    # du même résultat. Jusque-là, elle retombait sur le TTS — un adaptateur qui
    # n'aurait jamais su quoi faire d'un fichier audio en entrée.
    assert (
        worker_module("mlx-audio", "speech-to-text")
        == WORKER_MODULES_BY_CAPABILITY[("mlx-audio", "speech-to-text")]
    )
    assert worker_module("inconnu", "text-to-music") is None


# --- plan de génération ---------------------------------------------------------------


def test_une_description_est_exigee():
    with pytest.raises(WorkerError, match="prompt"):
        plan_generation(entree={"lyrics": "[verse]\nsalut"}, params={}, defaults={})
    with pytest.raises(WorkerError, match="prompt"):
        plan_generation(entree={"prompt": "   "}, params={}, defaults={})


def test_sans_paroles_le_modele_recoit_la_marque_instrumentale():
    """Le checkpoint exige des paroles ; un champ vide serait refusé.

    Traduire nous-mêmes le silence évite à l'utilisateur d'avoir à connaître une
    convention de MiniMax pour obtenir une pièce sans voix.
    """
    for muet in ({}, {"lyrics": ""}, {"lyrics": "   "}):
        plan = plan_generation(entree={"prompt": "jazz feutré", **muet}, params={}, defaults={})
        assert plan.kwargs["lyrics"] == INSTRUMENTAL


def test_les_paroles_fournies_passent_telles_quelles():
    paroles = "[verse]\nLe parc tient dans le budget\n[chorus]\nUn seul modèle à la fois"
    plan = plan_generation(entree={"prompt": "pop", "lyrics": paroles}, params={}, defaults={})
    assert plan.kwargs["lyrics"] == paroles


def test_l_ordre_des_couches_entree_job_manifeste():
    plan = plan_generation(
        entree={"prompt": "du job"},
        params={"prompt": "des options", "steps": 12},
        defaults={"prompt": "du manifeste", "duration_seconds": 20, "steps": 30},
    )
    assert plan.kwargs["text"] == "du job"
    assert plan.kwargs["steps"] == 12  # les options du variant priment sur ses défauts
    assert plan.kwargs["duration"] == 20.0


def test_les_pas_ont_un_repli_quand_rien_ne_les_declare():
    plan = plan_generation(entree={"prompt": "ambient"}, params={}, defaults={})
    assert plan.kwargs["steps"] == DEFAULT_STEPS


def test_les_reglages_du_contrat_que_le_modele_ignore_sont_signales():
    """Les passer les ferait absorber par le `**_` de `generate` sans effet.

    C'est le pire des deux mondes : l'utilisateur croit avoir réglé quelque
    chose, et rien dans la sortie ne le détrompe.
    """
    plan = plan_generation(
        entree={"prompt": "rock", "guidance_scale": 7.0, "temperature": 1.4},
        params={},
        defaults={},
    )
    assert "guidance_scale" not in plan.kwargs
    assert "temperature" not in plan.kwargs
    assert len(plan.warnings) == 2
    assert all("ignoré" in a for a in plan.warnings)


def test_la_graine_traverse_le_plan():
    plan = plan_generation(entree={"prompt": "x", "seed": 7}, params={}, defaults={})
    assert plan.kwargs["seed"] == 7


# --- recollage ------------------------------------------------------------------------


class _Segment:
    def __init__(self, audio):
        self.audio = audio


def test_un_seul_segment_court_circuite_la_concatenation():
    sentinelle = object()
    def concat(_):
        raise AssertionError("ne doit pas être appelé pour un segment unique")
    assert merge_segments([_Segment(sentinelle)], concat) is sentinelle


def test_plusieurs_segments_sont_recolles_dans_l_ordre():
    assert merge_segments([_Segment("a"), _Segment("b")], lambda parts: "".join(parts)) == "ab"


def test_aucun_segment_est_une_erreur():
    with pytest.raises(WorkerError, match="aucun segment"):
        merge_segments([], lambda parts: parts)


# --- cohérence avec le contrat --------------------------------------------------------


def test_le_contrat_expose_les_paroles(repo_root):
    """Un modèle de chanson en exige : un contrat qui les tait obligerait à les
    passer par un canal que l'UI ne saurait pas rendre."""
    contrat = json.loads(
        (repo_root / "registry" / "capabilities" / "text-to-music.json").read_text()
    )
    lyrics = contrat["input"]["properties"]["lyrics"]
    assert lyrics["type"] == "string"
    assert lyrics.get("x-ui") == "textarea"
    assert "instrumental" in lyrics["description"]


def test_steps_reste_hors_du_contrat(repo_root):
    """`steps` est propre à ce modèle : sa place est dans `options:`, pas dans le
    dénominateur commun de la capacité."""
    contrat = json.loads(
        (repo_root / "registry" / "capabilities" / "text-to-music.json").read_text()
    )
    assert "steps" not in contrat["input"]["properties"]
