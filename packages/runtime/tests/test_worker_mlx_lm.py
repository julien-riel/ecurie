"""Adaptateurs mlx-lm — ce qui se vérifie sans Apple Silicon ni mlx.

Trois capacités partagent un chargement : la génération de texte, la traduction
et l'appel d'outils. Ce qui les distingue — composer une invite, nettoyer une
sortie, extraire un appel validable — est du code pur, donc entièrement
testable dans le venv d'Écurie, qui n'a ni mlx ni mlx-lm. C'est la situation de
la CI, et c'est ce qui donne leur valeur à ces tests.

Trois d'entre eux couvrent des fautes réellement rencontrées au premier
lancement, le 21 août 2026, et qu'aucune relecture n'avait vues :

- `apply_chat_template` rend un `BatchEncoding` en transformers 5.x, pas une
  chaîne, et le message d'erreur ne parle ni du gabarit ni de la conversation ;
- le jeton de fin de tour se retrouve **dans le texte** rendu, ce qui ne fait
  échouer personne et pollue durablement la Bibliothèque ;
- les modèles n'ont pas de format commun pour un appel d'outil.
"""

import json

import pytest
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.mlx_lm import (
    Consigne,
    MlxLmWorker,
    _couper,
    _normaliser_invite,
    _sans_marqueur_de_fin,
    import_runtime,
)
from ecurie_runtime.workers.mlx_lm_tools import (
    MlxLmToolsWorker,
    _objets_nus,
    extraire_appels,
    valider,
)
from ecurie_runtime.workers.mlx_lm_translate import (
    MlxLmTranslateWorker,
    build_prompt,
    nettoyer,
    nom_de_langue,
)


def test_les_modules_s_importent_sans_mlx():
    """La CI n'a pas Apple Silicon : un import remonté au niveau du module
    ferait échouer la collecte des tests, pas seulement ces workers."""
    assert MlxLmWorker.name == "mlx-lm"
    assert MlxLmTranslateWorker.name == "mlx-lm-translate"
    assert MlxLmToolsWorker.name == "mlx-lm-tools"


def test_l_absence_du_runtime_nomme_la_reparation():
    with pytest.raises(WorkerError) as échec:
        import_runtime()
    assert "ecurie env sync mlx-lm" in str(échec.value)


# --- l'invite, quelle que soit la version de transformers ---------------------


class _Encodage(dict):
    """Ce que `apply_chat_template` rend en transformers 5.x : un lot, pas un texte."""

    def __init__(self, ids):
        super().__init__(input_ids=ids)
        self.input_ids = ids


def test_une_invite_deja_textuelle_passe_telle_quelle():
    assert _normaliser_invite("<|im_start|>user\nbonjour") == "<|im_start|>user\nbonjour"


def test_un_lot_de_jetons_est_ramene_a_une_sequence():
    """Le défaut du premier lancement : « Invalid type BatchEncoding received in
    array initialization », un message qui ne dit rien de sa cause."""
    assert _normaliser_invite(_Encodage([[1, 2, 3]])) == [1, 2, 3]
    assert _normaliser_invite({"input_ids": [[4, 5]]}) == [4, 5]


def test_une_liste_de_jetons_nue_est_acceptee():
    assert _normaliser_invite([7, 8, 9]) == [7, 8, 9]


def test_un_gabarit_d_un_type_inattendu_est_refuse_avec_son_type():
    with pytest.raises(WorkerError) as échec:
        _normaliser_invite(object())
    assert "object" in str(échec.value)


# --- le jeton de fin de tour ---------------------------------------------------


class _Tokenizer:
    def __init__(self, eos: str | None = "<|im_end|>") -> None:
        self.eos_token = eos


@pytest.mark.parametrize(
    "brut",
    ["réponse<|im_end|>", "réponse<|im_end|>\n", "réponse</s>", "réponse<|endoftext|>"],
)
def test_le_marqueur_de_fin_ne_reste_pas_dans_le_texte(brut):
    """Il ne fait échouer personne : il pollue une traduction notée au caractère
    près, casse un bloc de code qu'on voudrait exécuter, et reste pour toujours
    dans le fichier de la Bibliothèque."""
    assert _sans_marqueur_de_fin(brut, _Tokenizer()) == "réponse"


def test_plusieurs_marqueurs_empiles_sont_tous_retires():
    assert _sans_marqueur_de_fin("fini<|im_end|><|endoftext|>", _Tokenizer()) == "fini"


