"""Les trois capacités de texte servies par le moteur de mlx-vlm.

Ce qui se vérifie sans mlx tient en trois questions, et ce sont les trois qui
ont bloqué Qwen3.6-27B : la capacité choisit-elle le bon adaptateur, les
adaptateurs héritent-ils vraiment de leurs jumeaux `mlx-lm` plutôt que de les
recopier, et le format d'appel d'outils du modèle est-il lu ?

Le reste — le chargement, la génération, le pic — demande des poids et vit dans
`ecurie bench`.
"""

import pytest
from ecurie_runtime.envs import worker_module
from ecurie_runtime.workers.base import sans_raisonnement
from ecurie_runtime.workers.mlx_lm import MlxLmWorker, _formes_d_outils
from ecurie_runtime.workers.mlx_lm_tools import MlxLmToolsWorker, extraire_appels
from ecurie_runtime.workers.mlx_lm_translate import MlxLmTranslateWorker
from ecurie_runtime.workers.mlx_vlm_lm import SurMlxVlm
from ecurie_runtime.workers.mlx_vlm_text import MlxVlmTextWorker
from ecurie_runtime.workers.mlx_vlm_tools import MlxVlmToolsWorker
from ecurie_runtime.workers.mlx_vlm_translate import MlxVlmTranslateWorker

# (adaptateur neuf, son jumeau mlx-lm, capacité, module neuf, module jumeau).
# `text-generation` n'a pas d'entrée propre côté mlx-lm : c'est l'adaptateur par
# défaut du runtime qui la sert, et c'est ce qui rend ce tableau utile — il dit
# les deux chemins plutôt que d'en déduire un du nom de l'autre.
TRIOS = (
    (MlxVlmTextWorker, MlxLmWorker, "text-generation", "mlx_vlm_text", "workers.mlx_lm"),
    (
        MlxVlmTranslateWorker,
        MlxLmTranslateWorker,
        "translation",
        "mlx_vlm_translate",
        "mlx_lm_translate",
    ),
    (MlxVlmToolsWorker, MlxLmToolsWorker, "tool-use", "mlx_vlm_tools", "mlx_lm_tools"),
)


def test_les_modules_s_importent_sans_mlx():
    assert MlxVlmTextWorker.name == "mlx-vlm-text"
    assert MlxVlmTranslateWorker.name == "mlx-vlm-translate"
    assert MlxVlmToolsWorker.name == "mlx-vlm-tools"


@pytest.mark.parametrize(("neuf", "jumeau", "capacité", "module", "jumeau_module"), TRIOS)
def test_chaque_capacite_choisit_l_adaptateur_du_bon_runtime(
    neuf, jumeau, capacité, module, jumeau_module
):
    """Sans ces entrées, un modèle vision-langage n'avait aucun moyen de servir
    ces trois contrats : leurs adaptateurs n'existaient que sous `mlx-lm`."""
    assert worker_module("mlx-vlm", capacité).endswith(module)
    assert worker_module("mlx-lm", capacité).endswith(jumeau_module)


@pytest.mark.parametrize(("neuf", "jumeau", "capacité", "module", "jumeau_module"), TRIOS)
def test_l_adaptateur_herite_de_son_jumeau_plutot_que_de_le_recopier(
    neuf, jumeau, capacité, module, jumeau_module
):
    """Le jour où l'un des deux est corrigé, l'autre l'est aussi.

    C'est tout l'intérêt du montage : `infer` n'est pas redéfini, donc la
    composition de l'invite et la lecture de la réponse ne peuvent pas diverger
    entre les deux runtimes.
    """
    assert issubclass(neuf, jumeau)
    assert issubclass(neuf, SurMlxVlm)
    assert "infer" not in vars(neuf)


@pytest.mark.parametrize(("neuf", "jumeau", "capacité", "module", "jumeau_module"), TRIOS)
def test_les_surcharges_se_limitent_au_moteur(neuf, jumeau, capacité, module, jumeau_module):
    """`SurMlxVlm` passe avant la classe de base, sinon ses surcharges seraient
    masquées par celles du jumeau et le worker chargerait mlx-lm."""
    mro = neuf.__mro__
    assert mro.index(SurMlxVlm) < mro.index(jumeau)
    assert set(vars(SurMlxVlm)) >= {"_import_runtime", "_flux", "load", "engendrer"}


# --- le mode « thinking » ---------------------------------------------------------


def test_le_raisonnement_est_separe_de_la_reponse():
    réponse, raisonnement = sans_raisonnement("<think>je pèse le pour</think>\n\nBonjour.")

    assert réponse == "Bonjour."
    assert raisonnement == "je pèse le pour"


def test_un_gabarit_qui_amorce_deja_think_est_reconnu_sans_balise_ouvrante():
    """Qwen3.6 amorce `<think>` dans le gabarit : la réponse commence donc
    directement par le raisonnement, sans balise ouvrante à trouver."""
    réponse, raisonnement = sans_raisonnement("d'abord ceci</think>\n\nLa réponse.")

    assert (réponse, raisonnement) == ("La réponse.", "d'abord ceci")


def test_une_reponse_sans_raisonnement_traverse_intacte():
    assert sans_raisonnement("Bonjour.") == ("Bonjour.", "")


def test_un_raisonnement_jamais_referme_est_rendu_tel_quel():
    """La réponse est vide, et c'est exactement ce qu'il faut voir : le job a
    manqué de jetons pour répondre. La masquer donnerait un faux succès."""
    texte = "je réfléchis encore et le budget s'épuise"

    assert sans_raisonnement(texte) == (texte, "")


