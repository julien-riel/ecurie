"""Entrypoint Hunyuan3D 2.1 Shape — image vers maillage (CONCEPTION.md §5.2).

C'est le chemin `runtime: custom` : le superviseur lance ce fichier avec le python
de `runtimes/hunyuan3d/.venv`, et ne lui rend visible d'Écurie que
`ecurie_runtime.workers.base` (bibliothèque standard pure) par PYTHONPATH.

Trois particularités expliquent la forme de ce fichier.

1. `hy3dshape` n'est pas sur PyPI. Le code d'inférence de Tencent n'est publié que
   dans un dépôt Git, sans `setup.py` : `uv sync` ne peut pas l'installer, il faut
   le vendorer à la main sous `vendor/`. C'est la seule étape d'installation
   qu'`ecurie env sync hunyuan3d` ne fait pas, donc la première vérifiée par
   `load`, avant même torch — sinon la panne se présente comme un ImportError de
   torch et on répare la mauvaise chose.

2. Rien de ce fichier n'a été exécuté de bout en bout sur Apple Silicon, ni ici ni
   ailleurs à notre connaissance : la seule trace publique d'exécution du code 2.1
   sur MPS est un script tiers qui fait un forward DiT et un décodage VAE, pas la
   chaîne complète. Ce qui est écrit ici l'est d'après le source amont. Les points
   non vérifiés remontent en `caveats` dans les métriques du job, et les modes de
   panne prévisibles sortent en `WorkerError` nommant leur réparation, plutôt que
   d'attendre le plantage obscur.

3. Le code amont suppose CUDA à plusieurs endroits — jamais de façon obligatoire
   côté géométrie, mais assez pour salir l'exécution. Ces endroits sont désamorcés
   avant le premier import de `hy3dshape` (voir `desamorcer_environnement` et
   `neutraliser_sdp_kernel`), pas contournés en aval quand le mal est fait.

Le worker ne télécharge rien : `weights_path` est un chemin local déjà vérifié par
le superviseur, et le chargeur amont est recomposé pour pointer dessus.
"""

import contextlib
import gc
import importlib
import os
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecurie_runtime.workers.base import (
    InferRequest,
    InferResult,
    ProgressFn,
    Worker,
    WorkerError,
    main,
    peak_rss_bytes,
)

RACINE = Path(__file__).resolve().parent
DEPOT_AMONT = "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1"
VENDOR_DEFAUT = Path("vendor") / "Hunyuan3D-2.1"

# Chemins essayés pour trouver le dossier qui *contient* le paquet `hy3dshape`.
# Le premier est celui que produisent les deux commandes git du README ; le second
# couvre une copie aplatie faite à la main. On teste la présence de `pipelines.py`
# plutôt que celle du dossier : un `sparse-checkout` interrompu laisse une
# arborescence vide, qui échouerait plus tard et pour une raison illisible.
CANDIDATS_HY3DSHAPE = (
    VENDOR_DEFAUT / "hy3dshape",
    Path("vendor") / "hy3dshape",
)

SOUS_DOSSIER_DIT = "hunyuan3d-dit-v2-1"
NOM_SORTIE = "mesh.glb"

# Défauts du contrat `image-to-mesh` et du manifeste — délibérément plus modestes
# que ceux du pipeline amont (384 / 50), qui visent une carte de référence et non
# une machine à mémoire unifiée.
OCTREE_DEFAUT = 256
OCTREE_AUTORISES = (128, 256, 384, 512)
STEPS_DEFAUT = 30
STEPS_MIN, STEPS_MAX = 10, 100

# Défauts amont, repris tels quels faute de mesure qui justifierait autre chose.
GUIDANCE_DEFAUT = 5.0
NUM_CHUNKS_DEFAUT = 8000

# Limites assumées de cet adaptateur, remontées à chaque job. Elles ne sont pas
# décoratives : le contrôle d'admission et le banc d'essai s'appuient sur des
# chiffres que ce worker ne sait pas encore produire exactement.
CAVEATS = (
    "Tenue hors de cette machine non vérifiée : le chemin a passé le banc ici le "
    "2026-08-24, mais sur une seule machine, et aucune trace publique n'existe "
    "d'un Hunyuan3DDiTFlowMatchingPipeline 2.1 complet exécuté sur MPS.",
    "Géométrie seulement — la texturation PBR amont exige des noyaux CUDA compilés.",
    "peak_memory_bytes est une borne inférieure du pic réel, pas une mesure exacte.",
)


