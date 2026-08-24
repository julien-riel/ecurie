"""L'empreinte de séquence protéique — ce qui se vérifie sans poids et sans torch.

Une capacité, un runtime, et quatre pièces qui tiennent en code pur : le câblage
(runtime, capacité) → adaptateur, le nettoyage de la séquence, le refus de ce que
le modèle ne sait pas lire, et l'agrégation. Ce sont exactement les pièces dont
dépend l'honnêteté du vecteur — l'encodage lui-même demande les poids et vit dans
`ecurie bench`.

Le refus mérite d'être testé ici plutôt qu'au banc, et pour une raison mesurée
sur les poids livrés : `EsmTokenizer` **regroupe** les caractères qu'il ignore.
`MJQ`, `MJJQ` et `MJJJQ` rendent tous les trois `[<cls>, M, <unk>, Q, <eos>]` —
une suite étrangère de trois lettres devient un seul jeton, la séquence raccourcit,
et le vecteur est celui d'autre chose. Un banc au vert ne regarde pas ce qu'un
fichier contient ; ces tests-ci regardent.

Ils tournent en CI, sur des machines sans Apple Silicon, sans poids et sans venv
de runtime : rien de ce fichier n'importe torch ni transformers, pas plus que
l'adaptateur lui-même au niveau de son module.
"""

import pytest
from ecurie_runtime.envs import NOT_YET, worker_module
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.esm_embed import (
    ALPHABET_ESM,
    DEFAUT_MAX_LENGTH,
    EsmEmbedWorker,
    cosinus,
    lire_sequence,
    normaliser_l2,
    norme,
    plafond_residus,
    ressemble_a_un_acide_nucleique,
    verifier_agregation,
    verifier_alphabet,
    weights_dir,
)

UBIQUITINE = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"


# --- câblage ----------------------------------------------------------------------


def test_la_capacite_est_servie_par_son_adaptateur():
    assert worker_module("esm-torch", "protein-embed").endswith("esm_embed")


def test_le_runtime_esm_torch_n_a_pas_d_adaptateur_par_defaut():
    """Un runtime est une famille de bibliothèques, pas une promesse d'API commune.

    Sans capacité il n'y a rien à servir, et le message nomme celle qui existe
    plutôt que de laisser tomber sur un « adaptateur non livré ».
    """
    assert worker_module("esm-torch", None) is None
    assert "protein-embed" in NOT_YET["esm-torch"]


def test_l_adaptateur_s_instancie_sans_torch():
    """L'instanciation ne charge rien : c'est ce qui permet `--self-test` dans un
    env qui n'a pas encore été synchronisé, et l'import en CI sans Apple Silicon."""
    assert EsmEmbedWorker().name == "esm-embed"


# --- lecture de la séquence -------------------------------------------------------


def test_une_sequence_brute_passe_telle_quelle():
    séquence, avertissements = lire_sequence(UBIQUITINE, "sequence")

    assert séquence == UBIQUITINE
    assert avertissements == []


def test_un_enregistrement_fasta_perd_son_en_tete_et_ses_retours_a_la_ligne():
    """C'est la forme sous laquelle une séquence se copie depuis à peu près
    n'importe quelle base. Refuser le chevron enverrait éditer à la main ce que
    l'adaptateur sait faire ; garder les retours à la ligne changerait le
    découpage en jetons, un FASTA étant coupé tous les soixante caractères."""
    fasta = ">1UBQ_1|Chain A|UBIQUITIN|Homo sapiens\n" + UBIQUITINE[:40] + "\n" + UBIQUITINE[40:]

    séquence, avertissements = lire_sequence(fasta, "sequence")

    assert séquence == UBIQUITINE
    assert any("FASTA" in message for message in avertissements)


def test_un_fasta_multiple_n_encode_que_le_premier_et_le_dit():
    """Concaténer en silence fabriquerait une protéine qui n'existe pas, et le
    cosinus qui en sortirait serait celui de cette chimère."""
    fasta = f">a\n{UBIQUITINE}\n>b\nMKVLA\n"

    séquence, avertissements = lire_sequence(fasta, "sequence")

    assert séquence == UBIQUITINE
    assert any("2 enregistrements" in message for message in avertissements)


