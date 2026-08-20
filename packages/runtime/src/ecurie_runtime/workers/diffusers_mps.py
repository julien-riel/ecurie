"""Adaptateur `diffusers-mps` : text-to-image sur PyTorch/MPS (CONCEPTION.md §5.2).

Générique par construction : aucun modèle image n'est encore au registre, donc rien
ici ne suppose SDXL plutôt que SD1.5 ou un transformeur. Tout ce qui dépend du dépôt
— dimensions natives, scheduler, taille de latents — est lu sur le pipeline chargé et
renvoyé en options, jamais codé en dur.

Trois décisions structurent le fichier, et elles se paient si on les défait :

1. `torch` et `diffusers` ne sont importés que dans `load()`. La CI importe ce module
   sur une machine sans Apple Silicon et sans venv de runtime ; un import au niveau du
   module ferait échouer la collecte des tests avant même le premier `assert`.
2. Le pic mémoire annoncé au contrôle d'admission est le pic RSS du noyau, pas un
   compteur GPU — `torch.mps` n'expose aucun maximum (voir `peak_memory_bytes`).
3. Les leviers mémoire (attention slicing, VAE slicing/tiling) sont des réglages du
   manifeste, désactivés par défaut. La documentation de diffusers et la docstring de
   ses propres méthodes se contredisent sur leur intérêt en mémoire unifiée : ce sont
   des mesures à faire avec `ecurie bench`, pas des croyances à câbler.

Le worker ne télécharge rien : `weights_path` est un dossier local déjà résolu par le
superviseur, `HF_HUB_OFFLINE=1` est posé dans l'environnement du processus par
`envs.py`, et `local_files_only=True` est repassé à chaque étage de chargement.
"""

import gc
import importlib
import inspect
import secrets
import time
from collections.abc import Callable
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

ENV_NAME = "diffusers-mps"
IMAGE_NAME = "image.png"

# Miroir des `default` de registry/capabilities/text-to-image.json. La duplication est
# assumée : un worker ne lit pas le registre (workers/__init__.py, règle 2). Un test
# confronte les deux fichiers pour qu'une dérive se voie en CI, pas en production.
CONTRACT_DEFAULTS: dict[str, Any] = {
    "width": 1024,
    "height": 1024,
    "steps": 25,
    "guidance_scale": 3.5,
}

# `variant["quantization"]` décide du dtype ; fp16 est le défaut sur MPS. bf16 reste
# accessible mais n'est pas un chemin vérifié : les rapports publics vont de « non
# supporté » à « supporté par un chemin dix fois plus lent », aucun ne couvre torch 2.13.
# D'où le caveat remonté à l'UI plutôt qu'un refus, qui interdirait de le mesurer.
DTYPES = {"fp16": "float16", "bf16": "bfloat16", "fp32": "float32"}
DEFAULT_DTYPE = "fp16"

MESSAGE_ENV = (
    "diffusers et torch sont absents de cet environnement — "
    f"reconstruire avec : ecurie env sync {ENV_NAME}"
)


# --- préparation d'un job (pur, sans torch) ----------------------------------


@dataclass(frozen=True)
class Generation:
    """Ce qui sera demandé au pipeline, entièrement résolu et reproductible."""

    prompt: str
    negative_prompt: str | None
    width: int
    height: int
    steps: int
    guidance_scale: float
    seed: int

    def pipeline_kwargs(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "width": self.width,
            "height": self.height,
            "num_inference_steps": self.steps,
            "guidance_scale": self.guidance_scale,
        }

    def as_metrics(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "steps": self.steps,
            "guidance_scale": self.guidance_scale,
            "width": self.width,
            "height": self.height,
        }


def draw_seed() -> int:
    """Graine tirée quand l'utilisateur n'en fournit pas.

    Elle est retournée dans les métriques du job : un artefact dont on ne connaît pas
    la graine ne se rejoue pas, et un artefact non reproductible est un artefact perdu
    (ARCHITECTURE.md §9). Bornée à 2³² pour rester lisible et sérialisable en JSON.
    """
    return secrets.randbelow(2**32)


