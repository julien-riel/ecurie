"""Environnements isolés des runtimes (CONCEPTION.md §5.3).

Un venv par famille de runtime sous `runtimes/<env>/`, jamais un env partagé :
c'est la mitigation du premier risque du §13 de l'architecture, et la seule
raison pour laquelle `torch 2.9` et `torch 2.6` peuvent coexister dans le parc.

Le `pyproject.toml` de chaque env est versionné, son `.venv` ne l'est pas :
`ecurie env sync` le reconstruit. Le superviseur refuse de lancer un worker sur
un env absent et affiche la commande de réparation plutôt que de tenter sa chance
avec l'interpréteur d'Écurie — un worker qui démarre dans le mauvais
environnement échoue plus tard, plus loin, et pour une raison illisible.
"""

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ecurie_core.models import Variant

RUNTIMES_DIR = "runtimes"

# Le worker livré pour chaque runtime (CONCEPTION.md §5.2). `custom` n'a pas de
# module : il délègue à l'`entrypoint` du manifeste.
WORKER_MODULES = {
    "mlx-audio": "ecurie_runtime.workers.mlx_audio",
    "diffusers-mps": "ecurie_runtime.workers.diffusers_mps",
    "mlx-vlm": "ecurie_runtime.workers.mlx_vlm",
    "mlx-lm": "ecurie_runtime.workers.mlx_lm",
    # `torch-vision` n'a délibérément pas d'adaptateur par défaut : détourer et
    # agrandir ne partagent ni le modèle, ni l'appel, ni la sortie. Un runtime
    # est une famille de bibliothèques, pas une promesse d'API commune.
}

# Une même bibliothèque peut servir deux capacités par deux API distinctes :
# `mlx_audio.tts` pour la synthèse vocale, `mlx_audio.music` pour la chanson.
# Elles ne se ressemblent ni dans le chargement, ni dans l'appel, ni dans la
# sortie — les mêler dans un seul adaptateur donnerait un fichier qui commence
# par un aiguillage et ne se relit plus. La capacité déclarée par le modèle
# tranche, et le runtime reste le même.
WORKER_MODULES_BY_CAPABILITY = {
    ("mlx-audio", "text-to-music"): "ecurie_runtime.workers.mlx_audio_music",
    # Troisième emploi du même runtime : la diarisation ne transcrit pas, elle
    # découpe. Le modèle sait faire les deux ; le texte appartient à
    # `speech-to-text`, qui a son propre contrat.
    ("mlx-audio", "speaker-diarization"): "ecurie_runtime.workers.moss_diarize",
    # Sixième emploi, et le second sur les mêmes octets que la diarisation : le
    # modèle transcrit ET attribue, mais un contrat qui rendrait les deux ne
    # pourrait être rempli par aucun modèle de transcription ordinaire.
    ("mlx-audio", "speech-to-text"): "ecurie_runtime.workers.moss_transcribe",
    # Quatrième emploi : décrire un son n'est pas le transcrire, et c'est la
    # consigne qui tranche — sans elle, ces réseaux retombent sur leur défaut
    # d'usine, qui est de transcrire.
    ("mlx-audio", "audio-to-text"): "ecurie_runtime.workers.qwen2_audio",
    # Cinquième emploi, et le second à faire parler : choisir une voix qu'on
    # embarque et imiter une voix qu'on reçoit ne se remplacent pas l'un l'autre.
    ("mlx-audio", "voice-clone"): "ecurie_runtime.workers.omnivoice",
    # Mêmes poids que la lecture de document, autre question posée : décrire une
    # image et transcrire une page ne partagent ni l'appel ni la sortie.
    ("mlx-vlm", "image-to-text"): "ecurie_runtime.workers.mlx_vlm_describe",
    # Troisième emploi des mêmes poids. Décrire une vidéo n'est pas décrire une
    # suite d'images : le travail de l'adaptateur est de décider ce que le modèle
    # verra — cadence, budget d'images, chemin vidéo natif ou repli sur des
    # images fixes —, et cette décision n'a pas d'équivalent côté image.
    ("mlx-vlm", "video-to-text"): "ecurie_runtime.workers.mlx_vlm_video",
    # Quatrième emploi, et le seul des quatre à rendre une structure plutôt
    # qu'un texte : convertir les millièmes du modèle en pixels et tracer les
    # boîtes est tout le travail, et il n'a rien à faire dans la description.
    ("mlx-vlm", "image-detect"): "ecurie_runtime.workers.mlx_vlm_detect",
    # Mêmes poids que la génération d'image, autre pipeline : retoucher une zone
    # masquée n'est pas générer, et `AutoPipelineForInpainting` n'est pas
    # `AutoPipelineForText2Image`.
    ("diffusers-mps", "image-inpaint"): "ecurie_runtime.workers.diffusers_inpaint",
    # Troisième emploi des mêmes octets, et le plus simple : transformer une
    # image entière n'a ni masque à fabriquer ni raccord de bord à surveiller.
    # Ce qui le distingue de la retouche tient à `strength`, qui décide ici du
    # nombre de pas réellement exécutés.
    ("diffusers-mps", "image-to-image"): "ecurie_runtime.workers.diffusers_img2img",
    # Un modèle de langue sert trois capacités, et chacune a sa façon de
    # composer l'invite et de lire la réponse : traduire attend du texte,
    # appeler un outil attend du JSON validable.
    ("mlx-lm", "translation"): "ecurie_runtime.workers.mlx_lm_translate",
    ("mlx-lm", "tool-use"): "ecurie_runtime.workers.mlx_lm_tools",
    # Les mêmes trois capacités, servies par le moteur de mlx-vlm. Un modèle
    # vision-langage est d'abord un modèle de langue, et les familles récentes
    # n'arrivent plus que sous cette forme : Qwen3.6 écrit, traduit et appelle
    # des outils, mais son architecture `qwen3_5` n'est pas chargeable par
    # mlx-lm. Sans ces trois lignes, le parc voyait un modèle capable de trois
    # contrats de plus et n'avait aucun moyen de les lui demander.
    #
    # Les adaptateurs correspondants n'ajoutent presque rien : ils héritent des
    # trois du dessus et n'en changent que le moteur (voir `mlx_vlm_lm`).
    # Cinquième emploi de mlx-vlm, et le seul qui ne soit pas un modèle de
    # langue : SAM 3 segmente ce qu'on lui nomme. La capacité est déjà servie par
    # `torch-vision` avec SAM 2, qui suit un clic — deux façons de désigner, deux
    # runtimes, un seul contrat.
    ("mlx-vlm", "image-segment"): "ecurie_runtime.workers.sam3",
    ("mlx-vlm", "text-generation"): "ecurie_runtime.workers.mlx_vlm_text",
    ("mlx-vlm", "translation"): "ecurie_runtime.workers.mlx_vlm_translate",
    ("mlx-vlm", "tool-use"): "ecurie_runtime.workers.mlx_vlm_tools",
    ("torch-vision", "image-matting"): "ecurie_runtime.workers.birefnet",
    ("torch-vision", "image-upscale"): "ecurie_runtime.workers.swin2sr",
    # Troisième adaptateur du même runtime, et la même leçon : détourer décide
    # seul de ce qui est au premier plan, segmenter suit ce qu'on montre.
    ("torch-vision", "image-segment"): "ecurie_runtime.workers.sam2",
    # Premier worker du parc qui ne passe ni par MLX ni par torch : onnxruntime
    # est un troisième moteur d'inférence, et son isolement dans son propre env
    # est la raison d'être du §5.3.
    ("rtmlib", "video-to-motion"): "ecurie_runtime.workers.rtmw3d",
}


