"""Alignement forcé — ce qui se vérifie sans mlx, sans poids et sans Apple Silicon.

Trois choses tiennent dans du code pur, et ce sont les trois qui ont coûté le
plus cher à trouver : le ré-appariement de la ponctuation, qui se décale
silencieusement quand on l'improvise ; les refus motivés, qui doivent tomber
avant le job et non au milieu ; et la relecture de ce que l'adaptateur écrit,
parce que le banc d'essai vérifie qu'un fichier existe, jamais ce qu'il contient.
Cette dernière leçon vient de `moss-transcribe`, qui a passé ses trois cas en
livrant des marqueurs de locuteur dans un fichier annoncé en texte brut.

Ce qui demande les poids — la justesse des horodatages — vit dans `ecurie bench`
et dans le fichier de mesure, pas ici.
"""

import json
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from ecurie_runtime.envs import worker_module
from ecurie_runtime.workers.base import InferRequest, WorkerError
from ecurie_runtime.workers.qwen3_align import (
    DECOUPEURS_TIERS,
    ENV_NAME,
    MOTS_NAME,
    PLAFOND_DUR_S,
    SOUS_TITRES,
    Qwen3AlignWorker,
    empan,
    en_srt,
    en_vtt,
    repliques,
    surfaces_originales,
)

REPO_ROOT = Path(__file__).parents[3]
CONTRAT = REPO_ROOT / "registry" / "capabilities" / "audio-align.json"
CHARGE = REPO_ROOT / "registry" / "evals" / "bench" / "audio-align.json"


def demande(**champs) -> InferRequest:
    return InferRequest(
        job_id="j1",
        input=champs.pop("input", {}),
        params=champs.pop("params", {}),
        output_dir=champs.pop("output_dir", Path(".")),
        seed=champs.pop("seed", None),
    )


# --- doublures de la bibliothèque -------------------------------------------
#
# `surfaces_originales` reçoit `clean_token` et `split_segment_with_chinese` en
# argument précisément pour qu'on puisse les doubler ici. Ces deux-là reproduisent
# la règle de mlx-audio ; la production, elle, appelle les vraies — les
# approximer là-bas serait se décaler d'un mot le jour où l'amont change d'avis.


def _nettoyer(jeton: str) -> str:
    gardés = [c for c in jeton if c == "'" or unicodedata.category(c)[0] in ("L", "N")]
    return "".join(gardés)


def _est_cjk(c: str) -> bool:
    return 0x4E00 <= ord(c) <= 0x9FFF


def _decouper(segment: str) -> list[str]:
    morceaux: list[str] = []
    tampon: list[str] = []
    for c in segment:
        if _est_cjk(c):
            if tampon:
                morceaux.append("".join(tampon))
                tampon = []
            morceaux.append(c)
        else:
            tampon.append(c)
    if tampon:
        morceaux.append("".join(tampon))
    return morceaux


class FauxSignal:
    """Le strict nécessaire de ce que `load_audio` rend : une forme et une tranche."""

    def __init__(self, n: int) -> None:
        self.shape = (n,)

    def __getitem__(self, tranche: slice) -> "FauxSignal":
        return FauxSignal(min(self.shape[0], tranche.stop))


class FauxMx:
    def reset_peak_memory(self) -> None:
        pass

    def get_peak_memory(self) -> int:
        return 2_000_000_000

    def clear_cache(self) -> None:
        pass


class FauxProcesseur:
    clean_token = staticmethod(_nettoyer)
    split_segment_with_chinese = staticmethod(_decouper)


class FauxModele:
    """Rend les unités qu'on lui dicte, comme le vrai : nettoyées, sans ponctuation."""

    def __init__(self, unites: list[tuple[str, float, float]]) -> None:
        self.unites = unites
        self.aligner_processor = FauxProcesseur()
        self.vu: dict[str, Any] = {}

    def generate(self, *, audio: Any, text: str, language: str) -> Any:
        self.vu = {"text": text, "language": language, "audio": audio}
        segments = [{"text": t, "start": d, "end": f} for t, d, f in self.unites]
        return type("Résultat", (), {"segments": segments})()

    def get_supported_languages(self) -> list[str]:
        return ["english", "french"]


