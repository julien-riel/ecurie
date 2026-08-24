"""L'image et la consigne vers une action — ce qui se vérifie sans poids ni robot.

Une capacité, un runtime, et six pièces qui tiennent dans du code pur : le
câblage (runtime, capacité) → adaptateur, la lecture de la consigne et de l'état,
le refus de servir un tronçon anonyme, le contrôle de domaine, l'empreinte qui
porte la reproductibilité, et la résolution des deux dépôts de poids.

**Le refus d'un manifeste sans incarnation est le test le plus important de ce
fichier.** C'est la seule barrière entre un tronçon de sept flottants et un
tronçon dont on sait pour quelle machine il a été calculé, dans quelle convention
et dans quelle échelle. Aucune de ces trois choses ne se lit sur les poids ; sans
ce refus, un manifeste incomplet produirait des nombres parfaitement plausibles
que rien n'empêcherait de prendre pour des ordres universels.

**Le contrôle de domaine mérite le même soin.** Il est le seul contrôle de fond
disponible sans robot, et il n'est utile que si l'on distingue un dépassement de
sept centièmes sur une pince — mesuré, cela arrive — d'un tronçon parti ailleurs.
D'où le compte et la marge testés à côté du drapeau.

Ils tournent en CI, sur des machines sans Apple Silicon, sans poids et sans venv
de runtime : rien de ce fichier n'importe torch, lerobot, numpy ni PIL, pas plus
que l'adaptateur lui-même au niveau de son module.
"""

import json
from pathlib import Path

import pytest
from ecurie_runtime.envs import NOT_YET, worker_module
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.smolvla_act import (
    ESPACES,
    ETAT_MAX,
    PAS_DEFAUT,
    PAS_MAX,
    ROLE_VLM,
    UNITES,
    SmolvlaActWorker,
    domaine,
    empreinte,
    fichier_statistiques,
    lire_consigne,
    lire_etat,
    plan_action,
    resolve_image,
    verifier_options,
    version_lerobot,
    vlm_dir,
    weights_dir,
)

#: L'enveloppe publiée par le titulaire, à quatre décimales. La septième
#: composante est la pince : ses bornes valent exactement ±1 quand les six autres
#: tiennent au large, et c'est elle qui décide de presque tous les dépassements.
BORNES = {
    "min": [-0.9375, -0.9375, -0.9375, -0.2582, -0.375, -0.3675, -1.0],
    "max": [0.9375, 0.9375, 0.9375, 0.3557, 0.375, 0.375, 1.0],
}

INCARNATION = {
    "embodiment": "LIBERO — bras Franka Emika Panda simulé, contrôleur OSC de robosuite",
    "space": "cartesian-delta",
    "units": "controller-normalized",
    "gripper_index": 6,
}


def pas(pince: float = 0.5) -> list[float]:
    return [0.1, -0.2, 0.3, 0.01, 0.02, -0.01, pince]


# --- câblage ----------------------------------------------------------------------


def test_la_capacite_est_servie_par_son_adaptateur():
    assert worker_module("lerobot", "robot-action").endswith("smolvla_act")


def test_le_runtime_lerobot_n_a_pas_d_adaptateur_par_defaut():
    """Un runtime est une famille de bibliothèques, pas une promesse d'API commune."""
    assert worker_module("lerobot", None) is None
    assert "robot-action" in NOT_YET["lerobot"]


# --- l'incarnation, qui ne se lit pas sur les poids --------------------------------


def test_un_manifeste_sans_incarnation_est_refuse():
    """LE test de ce fichier : servir un tronçon anonyme, c'est publier sept flottants.

    Ni le robot, ni la convention, ni l'échelle ne se lisent dans un checkpoint.
    Le refus tombe au chargement, où il désigne le manifeste fautif, et non au
    premier job, où il aurait déjà coûté son warmup.
    """
    with pytest.raises(WorkerError) as levée:
        verifier_options({})
    message = str(levée.value)
    assert "embodiment" in message and "space" in message and "units" in message


@pytest.mark.parametrize("absente", ["embodiment", "space", "units"])
def test_chacune_des_trois_options_manque_a_elle_seule(absente):
    options = {clé: valeur for clé, valeur in INCARNATION.items() if clé != absente}
    with pytest.raises(WorkerError, match=absente):
        verifier_options(options)


def test_une_option_vide_vaut_une_option_absente():
    """Une chaîne blanche nommerait un robot qui n'a pas de nom."""
    with pytest.raises(WorkerError, match="embodiment"):
        verifier_options({**INCARNATION, "embodiment": "   "})


