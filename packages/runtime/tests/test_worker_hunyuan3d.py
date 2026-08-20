"""L'entrypoint Hunyuan3D, éprouvé sur une machine sans torch et sans poids.

Ces tests tournent en CI, donc sur du matériel qui n'a ni Apple Silicon, ni MPS, ni
les sept gigaoctets du checkpoint. Ils ne vérifient donc pas que le modèle produit
un maillage — c'est le rôle d'`ecurie bench` sur la vraie machine — mais les trois
choses qui cassent silencieusement autrement : que le module s'importe sans traîner
torch derrière lui, que l'étape de vendoring absente se dise avec sa commande de
réparation, et que la fusion des réglages d'un job fasse ce qu'annonce le contrat.
"""

import ast
import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest
from ecurie_runtime.workers.base import InferRequest

RACINE_DEPOT = Path(__file__).resolve().parents[3]
DOSSIER_RUNTIME = RACINE_DEPOT / "runtimes" / "hunyuan3d"
RUN_PY = DOSSIER_RUNTIME / "run.py"


@pytest.fixture(scope="module")
def run():
    """Le module chargé depuis son chemin — il n'est sur le PYTHONPATH d'aucun test.

    C'est exactement ce que fait le superviseur pour un `runtime: custom`, à ceci
    près qu'il le lance comme un script dans un autre venv.
    """
    spec = importlib.util.spec_from_file_location("essai_hunyuan3d_run", RUN_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture
def sans_reglages_locaux(monkeypatch):
    """Aucune des variables de mise au point ne doit teinter un test."""
    for nom in (
        "ECURIE_HY3DSHAPE_PATH",
        "ECURIE_HY3D_DEVICE",
        "ECURIE_HY3D_GARDER_SDP_KERNEL",
    ):
        monkeypatch.delenv(nom, raising=False)


# --- imports paresseux -------------------------------------------------------


def test_aucune_dependance_lourde_au_niveau_du_module():
    """Le module ne doit importer que la bibliothèque standard et `ecurie_runtime`.

    Vérifié sur l'arbre syntaxique plutôt que sur `sys.modules` : un import lourd
    ajouté sous un `try` au niveau du module passerait le second contrôle sur une
    machine qui n'a pas la bibliothèque, et casserait la CI le jour où elle l'a.
    """
    arbre = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    racines: set[str] = set()
    for nœud in arbre.body:
        if isinstance(nœud, ast.Import):
            racines |= {alias.name.split(".")[0] for alias in nœud.names}
        elif isinstance(nœud, ast.ImportFrom) and nœud.level == 0 and nœud.module:
            racines.add(nœud.module.split(".")[0])
    assert {r for r in racines if r not in sys.stdlib_module_names} == {"ecurie_runtime"}


def test_import_reussit_sans_torch(run):
    assert run.Hunyuan3DWorker.name == "hunyuan3d"
    assert run.Hunyuan3DWorker().torch is None
    assert "torch" not in sys.modules


# --- vendoring ---------------------------------------------------------------


def test_vendoring_absent_detecte(run, tmp_path, sans_reglages_locaux):
    assert run.trouver_hy3dshape(tmp_path) is None


def test_vendoring_present_detecte(run, tmp_path, sans_reglages_locaux):
    paquet = tmp_path / "vendor" / "Hunyuan3D-2.1" / "hy3dshape" / "hy3dshape"
    paquet.mkdir(parents=True)
    (paquet / "pipelines.py").write_text("", encoding="utf-8")
    assert run.trouver_hy3dshape(tmp_path) == paquet.parent


def test_sparse_checkout_interrompu_ne_compte_pas(run, tmp_path, sans_reglages_locaux):
    """Un dossier vide n'est pas un vendoring : il échouerait beaucoup plus loin."""
    (tmp_path / "vendor" / "Hunyuan3D-2.1" / "hy3dshape" / "hy3dshape").mkdir(parents=True)
    assert run.trouver_hy3dshape(tmp_path) is None


def test_message_de_vendoring_nomme_la_commande(run, tmp_path):
    message = run.message_vendoring(tmp_path)
    assert "git clone" in message
    assert "sparse-checkout set hy3dshape/hy3dshape" in message
    assert "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1" in message
    assert "PyPI" in message  # la raison, pas seulement le remède


def test_load_signale_le_vendoring_avant_torch(run, tmp_path, sans_reglages_locaux):
    """Le vendoring est vérifié en premier : c'est la seule étape qu'`env sync` ne fait pas.

    Sur une machine sans torch, l'ordre inverse donnerait un ImportError de torch et
    on irait resynchroniser un environnement qui n'a rien à se reprocher.
    """
    worker = run.Hunyuan3DWorker(racine=tmp_path)
    with pytest.raises(run.WorkerError) as erreur:
        worker.load({"weights_path": str(tmp_path)})
    assert "sparse-checkout" in str(erreur.value)


# --- localisation des poids --------------------------------------------------


def _poser_poids(dossier: Path, nom: str = "model.fp16.ckpt") -> Path:
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / "config.yaml").write_text("", encoding="utf-8")
    (dossier / nom).write_bytes(b"")
    return dossier


