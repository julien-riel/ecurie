"""`tools/narrate.py` without a model. Only the assembly test needs ffmpeg.

Loaded by path, like `test_openapi_fige.py` does: `tools/` is not a workspace package.
"""

import array
import importlib.util
import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

_CHEMIN = Path(__file__).parents[3] / "tools" / "narrate.py"
_SPEC = importlib.util.spec_from_file_location("ecurie_narrate", _CHEMIN)
assert _SPEC and _SPEC.loader
narrate = importlib.util.module_from_spec(_SPEC)
# `dataclass` looks its module up in sys.modules while building the class.
sys.modules[_SPEC.name] = narrate
_SPEC.loader.exec_module(narrate)


def test_les_titres_ne_sont_pas_dits_et_allongent_la_pause():
    segments = narrate.parse_script("# Intro\n\nFirst.\n\nSecond.\n\n## Setup\n\nThird.\n")
    assert [s.text for s in segments] == ["First.", "Second.", "Third."]
    assert [s.pause_after for s in segments] == [
        narrate.PAUSE_PARAGRAPH,
        narrate.PAUSE_SECTION,
        0.0,
    ]


def test_le_markdown_ne_s_entend_pas():
    segments = narrate.parse_script("- Open **the** [docs](https://x.y) and run `make`.\n")
    assert segments[0].text == "Open the docs and run make."


def test_un_long_paragraphe_se_coupe_entre_deux_phrases():
    phrase = "This sentence is about forty characters."
    segments = narrate.parse_script(" ".join([phrase] * 4), limit=90)
    assert [s.text for s in segments] == [f"{phrase} {phrase}", f"{phrase} {phrase}"]
    assert [s.pause_after for s in segments] == [narrate.PAUSE_SEGMENT, 0.0]


def test_le_lexique_change_ce_qui_est_dit_et_pas_ce_qui_est_ecrit():
    dit, jetons = narrate.apply_lexicon("Use (SQL), please.", {"SQL": "sequel server"})
    assert dit == "Use (sequel server), please."
    assert [j.display for j in jetons] == ["Use", "(SQL),", "please."]
    assert [j.spoken_words for j in jetons] == [1, 2, 1]


def test_le_lexique_respecte_la_casse():
    dit, _ = narrate.apply_lexicon("Read the json file.", {"JSON": "Jason"})
    assert dit == "Read the json file."


def test_une_cle_de_plusieurs_mots_est_refusee(tmp_path):
    fichier = tmp_path / "lexique.json"
    fichier.write_text('{"two words": "x"}', encoding="utf-8")
    with pytest.raises(narrate.NarrationError):
        narrate.load_lexicon(fichier)


def _unites(*bornes):
    return [{"text": "w", "start": a, "end": b} for a, b in bornes]


def test_un_mot_du_lexique_couvre_tous_ses_mots_dits():
    _, jetons = narrate.apply_lexicon("Use SQL now", {"SQL": "sequel server"})
    unites = _unites((0.0, 0.2), (0.2, 0.5), (0.5, 0.9), (0.9, 1.1))
    places = narrate.timed_tokens(jetons, unites, 1.2, offset=10.0)
    assert [(p.display, p.start, p.end) for p in places] == [
        ("Use", 10.0, 10.2),
        ("SQL", 10.2, 10.9),
        ("now", 10.9, 11.1),
    ]


def test_sans_alignement_les_mots_sont_places_en_proportion():
    _, jetons = narrate.apply_lexicon("one two", {})
    places = narrate.timed_tokens(jetons, None, 2.0, offset=1.0)
    assert [(p.start, p.end) for p in places] == [(1.0, 2.0), (2.0, 3.0)]


@pytest.mark.parametrize(
    ("unites", "empan", "attendu"),
    [
        (_unites((0.0, 1.0), (1.0, 2.0)), 2.0, True),
        (_unites((0.0, 1.0)), 1.0, False),  # un mot perdu
        (_unites((0.0, 0.1), (0.1, 0.2)), 0.2, False),  # effondré sur un instant
        (None, 0.0, False),
    ],
)
def test_un_alignement_faux_est_refuse(unites, empan, attendu):
    _, jetons = narrate.apply_lexicon("one two", {})
    assert narrate.alignment_is_usable(jetons, unites, empan, 2.5) is attendu


def test_une_phrase_finie_ferme_le_sous_titre():
    mots = [
        narrate.Timed("Hello", 0.0, 0.5),
        narrate.Timed("there.", 0.5, 1.0),
        narrate.Timed("Next", 1.2, 1.5),
    ]
    assert [c.display for c in narrate.build_cues(mots)] == ["Hello there.", "Next"]


def _mots(*textes, pas=0.4):
    return [narrate.Timed(t, i * pas, (i + 1) * pas) for i, t in enumerate(textes)]


