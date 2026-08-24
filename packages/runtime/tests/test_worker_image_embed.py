"""L'empreinte visuelle — ce qui se vérifie sans poids, sans venv et sans Metal.

Un vecteur de 768 nombres ne se relit pas : ce qui casse en silence dans cette
capacité n'est jamais la valeur, c'est ce qui décide de l'espace auquel elle
appartient. Trois choses tiennent donc en code pur, et elles sont ici — le
câblage (runtime, capacité) → adaptateur, le refus d'une agrégation qu'une
architecture ne peut pas honorer, et l'arithmétique de la définition soumise.

Ce qui demande les poids — la qualité des rangs, la fidélité d'un cosinus — vit
dans `ecurie bench` et, un jour, dans un golden set. Rien de ce fichier n'importe
torch, timm, numpy ni Pillow au niveau du module : la CI tourne sans Apple
Silicon.
"""

import json

import pytest
from ecurie_runtime.envs import worker_module
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.dinov3_embed import (
    AGREGATIONS,
    DEFAUT_COTE,
    FOND,
    FOND_NOM,
    Dinov3EmbedWorker,
    _nom_timm,
    aligner,
    cosinus,
    definition,
    normaliser_l2,
    verifier_agregation,
)
from ecurie_runtime.workers.torch_vision import resolve_image

# --- câblage ----------------------------------------------------------------------


def test_le_module_s_importe_sans_torch_ni_timm():
    assert Dinov3EmbedWorker.name == "dinov3-embed"


def test_l_empreinte_visuelle_a_son_adaptateur_et_les_trois_voisines_n_ont_pas_bougé():
    """Quatrième capacité de `torch-vision`, et les trois autres restent en place.

    Le seul prix payé par cet env pour la recevoir est un plancher relevé sur
    `timm` ; ce test dit que le câblage, lui, n'a rien déplacé.
    """
    assert worker_module("torch-vision", "image-embed").endswith("dinov3_embed")
    assert worker_module("torch-vision", "image-matting").endswith("birefnet")
    assert worker_module("torch-vision", "image-upscale").endswith("swin2sr")
    assert worker_module("torch-vision", "image-segment").endswith("sam2")
    assert worker_module("torch-vision", None) is None


def test_un_seul_adaptateur_pour_deux_familles():
    """`dinov3_embed` sert aussi `dinov2` : ce sont les mêmes appels timm.

    Un second fichier n'aurait différé que par une constante. Ce qui les sépare
    — carte de traits contre suite de jetons — se lit sur le tenseur rendu, pas
    sur le nom du modèle.
    """
    assert worker_module("torch-vision", "image-embed") == (
        "ecurie_runtime.workers.dinov3_embed"
    )


# --- agrégation : un espace vectoriel, pas un réglage -----------------------------


def test_l_agregation_par_defaut_est_la_seule_que_les_deux_formes_honorent():
    assert verifier_agregation(None, jetons=False) == "mean"
    assert verifier_agregation(None, jetons=True) == "mean"
    assert set(AGREGATIONS) == {"mean", "cls"}


def test_cls_est_accepte_sur_un_modele_a_jetons():
    assert verifier_agregation("cls", jetons=True) == "cls"
    assert verifier_agregation("CLS", jetons=True) == "cls"


def test_cls_sur_une_carte_de_traits_est_refuse_plutot_que_remplace():
    """Une ConvNeXt n'a ni CLS ni registres.

    Servir la moyenne sous le nom `cls` rendrait un vecteur d'un autre espace, et
    rien n'échouerait : le job serait vert et le cosinus suivant, faux.
    """
    with pytest.raises(WorkerError) as échec:
        verifier_agregation("cls", jetons=False, nom="convnext_small.dinov3_lvd1689m")
    message = str(échec.value)
    assert "convnext_small.dinov3_lvd1689m" in message
    assert "options.pooling" in message


def test_une_agregation_inconnue_liste_celles_qui_existent():
    with pytest.raises(WorkerError) as échec:
        verifier_agregation("moyenne", jetons=True)
    assert "mean" in str(échec.value) and "cls" in str(échec.value)


# --- définition soumise ------------------------------------------------------------


def test_le_pas_du_reseau_change_ce_que_max_side_veut_dire():
    """MESURÉ au banc : 256 vaut 256 en pas 32 et 252 en pas 14.

    C'est pourquoi le document de sortie porte la définition réellement soumise
    et pas celle demandée — deux modèles de cette capacité ne voient jamais la
    même image.
    """
    assert definition((768, 768), 256, 32) == (256, 256)
    assert definition((768, 768), 256, 14) == (252, 252)
    assert definition((768, 768), 512, 14) == (518, 518)
    assert definition((768, 768), 1024, 14) == (1022, 1022)


def test_les_proportions_sont_gardees_et_rien_n_est_rogne():
    """Encoder l'image entière : rogner ferait disparaître le bord, c'est-à-dire,
    sur une pièce photographiée de loin, le sujet."""
    largeur, hauteur = definition((1024, 512), 256, 32)
    assert largeur == 256
    assert hauteur == 128