# --- vendoring de hy3dshape --------------------------------------------------


def trouver_hy3dshape(racine: Path) -> Path | None:
    """Dossier à insérer dans `sys.path` : celui qui contient le paquet `hy3dshape`."""
    candidats: list[Path] = []
    forcé = os.environ.get("ECURIE_HY3DSHAPE_PATH")
    if forcé:
        candidats.append(Path(forcé).expanduser())
    candidats.extend(racine / relatif for relatif in CANDIDATS_HY3DSHAPE)
    for candidat in candidats:
        if (candidat / "hy3dshape" / "pipelines.py").is_file():
            return candidat
    return None


def message_vendoring(racine: Path) -> str:
    """Le message qui répare, pas celui qui constate."""
    cible = racine / VENDOR_DEFAUT
    attendu = cible / "hy3dshape" / "hy3dshape" / "pipelines.py"
    return (
        "hy3dshape est introuvable. Ce paquet n'existe pas sur PyPI : le code "
        f"d'inférence n'est publié que dans {DEPOT_AMONT}, sans setup.py, et "
        "`ecurie env sync hunyuan3d` ne peut donc pas l'installer. Le vendorer :\n"
        f"  git clone --depth 1 --filter=blob:none --sparse {DEPOT_AMONT} {cible}\n"
        f"  git -C {cible} sparse-checkout set hy3dshape/hy3dshape\n"
        f"Attendu ensuite : {attendu}\n"
        "Sinon, pointer ECURIE_HY3DSHAPE_PATH sur le dossier qui contient le paquet."
    )


# --- désamorçage des chemins CUDA amont --------------------------------------


def desamorcer_environnement() -> None:
    """Deux variables d'environnement amont font tomber le code sur CUDA.

    Elles sont retirées plutôt qu'ignorées : elles sont lues à des endroits que cet
    adaptateur ne contrôle pas (un décorateur de module, une garde d'import), donc
    les laisser en place reviendrait à parier sur l'ordre des imports.
    """
    for nom, conséquence in (
        ("HY3DGEN_DEBUG", "`synchronize_timer` passerait par torch.cuda.Event / synchronize"),
        ("USE_SAGEATTN", "`sageattention` est un noyau CUDA, absent de cet environnement"),
    ):
        if os.environ.pop(nom, None) is not None:
            journal(f"{nom} retirée de l'environnement : {conséquence}")


def neutraliser_sdp_kernel(torch: Any) -> str | None:
    """Remplace `torch.backends.cuda.sdp_kernel` par un contexte neutre hors CUDA.

    `hunyuandit.py` — l'architecture de la 2.1 — enveloppe *chaque* appel SDPA dans
    ce gestionnaire de contexte, sans condition de plateforme. Il ne fait que poser
    des drapeaux que seul le dispatcher CUDA consulte : sur MPS il ne sert à rien,
    mais il est marqué déprécié en amont, ce qui donne un `FutureWarning` par appel
    et par pas de diffusion, et il est annoncé pour suppression — le jour où torch
    le retire, `hunyuandit.py` lève un AttributeError au milieu du premier forward.
    Le neutraliser ici règle les deux, et n'enlève rien puisqu'il n'y a pas de CUDA.

    `ECURIE_HY3D_GARDER_SDP_KERNEL=1` restitue le comportement amont pour comparer.
    """
    if os.environ.get("ECURIE_HY3D_GARDER_SDP_KERNEL"):
        return None
    if torch.cuda.is_available():
        return None
    backends = torch.backends.cuda
    présent = getattr(backends, "sdp_kernel", None) is not None
    try:
        backends.sdp_kernel = lambda *_a, **_k: contextlib.nullcontext()
    except Exception as exc:  # noqa: BLE001 — certains modules de torch filtrent leurs écritures
        return (
            f"torch.backends.cuda.sdp_kernel n'a pas pu être neutralisé ({exc}) alors que "
            "hunyuandit.py l'appelle sans condition de plateforme : attendre un "
            "FutureWarning par pas de diffusion, et un échec si torch l'a déjà retiré."
        )
    journal("torch.backends.cuda.sdp_kernel neutralisé (pas de CUDA sur cette machine)")
    if présent:
        return None
    return (
        "torch.backends.cuda.sdp_kernel a disparu de cette version de torch ; un "
        "contexte neutre a été posé pour que hunyuandit.py s'exécute. Contournement "
        "non vérifié en conditions réelles."
    )


# --- localisation des poids --------------------------------------------------


