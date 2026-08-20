"""Adaptateur diffusers/MPS — ce qui se vérifie sans Apple Silicon ni torch.

Ces tests tournent dans le venv d'Écurie, qui n'a ni torch ni diffusers : c'est
exactement la situation de la CI, et c'est ce qui leur donne leur valeur. Ils
couvrent les trois choses qui cassent en silence — un import remonté au niveau du
module, une dérive entre les défauts du worker et le contrat de capacité, et un job
sans graine donc non rejouable.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from ecurie_runtime.workers.base import InferRequest, WorkerError
from ecurie_runtime.workers.diffusers_mps import (
    CONTRACT_DEFAULTS,
    ENV_NAME,
    IMAGE_NAME,
    DiffusersMpsWorker,
    Generation,
    detect_variant,
    draw_seed,
    plan_generation,
    step_progress,
    torch_dtype_name,
)

REPO_ROOT = Path(__file__).parents[3]
CONTRAT = REPO_ROOT / "registry" / "capabilities" / "text-to-image.json"

TORCH_PRÉSENT = importlib.util.find_spec("torch") is not None


def demande(**champs) -> InferRequest:
    """Une requête d'inférence telle que le superviseur la transmet."""
    return InferRequest(
        job_id="j1",
        input=champs.pop("input", {"prompt": "un cheval"}),
        params=champs.pop("params", {}),
        output_dir=champs.pop("output_dir", Path(".")),
        seed=champs.pop("seed", None),
    )


# --- imports paresseux -------------------------------------------------------


def test_module_importable_sans_torch():
    """L'invariant qui rend la CI possible : rien du runtime au niveau du module.

    Le sous-processus est lancé sans le venv du runtime et avec un `sys.modules`
    neuf ; importer torch au chargement du module ferait échouer cette commande.
    """
    code = (
        "import sys, ecurie_runtime.workers.diffusers_mps as m;"
        "print(m.ENV_NAME, 'torch' in sys.modules, 'diffusers' in sys.modules)"
    )
    résultat = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert résultat.returncode == 0, résultat.stderr
    assert résultat.stdout.split() == [ENV_NAME, "False", "False"]


@pytest.mark.skipif(TORCH_PRÉSENT, reason="torch installé : le chemin d'échec ne s'y produit pas")
def test_load_sans_diffusers_nomme_la_reparation():
    with pytest.raises(WorkerError) as échec:
        DiffusersMpsWorker().load({"weights_path": "/inexistant"})
    assert f"ecurie env sync {ENV_NAME}" in str(échec.value)


# --- fidélité au contrat de capacité -----------------------------------------


def test_defauts_du_worker_alignes_sur_le_contrat():
    """La duplication des défauts est assumée ; sa dérive ne l'est pas.

    Le worker ne peut pas lire le registre (il n'a qu'ecurie_runtime.*), donc les
    défauts du contrat sont recopiés dans le module. Ce test est la seule chose qui
    empêche les deux fichiers de diverger sans que personne ne s'en aperçoive.
    """
    contrat = json.loads(CONTRAT.read_text())["input"]["properties"]
    attendus = {clé: champ["default"] for clé, champ in contrat.items() if "default" in champ}
    assert CONTRACT_DEFAULTS == attendus


def test_sortie_declaree_par_le_contrat():
    contrat = json.loads(CONTRAT.read_text())["output"]
    assert contrat["required"] == ["image"]
    assert IMAGE_NAME.endswith(".png")


# --- résolution des paramètres -----------------------------------------------


def test_plan_prend_les_defauts_du_contrat():
    plan = plan_generation(demande(seed=7))
    assert (plan.width, plan.height) == (CONTRACT_DEFAULTS["width"], CONTRACT_DEFAULTS["height"])
    assert plan.steps == CONTRACT_DEFAULTS["steps"]
    assert plan.guidance_scale == CONTRACT_DEFAULTS["guidance_scale"]
    assert plan.negative_prompt is None


def test_le_variant_surclasse_le_contrat():
    plan = plan_generation(demande(seed=7), {"steps": 8, "guidance_scale": 1.0, "width": 768})
    assert (plan.steps, plan.guidance_scale, plan.width) == (8, 1.0, 768)
    assert plan.height == CONTRACT_DEFAULTS["height"]  # non redéfini par le variant