def worker_module(runtime: str, capability: str | None) -> str | None:
    """L'adaptateur d'un couple (runtime, capacité), ou du runtime à défaut."""
    if capability is not None:
        spécifique = WORKER_MODULES_BY_CAPABILITY.get((runtime, capability))
        if spécifique is not None:
            return spécifique
    return WORKER_MODULES.get(runtime)

# Ce que le v0.3 ne livre pas, et ce qu'il faut en penser (CONCEPTION.md §13.1).
NOT_YET = {
    "ollama": "proxy HTTP vers le serveur Ollama — repoussé, le parc initial n'en dépend pas",
    "comfy": "proxy HTTP vers ComfyUI — repoussé, le parc initial n'en dépend pas",
    "llama-cpp": "GGUF — passerait par Ollama ou LM Studio, hors périmètre v0.3",
    "torch-vision": (
        "sert image-matting et image-upscale, et rien d'autre : ces deux capacités "
        "ont chacune leur adaptateur, il n'y a pas d'appel commun à une famille "
        "de modèles de vision"
    ),
}


class EnvError(RuntimeError):
    """Environnement d'exécution inutilisable — le message porte la réparation."""


@dataclass(frozen=True)
class RuntimeEnv:
    name: str
    root: Path  # runtimes/<name>

    @property
    def pyproject(self) -> Path:
        return self.root / "pyproject.toml"

    @property
    def venv(self) -> Path:
        return self.root / ".venv"

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def declared(self) -> bool:
        return self.pyproject.is_file()

    @property
    def synced(self) -> bool:
        return self.python.is_file()

    @property
    def status(self) -> str:
        if not self.declared:
            return "non déclaré"
        return "prêt" if self.synced else "à synchroniser"

    @property
    def readme(self) -> Path | None:
        """Notice de l'environnement, quand il en a une.

        `uv sync` ne couvre pas tout : le runtime `hunyuan3d` demande en plus de
        copier à la main du code amont qui n'est publié sur aucun index. Annoncer
        « prêt » sans le dire enverrait l'utilisateur découvrir le manque au
        premier job, plusieurs gigaoctets de téléchargement plus tard.
        """
        chemin = self.root / "README.md"
        return chemin if chemin.is_file() else None

    @property
    def repair_hint(self) -> str:
        if not self.declared:
            return f"créer runtimes/{self.name}/pyproject.toml (voir CONCEPTION.md §5.3)"
        return f"ecurie env sync {self.name}"


