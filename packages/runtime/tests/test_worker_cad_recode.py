"""Le nuage vers CAO — ce qui se vérifie sans poids, sans torch et sans cadquery.

Une capacité, un runtime, et cinq pièces qui tiennent dans du code pur : le
câblage (runtime, capacité) → adaptateur, le refus de transformers ≥5,
l'extraction du programme depuis la sortie brute, la résolution des deux dépôts
de poids, et la forme du garde-fou d'exécution.

**Le refus de transformers ≥5 mérite d'être testé ici plutôt qu'au banc**, et
c'est le seul contrôle de version du parc qui refuse au lieu d'avertir. La raison
est mesurée : au-delà de 4.x, cette capacité ne tombe pas en panne — elle rend du
Python plausible et faux. Un banc au vert ne regarde pas ce qu'un fichier
contient ; ces tests-ci regardent, et celui-là garde la seule porte qui empêche
un programme CadQuery mensonger d'arriver jusqu'à un profil.

**L'extraction mérite le même soin.** Deux générations ratées produisent des
sorties dont la découpe *réussit* : celle qui a buté sur le plafond de jetons, et
celle qui est partie en boucle. Toutes deux ressemblent à un programme.

Ils tournent en CI, sur des machines sans Apple Silicon, sans poids et sans venv
de runtime : rien de ce fichier n'importe torch, transformers, trimesh ni
cadquery, pas plus que l'adaptateur lui-même au niveau de son module.
"""

from pathlib import Path

import pytest
from ecurie_runtime.envs import NOT_YET, worker_module
from ecurie_runtime.workers.base import WorkerError
from ecurie_runtime.workers.cad_recode import (
    CPU_S,
    DEBUT,
    DELAI_S,
    EXECUTEUR,
    FIN,
    JETONS_MAX,
    N_POINTS_ENTRAINEMENT,
    N_POINTS_MAX,
    VAR_VENDOR,
    CadRecodeWorker,
    cadrer,
    candidats_vendor,
    extraire_programme,
    plan_cao,
    plus_lointains,
    resolve_geometrie,
    tokenizer_dir,
    verifier_version_transformers,
    weights_dir,
)

PROGRAMME = (
    "import cadquery as cq\n"
    "w0=cq.Workplane('ZX',origin=(0,100,0))\n"
    "r=w0.workplane(offset=-200/2).box(200,200,200)"
)


def sortie_brute(programme: str = PROGRAMME, *, fin: bool = True) -> str:
    """Ce que `batch_decode` rend : le remplissage, l'ouverture, le programme, la fin."""
    return "<|im_end|>" * 256 + DEBUT + programme + (FIN if fin else "")


# --- câblage ----------------------------------------------------------------------


def test_la_capacite_est_servie_par_son_adaptateur():
    assert worker_module("cad-recode", "pointcloud-to-cad").endswith("cad_recode")


def test_le_runtime_cad_recode_n_a_pas_d_adaptateur_par_defaut():
    """Un runtime est une famille de bibliothèques, pas une promesse d'API commune."""
    assert worker_module("cad-recode", None) is None
    assert "pointcloud-to-cad" in NOT_YET["cad-recode"]


# --- le refus de transformers ≥5 --------------------------------------------------


def test_transformers_4_est_accepte():
    """La version installée et mesurée. Rien ne doit être levé."""
    verifier_version_transformers("4.57.6")


def test_transformers_5_est_refuse_en_nommant_ce_qui_arriverait():
    """LE risque numéro un de cette capacité. Mesuré sur les vrais poids : le
    chargement lève `all_tied_weights_keys`, et une fois rustiné la génération
    rend du Python plausible et FAUX. Il n'y a pas d'exception à attendre à
    l'exécution — d'où ce refus, qui est la seule barrière."""
    with pytest.raises(WorkerError) as échec:
        verifier_version_transformers("5.15.1")

    message = str(échec.value)
    assert "position_ids" in message and "cumsum" in message
    assert "< 5" in message