def test_une_convention_hors_vocabulaire_est_refusee():
    with pytest.raises(WorkerError) as levée:
        verifier_options({**INCARNATION, "space": "cartesien"})
    assert all(valeur in str(levée.value) for valeur in ESPACES)


def test_des_unites_hors_vocabulaire_sont_refusees():
    with pytest.raises(WorkerError) as levée:
        verifier_options({**INCARNATION, "units": "mm"})
    assert all(valeur in str(levée.value) for valeur in UNITES)


def test_l_incarnation_complete_est_rendue_telle_quelle():
    lue = verifier_options(INCARNATION)
    assert lue["space"] == "cartesian-delta"
    assert lue["units"] == "controller-normalized"
    assert lue["gripper_index"] == 6


def test_un_index_de_pince_booleen_est_refuse():
    """`True` vaut 1 en Python, et désignerait silencieusement la deuxième composante."""
    with pytest.raises(WorkerError, match="gripper_index"):
        verifier_options({**INCARNATION, "gripper_index": True})


# --- les réglages -----------------------------------------------------------------


def test_les_defauts_du_manifeste_servent_quand_le_job_ne_dit_rien():
    plan = plan_action(entree={}, params={}, defaults={"steps": 5, "seed": 3})
    assert (plan.steps, plan.seed) == (5, 3)


def test_l_entree_du_job_prime_sur_le_manifeste():
    plan = plan_action(entree={"steps": 2}, params={}, defaults={"steps": 5})
    assert plan.steps == 2


def test_sans_rien_les_defauts_du_contrat_s_appliquent():
    plan = plan_action(entree={}, params={}, defaults={})
    assert (plan.steps, plan.seed) == (PAS_DEFAUT, 0)


def test_un_nombre_de_pas_hors_bornes_est_refuse():
    """Le contrat borne déjà ; un worker peut être appelé sans passer par lui."""
    with pytest.raises(WorkerError, match=str(PAS_MAX)):
        plan_action(entree={"steps": PAS_MAX + 1}, params={}, defaults={})


# --- la consigne ------------------------------------------------------------------


def test_une_consigne_vide_est_refusee():
    with pytest.raises(WorkerError, match="instruction"):
        lire_consigne("   ")


def test_les_retours_a_la_ligne_sont_ramenes_a_des_espaces():
    texte, _ = lire_consigne("pick up\n  the red   cube\n")
    assert texte == "pick up the red cube"


def test_une_consigne_anglaise_ne_provoque_aucun_avertissement():
    _, avertissements = lire_consigne("push the blue ball to the left")
    assert avertissements == []


def test_un_caractere_hors_ascii_est_signale():
    _, avertissements = lire_consigne("ramasse le cube rouge posé à gauche")
    assert len(avertissements) == 1
    assert "anglais" in avertissements[0] and "muet" in avertissements[0]


def test_une_phrase_francaise_sans_accent_passe_au_travers():
    """La limite du contrôle, écrite plutôt que masquée.

    Il n'existe aucun moyen honnête de reconnaître une langue sur cinq mots. Ce
    test existe pour que personne ne croie que l'avertissement suffit : c'est le
    contrat, et lui seul, qui dit que la consigne doit être en anglais.
    """
    _, avertissements = lire_consigne("ramasse le cube rouge")
    assert avertissements == []


# --- l'état -----------------------------------------------------------------------


def test_une_dimension_fausse_nomme_les_deux_nombres():
    """Le message d'amont, lui, ne nomme ni le champ ni le fichier fautif.

    `RuntimeError: The size of tensor a (6) must match the size of tensor b (8)`
    tombe au fond du normaliseur et envoie chercher au mauvais endroit. Le refus
    est pris ici, où l'on sait dire lequel des deux nombres vient d'où.
    """
    with pytest.raises(WorkerError) as levée:
        lire_etat([0.0] * 6, 8)
    message = str(levée.value)
    assert "6" in message and "8" in message and "config.json" in message


def test_une_dimension_juste_passe():
    assert lire_etat([0.0] * 8, 8) == [0.0] * 8


def test_l_etat_arrive_aussi_en_json_depuis_le_terminal():
    assert lire_etat("[1, 2.5, -3]", 3) == [1.0, 2.5, -3.0]


def test_un_json_illisible_est_refuse_en_nommant_le_champ():
    with pytest.raises(WorkerError, match="state"):
        lire_etat("[1, 2,", 3)


def test_un_etat_vide_est_refuse():
    with pytest.raises(WorkerError, match="où le bras est"):
        lire_etat([], None)