def env_for(repo_root: Path, name: str) -> RuntimeEnv:
    return RuntimeEnv(name=name, root=repo_root / RUNTIMES_DIR / name)


def list_envs(repo_root: Path) -> list[RuntimeEnv]:
    base = repo_root / RUNTIMES_DIR
    if not base.is_dir():
        return []
    return [
        RuntimeEnv(name=child.name, root=child)
        for child in sorted(base.iterdir())
        if child.is_dir() and not child.name.startswith(".")
    ]


def sync_env(
    env: RuntimeEnv, *, repo_root: Path, uv_bin: str = "uv", timeout_s: float = 1800
) -> subprocess.CompletedProcess:
    """`uv sync` dans un env isolé. Réseau requis, donc jamais appelé par un test."""
    if not env.declared:
        raise EnvError(f"runtimes/{env.name}/pyproject.toml absent — {env.repair_hint}")
    return subprocess.run(
        sync_command(env, uv_bin=uv_bin),
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def sync_command(env: RuntimeEnv, *, uv_bin: str = "uv") -> list[str]:
    return [uv_bin, "sync", "--project", str(env.root)]


# --- lancement d'un worker ---------------------------------------------------


@dataclass(frozen=True)
class WorkerSpec:
    """Tout ce qu'il faut pour lancer un worker, sans rien deviner à l'exécution."""

    argv: list[str]
    env_vars: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    label: str = "worker"

    def with_args(self, *extra: str) -> "WorkerSpec":
        return WorkerSpec(
            argv=[*self.argv, *extra], env_vars=self.env_vars, cwd=self.cwd, label=self.label
        )


def runtime_src_dir() -> Path:
    """Le dossier `src/` qui contient `ecurie_runtime`, pour le PYTHONPATH du worker.

    Le venv d'un runtime n'a pas Écurie installé, et ne doit surtout pas l'avoir :
    on lui rend visibles les trois modules en bibliothèque standard pure dont le
    worker a besoin, et rien d'autre.
    """
    import ecurie_runtime

    return Path(ecurie_runtime.__file__).resolve().parent.parent


def spec_for_variant(
    repo_root: Path,
    variant: Variant,
    *,
    ref: str,
    capability: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> WorkerSpec:
    """Commande de lancement du worker d'un variant, ou `EnvError` motivée."""
    env = env_for(repo_root, variant.env_name)
    if not env.declared:
        raise EnvError(
            f"{ref} : environnement runtimes/{env.name}/ absent — {env.repair_hint}"
        )
    if not env.synced:
        raise EnvError(f"{ref} : venv de runtimes/{env.name}/ absent — {env.repair_hint}")

    if variant.runtime == "custom":
        if not variant.entrypoint:
            raise EnvError(f"{ref} : runtime custom sans entrypoint dans le manifeste")
        entrypoint = (repo_root / variant.entrypoint).resolve()
        # Un manifeste est une donnée, et les agents de veille en proposent par
        # pull request. Un `entrypoint` absolu ou remontant par `..` ferait
        # exécuter un script que la revue du dépôt n'a jamais vu : le chemin doit
        # rester sous la racine, où il est versionné et relu.
        if not entrypoint.is_relative_to(repo_root.resolve()):
            raise EnvError(
                f"{ref} : entrypoint hors du dépôt ({variant.entrypoint}) — "
                "un point d'entrée s'écrit en chemin relatif, et il est versionné"
            )
        if not entrypoint.is_file():
            raise EnvError(f"{ref} : entrypoint introuvable — {variant.entrypoint}")
        argv = [str(env.python), str(entrypoint)]
    else:
        module = worker_module(variant.runtime, capability)
        if module is None:
            raison = NOT_YET.get(variant.runtime, "adaptateur non livré")
            raise EnvError(f"{ref} : runtime {variant.runtime!r} — {raison}")
        argv = [str(env.python), "-m", module]

    env_vars = {
        "PYTHONPATH": _prepend_pythonpath(str(runtime_src_dir())),
        "PYTHONUNBUFFERED": "1",
        # Un worker ne télécharge jamais : `ecurie pull` est le seul chemin vers
        # le réseau, et il est explicite. Sans cette barrière, un `from_pretrained`
        # ferait apparaître des gigaoctets hors de toute comptabilité disque.
        "HF_HUB_OFFLINE": "1",
        **(extra_env or {}),
    }
    return WorkerSpec(argv=argv, env_vars=env_vars, cwd=repo_root, label=ref)


def _prepend_pythonpath(path: str) -> str:
    existing = os.environ.get("PYTHONPATH")
    return f"{path}{os.pathsep}{existing}" if existing else path


def check_envs(repo_root: Path, variants: Sequence[tuple[str, Variant]]) -> list[str]:
    """Problèmes d'environnement pour un ensemble de variants — pour `ecurie env`."""
    problèmes = []
    for ref, variant in variants:
        env = env_for(repo_root, variant.env_name)
        if not env.declared:
            problèmes.append(f"{ref} : runtimes/{env.name}/pyproject.toml absent")
        elif not env.synced:
            problèmes.append(f"{ref} : {env.repair_hint}")
    return problèmes