def worker_arme(unites: list[tuple[str, float, float]], *, echantillons: int) -> Qwen3AlignWorker:
    worker = Qwen3AlignWorker()
    worker.model = FauxModele(unites)
    worker.mx = FauxMx()
    worker.load_audio = lambda chemin, sr: FauxSignal(echantillons)
    worker.langues = ["english", "french"]
    return worker


# --- imports paresseux -------------------------------------------------------


def test_module_importable_sans_mlx():
    """La CI importe tous les adaptateurs sur une machine sans Apple Silicon."""
    code = (
        "import sys, ecurie_runtime.workers.qwen3_align as m;"
        "print(m.ENV_NAME, 'mlx' in sys.modules, 'mlx_audio' in sys.modules)"
    )
    résultat = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert résultat.returncode == 0, résultat.stderr
    assert résultat.stdout.split() == [ENV_NAME, "False", "False"]


# --- aiguillage --------------------------------------------------------------


def test_aligner_et_transcrire_sont_deux_adaptateurs_du_meme_runtime():
    """Neuvième emploi de `mlx-audio`, et le premier qui reçoit le texte.

    La confusion que le contrat existe pour rendre impossible se lirait ici en
    premier : si `audio-align` retombait sur l'adaptateur de transcription, le
    parc rendrait des mots là où on demande des instants.
    """
    assert worker_module("mlx-audio", "audio-align").endswith("qwen3_align")
    assert worker_module("mlx-audio", "speech-to-text").endswith("moss_transcribe")


# --- fidélité au contrat -----------------------------------------------------


def test_les_sorties_ecrites_sont_celles_que_le_contrat_exige():
    contrat = json.loads(CONTRAT.read_text())["output"]
    assert contrat["required"] == ["words", "units", "span_seconds"]
    assert MOTS_NAME.endswith(".json")
    # `subtitles` est la seule sortie facultative : elle n'est pas dans `required`,
    # et l'adaptateur ne la nomme dans son `output` que s'il l'a écrite — un
    # `result` qui annonce un fichier absent est plus grave que pas de fichier.
    assert "subtitles" not in contrat["required"]
    assert contrat["properties"]["subtitles"]["contentMediaType"] == "text/plain"


def test_les_formats_de_sous_titre_du_contrat_sont_ceux_que_l_adaptateur_sait_ecrire():
    """Un enum du contrat que le worker ne saurait pas rendre ferait un job en échec
    au moment de l'écriture, après avoir payé le calcul."""
    entrée = json.loads(CONTRAT.read_text())["input"]["properties"]
    assert set(entrée["subtitle_format"]["enum"]) == {"none", *SOUS_TITRES}


def test_le_plafond_du_contrat_reste_sous_le_mur_du_reseau():
    """300 s est la borne prudente de la carte Qwen ; 400 s est le mur d'architecture
    (5000 classes × 80 ms). Un contrat qui dépasserait le second promettrait des
    horodatages que le réseau ne peut pas représenter."""
    entrée = json.loads(CONTRAT.read_text())["input"]["properties"]
    assert entrée["max_seconds"]["maximum"] <= PLAFOND_DUR_S


def test_la_charge_type_fait_varier_le_parametre_qu_elle_declare():
    """Un `scaling_parameter` que les trois cas laissent constant ne donne aucune
    pente, et le banc jette l'ajustement sans que la charge ait l'air fautive."""
    charge = json.loads(CHARGE.read_text())
    valeurs = [c["input"][charge["scaling_parameter"]] for c in charge["cases"]]
    assert len(set(valeurs)) == 3
    assert valeurs == sorted(valeurs)


# --- ré-appariement de la ponctuation ----------------------------------------


def test_la_ponctuation_du_texte_source_est_rendue_a_chaque_unite():
    """Le cas dur, mesuré sur les vraies fonctions de la bibliothèque : treize
    jetons source pour dix unités rendues. « % », « — » et « : » n'ont aucun
    horodatage et disparaissent des deux côtés ; « 12,5 » et « Marie-Josée » en
    ont un, mais sous une forme que l'utilisateur ne reconnaîtrait pas."""
    texte = "Le 15 mars, Marie-Josée a payé 12,5 % — réf. R-1428 : d'accord."
    assert surfaces_originales(texte, _nettoyer, _decouper) == [
        "Le",
        "15",
        "mars,",
        "Marie-Josée",
        "a",
        "payé",
        "12,5",
        "réf.",
        "R-1428",
        "d'accord.",
    ]