def test_le_sous_titre_coupe_de_preference_a_la_ponctuation():
    texte = "Harbor, connect your first data source, build a dashboard, and share it with your team"
    mots = _mots(*texte.split(), pas=0.2)
    assert [c.display for c in narrate.build_cues(mots)] == [
        "Harbor, connect your first data source, build a dashboard,",
        "and share it with your team",
    ]


def test_un_sous_titre_orphelin_rejoint_le_precedent():
    cues = narrate._merge_orphans(
        [narrate.Timed("Share it with your", 0.0, 2.0), narrate.Timed("team.", 2.0, 2.4)]
    )
    assert [(c.display, c.start, c.end) for c in cues] == [("Share it with your team.", 0.0, 2.4)]


def test_une_phrase_courte_et_complete_garde_son_sous_titre():
    cues = narrate._merge_orphans(
        [narrate.Timed("Let's recap.", 0.0, 1.5), narrate.Timed("Great!", 1.6, 2.0)]
    )
    assert [c.display for c in cues] == ["Let's recap.", "Great!"]


def test_un_sous_titre_reste_lisible():
    mots = [narrate.Timed("word", i * 0.2, i * 0.2 + 0.2) for i in range(60)]
    for cue in narrate.build_cues(mots):
        lignes = narrate._wrap(cue.display).split("\n")
        assert len(lignes) <= 2
        assert all(len(ligne) <= narrate.CUE_LINE_CHARS for ligne in lignes)
        assert cue.end - cue.start <= narrate.CUE_MAX_SECONDS


def test_srt_et_vtt_ne_different_que_par_leur_forme():
    cues = [narrate.Timed("Hello there.", 1.5, 3661.25)]
    assert narrate.to_srt(cues) == "1\n00:00:01,500 --> 01:01:01,250\nHello there.\n"
    assert narrate.to_vtt(cues) == "WEBVTT\n\n00:00:01.500 --> 01:01:01.250\nHello there.\n"


def test_la_cle_change_avec_tout_ce_qui_change_le_son():
    base = narrate.segment_key("hi", "m@v", "a", 1.0, 1)
    assert base == narrate.segment_key("hi", "m@v", "a", 1.0, 1)
    assert base != narrate.segment_key("hi", "m@v", "b", 1.0, 1)
    assert base != narrate.segment_key("hi", "m@v", "a", 1.0, 2)
    assert base != narrate.segment_key("yo", "m@v", "a", 1.0, 1)


@pytest.mark.parametrize(
    ("niveau", "crete", "gain"),
    [(-20.0, -10.0, 4.0), (-25.6, -4.2, 2.7), (-20.0, -1.0, -0.5)],
    ids=["la cible décide", "le plafond décide", "le plafond baisse le son"],
)
def test_le_gain_atteint_la_cible_sans_passer_le_plafond(niveau, crete, gain):
    assert narrate.linear_gain(niveau, crete, -16.0) == pytest.approx(gain)


def _niveaux_par_fenetre(chemin: Path, fenetre: int = 4800) -> list[float]:
    with wave.open(str(chemin)) as lecteur:
        assert lecteur.getsampwidth() == 2
        assert lecteur.getnchannels() == 1
        brut = lecteur.readframes(lecteur.getnframes())
    signal = array.array("h", brut)
    return [
        math.sqrt(sum(x * x for x in signal[i : i + fenetre]) / fenetre)
        for i in range(0, len(signal) - fenetre, fenetre)
    ]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg absent")
def test_une_seule_valeur_de_gain_du_debut_a_la_fin(tmp_path):
    """Une voix basse et une crête isolée : le cas où `loudnorm` passait en dynamique."""
    source = tmp_path / "voix.wav"
    expression = (
        "0.05*sin(2*PI*220*t)*(1+0.5*sin(2*PI*3*t))+0.7*sin(2*PI*880*t)*between(t\\,1\\,1.02)"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         f"aevalsrc={expression}:s=48000:d=6", "-c:a", "pcm_s16le", str(source)],
        check=True,
    )  # fmt: skip

    niveau = narrate.assemble([(source, 0.0)], tmp_path, -16.0)

    assert niveau["mode"] == "linear"
    assert niveau["true_peak_db"] <= narrate.TRUE_PEAK_CEILING + 0.1
    assert niveau["measured_lufs"] < -16.0 - narrate.TARGET_TOLERANCE_LU
    avant = _niveaux_par_fenetre(source)
    apres = _niveaux_par_fenetre(tmp_path / "narration.wav")
    # La fenêtre de la crête est exclue : le reste suffit à voir un gain qui varie.
    rapports = [b / a for i, (a, b) in enumerate(zip(avant, apres, strict=False)) if i != 10]
    assert max(rapports) / min(rapports) < 1.01
    assert sum(rapports) / len(rapports) == pytest.approx(10 ** (niveau["gain_db"] / 20), rel=0.01)