def test_un_marqueur_au_milieu_du_texte_est_conserve():
    """On ne retire qu'en fin : au milieu, c'est du contenu — un extrait de code
    qui parle de gabarits de conversation, par exemple."""
    texte = "le jeton <|im_end|> termine le tour"
    assert _sans_marqueur_de_fin(texte, _Tokenizer()) == texte


def test_un_tokenizer_sans_jeton_de_fin_ne_casse_rien():
    assert _sans_marqueur_de_fin("réponse<|im_end|>", _Tokenizer(eos=None)) == "réponse"


# --- séquences d'arrêt ---------------------------------------------------------


def test_la_premiere_sequence_d_arret_tronque():
    texte, coupé = _couper("avant STOP après", ["STOP"])
    assert (texte, coupé) == ("avant", True)


def test_sans_sequence_d_arret_le_texte_est_intact():
    assert _couper("rien à couper", ["FIN"]) == ("rien à couper", False)
    assert _couper("rien à couper", None) == ("rien à couper", False)


def test_la_coupure_prend_la_sequence_la_plus_proche():
    texte, _ = _couper("a FIN b STOP c", ["STOP", "FIN"])
    assert texte == "a"


# --- consigne de conversation --------------------------------------------------


def test_la_consigne_systeme_precede_la_demande():
    messages = Consigne(system="sois bref", user="bonjour").messages()
    assert [m["role"] for m in messages] == ["system", "user"]


def test_une_consigne_systeme_vide_n_ajoute_aucun_message():
    assert Consigne(system="   ", user="bonjour").messages() == [
        {"role": "user", "content": "bonjour"}
    ]


# --- traduction ----------------------------------------------------------------


def test_les_codes_de_langue_deviennent_des_noms_francais():
    assert nom_de_langue("fr") == "français"
    assert nom_de_langue("fr-CA") == "français québécois"
    assert nom_de_langue("") is None


def test_un_code_de_langue_inconnu_passe_tel_quel():
    """Le modèle connaît bien plus de langues que la table : refuser au motif
    qu'elle n'y figure pas serait une limite inventée."""
    assert nom_de_langue("sw") == "sw"


def test_la_consigne_de_traduction_porte_les_deux_langues():
    invite = build_prompt("Bonjour.", "français", "anglais", "neutre", True)
    assert "du français vers le anglais" in invite
    assert "Bonjour." in invite


def test_sans_langue_de_depart_la_consigne_ne_l_invente_pas():
    invite = build_prompt("Bonjour.", None, "anglais", "neutre", False)
    assert "vers le anglais" in invite
    assert "du None" not in invite


def test_le_registre_demande_arrive_dans_la_consigne():
    assert "soutenu" in build_prompt("x", "français", "anglais", "soutenu", True)


def test_preserver_la_mise_en_forme_se_dit_ou_ne_se_dit_pas():
    assert "mise en forme" in build_prompt("x", None, "anglais", "neutre", True)
    assert "mise en forme" not in build_prompt("x", None, "anglais", "neutre", False)


@pytest.mark.parametrize(
    "brut",
    ["Voici la traduction : Hello.", "Traduction : Hello.", "Here is the translation: Hello."],
)
def test_le_preambule_est_retire_de_la_traduction(brut):
    """Il n'est pas dans l'original : au calcul du score, il compte comme des
    insertions, et il pénalise un modèle qui a pourtant bien traduit."""
    assert nettoyer(brut) == "Hello."


def test_une_traduction_sans_preambule_n_est_pas_amputee():
    assert nettoyer("  Hello there.  ") == "Hello there."


# --- appel d'outils : extraction -------------------------------------------------

OUTILS = [
    {
        "name": "lister_modeles",
        "description": "Liste les modèles.",
        "parameters": {
            "type": "object",
            "required": ["capability"],
            "properties": {"capability": {"type": "string"}},
        },
    }
]

APPEL = '{"name": "lister_modeles", "arguments": {"capability": "text-to-speech"}}'


def test_un_appel_balise_est_extrait_et_le_reste_aussi():
    appels, reste, stratégie = extraire_appels(f"Je regarde.\n<tool_call>\n{APPEL}\n</tool_call>")

    assert appels == [{"name": "lister_modeles", "arguments": {"capability": "text-to-speech"}}]
    assert reste == "Je regarde."
    assert stratégie == "tool_call"