def test_un_cote_plus_petit_que_le_pas_ne_tombe_jamais_a_zero():
    """Une bande très allongée : le petit côté vaut au moins un pas, sinon le
    réseau reçoit un tenseur de largeur nulle et échoue sur une forme."""
    assert definition((2000, 20), 256, 32) == (256, 32)


def test_l_alignement_prend_le_multiple_le_plus_proche():
    assert aligner(252, 14) == 252
    assert aligner(255, 14) == 252
    assert aligner(260, 14) == 266
    assert aligner(3, 32) == 32


def test_le_defaut_du_module_est_celui_du_contrat(repo_root):
    contrat = json.loads(
        (repo_root / "registry" / "capabilities" / "image-embed.json").read_text()
    )
    assert contrat["input"]["properties"]["max_side"]["default"] == DEFAUT_COTE


# --- vecteurs ----------------------------------------------------------------------


def test_le_cosinus_ne_depend_pas_de_la_normalisation():
    """`normalize` est un réglage d'affichage du vecteur, pas de la comparaison.

    Un job qui aurait rendu deux similarités selon ce réglage aurait été
    incomparable avec lui-même.
    """
    a = [3.0, 4.0, 0.0]
    b = [0.0, 4.0, 3.0]
    assert cosinus(a, b) == cosinus(normaliser_l2(a), normaliser_l2(b))


def test_deux_vecteurs_identiques_donnent_un():
    assert cosinus([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0


def test_deux_vecteurs_orthogonaux_donnent_zero():
    assert cosinus([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_un_vecteur_nul_ne_divise_pas_par_zero():
    assert cosinus([0.0, 0.0], [1.0, 1.0]) is None
    assert normaliser_l2([0.0, 0.0]) == [0.0, 0.0]


def test_deux_longueurs_differentes_disent_que_ce_ne_sont_pas_les_memes_poids():
    """768 des deux côtés ne prouve rien, mais 768 contre 384 prouve l'inverse :
    ces deux vecteurs ne viennent pas du même modèle, et leur cosinus serait un
    nombre sans référent."""
    with pytest.raises(WorkerError, match="même modèle"):
        cosinus([1.0] * 4, [1.0] * 3)


def test_la_normalisation_ramene_bien_a_un():
    assert normaliser_l2([3.0, 4.0]) == [0.6, 0.8]


# --- lecture de l'amont ------------------------------------------------------------


def test_le_nom_timm_porte_le_tag_du_depot():
    """Sans le tag, timm retombe sur la configuration par défaut de
    l'architecture — une autre normalisation, un autre `crop_pct`, une autre
    licence — et pas la moindre erreur pour le signaler."""
    config = {"architecture": "convnext_small", "pretrained_cfg": {"tag": "dinov3_lvd1689m"}}
    assert _nom_timm(config) == "convnext_small.dinov3_lvd1689m"


def test_un_depot_sans_tag_garde_l_architecture_seule():
    assert _nom_timm({"architecture": "convnext_small"}) == "convnext_small"


def test_un_config_sans_architecture_dit_que_ce_n_est_pas_un_depot_timm():
    with pytest.raises(WorkerError, match="timm"):
        _nom_timm({"num_classes": 0})


# --- entrées -----------------------------------------------------------------------


def test_le_champ_fautif_est_nomme_quand_il_y_en_a_deux(tmp_path):
    """Cette capacité reçoit deux images. Un message qui parlerait toujours
    d'`image` enverrait corriger le mauvais champ."""
    with pytest.raises(WorkerError, match="compare_to"):
        resolve_image("", tmp_path, "compare_to")
    with pytest.raises(WorkerError, match="compare_to"):
        resolve_image(str(tmp_path / "absente.png"), tmp_path, "compare_to")


# --- contrat -----------------------------------------------------------------------


def test_le_fond_compose_est_une_constante_et_non_un_parametre(repo_root):
    """Deux vecteurs produits sur deux fonds différents ne sont pas comparables.

    MESURÉ : le même `cube.png` lu sur fond blanc puis sur fond noir donne 0,9254
    de cosinus sur `dinov3@convnext-small` et 0,8148 sur `dinov2@vit-base`, quand
    le cube et la sphère sont à 0,6856 et 0,5560. La façon de lire le fichier
    pèse du quart aux deux cinquièmes de ce qui sépare deux objets — un paramètre
    l'aurait laissé arriver sans que rien ne le signale.
    """
    assert FOND == (255, 255, 255)
    assert FOND_NOM == "#FFFFFF"
    contrat = json.loads(
        (repo_root / "registry" / "capabilities" / "image-embed.json").read_text()
    )
    assert "fond" not in contrat["input"]["properties"]
    assert "pooling" not in contrat["input"]["properties"]


def test_le_contrat_declare_les_sorties_que_l_adaptateur_ecrit(repo_root):
    """Une sortie exigée par le contrat et jamais écrite fait échouer le job
    après l'inférence — c'est-à-dire après avoir payé."""
    contrat = json.loads(
        (repo_root / "registry" / "capabilities" / "image-embed.json").read_text()
    )
    assert set(contrat["output"]["required"]) == {"embedding", "dimensions"}
    assert "similarity" in contrat["output"]["properties"]