def plan_generation(
    request: InferRequest,
    defaults: dict[str, Any] | None = None,
    *,
    seed_source: Callable[[], int] = draw_seed,
) -> Generation:
    """Résout les paramètres d'un job : job > défauts du variant > contrat de capacité.

    Fonction pure, sans torch : c'est elle qui porte toute la logique testable de
    l'adaptateur, et la seule chose qu'un test peut vérifier sans Apple Silicon.
    """
    réglages = defaults or {}

    def valeur(clé: str) -> Any:
        for source in (request.get(clé), réglages.get(clé), CONTRACT_DEFAULTS.get(clé)):
            if source is not None:
                return source
        return None

    prompt = str(valeur("prompt") or "").strip()
    if not prompt:
        raise WorkerError("prompt vide — le contrat text-to-image exige une description")

    négatif = valeur("negative_prompt")
    négatif = str(négatif).strip() or None if négatif is not None else None

    # `request.seed` est le champ de protocole, rempli par le superviseur ; `seed` dans
    # l'entrée est ce que l'utilisateur a tapé. Le protocole gagne, sinon un rejeu par
    # graine imposée serait silencieusement écrasé par la valeur du formulaire d'origine.
    graine = request.seed if request.seed is not None else valeur("seed")
    graine = seed_source() if graine is None else int(graine)
    if graine < 0:
        raise WorkerError(f"graine négative : {graine}")

    return Generation(
        prompt=prompt,
        negative_prompt=négatif,
        width=_dimension(valeur("width"), "width"),
        height=_dimension(valeur("height"), "height"),
        steps=_entier_positif(valeur("steps"), "steps"),
        guidance_scale=float(valeur("guidance_scale")),
        seed=graine,
    )


def _dimension(valeur: Any, nom: str) -> int:
    """Garde-fou minimal : le multiple de 8 imposé par le VAE.

    Le contrat exige un multiple de 64 et le vérifie en amont, au chargement du job.
    On ne le redouble pas ici — mais un multiple de 8 est ce en dessous de quoi le
    pipeline échoue au fond du décodeur, avec une erreur de forme de tenseur que
    personne ne relie à sa cause.
    """
    pixels = _entier_positif(valeur, nom)
    if pixels % 8:
        raise WorkerError(f"{nom} = {pixels} : le VAE exige un multiple de 8 pixels")
    return pixels


def _entier_positif(valeur: Any, nom: str) -> int:
    try:
        entier = int(valeur)
    except (TypeError, ValueError) as exc:
        raise WorkerError(f"{nom} : entier attendu, reçu {valeur!r}") from exc
    if entier <= 0:
        raise WorkerError(f"{nom} = {entier} : valeur strictement positive attendue")
    return entier


def detect_variant(weights_path: Path) -> str | None:
    """Suffixe de variante des poids présents (`fp16`, `bf16`), ou None.

    Un dépôt diffusers sert souvent le même modèle en plusieurs précisions :
    `diffusion_pytorch_model.safetensors` à côté de
    `diffusion_pytorch_model.fp16.safetensors`. `from_pretrained` ne prend la
    seconde que si on lui passe `variant="fp16"` — sinon il cherche la première,
    et échoue sur « no file named … » quand les `allow_patterns` du manifeste
    n'ont téléchargé que la variante légère, ce qui est précisément ce qu'on
    veut sur une machine à 24 Go.

    On le déduit des fichiers présents plutôt que de la `quantization` du
    manifeste : les deux ne disent pas la même chose. `quantization` décrit le
    dtype d'exécution, la variante décrit le nom des fichiers sur le disque, et
    un dépôt peut très bien ne servir qu'une précision sans la suffixer.
    """
    for suffixe in ("fp16", "bf16"):
        if any(weights_path.rglob(f"*.{suffixe}.safetensors")):
            # Une variante ne sert que si le fichier sans suffixe manque : quand
            # les deux sont là, `from_pretrained` sait déjà choisir.
            nus = [
                p
                for p in weights_path.rglob("*.safetensors")
                if not p.name.endswith((".fp16.safetensors", ".bf16.safetensors"))
            ]
            return None if nus else suffixe
    return None