def test_une_version_illisible_est_refusee_plutot_qu_admise():
    """On ne sait pas, donc on refuse en le disant. Admettre par défaut ferait
    passer une préversion de la 5 exactement comme la 5."""
    with pytest.raises(WorkerError, match="illisible"):
        verifier_version_transformers("cinq")


# --- extraction du programme ------------------------------------------------------


def test_le_programme_est_extrait_entre_les_deux_balises():
    programme, avertissements = extraire_programme(sortie_brute())

    assert programme.startswith("import cadquery as cq")
    assert DEBUT not in programme and FIN not in programme
    assert avertissements == []


def test_une_generation_sans_jeton_de_fin_est_signalee_comme_tronquee():
    """La découpe **réussit** : sans `<|endoftext|>` on prend tout ce qui reste, et
    le résultat a toutes les apparences d'un programme. C'est l'avertissement, et
    lui seul, qui dit qu'il est coupé au milieu d'une ligne."""
    programme, avertissements = extraire_programme(sortie_brute(fin=False))

    assert programme.strip()
    assert any("plafond de jetons" in a for a in avertissements)


def test_un_second_jeton_d_ouverture_est_la_signature_de_la_boucle():
    """Exactement ce que produit transformers ≥5 : `import cadquery as
    cq\\nw0r<|im_start|>import cadquery…` en boucle. Le contrôle de version
    devrait l'avoir rendu impossible ; ce test-ci est la seconde barrière."""
    charabia = f"import cadquery as cq\nw0r{DEBUT}import cadquery as cq\nw0r"
    _, avertissements = extraire_programme(sortie_brute(charabia))

    assert any("boucle" in a for a in avertissements)


def test_une_sortie_sans_jeton_d_ouverture_est_refusee():
    with pytest.raises(WorkerError, match="ne contient pas"):
        extraire_programme("import cadquery as cq\n")


def test_un_programme_vide_entre_les_balises_est_refuse():
    with pytest.raises(WorkerError, match="aucun programme"):
        extraire_programme(sortie_brute("   \n  "))


def test_un_programme_qui_ne_parle_pas_de_cadquery_est_signale():
    """L'exécuter ferait tourner du code dont on ne sait rien, et le contrat ne
    décrit que du CadQuery."""
    _, avertissements = extraire_programme(sortie_brute("import os\nr = os.listdir('/')"))

    assert any("cadquery" in a for a in avertissements)


# --- résolution de la demande -----------------------------------------------------


def test_l_entree_du_job_prime_sur_les_defauts_du_manifeste():
    plan = plan_cao(
        entree={"max_new_tokens": 512},
        params={},
        defaults={"max_new_tokens": 768, "n_points": 256, "seed": 7},
    )

    assert (plan.max_new_tokens, plan.n_points, plan.seed) == (512, 256, 7)


def test_l_execution_est_fausse_par_defaut():
    """La décision de sécurité de cette capacité. Ce code n'a été relu par
    personne, et le défaut prudent est de ne pas l'exécuter — le programme reste
    rendu, et c'est là qu'est la valeur."""
    assert plan_cao(entree={}, params={}, defaults={}).executer is False


def test_un_n_points_autre_que_la_valeur_d_entrainement_est_signale():
    """Le seul réglage de cette capacité dont une valeur **légale** dégrade la
    sortie en silence : le programme reste syntaxiquement valide."""
    plan = plan_cao(entree={"n_points": 128}, params={}, defaults={})

    assert plan.n_points == 128
    assert any("hors distribution" in a for a in plan.warnings)


def test_la_valeur_d_entrainement_ne_produit_aucun_avertissement():
    plan = plan_cao(entree={"n_points": N_POINTS_ENTRAINEMENT}, params={}, defaults={})

    assert plan.warnings == ()


