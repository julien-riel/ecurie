"""Adaptateur de débruitage — le traitement du signal, et le refus qui le garde.

Cet adaptateur porte tout le DSP de DeepFilterNet autour d'un réseau qui, lui,
est publié. Deux choses se vérifient sans Apple Silicon et sans les poids :

- **la chaîne analyse → synthèse rend l'entrée**. C'est la propriété qui décide
  de tout le reste : une STFT mal mise à l'échelle produit quand même un fichier,
  simplement dégradé. Le test la mesure au lieu de la supposer ;
- **le refus tient**. La chaîne complète dégrade le signal (mesuré le 24 août
  2026, voir l'en-tête du module) ; tant que la comparaison à la référence
  `libdf` n'a pas eu lieu, aucun job ne doit écrire un audio soi-disant nettoyé.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from ecurie_runtime.envs import WORKER_MODULES, WORKER_MODULES_BY_CAPABILITY, worker_module
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.dfn3_denoise import (
    OPTION_FORCER,
    SAMPLE_RATE_MODELE,
    SAMPLE_RATES_SORTIE,
    Dfn3DenoiseWorker,
    analyser,
    filtre_profond,
    norm_alpha,
    plan_denoise,
    synthetiser,
)

REPO_ROOT = Path(__file__).parents[3]
CONTRAT = REPO_ROOT / "registry" / "capabilities" / "audio-denoise.json"


def plan(**champs):
    return plan_denoise(
        entree=champs.pop("entree", {}),
        params=champs.pop("params", {}),
        defaults=champs.pop("defaults", {}),
    )


# --- imports paresseux -------------------------------------------------------


def test_module_importable_sans_mlx():
    code = (
        "import sys, ecurie_runtime.workers.dfn3_denoise as m;"
        "print(m.Dfn3DenoiseWorker.name, 'mlx' in sys.modules, 'numpy' in sys.modules)"
    )
    résultat = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert résultat.returncode == 0, résultat.stderr
    assert résultat.stdout.split() == ["dfn3-denoise", "False", "False"]


# --- choix de l'adaptateur ---------------------------------------------------


def test_la_capacite_choisit_cet_adaptateur():
    """Sans cette entrée, le worker TTS recevait DeepFilterNet3 et le refusait
    par « Model type deepfilternet3 not supported for tts »."""
    assert worker_module("mlx-audio", "audio-denoise").endswith("dfn3_denoise")
    assert worker_module("mlx-audio", None) == WORKER_MODULES["mlx-audio"]
    assert ("mlx-audio", "audio-denoise") in WORKER_MODULES_BY_CAPABILITY


# --- le refus ----------------------------------------------------------------


def test_le_chargement_refuse_tant_que_le_dsp_n_est_pas_valide():
    with pytest.raises(WorkerError) as échec:
        Dfn3DenoiseWorker().load({"weights_path": "/inexistant"})
    message = str(échec.value)
    # Le message doit porter les trois choses qu'un lecteur cherchera : ce qui a
    # été mesuré, pourquoi ça bloque, et comment passer outre pour mesurer.
    assert "DÉGRADE" in message
    assert "libdf" in message
    assert OPTION_FORCER in message


def test_l_option_du_variant_leve_le_refus_et_va_jusqu_aux_poids():
    """Levé le refus, le chargement reprend son cours normal : le message suivant
    doit parler des poids, pas du DSP."""
    with pytest.raises(WorkerError) as échec:
        Dfn3DenoiseWorker().load(
            {"weights_path": "/inexistant", "options": {OPTION_FORCER: True}}
        )
    assert "DÉGRADE" not in str(échec.value)


# --- fidélité au contrat -----------------------------------------------------


def test_les_frequences_de_sortie_suivent_le_contrat():
    contrat = json.loads(CONTRAT.read_text())["input"]["properties"]
    assert sorted(SAMPLE_RATES_SORTIE) == sorted(contrat["sample_rate"]["enum"])
    assert contrat["sample_rate"]["default"] == SAMPLE_RATE_MODELE


def test_sortie_declaree_par_le_contrat():
    contrat = json.loads(CONTRAT.read_text())["output"]
    assert contrat["required"] == ["audio"]
    assert contrat["properties"]["audio"]["contentMediaType"] == "audio/wav"


# --- résolution d'un job -----------------------------------------------------


def test_les_defauts():
    demande = plan()
    assert demande.strength == 1.0
    assert demande.sample_rate == SAMPLE_RATE_MODELE
    assert demande.warnings == ()


def test_une_force_hors_bornes_est_refusee():
    with pytest.raises(WorkerError):
        plan(entree={"strength": 1.5})


def test_une_frequence_hors_contrat_est_refusee():
    with pytest.raises(WorkerError) as échec:
        plan(entree={"sample_rate": 22050})
    assert "48000" in str(échec.value)


def test_la_decoupe_est_signalee_comme_inoperante():
    """Le réseau est récurrent et le portage MLX n'expose pas l'état de ses GRU :
    découper s'entendrait à chaque raccord."""
    avertissements = plan(entree={"segment_seconds": 5}).warnings
    assert avertissements and "segment_seconds" in avertissements[0]