def test_un_jeton_sans_lettre_ni_chiffre_ne_donne_aucune_unite():
    assert surfaces_originales("bonjour — — : ?", _nettoyer, _decouper) == ["bonjour"]


def test_un_jeton_qui_se_decoupe_rend_ses_morceaux_et_non_sa_forme_source():
    """Aucune forme originale ne correspond une à une aux idéogrammes d'un même
    jeton : leur rendre le jeton entier ferait trois fois le même texte."""
    assert surfaces_originales("你好吗", _nettoyer, _decouper) == ["你", "好", "吗"]


def test_un_texte_vide_ne_donne_aucune_surface():
    assert surfaces_originales("   ", _nettoyer, _decouper) == []


# --- la sonde ----------------------------------------------------------------


def test_l_empan_est_la_fin_du_dernier_moins_le_debut_du_premier():
    mots = [{"start": 1.28, "end": 2.0}, {"start": 2.0, "end": 9.21}]
    assert empan(mots) == 7.93


def test_aucun_mot_donne_un_empan_nul():
    assert empan([]) == 0.0


# --- sous-titres -------------------------------------------------------------


def test_une_replique_se_ferme_a_la_fin_d_une_phrase():
    """Trouvé en relisant la sortie du banc : sans cette règle, une réplique
    enjambait « … sans un mot. On lui a demandé … »."""
    mots = [
        {"text": "Bonjour.", "start": 0.0, "end": 0.5},
        {"text": "Ça", "start": 0.6, "end": 0.8},
        {"text": "va", "start": 0.8, "end": 1.0},
    ]
    groupes = repliques(mots)
    assert [g["text"] for g in groupes] == ["Bonjour.", "Ça va"]


def test_un_silence_coupe_la_replique():
    mots = [
        {"text": "un", "start": 0.0, "end": 0.2},
        {"text": "deux", "start": 3.0, "end": 3.2},
    ]
    assert len(repliques(mots)) == 2


def test_une_phrase_trop_longue_se_coupe_quand_meme():
    """La fin de phrase prime, mais elle ne donne pas droit à trois lignes."""
    mots = [{"text": "mot", "start": i * 0.1, "end": i * 0.1 + 0.05} for i in range(40)]
    groupes = repliques(mots)
    assert len(groupes) > 1
    assert all(len(g["text"]) <= 84 for g in groupes)


def test_le_srt_numerote_et_use_de_la_virgule_decimale():
    mots = [{"text": "Bonjour.", "start": 1.5, "end": 2.25}]
    assert en_srt(mots).splitlines()[:3] == [
        "1",
        "00:00:01,500 --> 00:00:02,250",
        "Bonjour.",
    ]


def test_le_vtt_s_annonce_et_use_du_point_decimal():
    mots = [{"text": "Bonjour.", "start": 1.5, "end": 2.25}]
    lignes = en_vtt(mots).splitlines()
    assert lignes[0] == "WEBVTT"
    assert lignes[2] == "00:00:01.500 --> 00:00:02.250"


def test_un_millieme_qui_arrondit_a_la_seconde_ne_produit_pas_1000():
    """3,9996 s donnerait « 00:00:03,1000 » sans le report — un horodatage que
    tous les lecteurs refusent, sur un fichier par ailleurs valide."""
    mots = [{"text": "a", "start": 3.9996, "end": 59.9999}]
    assert "00:00:04,000 --> 00:01:00,000" in en_srt(mots)


# --- refus motivés -----------------------------------------------------------


def test_infer_avant_load_le_dit():
    with pytest.raises(WorkerError, match="infer avant load"):
        Qwen3AlignWorker().infer(demande(), lambda *_: None)


def test_un_texte_absent_rappelle_que_cette_capacite_ne_transcrit_pas():
    """Le refus le plus utile du fichier : c'est exactement la confusion que le
    contrat existe pour empêcher."""
    worker = worker_arme([], echantillons=16_000)
    with pytest.raises(WorkerError) as échec:
        worker.infer(demande(input={"audio": "a.wav"}), lambda *_: None)
    assert "« text » est obligatoire" in str(échec.value)
    assert "speech-to-text" in str(échec.value)