def test_un_parametre_hors_des_bornes_du_contrat_est_refuse():
    with pytest.raises(WorkerError, match=str(N_POINTS_MAX)):
        plan_cao(entree={"n_points": N_POINTS_MAX + 1}, params={}, defaults={})
    with pytest.raises(WorkerError, match=str(JETONS_MAX)):
        plan_cao(entree={"max_new_tokens": JETONS_MAX + 1}, params={}, defaults={})


def test_un_booleen_n_est_pas_un_entier():
    """`True` vaut 1 pour `int()`, et `n_points: true` deviendrait `n_points: 1`
    sans que rien ne le dise."""
    with pytest.raises(WorkerError, match="booléen"):
        plan_cao(entree={"n_points": True}, params={}, defaults={})


# --- poids, tokenizer, entrées ----------------------------------------------------


def test_un_chemin_de_poids_absent_est_refuse_avec_ce_qui_repare():
    with pytest.raises(WorkerError, match="superviseur"):
        weights_dir({})


def test_un_dossier_de_poids_inexistant_dit_qu_un_worker_ne_telecharge_pas(tmp_path: Path):
    with pytest.raises(WorkerError) as échec:
        weights_dir({"weights_path": str(tmp_path / "absent")})

    assert "ne télécharge jamais" in str(échec.value)


def test_un_tokenizer_absent_renvoie_vers_extra_sources():
    """Le dépôt des poids de CAD-Recode n'en contient aucun : c'est un second
    dépôt qu'il faut déclarer, pas un fichier à aller chercher."""
    with pytest.raises(WorkerError) as échec:
        tokenizer_dir({"weights_path": "/x"})

    message = str(échec.value)
    assert "extra_sources" in message and "role: tokenizer" in message


def test_un_dossier_de_tokenizer_sans_tokenizer_json_nomme_les_allow_patterns(tmp_path: Path):
    with pytest.raises(WorkerError, match="allow_patterns"):
        tokenizer_dir({"extra_paths": {"tokenizer": str(tmp_path)}})


def test_une_geometrie_relative_se_resout_dans_le_dossier_du_job(tmp_path: Path):
    """Le superviseur copie l'entrée dans le dossier du job et transmet un chemin
    relatif : c'est ce qui rend un job rejouable ailleurs."""
    (tmp_path / "inputs").mkdir()
    fichier = tmp_path / "inputs" / "piece.ply"
    fichier.write_bytes(b"")

    assert resolve_geometrie("inputs/piece.ply", tmp_path) == fichier


def test_un_champ_de_geometrie_vide_nomme_le_champ():
    with pytest.raises(WorkerError, match="geometrie"):
        resolve_geometrie("", Path("."))


def test_un_format_non_gere_liste_ceux_qui_le_sont(tmp_path: Path):
    fichier = tmp_path / "piece.step"
    fichier.write_bytes(b"")

    with pytest.raises(WorkerError) as échec:
        resolve_geometrie(str(fichier), tmp_path)

    message = str(échec.value)
    assert ".ply" in message and ".glb" in message


# --- le code vendoré --------------------------------------------------------------


def test_la_variable_d_environnement_prime_sur_toute_recherche():
    assert candidats_vendor(Path("/a/b"), Path("/c"), "/ailleurs") == [Path("/ailleurs")]


def test_le_vendor_se_trouve_en_remontant_jusqu_a_runtimes(tmp_path: Path):
    """Et non par un `parents[n]` : le premier lancement a échoué exactement
    là-dessus, sur un comptage qui désignait `packages/` au lieu de la racine."""
    racine = tmp_path / "depot"
    (racine / "runtimes" / "cad-recode").mkdir(parents=True)
    profond = racine / "packages" / "runtime" / "src" / "w" / "cad_recode.py"
    profond.parent.mkdir(parents=True)

    assert candidats_vendor(profond, tmp_path)[0] == racine / "runtimes" / "cad-recode" / "vendor"


