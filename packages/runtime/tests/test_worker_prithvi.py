"""L'imagerie satellite — ce qui se vérifie sans poids, sans rasterio et sans Metal.

Deux capacités, un runtime, et cinq pièces qui tiennent dans du code pur : le
câblage (runtime, capacité) → adaptateur, la sélection de bandes, le plan de
tuilage, le dépouillement du préfixe des poids, et le refus d'un chargement
partiel. Ce sont exactement les pièces dont dépend l'honnêteté de la sortie — le
reste, l'inférence, demande les poids et vit dans `ecurie bench`.

**Trois d'entre elles sont testées ici parce qu'elles échouent en silence.** La
sélection de bandes d'abord : un masque calculé sur le rouge, le proche
infrarouge et la vapeur d'eau a exactement l'aspect d'un masque juste. Le préfixe
des poids ensuite : mesuré sur cette machine, le point de contrôle de
Sen1Floods11 chargé sans dépouiller son préfixe `model.` laisse 368 poids
manquants et 381 inattendus, et rend un réseau qui tourne en produisant du bruit
sans lever d'exception. Le plan de tuilage enfin : une tuile mal posée déborde de
la scène ou en laisse une bande dehors, et le masque reste plausible.

Ils tournent en CI, sur des machines sans Apple Silicon, sans poids et sans venv
de runtime : rien de ce fichier n'importe torch, rasterio ni terratorch, pas plus
que les adaptateurs eux-mêmes au niveau de leurs modules.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from ecurie_runtime.envs import NOT_YET, worker_module
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.prithvi_base import (
    BANDES,
    CANAUX,
    aligner_multiple,
    depouiller_prefixe,
    exiger_chargement_complet,
    plan_tuiles,
    resolve_raster,
    unique_fichier,
    verifier_bandes,
    weights_dir,
)
from ecurie_runtime.workers.prithvi_embed import (
    PATCH,
    PrithviEmbedWorker,
    cosinus,
    jetons_de_tete,
    moyenne_vecteurs,
    normaliser_l2,
    tuile_alignee,
    verifier_agregation,
)
from ecurie_runtime.workers.prithvi_segment import (
    PAS_MPS,
    PrithviSegmentWorker,
    classe_interet,
    noms_classes,
)

# --- câblage ----------------------------------------------------------------------


def test_les_deux_capacites_sont_servies_par_leur_adaptateur():
    assert worker_module("terratorch", "geo-segment").endswith("prithvi_segment")
    assert worker_module("terratorch", "geo-embed").endswith("prithvi_embed")


def test_le_runtime_terratorch_n_a_pas_d_adaptateur_par_defaut():
    """Un runtime est une famille de bibliothèques, pas une promesse d'API commune.

    Segmenter et encoder ne partagent que l'encodeur : l'une rend une carte de
    classes, l'autre un vecteur. Sans capacité il n'y a rien à servir, et le
    message nomme les deux qui existent.
    """
    assert worker_module("terratorch", None) is None
    assert "geo-segment" in NOT_YET["terratorch"]
    assert "geo-embed" in NOT_YET["terratorch"]


def test_les_adaptateurs_s_instancient_sans_torch():
    """C'est ce que `--self-test` fait dans le venv du runtime, et ce que la CI
    fait sans Apple Silicon : construire l'objet ne doit toucher ni Metal ni les
    poids."""
    assert PrithviSegmentWorker().name == "prithvi-segment"
    assert PrithviEmbedWorker().name == "prithvi-embed"


# --- sélection de bandes ----------------------------------------------------------


def test_le_defaut_du_contrat_designe_les_six_bandes_de_prithvi():
    """[1, 2, 3, 8, 11, 12] sur un produit Sentinel-2 à treize bandes : B2, B3,
    B4, B8A, B11 et B12, soit bleu, vert, rouge, proche infrarouge étroit et les
    deux moyens infrarouges."""
    assert verifier_bandes([1, 2, 3, 8, 11, 12], 13) == (1, 2, 3, 8, 11, 12)
    assert len(BANDES) == CANAUX


def test_un_raster_deja_reduit_aux_six_bandes_se_lit_a_partir_de_zero():
    assert verifier_bandes([0, 1, 2, 3, 4, 5], 6) == (0, 1, 2, 3, 4, 5)


def test_cinq_bandes_sont_refusees_avec_la_raison():
    """Le refus tombe ici plutôt que dans la convolution : le message d'amont
    parlerait de formes de tenseurs, quand la cause est un canal manquant."""
    with pytest.raises(WorkerError, match="6 attendues"):
        verifier_bandes([1, 2, 3, 8, 11], 13)


def test_une_bande_hors_du_raster_est_refusee_en_nommant_le_repli():
    """L'erreur la plus probable de cette famille : le défaut du contrat suppose
    treize bandes, et un raster déjà réduit aux six utiles n'en a que six."""
    with pytest.raises(WorkerError, match=r"\[0, 1, 2, 3, 4, 5\]"):
        verifier_bandes([1, 2, 3, 8, 11, 12], 6)