@dataclass(frozen=True)
class Poids:
    """Ce que le chargeur amont attend : un dossier, et le nom exact du checkpoint."""

    dossier: Path
    fichier: str
    variante: str | None
    safetensors: bool


def fichier_poids(dossier: Path) -> tuple[str, str | None, bool] | None:
    """Nom réel du checkpoint, et ce qu'il implique pour `variant` / `use_safetensors`.

    Le chargeur amont recompose `model[.<variant>].<ext>` à partir de ses arguments.
    On relit le nom du fichier réellement présent plutôt que de supposer `fp16` :
    le manifeste épingle une révision, et une révision peut publier autre chose.
    """
    for extension, safetensors in ((".ckpt", False), (".safetensors", True)):
        for chemin in sorted(dossier.glob(f"model*{extension}")):
            milieu = chemin.name[len("model") : -len(extension)].lstrip(".")
            return chemin.name, (milieu or None), safetensors
    return None


def localiser_poids(weights_path: Any) -> Poids:
    """Résout le dossier du DiT à partir du chemin vérifié par le superviseur.

    `weights_path` peut désigner la racine du dépôt téléchargé ou directement le
    sous-dossier du DiT — les deux se rencontrent selon la façon dont `ecurie pull`
    a rangé la révision. Le worker ne devine rien d'autre : il ne remonte pas
    l'arborescence à la recherche d'un checkpoint, sous peine de charger un jour les
    poids d'un autre variant que celui qu'annonce le manifeste du job.
    """
    if not weights_path:
        raise WorkerError(
            "variant sans `weights_path` : le superviseur n'a pas résolu les poids. "
            "Le worker ne télécharge jamais — voir `ecurie pull`."
        )
    base = Path(str(weights_path)).expanduser()
    if base.is_file():
        base = base.parent
    if not base.is_dir():
        raise WorkerError(f"poids introuvables : {base} — les retélécharger avec `ecurie pull`")

    for dossier in (base, base / SOUS_DOSSIER_DIT):
        trouvé = fichier_poids(dossier) if dossier.is_dir() else None
        if trouvé and (dossier / "config.yaml").is_file():
            fichier, variante, safetensors = trouvé
            return Poids(dossier, fichier, variante, safetensors)

    raise WorkerError(
        f"aucun couple (config.yaml, model*.ckpt) sous {base} ni sous "
        f"{base / SOUS_DOSSIER_DIT}. La partie géométrie de tencent/Hunyuan3D-2.1 "
        f"tient dans deux fichiers, `{SOUS_DOSSIER_DIT}/config.yaml` et "
        f"`{SOUS_DOSSIER_DIT}/model.fp16.ckpt` — les retélécharger avec `ecurie pull`."
    )


# --- préparation d'un job ----------------------------------------------------


@dataclass(frozen=True)
class Arguments:
    """Réglages d'un job, vérifiés avant que le moindre tenseur ne soit alloué."""

    image: Path
    sortie: Path
    octree_resolution: int
    num_inference_steps: int
    guidance_scale: float
    num_chunks: int
    seed: int | None


def preparer_arguments(request: InferRequest, defauts: Mapping[str, Any]) -> Arguments:
    """Fusionne les sources de réglages et refuse tout de suite ce qui est invalide.

    Priorité : entrée du job, puis `params` du variant (les deux sont déjà fusionnés
    par `InferRequest.get`), puis les `defaults` du manifeste, puis les défauts du
    contrat de capacité. Le contrat est validé côté API, mais un worker qui fait
    confiance à son appelant plante en plein milieu d'une diffusion de dix minutes
    au lieu d'échouer en une milliseconde : la vérification est refaite ici.
    """

    def lire(clé: str, defaut: Any) -> Any:
        valeur = request.get(clé)
        return defauts.get(clé, defaut) if valeur is None else valeur

    brut = request.get("image")
    if not brut:
        raise WorkerError("entrée `image` absente — la capacité image-to-mesh l'exige")
    image = Path(str(brut)).expanduser()
    if not image.is_absolute():
        # Le superviseur copie l'entrée dans le dossier du job : un chemin relatif
        # s'y rapporte, jamais au répertoire courant du worker.
        image = request.output_dir / image
    image = image.resolve()
    if not image.is_file():
        raise WorkerError(f"image introuvable : {image}")

    octree = _entier(lire("octree_resolution", OCTREE_DEFAUT), "octree_resolution")
    if octree not in OCTREE_AUTORISES:
        admises = ", ".join(str(v) for v in OCTREE_AUTORISES)
        raise WorkerError(f"octree_resolution={octree} : valeurs admises {admises}")

    steps = _entier(lire("num_inference_steps", STEPS_DEFAUT), "num_inference_steps")
    if not STEPS_MIN <= steps <= STEPS_MAX:
        raise WorkerError(
            f"num_inference_steps={steps} hors de [{STEPS_MIN}, {STEPS_MAX}]"
        )

    # La graine du protocole prime sur celle de l'entrée : c'est elle que le
    # superviseur écrit au manifeste du job, donc elle qui doit être rejouable.
    graine = request.seed if request.seed is not None else lire("seed", None)

    return Arguments(
        image=image,
        sortie=request.output_dir / NOM_SORTIE,
        octree_resolution=octree,
        num_inference_steps=steps,
        guidance_scale=float(lire("guidance_scale", GUIDANCE_DEFAUT)),
        num_chunks=_entier(lire("num_chunks", NUM_CHUNKS_DEFAUT), "num_chunks"),
        seed=None if graine is None else _entier(graine, "seed"),
    )