def test_les_blancs_internes_sont_retires():
    """Le tokenizer d'ESM traite l'espace en séparateur : une séquence recopiée
    par blocs de dix passerait sans erreur et encoderait autre chose."""
    assert lire_sequence("MQIF VKTL\tTGKT", "sequence")[0] == "MQIFVKTLTGKT"


def test_la_casse_est_remontee():
    """Le vocabulaire d'ESM n'a que des majuscules ; une séquence en minuscules
    tomberait entièrement sur le jeton inconnu."""
    assert lire_sequence("mqifvk", "sequence")[0] == "MQIFVK"


def test_une_entree_vide_nomme_le_champ_fautif():
    """Deux champs portent une séquence — `sequence` et `compare_to` — et un
    message qui parlerait toujours du premier enverrait corriger le mauvais."""
    with pytest.raises(WorkerError, match="compare_to"):
        lire_sequence("   ", "compare_to")


def test_un_fasta_sans_sequence_est_refuse():
    with pytest.raises(WorkerError, match="en-tête"):
        lire_sequence(">1UBQ_1|Chain A\n", "sequence")


# --- alphabet ---------------------------------------------------------------------


def test_une_sequence_standard_ne_produit_aucun_avertissement():
    assert verifier_alphabet(UBIQUITINE, ALPHABET_ESM, "sequence") == []


def test_les_residus_ambigus_du_vocabulaire_sont_acceptes():
    """X, B, Z, U et O sont dans `vocab.txt` — X à l'identifiant 24, pas 3. Les
    refuser écarterait des séquences parfaitement lisibles par le modèle."""
    assert verifier_alphabet("MQXBZUO", ALPHABET_ESM, "sequence") == []


def test_une_lettre_hors_alphabet_est_refusee_en_la_situant():
    """LE refus de ce fichier. Mesuré sur les poids livrés : `MJQ`, `MJJQ` et
    `MJJJQ` rendent tous les trois les mêmes cinq jetons — une suite étrangère
    devient UN jeton, quelle que soit sa longueur. Encoder rendrait un vecteur de
    norme 1, une `length` plausible et un cosinus qui n'est celui de rien."""
    with pytest.raises(WorkerError, match="position 5"):
        verifier_alphabet("MQIFJ", ALPHABET_ESM, "sequence")


def test_le_refus_nomme_le_champ_et_le_caractere():
    with pytest.raises(WorkerError) as échec:
        verifier_alphabet("MQIF1VK", ALPHABET_ESM, "compare_to")

    assert "compare_to" in str(échec.value)
    assert "'1'" in str(échec.value)


def test_un_alignement_est_encode_mais_signale():
    """`.` et `-` SONT dans le vocabulaire : les refuser serait mentir sur ce que
    le modèle sait lire. Mais une ligne d'alignement n'est pas la séquence
    qu'elle représente, et rien dans le vecteur ne le montrerait."""
    avertissements = verifier_alphabet("MQIF--VKTL", ALPHABET_ESM, "sequence")

    assert any("alignement" in message for message in avertissements)


def test_une_sequence_d_adn_est_signalee():
    """A, C, G et T sont quatre acides aminés valides. Mesuré, un fragment d'ADN
    de 75 bases s'encode sans erreur et rend un cosinus de 0,531 avec
    l'ubiquitine — la plage exacte de deux protéines sans rapport. Aucune
    vérification ne peut l'écarter ; le worker le dit."""
    adn = "ATGGTGAGCAAGGGCGAGGAGCTGTTCACCGGGGTGGTGCCCATCCTGGTCGAGCTGGACGGCGACGTAAACGGC"

    avertissements = verifier_alphabet(adn, ALPHABET_ESM, "sequence")

    assert any("nucléotides" in message for message in avertissements)


def test_un_peptide_court_de_quatre_lettres_n_est_pas_pris_pour_de_l_adn():
    """`GATTACA` est une protéine possible. Le seuil existe pour que le message
    ne devienne pas du bruit sur les entrées où la coïncidence est banale."""
    assert not ressemble_a_un_acide_nucleique("GATTACA")
    assert verifier_alphabet("GATTACA", ALPHABET_ESM, "sequence") == []