def test_un_indice_negatif_est_refuse():
    with pytest.raises(WorkerError, match="jamais négatifs"):
        verifier_bandes([-1, 2, 3, 8, 11, 12], 13)


def test_un_indice_qui_n_est_pas_un_entier_est_refuse_en_le_citant():
    with pytest.raises(WorkerError, match="B8A"):
        verifier_bandes([1, 2, 3, "B8A", 11, 12], 13)
    with pytest.raises(WorkerError, match="pas entier"):
        verifier_bandes([1, 2, 3, 8.5, 11, 12], 13)


def test_l_absence_de_bandes_est_refusee_plutot_que_devinee():
    """Un worker peut être appelé sans passer par la validation du contrat, et
    c'est alors ici que la sortie cesserait de dire ce qu'elle a lu."""
    with pytest.raises(WorkerError, match="band_indices absent"):
        verifier_bandes(None)


# --- plan de tuilage --------------------------------------------------------------


def test_une_scene_plus_petite_qu_une_tuile_fait_une_seule_fenetre():
    """C'est le rembourrage du bord qui s'en occupe, et lui seul : découper une
    scène de 300 pixels en tuiles de 384 n'aurait aucun sens."""
    assert plan_tuiles(300, 384) == (0,)
    assert plan_tuiles(384, 384) == (0,)


def test_une_division_exacte_donne_des_tuiles_jointives():
    assert plan_tuiles(768, 384) == (0, 384)
    assert plan_tuiles(768, 192) == (0, 192, 384, 576)


def test_la_derniere_tuile_est_recollee_au_bord_plutot_que_debordee():
    """Une tuile qui sortirait de la scène devrait être rembourrée, et rembourrer
    au milieu d'une image qu'on possède est de l'invention là où il y a de la
    donnée. Le prix est un recouvrement supplémentaire, que l'accumulation des
    logits absorbe."""
    départs = plan_tuiles(768, 576)
    assert départs == (0, 192)
    assert départs[-1] + 576 == 768


def test_le_recouvrement_rapproche_les_departs():
    assert plan_tuiles(768, 384, 192) == (0, 192, 384)


def test_un_recouvrement_aussi_grand_que_la_tuile_est_refuse():
    """Sans cette borne, deux tuiles voisines ne progressent plus et la boucle
    ne s'arrête jamais."""
    with pytest.raises(WorkerError, match="strictement sous"):
        plan_tuiles(768, 384, 384)


def test_les_departs_couvrent_toujours_la_scene_entiere():
    """La propriété qui compte, et la seule qui se vérifie sans le modèle : aucun
    pixel n'est laissé dehors, aucune tuile ne déborde."""
    for cote in (37, 300, 512, 768, 1000, 4096):
        for tuile in (192, 384, 576):
            for recouvrement in (0, 64, 128):
                départs = plan_tuiles(cote, tuile, recouvrement)
                assert départs[0] == 0
                assert all(0 <= d and d + min(tuile, cote) <= cote for d in départs)
                couverts = set()
                for départ in départs:
                    couverts.update(range(départ, min(départ + tuile, cote)))
                assert couverts == set(range(cote))


# --- rembourrage ------------------------------------------------------------------


def test_le_multiple_superieur_est_celui_que_metal_accepte():
    """512 est la taille native des chips que le dépôt d'amont publie, et c'est
    exactement celle que MPS refuse : le décodeur agrège vers 6 × 6 sur une carte
    réduite d'un facteur 64."""
    assert aligner_multiple(512, PAS_MPS) == 576
    assert aligner_multiple(576, PAS_MPS) == 576
    assert aligner_multiple(300, PAS_MPS) == 384
    assert aligner_multiple(1, PAS_MPS) == PAS_MPS