def _entier(valeur: Any, nom: str) -> int:
    if isinstance(valeur, bool):
        raise WorkerError(f"{nom} : entier attendu, booléen reçu")
    if isinstance(valeur, int):
        return valeur
    try:
        return int(str(valeur).strip())
    except (TypeError, ValueError) as exc:
        raise WorkerError(f"{nom} : entier attendu, reçu {valeur!r}") from exc


# --- worker ------------------------------------------------------------------


class Hunyuan3DWorker(Worker):
    name = "hunyuan3d"

    def __init__(self, racine: Path | None = None) -> None:
        # Rien de lourd ici : `--self-test` doit pouvoir instancier l'adaptateur sur
        # une machine sans torch, et un test de CI importe ce module tel quel.
        self.racine = racine or RACINE
        self.variant: dict[str, Any] = {}
        self.torch: Any = None
        self.pipe: Any = None
        self.device = "?"
        self.caveats: list[str] = []

    # -- chargement -----------------------------------------------------------

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        self.variant = variant or {}
        self.caveats = list(CAVEATS)

        # Ordre voulu : les deux vérifications qui ne coûtent qu'un `stat` d'abord.
        # Un environnement à moitié installé doit se dire en une seconde, pas après
        # sept gigaoctets de torch.load.
        vendor = trouver_hy3dshape(self.racine)
        if vendor is None:
            raise WorkerError(message_vendoring(self.racine))
        poids = localiser_poids(self.variant.get("weights_path"))

        desamorcer_environnement()

        # `smart_load_model` recompose `$HY3DGEN_MODELS/<model_path>/<subfolder>` :
        # on lui rend le chemin déjà vérifié par le superviseur, découpé en trois,
        # au lieu de l'identifiant Hugging Face. Sans cela il chercherait sous
        # ~/.cache/hy3dgen, ne trouverait rien, et appellerait snapshot_download —
        # que HF_HUB_OFFLINE=1 fait échouer, mais sur un message parlant de réseau
        # là où le problème serait un chemin. Posé avant l'import : la variable peut
        # être lue à l'import du module amont.
        os.environ["HY3DGEN_MODELS"] = str(poids.dossier.parent.parent)

        torch = _module("torch", "PyTorch")
        self.torch = torch
        self.device = choisir_device(torch)
        if self.device == "cpu":
            self.caveats.append(
                "exécution sur CPU : ni MPS ni CUDA disponibles — durée sans rapport "
                "avec un usage normal"
            )
            journal("ni MPS ni CUDA : exécution sur CPU")
        note = neutraliser_sdp_kernel(torch)
        if note:
            self.caveats.append(note)

        if str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        pipelines = importer_pipelines()

        journal(
            f"chargement de {poids.dossier}/{poids.fichier} sur {self.device} "
            "(torch.load du checkpoint en RAM avant transfert : c'est le pic mémoire attendu)"
        )
        self.pipe = pipelines.Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
            poids.dossier.parent.name,
            subfolder=poids.dossier.name,
            variant=poids.variante,
            use_safetensors=poids.safetensors,
            device=self.device,
            dtype=torch.float16,
        )
        # Ni `enable_flashvdm()` ni `compile()` : le premier ne connaît pas la 2.1
        # dans sa table de VAE et se contenterait de changer le décodeur volumique,
        # le second n'a jamais été essayé sur MPS.
        return {"device": self.device, "surface_extractors": ["mc"]}

    # -- inférence ------------------------------------------------------------

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.pipe is None or self.torch is None:
            raise WorkerError("infer avant load — aucun pipeline en mémoire")

        args = preparer_arguments(request, self.variant.get("defaults") or {})
        progress(5, "lecture de l'image")
        image, remarques = ouvrir_image(args.image)

        generator = self.torch.Generator()  # CPU : le bruit initial y est créé puis transféré
        if args.seed is not None:
            generator.manual_seed(args.seed)

        # Un seul appel couvre la diffusion, la requête de la grille SDF et le
        # marching cubes : la signature vérifiée n'expose aucun callback, donc il n'y
        # a pas de jalon honnête à poser entre les deux. D'où le battement.
        libellé = f"diffusion et extraction de surface ({args.num_inference_steps} pas)"
        progress(20, libellé)
        with battement(progress, 20, libellé):
            maillages = self.pipe(
                image=image,
                num_inference_steps=args.num_inference_steps,
                guidance_scale=args.guidance_scale,
                generator=generator,
                octree_resolution=args.octree_resolution,
                num_chunks=args.num_chunks,
                output_type="trimesh",
                # `mc_algo` reste au défaut : l'autre extracteur (`dmc`) réclame
                # `diso`, qui est CUDA. Et pas de barre tqdm — il n'y a pas de
                # terminal au bout de stderr, seulement un fichier de journal que
                # des milliers de retours chariot rendraient illisible.
                enable_pbar=False,
            )

        progress(85, "export GLB")
        maillage = premier_maillage(maillages)
        maillage.export(str(args.sortie))
        if not args.sortie.is_file():
            raise WorkerError(f"l'export n'a produit aucun fichier : {args.sortie}")

        progress(98, "terminé")
        metrics: dict[str, Any] = {
            "device": self.device,
            "octree_resolution": args.octree_resolution,
            "num_inference_steps": args.num_inference_steps,
            "guidance_scale": args.guidance_scale,
            "seed": args.seed,
            "mesh_bytes": args.sortie.stat().st_size,
            "caveats": [*self.caveats, *remarques],
            **statistiques_maillage(maillage),
        }
        # Nom relatif au dossier du job : c'est l'API qui sert le fichier, elle ne
        # doit jamais recevoir un chemin absolu de la machine.
        return InferResult(output={"mesh": NOM_SORTIE}, metrics=metrics)

    # -- fin de vie et mesures ------------------------------------------------

    def unload(self) -> None:
        self.pipe = None
        self.variant = {}
        gc.collect()
        mps = getattr(self.torch, "mps", None)
        if mps is not None and hasattr(mps, "empty_cache"):
            try:
                mps.empty_cache()
            except Exception as exc:  # noqa: BLE001 — on rend la main de toute façon
                journal(f"empty_cache a échoué : {exc}")

    def peak_memory_bytes(self) -> int | None:
        """Le plus grand du pic RSS noyau et de l'allocation Metal courante.

        Ce que ce nombre est vraiment : `torch.mps` n'offre pas d'équivalent de
        `torch.cuda.max_memory_allocated`, donc côté GPU on ne dispose que d'une
        valeur *instantanée*, jamais d'un pic. `ru_maxrss`, lui, est un vrai pic, et
        sur mémoire unifiée il englobe normalement le `torch.load` du checkpoint —
        probablement le vrai maximum de ce worker. Le maximum des deux est donc une
        borne inférieure honnête, pas une mesure : le contrôle d'admission doit le
        savoir, et `ecurie bench` reste la seule source d'un profil digne de foi.
        """
        valeurs = [peak_rss_bytes()]
        mps = getattr(self.torch, "mps", None)
        if mps is not None:
            try:
                valeurs.append(int(mps.driver_allocated_memory()))
            except Exception as exc:  # noqa: BLE001 — une sonde absente n'est pas une panne
                journal(f"driver_allocated_memory indisponible : {exc}")
        connues = [v for v in valeurs if v]
        return max(connues) if connues else None