def test_le_job_surclasse_le_variant():
    requête = demande(input={"prompt": "p", "steps": 40}, params={"guidance_scale": 6.0}, seed=7)
    plan = plan_generation(requête, {"steps": 8, "guidance_scale": 1.0})
    assert plan.steps == 40
    assert plan.guidance_scale == 6.0


def test_negative_prompt_vide_devient_absent():
    """Une chaîne vide n'est pas un prompt négatif : elle ferait travailler l'encodeur
    de texte pour rien, et se lirait comme une contrainte dans le manifeste du job."""
    plan = plan_generation(demande(input={"prompt": "p", "negative_prompt": "   "}, seed=1))
    assert plan.negative_prompt is None


def test_prompt_vide_refuse():
    with pytest.raises(WorkerError, match="prompt vide"):
        plan_generation(demande(input={"prompt": "   "}))


def test_dimension_hors_multiple_de_huit_refusee():
    with pytest.raises(WorkerError, match="multiple de 8"):
        plan_generation(demande(input={"prompt": "p", "width": 1020}, seed=1))


def test_steps_non_entier_refuse():
    with pytest.raises(WorkerError, match="steps"):
        plan_generation(demande(input={"prompt": "p", "steps": "beaucoup"}, seed=1))


# --- graine ------------------------------------------------------------------


def test_graine_tiree_quand_absente():
    """Un job sans graine reste rejouable : le worker en tire une et la retourne."""
    plan = plan_generation(demande(), seed_source=lambda: 4242)
    assert plan.seed == 4242
    assert plan.as_metrics()["seed"] == 4242


def test_graine_du_protocole_prime_sur_celle_de_l_entree():
    """Un rejeu impose la graine par le champ de protocole ; le formulaire d'origine
    ne doit pas la reprendre au passage."""
    plan = plan_generation(demande(input={"prompt": "p", "seed": 1}, seed=99))
    assert plan.seed == 99


def test_graine_du_variant_utilisee_a_defaut():
    plan = plan_generation(demande(), {"seed": 123})
    assert plan.seed == 123


def test_graine_tiree_dans_les_bornes_du_contrat():
    contrat = json.loads(CONTRAT.read_text())["input"]["properties"]["seed"]
    graines = {draw_seed() for _ in range(50)}
    assert len(graines) > 1  # sinon ce n'est pas un tirage
    assert all(g >= contrat["minimum"] for g in graines)


def test_graine_negative_refusee():
    with pytest.raises(WorkerError, match="négative"):
        plan_generation(demande(seed=-1))


# --- dtype -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quantization", "attendu"),
    [(None, "float16"), ("none", "float16"), ("fp16", "float16"), ("bf16", "bfloat16"),
     ("fp32", "float32")],
)
def test_dtype_par_quantization(quantization, attendu):
    assert torch_dtype_name(quantization) == attendu


def test_quantization_non_supportee_le_dit():
    """La quantification à la volée n'est pas câblée : le worker le dit, il ne charge
    pas un fp16 en faisant semblant d'avoir compris le manifeste."""
    with pytest.raises(WorkerError, match="4bit"):
        torch_dtype_name("4bit")


# --- progression -------------------------------------------------------------


def test_progression_croissante_et_bornee():
    valeurs = [step_progress(pas, 25) for pas in range(1, 26)]
    assert valeurs == sorted(valeurs)
    assert 0 < valeurs[0] < valeurs[-1] < 92  # 92 est réservé à l'encodage PNG


def test_progression_supporte_un_pas_unique():
    assert 0 < step_progress(1, 1) < 92


# --- forme de la sortie ------------------------------------------------------


def test_kwargs_du_pipeline_portent_les_noms_de_diffusers():
    """Les noms sont ceux de `__call__`, pas ceux du contrat : c'est ici que la
    traduction a lieu, et un renommage silencieux ferait générer un 512×512 par défaut."""
    plan = Generation("p", None, 768, 512, 20, 3.0, 7)
    assert plan.pipeline_kwargs() == {
        "prompt": "p",
        "negative_prompt": None,
        "width": 768,
        "height": 512,
        "num_inference_steps": 20,
        "guidance_scale": 3.0,
    }