def test_l_encodeur_s_aligne_sur_le_patch_et_non_sur_le_decodeur():
    """La contrainte des 192 appartient au décodeur d'UperNet ; l'encodeur seul
    accepte tout multiple de 16, ce qui a été mesuré à 192, 224, 256, 384, 512,
    576 et 768."""
    assert tuile_alignee(512) == (512, None)
    aligné, note = tuile_alignee(500)
    assert aligné == 512
    assert note is not None and "moyenne" in note
    assert aligner_multiple(500, PATCH) == 512


def test_une_tuile_vide_est_refusee():
    with pytest.raises(WorkerError, match="au moins un pixel"):
        tuile_alignee(0)


# --- chargement des poids ---------------------------------------------------------


def test_le_prefixe_du_fine_tune_est_depouille():
    poids = {"model.encoder.cls_token": 1, "model.head.head.2.bias": 2}
    assert depouiller_prefixe(poids, "model.") == {"encoder.cls_token": 1, "head.head.2.bias": 2}


def test_le_decodeur_de_reconstruction_est_ecarte_de_l_encodeur():
    """Le point de contrôle pré-entraîné porte 402 clés, dont 106 appartiennent au
    décodeur de l'auto-encodeur masqué : l'empreinte n'en a que faire."""
    poids = {"encoder.pos_embed": 1, "encoder.blocks.0.attn.qkv.weight": 2, "decoder.mask_token": 3}
    retenus = depouiller_prefixe(poids, "encoder.")
    assert set(retenus) == {"pos_embed", "blocks.0.attn.qkv.weight"}


def test_un_prefixe_absent_est_un_refus_et_non_un_dictionnaire_vide():
    """LE piège de cette famille. Un dictionnaire vide chargé avec `strict=False`
    laisse le réseau à ses valeurs d'initialisation : il tourne, il rend une
    sortie de forme normale, et cette sortie est du bruit."""
    with pytest.raises(WorkerError, match="rend du bruit"):
        depouiller_prefixe({"backbone.cls_token": 1}, "model.")


def test_un_chargement_complet_passe_sans_bruit():
    exiger_chargement_complet(SimpleNamespace(missing_keys=[], unexpected_keys=[]), "essai")


def test_un_chargement_partiel_est_refuse_en_citant_les_deux_cotes():
    """Mesuré : sans dépouiller le préfixe, le fine-tune rend 368 manquants et 381
    inattendus. `strict=False` sert ici à **lire** le rapport, jamais à tolérer
    l'écart."""
    rapport = SimpleNamespace(
        missing_keys=[f"encoder.blocks.{i}.attn.qkv.weight" for i in range(368)],
        unexpected_keys=[f"model.encoder.blocks.{i}.attn.qkv.weight" for i in range(381)],
    )
    with pytest.raises(WorkerError) as échec:
        exiger_chargement_complet(rapport, "chargement d'essai")
    message = str(échec.value)
    assert "368" in message and "381" in message


# --- légende du manifeste ---------------------------------------------------------


def test_la_legende_vient_du_manifeste():
    assert noms_classes(["hors eau", "eau de crue"], 2) == ("hors eau", "eau de crue")


def test_une_legende_absente_ne_bloque_pas_le_job():
    """Une capacité rendue indisponible par une question d'affichage serait une
    capacité perdue pour rien."""
    assert noms_classes(None, 3) == ("classe 0", "classe 1", "classe 2")
    assert noms_classes(["eau"], 2) == ("eau", "classe 1")


def test_une_legende_plus_longue_que_le_modele_est_refusee():
    """Elle signale un manifeste écrit pour d'autres poids, et le contresens ne
    s'arrêterait pas au libellé."""
    with pytest.raises(WorkerError, match="d'autres poids"):
        noms_classes(["a", "b", "c"], 2)


def test_la_classe_dont_on_compte_la_couverture_est_declaree():
    assert classe_interet(1, 2) == 1
    assert classe_interet(None, 2) == 1
    assert classe_interet(0, 1) == 0


def test_une_classe_hors_du_modele_est_refusee():
    with pytest.raises(WorkerError, match="numérotées de 0 à 1"):
        classe_interet(2, 2)


# --- agrégation et vecteurs -------------------------------------------------------


def test_l_agregation_par_defaut_est_la_moyenne_des_patches():
    assert verifier_agregation(None) == "mean"
    assert verifier_agregation("CLS") == "cls"


def test_une_agregation_inconnue_est_refusee_en_nommant_le_champ():
    with pytest.raises(WorkerError, match="options.pooling"):
        verifier_agregation("max")