def torch_dtype_name(quantization: str | None) -> str:
    """Nom de l'attribut `torch.<dtype>` correspondant à la quantification du variant."""
    if not quantization or quantization == "none":
        return DTYPES[DEFAULT_DTYPE]
    nom = DTYPES.get(quantization)
    if nom is None:
        raise WorkerError(
            f"quantization {quantization!r} : l'adaptateur {ENV_NAME} ne charge que "
            f"{', '.join(sorted(DTYPES))}. La quantification à la volée (bitsandbytes, "
            "quanto, GGUF) n'est pas câblée dans le v0.3."
        )
    return nom


def step_progress(step: int, total: int) -> int:
    """Pas de débruitage → pourcentage. Bornes réservées au reste du job."""
    return 5 + int(83 * min(step, total) / max(1, total))


# --- worker ------------------------------------------------------------------


class DiffusersMpsWorker(Worker):
    name = ENV_NAME

    def __init__(self) -> None:
        self.torch: Any = None
        self.pipe: Any = None
        self.defaults: dict[str, Any] = {}
        self.dtype_name: str = DTYPES[DEFAULT_DTYPE]
        # Plus haut `driver_allocated_memory` vu depuis le démarrage : c'est lui
        # qui fait le profil, pas le RSS (voir `peak_memory_bytes`).
        self._peak_driver: int = 0
        self.caveats: list[str] = []
        self.step_callback = False

    # --- chargement ----------------------------------------------------------

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        torch, auto_pipeline = _import_diffusers()
        self.torch = torch
        self.defaults = dict(variant.get("defaults") or {})
        self.caveats = []

        if not torch.backends.mps.is_available():
            raise WorkerError(
                "backend MPS indisponible (torch.backends.mps.is_available() est faux) — "
                "cet adaptateur ne sert que sur Apple Silicon ; vérifier que "
                f"runtimes/{ENV_NAME}/.venv utilise un Python arm64"
            )

        # `from_pretrained` ne court-circuite le réseau que si on lui passe un DOSSIER ;
        # sur une chaîne quelconque contenant un « / » il la prend pour un identifiant de
        # dépôt et échoue par un message qui parle du Hub, pas des poids manquants. Le
        # chemin vide est écarté avant toute chose : `Path("")` vaut `Path(".")`, qui est
        # bien un dossier — on chargerait alors le répertoire courant.
        brut = str(variant.get("weights_path") or "").strip()
        ref = variant.get("ref") or "<ref>"
        if not brut or not Path(brut).is_dir():
            raise WorkerError(
                f"poids absents : {brut or '(chemin vide)'} n'est pas un dossier — "
                f"télécharger avec : ecurie pull {ref}"
            )
        chemin = Path(brut)

        self.dtype_name = torch_dtype_name(variant.get("quantization"))
        if self.dtype_name == "bfloat16":
            self.caveats.append(
                "bfloat16 sur MPS n'est pas un chemin vérifié (support douteux et lent "
                "selon les rapports publics) — mesurer avant de le retenir"
            )

        variante = detect_variant(chemin)
        if variante:
            self.caveats.append(f"poids chargés depuis la variante « {variante} » du dépôt")

        try:
            pipe = auto_pipeline.from_pretrained(
                str(chemin),
                # Voir `detect_variant` : sans ce kwarg, un dépôt dont on n'a
                # téléchargé que les fichiers `*.fp16.safetensors` est déclaré
                # incomplet.
                variant=variante,
                # Le kwarg est bien `torch_dtype` et non `dtype` : `from_pretrained` fait
                # `kwargs.pop("torch_dtype")`, et `dtype` n'est accepté que par `.to()`.
                # Une valeur passée sous le mauvais nom est silencieusement ignorée et le
                # pipeline se charge en float32 — deux fois la mémoire, sans un mot.
                # C'est ce que verrouille le cap `diffusers<0.40` du pyproject de l'env.
                torch_dtype=getattr(torch, self.dtype_name),
                # Refuser les poids `.bin` n'est pas une préférence de format : les
                # désérialiser exécute du code arbitraire, ce qu'un worker lancé
                # automatiquement sur tout le parc ne peut pas se permettre.
                use_safetensors=True,
                local_files_only=True,
                # Ignorés avec un simple avertissement par les pipelines qui n'en ont pas.
                # Ce n'est pas une position sur le contenu : le safety checker est un CLIP
                # d'environ 1 Go qui entrerait dans le budget mémoire sans jamais figurer
                # dans le profil mesuré du variant, et ferait mentir le contrôle d'admission.
                safety_checker=None,
                requires_safety_checker=False,
            )
        except Exception as exc:
            raise WorkerError(
                f"chargement impossible depuis {chemin} : {type(exc).__name__}: {exc}"
            ) from exc

        pipe.set_progress_bar_config(disable=True)
        pipe = pipe.to("mps")
        self.pipe = pipe

        self._apply_engine_settings()
        self.step_callback = _accepts_step_callback(pipe)
        if not self.step_callback:
            self.caveats.append(
                "ce pipeline n'accepte pas callback_on_step_end : le job n'émettra "
                "aucune progression entre son début et son écriture"
            )
        self.caveats.append(
            "peak_memory_bytes est le plus haut driver_allocated_memory relevé : "
            "torch.mps n'expose aucun pic, et le RSS ne compte pas la mémoire Metal"
        )
        return self._options()

    def _apply_engine_settings(self) -> None:
        """Réglages du moteur, lus une fois pour toutes dans `variant["defaults"]`.

        Le même dictionnaire porte les défauts de génération du variant (`steps`,
        `guidance_scale`…) : c'est voulu, un manifeste n'a qu'un endroit où régler son
        variant. Ce qui les sépare est la lecture — ici on ne va chercher que les quatre
        clés nommées ci-dessous, `plan_generation` que celles du contrat de capacité.
        Aucune clé n'est passée en bloc à quoi que ce soit.
        """
        pipe = self.pipe
        nom_scheduler = self.defaults.get("scheduler")
        if nom_scheduler:
            compatibles = {c.__name__: c for c in pipe.scheduler.compatibles}
            classe = compatibles.get(str(nom_scheduler))
            if classe is None:
                raise WorkerError(
                    f"scheduler {nom_scheduler!r} incompatible avec ce pipeline — "
                    f"disponibles : {', '.join(sorted(compatibles))}"
                )
            # Échange à chaud, sans réallouer les poids : c'est tout l'intérêt de le
            # régler par manifeste plutôt que d'en faire un variant à part entière.
            pipe.scheduler = classe.from_config(pipe.scheduler.config)

        tranche = self.defaults.get("attention_slicing", False)
        if tranche:
            # La doc MPS le recommande sous 64 Go ; la docstring de la méthode avertit
            # qu'avec SDPA — le chemin par défaut de torch ≥ 2.6 — il peut « seriously
            # slow down ». Les deux ne peuvent pas avoir raison sur la même machine :
            # d'où le réglage, désactivé par défaut, et la mesure à faire.
            pipe.enable_attention_slicing("auto" if tranche is True else tranche)

        vae = getattr(pipe, "vae", None)
        if vae is None:
            return
        # API pérenne : `pipe.enable_vae_slicing()` est déprécié depuis 0.39 au profit de
        # `pipe.vae.enable_slicing()`. Les deux réglages sont sans effet dans le cas
        # nominal — le slicing ne s'active qu'au-delà d'une image par lot, le tiling
        # qu'au-delà de la taille de tuile du VAE — et n'existent ici que pour les
        # résolutions au-dessus du natif, où ils deviennent le seul levier.
        if self.defaults.get("vae_slicing"):
            vae.enable_slicing()
        if self.defaults.get("vae_tiling"):
            vae.enable_tiling()

    def _options(self) -> dict[str, Any]:
        """Ce que l'UI peut proposer, lu sur le pipeline réel plutôt que supposé."""
        pipe = self.pipe
        options: dict[str, Any] = {
            "pipeline": type(pipe).__name__,
            "device": "mps",
            "dtype": self.dtype_name,
            "scheduler": type(pipe.scheduler).__name__,
            "schedulers": sorted({c.__name__ for c in pipe.scheduler.compatibles}),
            "components": sorted(pipe.components),
            "caveats": self.caveats,
        }
        natif = _native_size(pipe)
        if natif is not None:
            options["native_width"], options["native_height"] = natif
        budget = self._mps_counters().get("mps_recommended_max_bytes")
        if budget:
            options["recommended_max_memory_bytes"] = budget
        return options

    # --- exécution -----------------------------------------------------------

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        if self.pipe is None:
            raise WorkerError("infer avant load — aucun pipeline en mémoire")
        plan = plan_generation(request, self.defaults)
        progress(2, "préparation")

        # Générateur CPU : chemin nominal et silencieux sur MPS (diffusers rapatrie le
        # tirage sur CPU puis déplace le tenseur), et le seul qui donne deux fois la même
        # image pour la même graine. Recréé à chaque job : l'état d'un Generator est
        # consommé, le réutiliser donnerait une autre image à graine égale.
        générateur = self.torch.Generator(device="cpu").manual_seed(plan.seed)

        appel: dict[str, Any] = {
            **plan.pipeline_kwargs(),
            "generator": générateur,
            # Une image à la fois : le batch est documenté comme instable sur MPS, et
            # c'est de toute façon la granularité du contrat de capacité.
            "num_images_per_prompt": 1,
            "output_type": "pil",
            "return_dict": True,
        }
        if self.step_callback:
            appel["callback_on_step_end"] = _step_reporter(progress, plan.steps)

        départ = time.monotonic()
        sortie = self.pipe(**appel)
        # MPS est asynchrone : sans cette barrière, la durée mesurée est celle de la mise
        # en file d'attente, pas celle du calcul, et le profil du variant est faux.
        self.torch.mps.synchronize()
        génération_ms = int((time.monotonic() - départ) * 1000)

        progress(92, "encodage PNG")
        image = sortie.images[0]
        image.save(request.output_dir / IMAGE_NAME, format="PNG")

        compteurs = self._mps_counters()
        self.torch.mps.empty_cache()
        return InferResult(
            output={"image": IMAGE_NAME},
            metrics={
                **plan.as_metrics(),
                "scheduler": type(self.pipe.scheduler).__name__,
                "dtype": self.dtype_name,
                "generate_ms": génération_ms,
                "ms_per_step": round(génération_ms / max(1, plan.steps), 1),
                **compteurs,
            },
        )

    def unload(self) -> None:
        """Rend la mémoire au budget, pas seulement à Python.

        Sans `empty_cache`, l'allocateur MPS garde ses pools : le processus a bien
        libéré ses tenseurs, le pilote Metal tient toujours les octets, et le résident
        suivant se voit refuser l'admission pour une mémoire que plus personne n'utilise.
        """
        self.pipe = None
        if self.torch is not None:
            gc.collect()
            try:
                self.torch.mps.empty_cache()
            except (AttributeError, RuntimeError):
                pass

    # --- mesures -------------------------------------------------------------

    def peak_memory_bytes(self) -> int | None:
        """Le plus haut relevé de `driver_allocated_memory`, et non le pic RSS.

        Le RSS ne voit pas la mémoire Metal, et ce n'est pas une nuance : mesuré
        le 20 août 2026 sur SDXL fp16, `ru_maxrss` plafonnait à **0,42 Gio**
        pendant que le driver Metal en réservait **15,95** — quatre-vingt-dix
        pour cent du budget de la machine. Un profil écrit sur le RSS aurait fait
        croire au contrôle d'admission qu'un modèle image tient dans un demi-
        gigaoctet, et l'aurait laissé cohabiter avec un modèle de huit : l'OOM
        que ce jalon existe pour empêcher. La mémoire unifiée adosse bien les
        tampons Metal aux pages physiques, mais le noyau ne les impute pas au RSS
        du processus.

        `driver_allocated_memory` est instantané et **redescend** : 15,95 Gio à
        la fin d'une génération, 9,25 une fraction de seconde plus tard. C'est
        pourquoi le maximum est tenu à chaque relevé (`_mps_counters`) plutôt que
        lu une fois à la fin — sinon on réserverait au budget un chiffre pris
        après que le driver a déjà rendu la mémoire. Le RSS reste en filet pour
        les parties qui, elles, sont bien en mémoire CPU.
        """
        self._mps_counters()  # met à jour le maximum au passage
        return max(self._peak_driver, peak_rss_bytes() or 0) or None

    def _mps_counters(self) -> dict[str, int]:
        """Compteurs MPS instantanés. Aucun n'est un pic — les noms le disent.

        Tout relevé nourrit au passage le maximum retenu pour le profil. C'est
        nécessaire parce que `driver_allocated_memory` **redescend** : mesuré le
        20 août 2026, il valait 15,95 Gio à la fin d'une génération et 9,25 une
        fraction de seconde plus tard, quand le worker répondait au protocole.
        Un pic relevé après coup manque donc ce qu'il faut réserver, et c'est le
        relevé fait au plus près du calcul qui compte.
        """
        mps = getattr(self.torch, "mps", None)
        if mps is None:
            return {}
        try:
            compteurs = {
                "mps_current_allocated_bytes": int(mps.current_allocated_memory()),
                "mps_driver_allocated_bytes": int(mps.driver_allocated_memory()),
                "mps_recommended_max_bytes": int(mps.recommended_max_memory()),
            }
        except (AttributeError, RuntimeError):
            return {}
        self._peak_driver = max(self._peak_driver, compteurs["mps_driver_allocated_bytes"])
        return compteurs