def test_un_audio_introuvable_donne_le_chemin(tmp_path):
    worker = worker_arme([], echantillons=16_000)
    with pytest.raises(WorkerError, match="audio introuvable"):
        worker.infer(
            demande(input={"audio": "absent.wav", "text": "bonjour"}, output_dir=tmp_path),
            lambda *_: None,
        )


def test_une_fenetre_au_dela_du_mur_du_reseau_est_refusee(tmp_path):
    worker = worker_arme([], echantillons=16_000)
    with pytest.raises(WorkerError) as échec:
        worker.infer(
            demande(
                input={"audio": "a.wav", "text": "bonjour", "max_seconds": 500},
                output_dir=tmp_path,
            ),
            lambda *_: None,
        )
    assert "5000 classes" in str(échec.value)


def test_les_deux_langues_sans_decoupeur_sont_nommees():
    assert set(DECOUPEURS_TIERS) == {"japanese", "korean"}
    assert DECOUPEURS_TIERS["japanese"] == "nagisa"


def test_une_langue_sans_decoupeur_est_refusee_avec_le_paquet_et_la_reparation(monkeypatch):
    """Refusée à l'entrée plutôt que laissée lever un ImportError au milieu du job.

    Le refus est vérifié et non décrété : il s'ouvre de lui-même le jour où le
    paquet entre dans l'env, ce que la doublure exprime en nommant un module qui
    n'existera jamais.
    """
    monkeypatch.setattr(
        "ecurie_runtime.workers.qwen3_align.DECOUPEURS_TIERS",
        {"klingon": "un_module_qui_n_existe_pas"},
    )
    worker = Qwen3AlignWorker()
    with pytest.raises(WorkerError) as échec:
        worker._exiger_decoupeur("klingon")
    assert "un_module_qui_n_existe_pas" in str(échec.value)
    assert f"ecurie env sync {ENV_NAME}" in str(échec.value)


def test_une_langue_avec_decoupeur_integre_passe():
    Qwen3AlignWorker()._exiger_decoupeur("french")


def test_un_format_de_sous_titre_inconnu_est_refuse(tmp_path):
    worker = worker_arme([], echantillons=16_000)
    with pytest.raises(WorkerError, match="subtitle_format"):
        worker.infer(
            demande(
                input={"audio": "a.wav", "text": "bonjour", "subtitle_format": "ass"},
                output_dir=tmp_path,
            ),
            lambda *_: None,
        )


# --- ce que l'adaptateur écrit vraiment --------------------------------------


def _fichier_audio(tmp_path: Path) -> Path:
    chemin = tmp_path / "a.wav"
    chemin.write_bytes(b"RIFF")
    return chemin


def test_le_fichier_de_mots_porte_les_formes_du_texte_source(tmp_path):
    """La leçon de `moss-transcribe`, appliquée : on ouvre le fichier.

    Le modèle rend « TroisRivières » ; le contrat promet « le texte qui a été
    fourni, ponctuation comprise ». Un job vert dont le fichier dit autre chose
    que le contrat est exactement ce que le banc d'essai ne voit pas.
    """
    worker = worker_arme([("de", 0.0, 0.5), ("TroisRivières", 0.5, 1.4)], echantillons=32_000)
    résultat = worker.infer(
        demande(
            input={"audio": str(_fichier_audio(tmp_path)), "text": "de Trois-Rivières."},
            output_dir=tmp_path,
        ),
        lambda *_: None,
    )
    écrit = json.loads((tmp_path / MOTS_NAME).read_text())
    assert [m["text"] for m in écrit] == ["de", "Trois-Rivières."]
    assert résultat.output["units"] == 2
    assert résultat.output["span_seconds"] == 1.4
    assert résultat.output["duration_seconds"] == 2.0
    assert "subtitles" not in résultat.output


def test_un_decompte_qui_ne_tombe_pas_garde_les_formes_du_modele_et_le_dit(tmp_path):
    """S'abstenir plutôt que se décaler : un ré-appariement d'un cran attribuerait
    chaque horodatage au mot suivant, ce qui est pire que rendre la forme nettoyée."""
    worker = worker_arme([("un", 0.0, 0.5)], echantillons=32_000)
    résultat = worker.infer(
        demande(
            input={"audio": str(_fichier_audio(tmp_path)), "text": "un deux trois"},
            output_dir=tmp_path,
        ),
        lambda *_: None,
    )
    écrit = json.loads((tmp_path / MOTS_NAME).read_text())
    assert [m["text"] for m in écrit] == ["un"]
    assert any("formes originales non rétablies" in a for a in résultat.metrics["avertissements"])