def test_un_appel_nu_est_extrait():
    appels, _, stratégie = extraire_appels(APPEL)
    assert len(appels) == 1
    assert stratégie == "json_nu"


def test_un_appel_noye_dans_du_texte_est_extrait_et_signale():
    """La stratégie est rapportée : un modèle qu'il faut aller repêcher dans de
    la prose n'est pas au même niveau qu'un modèle qui balise proprement."""
    appels, _, stratégie = extraire_appels(f"Bien sûr ! {APPEL} Voilà.")
    assert len(appels) == 1
    assert stratégie == "json_noye"


def test_un_bloc_de_code_json_est_reconnu():
    appels, _, stratégie = extraire_appels(f"```json\n{APPEL}\n```")
    assert len(appels) == 1
    assert stratégie == "json_fence"


def test_la_forme_openai_avec_function_est_comprise():
    brut = '{"function": {"name": "lister_modeles", "arguments": {"capability": "x"}}}'
    appels, _, _ = extraire_appels(brut)
    assert appels == [{"name": "lister_modeles", "arguments": {"capability": "x"}}]


def test_des_arguments_serialises_deux_fois_sont_redecodes():
    """Certains modèles rendent `arguments` comme une chaîne JSON. Le prendre au
    mot donnerait un appel dont tous les arguments manquent."""
    brut = '{"name": "lister_modeles", "arguments": "{\\"capability\\": \\"x\\"}"}'
    appels, _, _ = extraire_appels(brut)
    assert appels[0]["arguments"] == {"capability": "x"}


def test_un_tableau_de_deux_appels_est_extrait_en_entier():
    brut = f'<tool_call>[{APPEL}, {{"name": "autre", "arguments": {{}}}}]</tool_call>'
    appels, _, _ = extraire_appels(brut)
    assert [a["name"] for a in appels] == ["lister_modeles", "autre"]


def test_une_reponse_en_clair_ne_produit_aucun_appel():
    """Savoir s'abstenir fait partie de la compétence : le texte doit survivre
    intact, et la liste d'appels être vide plutôt qu'absente."""
    appels, reste, stratégie = extraire_appels("Aucun outil ne convient ici.")
    assert appels == []
    assert reste == "Aucun outil ne convient ici."
    assert stratégie == "aucun"


def test_un_json_invalide_ne_fait_pas_echouer_l_extraction():
    appels, _, _ = extraire_appels("<tool_call>{ceci n'est pas du json}</tool_call>")
    assert appels == []


def test_les_accolades_dans_une_chaine_ne_trompent_pas_le_decoupage():
    """Compter les accolades sans tenir compte des chaînes couperait l'objet au
    milieu, et l'appel deviendrait du JSON invalide."""
    brut = '{"name": "lister_modeles", "arguments": {"capability": "a{b}c"}}'
    assert _objets_nus(f"texte {brut} texte") == [brut]


# --- appel d'outils : validation superficielle ------------------------------------


def test_un_outil_inconnu_est_reproche_par_son_nom():
    reproches = valider([{"name": "inexistant", "arguments": {}}], OUTILS)
    assert "inexistant" in reproches[0]


def test_un_argument_obligatoire_manquant_est_reproche():
    """Un appel bien nommé dont il manque un argument ne s'exécute pas : c'est un
    échec complet, pas un demi-succès."""
    reproches = valider([{"name": "lister_modeles", "arguments": {}}], OUTILS)
    assert "capability" in reproches[0]


def test_un_appel_complet_ne_recoit_aucun_reproche():
    assert valider([{"name": "lister_modeles", "arguments": {"capability": "x"}}], OUTILS) == []


def test_les_references_du_golden_set_ont_la_forme_que_le_worker_produit(repo_root):
    """Le jeu d'essai et l'adaptateur doivent parler de la même chose.

    Une référence écrite dans une forme que le worker ne produit jamais ferait
    noter zéro un modèle parfait, et le défaut serait dans le harnais.
    """
    dossier = repo_root / "registry" / "evals" / "golden" / "tool-use"
    manifeste = json.loads((dossier / "manifest.json").read_text())
    for cas in manifeste["cases"]:
        attendus = json.loads((dossier / cas["reference"]["json_file"]).read_text())
        outils = cas["input"]["tools"]
        # Une référence est un ensemble d'appels que `valider` accepte sans rien
        # reprocher : sinon la vérité terrain elle-même serait invalide.
        assert valider(attendus, outils) == [], cas["id"]