# --- appels d'outils en XML -------------------------------------------------------


def test_le_format_xml_de_qwen36_est_extrait():
    """Le gabarit `qwen3_coder` n'émet pas de JSON. L'extracteur rendait zéro
    appel sur un modèle qui avait pourtant choisi le bon outil."""
    texte = (
        "<tool_call>\n<function=météo>\n"
        "<parameter=ville>\nParis\n</parameter>\n"
        "</function>\n</tool_call>"
    )

    appels, reste, stratégie = extraire_appels(texte)

    assert stratégie == "xml_function"
    assert appels == [{"name": "météo", "arguments": {"ville": "Paris"}}]
    assert reste == ""


def test_les_valeurs_xml_qui_ont_la_forme_d_un_scalaire_json_sont_retypees():
    texte = (
        "<function=prévoir><parameter=jours>3</parameter>"
        "<parameter=précis>true</parameter>"
        "<parameter=filtres>[\"pluie\"]</parameter></function>"
    )

    appels, _, _ = extraire_appels(texte)

    assert appels[0]["arguments"] == {"jours": 3, "précis": True, "filtres": ["pluie"]}


def test_une_valeur_de_texte_reste_du_texte():
    """Sans cette garde, une ville nommée « NaN » deviendrait un flottant et une
    référence « 007 » un entier."""
    texte = "<function=chercher><parameter=code>007</parameter></function>"

    appels, _, _ = extraire_appels(texte)

    assert appels[0]["arguments"]["code"] == "007"


def test_le_commentaire_en_clair_qui_precede_l_appel_est_conserve():
    """Le gabarit autorise une justification avant l'appel, et elle a sa place
    dans `text.txt` — mais pas dans les arguments."""
    texte = "Je consulte la météo.\n<tool_call><function=météo></function></tool_call>"

    appels, reste, stratégie = extraire_appels(texte)

    assert stratégie == "xml_function"
    assert appels == [{"name": "météo", "arguments": {}}]
    assert reste == "Je consulte la météo."


def test_le_json_balise_continue_de_passer_par_sa_propre_strategie():
    """La stratégie XML s'ajoute aux autres, elle ne les remplace pas."""
    balisé = '<tool_call>{"name": "f", "arguments": {"x": 1}}</tool_call>'

    appels, _, stratégie = extraire_appels(balisé)

    assert stratégie == "tool_call"
    assert appels == [{"name": "f", "arguments": {"x": 1}}]


def test_une_reponse_sans_appel_ne_fabrique_pas_d_appel():
    clair = "Aucun outil ne convient ici."

    assert extraire_appels(clair) == ([], clair, "aucun")


# --- appels d'outils au format Gemma ----------------------------------------------


def test_le_format_de_gemma4_est_extrait():
    """Quatrième format rencontré, et le plus déroutant : du JSON dont les clés
    sont nues et les guillemets rendus par un jeton spécial."""
    texte = '<|tool_call>call:meteo{ville:<|"|>Paris<|"|>}<tool_call|>'

    appels, reste, stratégie = extraire_appels(texte)

    assert stratégie == "gemma_tool_call"
    assert appels == [{"name": "meteo", "arguments": {"ville": "Paris"}}]
    assert reste == ""


def test_les_scalaires_nus_de_gemma_sont_retypes():
    texte = '<|tool_call>call:reserver{couverts:4,terrasse:true,ville:<|"|>Lyon<|"|>}<tool_call|>'

    appels, _, _ = extraire_appels(texte)

    assert appels[0]["arguments"] == {"couverts": 4, "terrasse": True, "ville": "Lyon"}


def test_une_virgule_dans_une_chaine_ne_coupe_pas_l_argument():
    """Un `split(",")` couperait la valeur en deux, dont la seconde moitié
    n'aurait pas de clé — et serait perdue sans que rien ne le signale."""
    texte = '<|tool_call>call:x{a:<|"|>Lyon, 3e arrondissement<|"|>}<tool_call|>'

    appels, _, _ = extraire_appels(texte)

    assert appels[0]["arguments"] == {"a": "Lyon, 3e arrondissement"}


def test_le_commentaire_qui_precede_un_appel_gemma_est_conserve():
    texte = 'Je consulte.\n<|tool_call>call:meteo{ville:<|"|>Nice<|"|>}<tool_call|>'

    appels, reste, _ = extraire_appels(texte)

    assert appels[0]["name"] == "meteo"
    assert reste == "Je consulte."


def test_les_outils_plats_sont_aussi_proposes_enveloppes_au_gabarit():
    """Qwen3 lit un outil plat, Gemma 4 lit `tool.function.name` et lève sur la
    forme plate. Sans cette seconde tentative, `template_tools` rendait faux et
    l'on mesurait un repli là où l'appel natif était disponible."""
    plat = {"name": "f", "parameters": {}}

    formes = _formes_d_outils([plat])

    assert formes[0] == [plat]
    assert formes[1] == [{"type": "function", "function": plat}]


def test_un_outil_deja_enveloppe_n_est_pas_propose_deux_fois():
    enveloppé = {"type": "function", "function": {"name": "f", "parameters": {}}}

    assert _formes_d_outils([enveloppé]) == [[enveloppé]]


def test_sans_outil_il_n_y_a_rien_a_proposer():
    assert _formes_d_outils(None) == []
    assert _formes_d_outils([]) == []