def test_un_booleen_n_est_pas_un_nombre():
    """`True` vaut 1.0 en Python et passerait pour une coordonnée."""
    with pytest.raises(WorkerError, match=r"state.*\[1\]"):
        lire_etat([0.0, True, 0.0], 3)


def test_un_etat_demesure_est_refuse_meme_sans_dimension_attendue():
    with pytest.raises(WorkerError, match=str(ETAT_MAX)):
        lire_etat([0.0] * (ETAT_MAX + 1), None)


def test_sans_statistiques_toute_dimension_est_acceptee():
    """Un variant qui ne publie pas ses statistiques reste servable, et c'est dit ailleurs."""
    assert len(lire_etat([0.0] * 14, None)) == 14


# --- le domaine -------------------------------------------------------------------


def test_un_troncon_dans_l_enveloppe_est_ok():
    contrôle = domaine([pas(0.5), pas(-0.5)], BORNES)
    assert contrôle["domain_ok"] is True
    assert contrôle["out_of_domain"] == 0
    assert contrôle["domain_margin"] == 0.0


def test_un_depassement_de_pince_est_compte_et_mesure():
    """Le cas réellement observé : la pince seule, de quelques centièmes.

    Mesuré sur un vrai job, 32 valeurs hors bornes sur 350, toutes sur l'axe de
    la pince, minimum -1,0689 pour une borne à -1. Un drapeau seul ferait passer
    cela pour la même chose qu'un tronçon parti ailleurs.
    """
    contrôle = domaine([pas(-1.0689), pas(0.5)], BORNES)
    assert contrôle["domain_ok"] is False
    assert contrôle["out_of_domain"] == 1
    # La marge est rapportée à l'étendue de l'axe, qui vaut 2 pour la pince.
    assert contrôle["domain_margin"] == pytest.approx(0.0689 / 2.0, abs=1e-4)


def test_la_marge_rapporte_le_depassement_a_l_etendue_de_son_axe():
    """Deux centièmes sur une pince à ±1 et sur une rotation à ±0,26 ne se valent pas."""
    pince = domaine([pas(1.02)], BORNES)["domain_margin"]
    rotation = domaine([[0.1, -0.2, 0.3, 0.3757, 0.02, -0.01, 0.5]], BORNES)["domain_margin"]
    assert rotation > pince


def test_sans_bornes_publiees_le_domaine_est_faux_et_le_dit():
    """Faux faute de pouvoir répondre, et non par constat — la nuance est dans l'avertissement."""
    contrôle = domaine([pas()], None)
    assert contrôle["domain_ok"] is False
    assert contrôle["out_of_domain"] == 0
    assert contrôle["domain_margin"] is None
    assert contrôle["warnings"] and "pas les statistiques" in contrôle["warnings"][0]


# --- l'empreinte ------------------------------------------------------------------


def test_deux_troncons_identiques_ont_la_meme_empreinte():
    assert empreinte([pas(), pas(0.1)]) == empreinte([pas(), pas(0.1)])


def test_un_dernier_bit_change_l_empreinte():
    """C'est exactement l'écart qu'on cherche à voir : arrondir le masquerait."""
    assert empreinte([pas(0.5)]) != empreinte([pas(0.5000001)])


def test_l_ordre_des_pas_change_l_empreinte():
    """Un tronçon est une séquence : deux mêmes pas dans l'autre ordre est un autre geste."""
    assert empreinte([pas(0.1), pas(0.9)]) != empreinte([pas(0.9), pas(0.1)])


# --- la lecture de l'amont --------------------------------------------------------


def test_le_fichier_de_statistiques_se_lit_dans_le_pipeline_et_non_par_son_nom():
    """Le nom porte le RANG de l'étape : une étape insérée avant le décalerait."""
    pipeline = {
        "steps": [
            {"registry_name": "to_batch_processor", "config": {}},
            {"registry_name": "device_processor", "config": {"device": "cuda"}},
            {
                "registry_name": "normalizer_processor",
                "state_file": "policy_preprocessor_step_9_normalizer_processor.safetensors",
            },
        ]
    }
    assert fichier_statistiques(pipeline).endswith("step_9_normalizer_processor.safetensors")


def test_un_pipeline_sans_normaliseur_ne_rend_rien():
    assert fichier_statistiques({"steps": [{"registry_name": "to_batch_processor"}]}) is None


def test_une_version_de_lerobot_mesuree_ne_dit_rien():
    assert version_lerobot("0.6.1") == []