def test_un_fondu_partiel_est_annonce_comme_tel():
    avertissements = plan(entree={"strength": 0.5}).warnings
    assert any("fondu" in a for a in avertissements)


# --- le facteur des normalisations glissantes --------------------------------


def test_alpha_reproduit_la_reference():
    """`df.utils.get_norm_alpha` : exp(-hop/(tau*sr)) arrondi à trois décimales."""
    assert norm_alpha(480, 48_000, 1.0) == 0.99


def test_alpha_reste_strictement_inferieur_a_un():
    """La boucle de précision de la référence existe pour ça : un alpha de 1
    figerait l'état, et la normalisation ne suivrait plus le signal."""
    assert norm_alpha(480, 48_000, 1000.0) < 1.0


# --- le traitement du signal -------------------------------------------------


def test_analyse_puis_synthese_rendent_le_signal():
    """La propriété qui valide l'échelle de la STFT, la fenêtre et le recouvrement.

    Sans elle, tout le reste est faux — et le fichier produit le serait
    silencieusement.
    """
    np = pytest.importorskip("numpy")
    hop = 480
    # Fenêtre de Vorbis, celle que le dépôt livre dans auxiliary.npz.
    n = np.arange(2 * hop)
    window = np.sin(np.pi / 2 * np.sin(np.pi * (n + 0.5) / (2 * hop)) ** 2).astype(np.float32)
    générateur = np.random.default_rng(0)
    signal = générateur.standard_normal(5 * hop).astype(np.float32) * 0.1

    spectre = analyser(signal, window, hop, np)
    reconstruit = synthetiser(spectre, window, hop, np)[: len(signal)]
    assert np.abs(reconstruit - signal).max() < 1e-5


def test_une_fenetre_sans_recouvrement_de_moitie_est_refusee():
    np = pytest.importorskip("numpy")
    with pytest.raises(WorkerError):
        analyser(np.zeros(960, dtype=np.float32), np.ones(500, dtype=np.float32), 480, np)


def test_le_filtre_profond_lit_le_passe_et_le_futur():
    """Ordre 5, anticipation 2 : deux trames de passé, la courante, deux de futur.

    Le test isole un seul tap non nul et vérifie *quelle* trame il est allé
    chercher — un décalage d'une trame ne s'entend pas sur un fichier, mais
    détruit la cohérence de phase que le filtre est censé exploiter.
    """
    np = pytest.importorskip("numpy")
    trames, bins, ordre = 6, 3, 5
    spectre = np.zeros((trames, bins), dtype=np.complex64)
    spectre[:, 0] = np.arange(trames)  # une trame se reconnaît à sa valeur

    for tap, décalage_attendu in enumerate((-2, -1, 0, 1, 2)):
        coefs = np.zeros((ordre, trames, bins), dtype=np.complex64)
        coefs[tap] = 1.0
        sortie = filtre_profond(spectre, coefs, ordre, 2, np)
        t = 3  # une trame du milieu, loin des bords
        assert sortie[t, 0].real == pytest.approx(t + décalage_attendu)


def test_le_filtre_profond_ne_couvre_que_ses_bins():
    np = pytest.importorskip("numpy")
    spectre = np.ones((4, 10), dtype=np.complex64)
    coefs = np.zeros((5, 4, 3), dtype=np.complex64)
    sortie = filtre_profond(spectre, coefs, 5, 2, np)
    assert sortie.shape == (4, 3)