# --- accès au runtime --------------------------------------------------------


def _import_diffusers() -> tuple[Any, Any]:
    """Importe torch et diffusers, ou explique comment réparer l'environnement."""
    try:
        torch = importlib.import_module("torch")
        diffusers = importlib.import_module("diffusers")
    except ImportError as exc:
        raise WorkerError(f"{MESSAGE_ENV} ({exc})") from exc
    return torch, diffusers.AutoPipelineForText2Image


def _accepts_step_callback(pipe: Any) -> bool:
    """Le pipeline accepte-t-il `callback_on_step_end` ?

    On le constate au lieu de le supposer : l'API unifiée de callback est récente et
    des pipelines communautaires gardent l'ancienne signature. Perdre la progression
    dégrade l'UI ; passer un kwarg inconnu à `__call__` fait échouer le job.
    """
    try:
        signature = inspect.signature(type(pipe).__call__)
    except (TypeError, ValueError):
        return False
    return "callback_on_step_end" in signature.parameters


def _step_reporter(progress: ProgressFn, total: int) -> Any:
    """Callback de diffusers branché sur la progression du protocole.

    Un job image dure des dizaines de secondes : sans ces messages, l'Atelier n'aurait
    rien à afficher entre l'envoi et l'image (ARCHITECTURE.md §9). Le contrat de
    diffusers impose de retourner le dictionnaire reçu — ne pas le faire vide les
    tenseurs que le pipeline réinjecte au pas suivant.
    """

    def au_pas(_pipe: Any, step: int, _timestep: Any, kwargs: dict) -> dict:
        fait = step + 1
        progress(step_progress(fait, total), f"débruitage {fait}/{total}")
        return kwargs

    return au_pas


def _native_size(pipe: Any) -> tuple[int, int] | None:
    """Résolution native, si la config du pipeline la donne.

    `sample_size × vae_scale_factor` est ce que le pipeline utilise quand height/width
    valent None — 512 pour une SD1.5, 1024 pour une SDXL. C'est une information utile à
    l'UI, jamais une contrainte : on la renvoie quand elle est lisible, on se tait sinon.
    """
    dorsale = getattr(pipe, "unet", None) or getattr(pipe, "transformer", None)
    échantillon = getattr(getattr(dorsale, "config", None), "sample_size", None)
    facteur = getattr(pipe, "vae_scale_factor", None)
    if not isinstance(échantillon, int) or not isinstance(facteur, int):
        return None
    côté = échantillon * facteur
    return (côté, côté) if côté > 0 else None


if __name__ == "__main__":
    raise SystemExit(main(DiffusersMpsWorker))