def test_poids_designes_par_la_racine_du_depot(run, tmp_path):
    dit = _poser_poids(tmp_path / "Hunyuan3D-2.1" / "hunyuan3d-dit-v2-1")
    poids = run.localiser_poids(tmp_path / "Hunyuan3D-2.1")
    assert poids.dossier == dit
    assert (poids.fichier, poids.variante, poids.safetensors) == ("model.fp16.ckpt", "fp16", False)


def test_poids_designes_directement(run, tmp_path):
    dit = _poser_poids(tmp_path / "hunyuan3d-dit-v2-1", nom="model.safetensors")
    poids = run.localiser_poids(dit)
    assert poids.dossier == dit
    assert (poids.variante, poids.safetensors) == (None, True)


def test_poids_absents_renvoient_a_ecurie_pull(run, tmp_path):
    with pytest.raises(run.WorkerError, match="ecurie pull"):
        run.localiser_poids(tmp_path)


def test_variant_sans_weights_path(run):
    with pytest.raises(run.WorkerError, match="weights_path"):
        run.localiser_poids(None)


# --- préparation des arguments -----------------------------------------------


def requete(dossier: Path, *, graine=None, params=None, **entree) -> InferRequest:
    """`graine` est la graine du protocole ; `seed=` en mot-clé va dans l'entrée du job."""
    (dossier / "entree.png").write_bytes(b"")
    entree.setdefault("image", "entree.png")
    return InferRequest(
        job_id="j1",
        input=entree,
        params=params or {},
        output_dir=dossier,
        seed=graine,
    )


def test_defauts_du_contrat(run, tmp_path):
    args = run.preparer_arguments(requete(tmp_path), {})
    assert (args.octree_resolution, args.num_inference_steps) == (256, 30)
    assert (args.guidance_scale, args.num_chunks) == (5.0, 8000)
    assert args.seed is None
    assert args.sortie == tmp_path / "mesh.glb"


def test_defauts_du_manifeste_priment_sur_le_contrat(run, tmp_path):
    args = run.preparer_arguments(
        requete(tmp_path), {"octree_resolution": 384, "num_inference_steps": 50}
    )
    assert (args.octree_resolution, args.num_inference_steps) == (384, 50)


def test_entree_du_job_prime_sur_le_manifeste(run, tmp_path):
    args = run.preparer_arguments(
        requete(tmp_path, octree_resolution=512), {"octree_resolution": 384}
    )
    assert args.octree_resolution == 512


def test_params_du_variant_priment_sur_le_manifeste(run, tmp_path):
    args = run.preparer_arguments(
        requete(tmp_path, params={"num_inference_steps": 12}), {"num_inference_steps": 50}
    )
    assert args.num_inference_steps == 12


def test_entiers_transmis_en_chaine(run, tmp_path):
    """Une valeur venant de `-p k=v` arrive en texte : elle ne doit pas faire échouer un job."""
    args = run.preparer_arguments(requete(tmp_path, octree_resolution="384"), {})
    assert args.octree_resolution == 384