def test_les_jetons_de_tete_sont_deduits_et_non_supposes():
    """Le réseau porte un CLS aujourd'hui ; une famille voisine y ajouterait des
    registres, et une moyenne qui les mêlerait aux patches rendrait un vecteur qui
    n'est celui d'aucun espace."""
    assert jetons_de_tete(2305, 2304) == 1
    assert jetons_de_tete(2309, 2304) == 5
    assert jetons_de_tete(2304, 2304) == 0


def test_moins_de_jetons_que_de_patches_est_un_refus():
    with pytest.raises(WorkerError, match="l'empreinte de rien"):
        jetons_de_tete(100, 2304)


def test_l_empreinte_de_scene_est_la_moyenne_des_tuiles():
    """Moyenne et non concaténation : deux scènes de tailles différentes n'ont pas
    le même nombre de tuiles, et une empreinte dont la longueur dépendrait du
    découpage ne se comparerait à rien."""
    assert moyenne_vecteurs([[1.0, 0.0], [0.0, 1.0]]) == [0.5, 0.5]


def test_des_tuiles_de_longueurs_differentes_sont_refusees():
    with pytest.raises(WorkerError, match="longueurs différentes"):
        moyenne_vecteurs([[1.0, 0.0], [0.0]])


def test_une_scene_sans_tuile_est_refusee():
    with pytest.raises(WorkerError, match="scène est vide"):
        moyenne_vecteurs([])


def test_la_normalisation_ne_divise_jamais_par_zero():
    assert normaliser_l2([0.0, 0.0]) == [0.0, 0.0]
    assert normaliser_l2([3.0, 4.0]) == [0.6, 0.8]


def test_le_cosinus_est_invariant_d_echelle():
    """C'est ce qui permet à `normalize` de ne rien changer à la similarité : un
    job qui aurait rendu deux valeurs selon un réglage d'affichage aurait été
    incomparable avec lui-même."""
    assert cosinus([1.0, 0.0], [7.0, 0.0]) == 1.0
    assert cosinus([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosinus([1.0, 0.0], [0.0, 0.0]) is None


def test_deux_vecteurs_de_longueurs_differentes_ne_se_comparent_pas():
    with pytest.raises(WorkerError, match="même modèle"):
        cosinus([1.0, 0.0], [1.0, 0.0, 0.0])


# --- entrées et poids -------------------------------------------------------------


def test_un_chemin_relatif_est_resolu_dans_le_dossier_du_job(tmp_path: Path):
    """C'est ce qui rend un job rejouable ailleurs : le superviseur copie l'entrée
    à côté du manifeste et n'en transmet que le nom."""
    (tmp_path / "scene.tif").write_bytes(b"II*\x00")
    assert resolve_raster("scene.tif", tmp_path) == tmp_path / "scene.tif"


def test_une_image_est_refusee_avec_la_raison_qui_fait_exister_la_capacite(tmp_path: Path):
    """Un PNG ne peut pas porter le proche infrarouge, et c'est exactement ce qui
    sépare cette famille d'`image-segment`."""
    (tmp_path / "photo.png").write_bytes(b"\x89PNG")
    with pytest.raises(WorkerError, match="proche infrarouge"):
        resolve_raster("photo.png", tmp_path)


def test_un_raster_absent_nomme_le_champ_fautif(tmp_path: Path):
    with pytest.raises(WorkerError, match="compare_to introuvable"):
        resolve_raster("autre.tif", tmp_path, "compare_to")


def test_un_champ_vide_est_refuse(tmp_path: Path):
    with pytest.raises(WorkerError, match="le champ `raster` est vide"):
        resolve_raster(None, tmp_path)


def test_le_worker_ne_telecharge_jamais(tmp_path: Path):
    with pytest.raises(WorkerError, match="ne télécharge jamais"):
        weights_dir({"weights_path": str(tmp_path / "absent")})
    with pytest.raises(WorkerError, match="aucun chemin de poids"):
        weights_dir({})


def test_le_point_de_controle_est_unique_ou_nomme(tmp_path: Path):
    """Deux `.pt` dans le même dossier, c'est au manifeste de dire lequel — un
    adaptateur qui devinerait chargerait un jour l'autre."""
    with pytest.raises(WorkerError, match="allow_patterns"):
        unique_fichier(tmp_path, "*.pt", "point de contrôle")
    (tmp_path / "a.pt").write_bytes(b"")
    assert unique_fichier(tmp_path, "*.pt", "point de contrôle").name == "a.pt"
    (tmp_path / "b.pt").write_bytes(b"")
    with pytest.raises(WorkerError, match="2 fichiers"):
        unique_fichier(tmp_path, "*.pt", "point de contrôle")