# --- agrégation -------------------------------------------------------------------


def test_l_agregation_par_defaut_est_la_moyenne():
    assert verifier_agregation(None) == "mean"


def test_l_agregation_du_variant_est_reprise():
    assert verifier_agregation("cls") == "cls"


def test_une_agregation_inconnue_est_refusee_en_disant_quoi_corriger():
    """Elle appartient au variant et non au contrat : mesuré sur le chemin livré,
    la paire ubiquitine / lysozyme rend 0,6649 en `mean` et 0,9615 en `cls` —
    mêmes protéines, mêmes poids, et une échelle qui n'a plus rien à voir. Deux
    espaces, pas deux réglages."""
    with pytest.raises(WorkerError, match="options.pooling"):
        verifier_agregation("pooler")


# --- plafond de résidus -----------------------------------------------------------


def test_le_plafond_par_defaut_est_celui_du_contrat():
    assert plafond_residus(None) == DEFAUT_MAX_LENGTH


def test_un_plafond_hors_bornes_est_refuse():
    """Redit ici alors que le contrat le borne déjà : un worker peut être appelé
    sans passer par la validation du contrat, et le tokenizer d'ESM déclare un
    `model_max_length` de 10^30 — sans ce garde-fou, cent mille résidus
    partiraient tels quels."""
    with pytest.raises(WorkerError, match=r"\[16 ; 2048\]"):
        plafond_residus(4096)
    with pytest.raises(WorkerError, match=r"\[16 ; 2048\]"):
        plafond_residus(8)


def test_un_plafond_qui_n_est_pas_un_entier_nomme_le_parametre():
    with pytest.raises(WorkerError, match="max_length"):
        plafond_residus("mille")


# --- géométrie du vecteur ---------------------------------------------------------


def test_la_normalisation_ramene_a_la_norme_un():
    assert norme(normaliser_l2([3.0, 4.0])) == pytest.approx(1.0)


def test_un_vecteur_nul_traverse_la_normalisation_sans_division():
    """Diviser par zéro ne dit rien de plus qu'un vecteur nul."""
    assert normaliser_l2([0.0, 0.0]) == [0.0, 0.0]


def test_le_cosinus_est_invariant_d_echelle():
    """C'est ce qui permet au job de rendre la même `similarity` que `normalize`
    soit vrai ou faux : un job qui aurait rendu deux nombres selon un réglage
    d'affichage aurait été incomparable avec lui-même."""
    a, b = [1.0, 2.0, 3.0], [2.0, 4.0, 6.0]

    assert cosinus(a, b) == pytest.approx(1.0)
    assert cosinus(normaliser_l2(a), normaliser_l2(b)) == pytest.approx(1.0)


def test_un_cosinus_entre_deux_longueurs_differentes_est_refuse():
    """Deux vecteurs de longueurs différentes ne viennent pas du même modèle, et
    le nombre qui en sortirait ne voudrait rien dire."""
    with pytest.raises(WorkerError, match="longueurs différentes"):
        cosinus([1.0, 0.0], [1.0, 0.0, 0.0])


def test_un_vecteur_nul_ne_donne_pas_de_cosinus():
    assert cosinus([0.0, 0.0], [1.0, 0.0]) is None


# --- poids ------------------------------------------------------------------------


def test_un_chemin_de_poids_absent_est_refuse_avec_ce_qui_repare():
    with pytest.raises(WorkerError, match="superviseur"):
        weights_dir({})


def test_un_chemin_de_poids_qui_n_est_pas_un_dossier_est_refuse(tmp_path):
    """`from_pretrained` ne court-circuite le réseau que sur un dossier : sur une
    chaîne quelconque il la prend pour un identifiant de dépôt et parle du Hub."""
    fichier = tmp_path / "model.safetensors"
    fichier.write_bytes(b"")

    with pytest.raises(WorkerError, match="ne télécharge jamais"):
        weights_dir({"weights_path": str(fichier)})