# --- outillage ---------------------------------------------------------------


def choisir_device(torch: Any) -> str:
    forcé = os.environ.get("ECURIE_HY3D_DEVICE")
    if forcé:
        return forcé
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def importer_pipelines() -> Any:
    """`hy3dshape.pipelines`, avec le seul import manquant qui a une cause connue.

    Importer un sous-module exécute d'abord le `__init__.py` du paquet, lequel
    importe `postprocessors`, lequel importe `pymeshlab` au niveau du module :
    `pymeshlab` est donc requis même pour ne produire que de la géométrie, contre
    ce que laisse croire la démo minimale amont.
    """
    try:
        return importlib.import_module("hy3dshape.pipelines")
    except ImportError as exc:
        manquant = getattr(exc, "name", None) or ""
        if manquant.split(".")[0] == "pymeshlab":
            raise WorkerError(
                "hy3dshape/__init__.py importe `postprocessors`, qui importe "
                "`pymeshlab` au niveau du module : le paquet est requis même pour la "
                "seule géométrie. Vérifier qu'il figure dans "
                "runtimes/hunyuan3d/pyproject.toml, puis `ecurie env sync hunyuan3d`."
            ) from exc
        raise WorkerError(
            f"hy3dshape est vendoré mais ne s'importe pas ({exc}). Si le module "
            "manquant est une dépendance, l'ajouter à runtimes/hunyuan3d/pyproject.toml "
            "puis `ecurie env sync hunyuan3d` ; s'il appartient à hy3dshape, le "
            "sparse-checkout est incomplet — voir runtimes/hunyuan3d/README.md."
        ) from exc