def test_metriques_portent_de_quoi_rejouer():
    métriques = Generation("p", "flou", 1024, 1024, 25, 3.5, 7).as_metrics()
    assert métriques["seed"] == 7
    assert set(métriques) >= {"seed", "steps", "guidance_scale", "width", "height"}


# --- variante de poids sur le disque -------------------------------------------------


def _poser(dossier, *noms):
    for nom in noms:
        cible = dossier / nom
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_bytes(b"")
    return dossier


def test_variante_detectee_quand_seuls_les_fichiers_fp16_sont_la(tmp_path):
    """Le cas rencontré au premier chargement réel de SDXL.

    Les `allow_patterns` du manifeste ne prennent que la variante légère — c'est
    tout l'intérêt sur une machine à 24 Go, 6,6 Gio au lieu de 71. Sans le kwarg
    `variant`, `from_pretrained` cherche le fichier sans suffixe et déclare le
    dépôt incomplet.
    """
    dossier = _poser(
        tmp_path,
        "unet/diffusion_pytorch_model.fp16.safetensors",
        "vae/diffusion_pytorch_model.fp16.safetensors",
        "model_index.json",
    )
    assert detect_variant(dossier) == "fp16"


def test_aucune_variante_quand_les_poids_nus_sont_presents(tmp_path):
    """Les deux précisions côte à côte : `from_pretrained` sait déjà choisir, et
    lui imposer une variante l'empêcherait de prendre ce que le dtype demande."""
    dossier = _poser(
        tmp_path,
        "unet/diffusion_pytorch_model.safetensors",
        "unet/diffusion_pytorch_model.fp16.safetensors",
    )
    assert detect_variant(dossier) is None


def test_aucune_variante_sur_un_depot_sans_suffixe(tmp_path):
    dossier = _poser(tmp_path, "unet/diffusion_pytorch_model.safetensors")
    assert detect_variant(dossier) is None


def test_variante_bf16_reconnue(tmp_path):
    dossier = _poser(tmp_path, "transformer/diffusion_pytorch_model.bf16.safetensors")
    assert detect_variant(dossier) == "bf16"


# --- pic mémoire ---------------------------------------------------------------------


class _MpsFactice:
    """Compteurs MPS d'un torch de doublure. Le driver monte, puis redescend."""

    def __init__(self, suite):
        self._suite = list(suite)

    def current_allocated_memory(self):
        return 1
    def driver_allocated_memory(self):
        return self._suite.pop(0) if self._suite else 0
    def recommended_max_memory(self):
        return 17 * 2**30


def test_le_pic_retient_le_plus_haut_driver_vu(monkeypatch):
    """Mesuré le 20 août 2026 : le RSS plafonnait à 0,42 Gio pendant que le driver
    Metal en réservait 15,95. Un profil écrit sur le RSS aurait laissé cohabiter
    un modèle image et un modèle de huit gigaoctets — l'OOM que le contrôle
    d'admission existe pour empêcher.
    """
    from ecurie_runtime.workers import diffusers_mps as module

    worker = module.DiffusersMpsWorker()
    worker.torch = type("T", (), {"mps": _MpsFactice([6 * 2**30, 15 * 2**30, 2 * 2**30])})()
    monkeypatch.setattr(module, "peak_rss_bytes", lambda: 400 * 2**20)

    assert worker.peak_memory_bytes() == 6 * 2**30
    assert worker.peak_memory_bytes() == 15 * 2**30
    # Le driver a rendu de la mémoire — il redescend vraiment, c'est mesuré. Le
    # profil garde le plus haut vu, sinon on réserverait au budget un chiffre
    # relevé après coup, très inférieur à ce que la génération a réellement pris.
    assert worker.peak_memory_bytes() == 15 * 2**30


def test_le_rss_sert_de_filet_quand_mps_est_muet(monkeypatch):
    from ecurie_runtime.workers import diffusers_mps as module

    worker = module.DiffusersMpsWorker()
    worker.torch = type("T", (), {"mps": None})()
    monkeypatch.setattr(module, "peak_rss_bytes", lambda: 3 * 2**30)
    assert worker.peak_memory_bytes() == 3 * 2**30