def test_chemin_image_relatif_au_dossier_du_job(run, tmp_path):
    args = run.preparer_arguments(requete(tmp_path), {})
    assert args.image == (tmp_path / "entree.png").resolve()


def test_chemin_image_absolu_conserve(run, tmp_path):
    ailleurs = tmp_path / "ailleurs"
    ailleurs.mkdir()
    source = ailleurs / "photo.png"
    source.write_bytes(b"")
    args = run.preparer_arguments(requete(tmp_path, image=str(source)), {})
    assert args.image == source.resolve()


def test_image_absente_nomme_le_chemin(run, tmp_path):
    requête = requete(tmp_path, image="perdue.png")
    with pytest.raises(run.WorkerError) as erreur:
        run.preparer_arguments(requête, {})
    assert str(tmp_path / "perdue.png") in str(erreur.value)


def test_image_manquante_dans_l_entree(run, tmp_path):
    requête = InferRequest(job_id="j1", input={}, params={}, output_dir=tmp_path)
    with pytest.raises(run.WorkerError, match="image-to-mesh"):
        run.preparer_arguments(requête, {})


def test_octree_hors_enum_liste_les_valeurs_admises(run, tmp_path):
    with pytest.raises(run.WorkerError) as erreur:
        run.preparer_arguments(requete(tmp_path, octree_resolution=300), {})
    assert "128, 256, 384, 512" in str(erreur.value)


def test_steps_hors_bornes(run, tmp_path):
    with pytest.raises(run.WorkerError, match=r"\[10, 100\]"):
        run.preparer_arguments(requete(tmp_path, num_inference_steps=5), {})


def test_valeur_non_entiere_refusee(run, tmp_path):
    with pytest.raises(run.WorkerError, match="octree_resolution"):
        run.preparer_arguments(requete(tmp_path, octree_resolution="beaucoup"), {})


def test_graine_du_protocole_prime_sur_celle_de_l_entree(run, tmp_path):
    """C'est la graine du protocole que le superviseur écrit au manifeste du run."""
    args = run.preparer_arguments(requete(tmp_path, graine=7, seed=3), {})
    assert args.seed == 7


def test_graine_de_l_entree_utilisee_a_defaut(run, tmp_path):
    args = run.preparer_arguments(requete(tmp_path, seed=3), {})
    assert args.seed == 3


# --- environnement isolé -----------------------------------------------------


def test_pyproject_ecarte_les_dependances_cuda():
    """Le jour où une dépendance CUDA se glisse ici, `ecurie env sync` casse sur Mac."""
    données = tomllib.loads((DOSSIER_RUNTIME / "pyproject.toml").read_text(encoding="utf-8"))
    dépendances = " ".join(données["project"]["dependencies"]).lower()
    for interdit in (
        "cupy",
        "diso",
        "sageattention",
        "custom_rasterizer",
        "bpy",
        "deepspeed",
        "pytorch-lightning",
        "xatlas",
        "open3d",
        "realesrgan",
    ):
        assert interdit not in dépendances, f"{interdit} n'a rien à faire dans cet env"


def test_pyproject_ne_pretend_pas_installer_hy3dshape():
    """`hy3dshape` n'est pas sur PyPI : l'y déclarer donnerait un `uv sync` en échec."""
    données = tomllib.loads((DOSSIER_RUNTIME / "pyproject.toml").read_text(encoding="utf-8"))
    dépendances = " ".join(données["project"]["dependencies"]).lower()
    assert "hy3dshape" not in dépendances
    # L'extracteur de surface par défaut en dépend, et c'est le seul utilisable ici.
    assert "scikit-image" in dépendances
    assert "trimesh" in dépendances


def test_readme_documente_le_vendoring_la_synchro_et_la_licence():
    texte = (DOSSIER_RUNTIME / "README.md").read_text(encoding="utf-8")
    assert "ecurie env sync hunyuan3d" in texte
    assert "sparse-checkout" in texte
    assert "tencent-hunyuan-community" in texte