def ouvrir_image(chemin: Path) -> tuple[Any, list[str]]:
    """Charge l'entrée en RGBA et dit ce qui manque plutôt que de le corriger.

    Le préprocesseur amont recadre l'objet d'après le canal alpha. Une image sans
    fond détouré donne donc un maillage qui inclut le décor — c'est un défaut de
    qualité, pas une panne, et le détourage automatique passerait par `rembg`, qui
    télécharge son modèle au premier appel. Un worker ne télécharge pas : on
    signale, on n'installe pas une dépendance réseau en douce.
    """
    module = _module("PIL.Image", "Pillow")
    remarques: list[str] = []
    image = module.open(chemin)
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    # Le test porte sur l'alpha après conversion, pas sur le mode d'origine : un
    # PNG palettisé transparent est bien détouré, un RGBA tout opaque ne l'est pas.
    if image.getchannel("A").getextrema()[0] == 255:
        remarques.append(
            "fond non détouré (canal alpha entièrement opaque) : le recadrage amont "
            "s'appuie sur l'alpha, la géométrie peut donc inclure le décor. Détourer "
            "en amont du job."
        )
    return image, remarques


def premier_maillage(sortie: Any) -> Any:
    if isinstance(sortie, list | tuple):
        if not sortie:
            raise WorkerError("le pipeline n'a rendu aucun maillage")
        return sortie[0]
    return sortie


def statistiques_maillage(maillage: Any) -> dict[str, int]:
    stats: dict[str, int] = {}
    for clé in ("faces", "vertices"):
        forme = getattr(getattr(maillage, clé, None), "shape", None)
        if forme:
            stats[clé] = int(forme[0])
    return stats


@contextlib.contextmanager
def battement(progress: ProgressFn, pct: int, libellé: str, période_s: float = 20.0):
    """Signe de vie pendant l'appel unique au pipeline.

    Le superviseur relance son compte à rebours à chaque `ev:progress` : sans
    battement, un job long serait tué pour silence alors qu'il avance. Le
    pourcentage, lui, ne bouge pas — le pipeline n'expose aucun callback, donc
    l'adaptateur n'a rien de vrai à dire sur l'avancement, et une barre immobile
    vaut mieux qu'une barre qui invente.
    """
    arrêt = threading.Event()
    début = time.monotonic()

    def boucle() -> None:
        while not arrêt.wait(période_s):
            progress(pct, f"{libellé} — {int(time.monotonic() - début)} s")

    fil = threading.Thread(target=boucle, name="battement", daemon=True)
    fil.start()
    try:
        yield
    finally:
        # Arrêté avant que l'appelant n'émette son `ev:result` : deux écrivains sur
        # le canal, c'est une ligne JSON coupée en deux.
        arrêt.set()
        fil.join(timeout=5)


def _module(nom: str, quoi: str) -> Any:
    try:
        return importlib.import_module(nom)
    except ImportError as exc:
        raise WorkerError(
            f"{quoi} manque dans runtimes/hunyuan3d/.venv ({exc}) — "
            "`ecurie env sync hunyuan3d`"
        ) from exc


def journal(message: str) -> None:
    """stderr, jamais stdout : le descripteur 1 appartient au protocole."""
    sys.stderr.write(f"[hunyuan3d] {message}\n")
    sys.stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main(Hunyuan3DWorker))