def test_un_vendor_absent_dit_ou_il_a_cherche_et_donne_les_commandes(tmp_path: Path, monkeypatch):
    """Le code d'inférence est sous CC BY-NC 4.0 et ne peut pas être versionné
    ici : le message doit porter les deux commandes, faute de quoi on les
    découvre après trois gigaoctets de téléchargement."""
    from ecurie_runtime.workers import cad_recode

    monkeypatch.setenv(VAR_VENDOR, str(tmp_path / "nulle-part"))
    with pytest.raises(WorkerError) as échec:
        cad_recode.vendor_dir()

    message = str(échec.value)
    assert "git clone" in message and "vendorer.py" in message
    assert "CC BY-NC" in message


# --- le garde-fou d'exécution -----------------------------------------------------


def test_l_executeur_est_du_python_valide():
    """Il est transporté comme une chaîne et écrit dans le dossier du job : une
    faute de frappe ne se verrait qu'au premier job qui exécute, c'est-à-dire
    après le chargement de trois gigaoctets."""
    compile(EXECUTEUR, "executeur.py", "exec")


def test_l_executeur_annonce_ce_qu_il_ne_borne_pas():
    """Mesuré : macOS refuse `RLIMIT_AS`, `RLIMIT_DATA` et `RLIMIT_RSS` quelle que
    soit la valeur demandée. Un garde-fou dont on croit à tort qu'il tient est
    pire que pas de garde-fou du tout — le fichier le dit donc en clair."""
    assert "RLIMIT_AS" in EXECUTEUR and "refusées par le noyau" in EXECUTEUR


def test_la_borne_processeur_mord_avant_le_delai_d_horloge():
    """Aux deux à la même valeur, la boucle infinie du jeu d'épreuve était
    toujours coupée par le délai du parent et jamais par SIGXCPU : les deux
    arrivaient ensemble et le parent gagnait. Chacune doit avoir son rôle."""
    assert CPU_S < DELAI_S


def test_la_commande_d_execution_isole_l_interpreteur(tmp_path: Path, monkeypatch):
    """`-I` coupe PYTHONPATH et le site utilisateur, l'environnement est vidé, et
    le dossier de travail est celui du job. Vérifié sur la commande construite :
    ces quatre points ne se relisent nulle part ailleurs."""
    from ecurie_runtime.workers import cad_recode

    vues: dict = {}

    def faux_run(commande, **kw):
        vues["commande"], vues["kw"] = commande, kw
        raise TimeoutErreur

    class TimeoutErreur(Exception):
        pass

    monkeypatch.setattr(cad_recode.subprocess, "TimeoutExpired", TimeoutErreur)
    monkeypatch.setattr(cad_recode.subprocess, "run", faux_run)

    programme = tmp_path / "programme.py"
    programme.write_text("r = 1\n")
    rapport = CadRecodeWorker()._executer(programme, tmp_path)

    assert vues["commande"][1] == "-I"
    assert vues["kw"]["cwd"] == str(tmp_path)
    assert set(vues["kw"]["env"]) == {"PATH", "HOME"}
    assert vues["kw"]["timeout"] == DELAI_S
    assert rapport["execution"] == "delai-depasse"


def test_un_echec_d_execution_est_une_valeur_et_non_une_exception(tmp_path: Path, monkeypatch):
    """Le programme reste utile même quand il ne compile pas : c'est tout ce que
    cette capacité promet de rendre."""
    from ecurie_runtime.workers import cad_recode

    def faux_run(commande, **kw):
        raise OSError("interpréteur introuvable")

    monkeypatch.setattr(cad_recode.subprocess, "run", faux_run)
    programme = tmp_path / "programme.py"
    programme.write_text("r = 1\n")

    rapport = CadRecodeWorker()._executer(programme, tmp_path)

    assert rapport["execution"] == "erreur"
    assert "interpréteur introuvable" in rapport["erreur"]