def test_un_empan_effondre_est_signale(tmp_path):
    """La sonde du contrat, rendue actionnable. C'est ce que produit un texte dont
    une phrase revient mot pour mot : le modèle colle sur une occurrence et tasse
    le reste sur un instant, sans lever d'exception."""
    worker = worker_arme([("un", 19.8, 19.84), ("deux", 19.84, 19.84)], echantillons=320_000)
    résultat = worker.infer(
        demande(
            input={"audio": str(_fichier_audio(tmp_path)), "text": "un deux"},
            output_dir=tmp_path,
        ),
        lambda *_: None,
    )
    assert any("effondré" in a for a in résultat.metrics["avertissements"])


def test_une_troncature_est_dite_avec_les_deux_durees(tmp_path):
    worker = worker_arme([("un", 0.0, 0.5)], echantillons=320_000)
    résultat = worker.infer(
        demande(
            input={
                "audio": str(_fichier_audio(tmp_path)),
                "text": "un",
                "max_seconds": 5,
            },
            output_dir=tmp_path,
        ),
        lambda *_: None,
    )
    assert résultat.output["duration_seconds"] == 5.0
    assert any("tronqué à 5 s sur 20.0 s" in a for a in résultat.metrics["avertissements"])


def test_une_langue_hors_liste_annoncee_est_signalee_sans_refus(tmp_path):
    """Un code ISO retomberait sans erreur sur le découpage par espaces. Pour le
    français c'est le bon comportement ; pour « zh » ce serait faux, et rien ne le
    dirait. D'où l'avertissement plutôt que le refus."""
    worker = worker_arme([("un", 0.0, 0.5)], echantillons=32_000)
    résultat = worker.infer(
        demande(
            input={"audio": str(_fichier_audio(tmp_path)), "text": "un", "language": "fr"},
            output_dir=tmp_path,
        ),
        lambda *_: None,
    )
    assert any("codes ISO" in a for a in résultat.metrics["avertissements"])
    assert worker.model.vu["language"] == "fr"


def test_les_sous_titres_ne_sont_ecrits_que_si_on_les_demande(tmp_path):
    worker = worker_arme([("Bonjour", 0.0, 0.5)], echantillons=32_000)
    résultat = worker.infer(
        demande(
            input={
                "audio": str(_fichier_audio(tmp_path)),
                "text": "Bonjour.",
                "subtitle_format": "vtt",
            },
            output_dir=tmp_path,
        ),
        lambda *_: None,
    )
    assert résultat.output["subtitles"] == SOUS_TITRES["vtt"]
    assert (tmp_path / SOUS_TITRES["vtt"]).read_text().startswith("WEBVTT")


# --- chargement --------------------------------------------------------------


def test_un_objet_qui_n_est_pas_un_aligneur_est_refuse_au_chargement(tmp_path, monkeypatch):
    """Le piège que l'en-tête du module décrit : `load_model` passe par la classe
    ASR, qui aiguille d'après un `model_type` de sous-config. Le jour où
    l'indirection disparaît, on obtiendrait un modèle de transcription chargé en
    `strict=False` sur des poids d'aligneur — sans exception, et avec une sortie
    qui n'est pas celle du contrat."""
    monkeypatch.setattr(
        "ecurie_runtime.workers.qwen3_align._import_runtime",
        lambda: (FauxMx(), lambda _chemin: object(), lambda *a, **k: None),
    )
    with pytest.raises(WorkerError) as échec:
        Qwen3AlignWorker().load({"weights_path": str(tmp_path), "ref": "m@v"})
    assert "n'est pas un aligneur" in str(échec.value)
    assert "aligner_processor" in str(échec.value)


def test_des_poids_absents_nomment_la_commande_de_telechargement(monkeypatch):
    monkeypatch.setattr(
        "ecurie_runtime.workers.qwen3_align._import_runtime",
        lambda: (FauxMx(), lambda _chemin: object(), lambda *a, **k: None),
    )
    with pytest.raises(WorkerError, match="ecurie pull m@v"):
        Qwen3AlignWorker().load({"weights_path": "/nulle/part", "ref": "m@v"})