def test_une_autre_branche_de_lerobot_est_signalee_sans_etre_refusee():
    """Signalée et non refusée : ici la panne serait bruyante, pas silencieuse."""
    avertissements = version_lerobot("0.7.0")
    assert len(avertissements) == 1 and "pyproject.toml" in avertissements[0]


# --- les deux dépôts --------------------------------------------------------------


def test_des_poids_absents_rappellent_qu_un_worker_ne_telecharge_pas():
    with pytest.raises(WorkerError, match="ne télécharge jamais"):
        weights_dir({"weights_path": "/ce/chemin/n/existe/pas"})


def test_un_encodeur_absent_renvoie_vers_extra_sources():
    with pytest.raises(WorkerError) as levée:
        vlm_dir({"extra_paths": {}})
    message = str(levée.value)
    assert "extra_sources" in message and f"role: {ROLE_VLM}" in message


def test_un_encodeur_incomplet_renvoie_vers_les_allow_patterns(tmp_path):
    """Un dépôt ramené à moitié ne se répare pas comme un dépôt absent."""
    with pytest.raises(WorkerError, match="allow_patterns"):
        vlm_dir({"extra_paths": {ROLE_VLM: str(tmp_path)}})


def test_un_encodeur_complet_est_accepte(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"model_type": "smolvlm"}))
    assert vlm_dir({"extra_paths": {ROLE_VLM: str(tmp_path)}}) == tmp_path


# --- les vues ---------------------------------------------------------------------


def test_une_vue_est_resolue_depuis_le_dossier_du_job(tmp_path):
    """Le superviseur transmet un chemin relatif : c'est ce qui rend le job rejouable."""
    (tmp_path / "inputs").mkdir()
    image = tmp_path / "inputs" / "vue.png"
    image.write_bytes(b"")
    assert resolve_image("inputs/vue.png", tmp_path, "image") == image


def test_un_format_non_gere_est_refuse(tmp_path):
    fichier = tmp_path / "vue.tif"
    fichier.write_bytes(b"")
    with pytest.raises(WorkerError, match="format non géré"):
        resolve_image(str(fichier), tmp_path, "image2")


def test_une_vue_manquante_nomme_le_champ(tmp_path):
    with pytest.raises(WorkerError, match="image3"):
        resolve_image("absente.png", tmp_path, "image3")


# --- l'adaptateur sans poids ------------------------------------------------------


def test_l_adaptateur_s_instancie_sans_torch_ni_lerobot():
    """La CI n'a ni Apple Silicon, ni venv de runtime, ni poids."""
    worker = SmolvlaActWorker()
    assert worker.name == "smolvla-act"
    assert worker.torch is None and worker.policy is None


def test_inferer_avant_de_charger_est_refuse():
    with pytest.raises(WorkerError, match="infer avant load"):
        SmolvlaActWorker().infer(None, lambda pct, note="": None)


def test_un_chargement_sans_incarnation_echoue_avant_tout_import(tmp_path):
    """Le refus du manifeste précède l'import de lerobot : il tient donc en CI.

    C'est aussi ce qui le rend utile — il tombe avant d'avoir ouvert trois
    gigaoctets, là où il désigne encore ce qu'il faut corriger.
    """
    (tmp_path / "config.json").write_text("{}")
    with pytest.raises(WorkerError, match="embodiment"):
        SmolvlaActWorker().load(
            {
                "weights_path": str(tmp_path),
                "extra_paths": {ROLE_VLM: str(tmp_path)},
                "options": {},
            }
        )


def test_le_contrat_de_la_capacite_declare_ce_que_l_adaptateur_rend():
    """Les enums du contrat et ceux de l'adaptateur ne peuvent pas diverger en silence.

    Ils sont écrits deux fois — dans le JSON que l'UI rend et dans le module qui
    valide le manifeste —, et deux vocabulaires qui glissent l'un par rapport à
    l'autre donneraient un manifeste accepté au chargement et refusé par le
    contrat, ou l'inverse.
    """
    racine = Path(__file__).resolve().parents[3]
    contrat = json.loads(
        (racine / "registry" / "capabilities" / "robot-action.json").read_text()
    )
    sorties = contrat["output"]["properties"]
    assert tuple(sorties["space"]["enum"]) == ESPACES
    assert tuple(sorties["units"]["enum"]) == UNITES
    assert contrat["input"]["properties"]["steps"]["maximum"] == PAS_MAX
    assert contrat["input"]["properties"]["steps"]["default"] == PAS_DEFAUT
    assert contrat["input"]["properties"]["state"]["maxItems"] == ETAT_MAX