# --- géométrie ---------------------------------------------------------------------
#
# numpy n'est pas dans l'env racine d'Écurie : ces deux-là se sautent en CI plutôt
# que d'y faire entrer une dépendance pour deux fonctions. Elles restent
# vérifiées partout où numpy est là, y compris dans le venv du runtime.


def test_le_cadrage_ramene_dans_un_cube_de_cote_2():
    np = pytest.importorskip("numpy")
    points = np.array([[0.0, 0.0, 0.0], [120.0, 100.0, 50.0], [40.0, 40.0, 25.0]])

    cadré = cadrer(points, np)

    assert cadré.min() == pytest.approx(-1.0)
    assert cadré.max() == pytest.approx(1.0)
    # Les proportions survivent : c'est tout ce que cette capacité conserve de la
    # pièce, et le manifeste le dit — l'échelle, elle, est effacée.
    étendues = cadré.max(axis=0) - cadré.min(axis=0)
    assert étendues[1] / étendues[0] == pytest.approx(100 / 120)


def test_une_geometrie_plate_ne_divise_pas_par_zero():
    """Un plan ou un segment a une étendue nulle sur un axe. Mieux vaut un nuage
    hors cadrage, que le modèle traitera mal mais visiblement, qu'une coordonnée
    infinie qui traverserait tout jusqu'à la génération."""
    np = pytest.importorskip("numpy")
    points = np.zeros((4, 3))

    assert np.isfinite(cadrer(points, np)).all()


def test_l_echantillonnage_part_de_l_indice_zero_et_prend_les_extremes():
    """Départ fixé comme l'amont, dont le défaut `random_start_point=False` a été
    relu : un départ tiré au sort rendrait la charge type irreproductible."""
    np = pytest.importorskip("numpy")
    points = np.array([[0.0, 0, 0], [0.1, 0, 0], [10.0, 0, 0], [5.0, 0, 0]])

    indices = plus_lointains(points, 3, np)

    assert indices[0] == 0
    assert indices[1] == 2  # le plus lointain du premier
    assert set(int(i) for i in indices) == {0, 2, 3}


def test_demander_plus_de_points_qu_il_n_y_en_a_les_rend_tous():
    np = pytest.importorskip("numpy")
    points = np.zeros((5, 3))

    assert list(plus_lointains(points, 12, np)) == [0, 1, 2, 3, 4]


def test_ce_que_le_programme_laisse_dans_le_job_est_dit(tmp_path: Path, monkeypatch):
    """Le dossier de travail du sous-processus est celui du job, et c'est
    délibéré : un programme qui écrit `sortie.stl` doit le déposer là où on peut
    le voir. Mais confiner sans regarder ce qu'on a confiné ne vaut pas
    grand-chose — un fichier apparu dans le dossier d'un job est une information,
    et elle se dit."""
    from ecurie_runtime.workers import cad_recode

    def faux_run(commande, **kw):
        (tmp_path / ".cache").mkdir()
        (tmp_path / "surprise.stl").write_bytes(b"")
        raise OSError("peu importe")

    monkeypatch.setattr(cad_recode.subprocess, "run", faux_run)
    programme = tmp_path / "programme.py"
    programme.write_text("r = 1\n")

    rapport = CadRecodeWorker()._executer(programme, tmp_path)

    trace = [a for a in rapport["warnings"] if "hors de ses sorties" in a]
    assert trace and ".cache" in trace[0] and "surprise.stl" in trace[0]


def test_les_sorties_declarees_ne_sont_pas_signalees_comme_des_traces(tmp_path: Path):
    """`piece.step`, `piece.glb`, `execution.json` et `executeur.py` sont
    attendus : les compter comme des traces noierait le vrai signal."""
    from ecurie_runtime.workers.cad_recode import DEPOSES, _traces

    assert _traces(set(), set(DEPOSES)) == []
