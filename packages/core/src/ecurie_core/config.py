"""Configuration machine (~/.ecurie/config.toml).

État local à la machine, jamais versionné : chemins des gestionnaires scannés
et budget mémoire. Générée avec autodétection au premier lancement, puis
modifiable librement — un chemin absent au scan est ignoré, pas une erreur.

`ECURIE_HOME` remplace ~/.ecurie (utilisé par les tests).
"""

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONFIG_NAME = "config.toml"

_AUTODETECT = {
    "hf_hub": Path("~/.cache/huggingface/hub"),
    "ollama": Path("~/.ollama/models"),
    "lmstudio": Path("~/.lmstudio/models"),
}


def ecurie_home() -> Path:
    return Path(os.environ.get("ECURIE_HOME", str(Path.home() / ".ecurie"))).expanduser()


class ScanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hf_hub: Path | None = None
    ollama: Path | None = None
    lmstudio: Path | None = None
    comfy: list[Path] = Field(default_factory=list)
    declared: list[Path] = Field(default_factory=list)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_budget: Literal["auto"] | int = "auto"
    tier_volumes: list[Path] = Field(default_factory=list)
    scan: ScanConfig = Field(default_factory=ScanConfig)

    # --- exécution (v0.3) ----------------------------------------------------
    # La politique du §7 de l'architecture, en réglages : un seul modèle lourd
    # résident à la fois, les légers restent chauds.
    #
    # Le seuil est à 8 Gio, et non aux 6 Go que l'architecture avait avancés
    # avant toute mesure. Les quatre profils relevés sur la machine de référence
    # tranchent : voix 7,65 Gio, lecture de document 6,25, image 15,95, musique
    # 13,8 à 23,9 selon la durée demandée. À 6 Go, les quatre sont « lourds »,
    # donc aucun ne cohabite jamais — la politique ne distingue plus rien. À
    # 8 Gio, la voix et la lecture de document restent chaudes ensemble (13,9 Gio
    # sur les 17,76 disponibles) et seules l'image et la musique se disputent la
    # place, ce qui est exactement ce que la règle veut dire.
    max_heavy_resident: int = Field(default=1, ge=0)
    heavy_threshold_bytes: int = Field(default=8 * (1 << 30), ge=0)
    # Un worker résident oublié garderait sa mémoire indéfiniment : il se retire
    # de lui-même après ce délai sans travail. 0 = jamais.
    resident_idle_timeout_s: int = Field(default=1800, ge=0)
    # Garde de disque de `ecurie pull` : refuser un téléchargement qui ferait
    # passer l'espace libre sous cette part du volume.
    free_disk_ratio: float = Field(default=0.15, ge=0.0, le=0.9)

    @property
    def state_db(self) -> Path:
        return ecurie_home() / "state.db"

    @property
    def outputs_dir(self) -> Path:
        """Sorties des jobs — un dossier par job, avec son manifeste (CONCEPTION.md §6)."""
        return ecurie_home() / "outputs"

    @property
    def uploads_dir(self) -> Path:
        """Fichiers déposés par l'UI — image choisie, capture de la caméra ou du micro.

        Ils ne sont pas des sorties et n'ont rien à faire dans `outputs/` : ce
        sont des **entrées** dont le navigateur ne peut pas donner le chemin
        réel, et que le serveur écrit donc lui-même pour en fabriquer un. Une
        fois le job lancé, `runner.stage_inputs` en copie une deuxième fois le
        contenu dans le dossier du job, qui est ce qui fait foi : ce dossier-ci
        est un sas, pas une bibliothèque, et il se purge (voir
        `ecurie_api.uploads`).
        """
        return ecurie_home() / "uploads"

    @property
    def trash_dir(self) -> Path:
        return ecurie_home() / "trash"

    @property
    def plans_dir(self) -> Path:
        return ecurie_home() / "plans"


def autodetect_scan() -> ScanConfig:
    found = {
        key: path for key, default in _AUTODETECT.items() if (path := default.expanduser()).is_dir()
    }
    return ScanConfig(**found)


def load_config() -> Config:
    """Charge la config, en la générant au premier lancement."""
    path = ecurie_home() / CONFIG_NAME
    if not path.is_file():
        config = Config(scan=autodetect_scan())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_config(config))
        return config
    return Config.model_validate(tomllib.loads(path.read_text()))


def render_config(config: Config) -> str:
    def toml_str(p: Path) -> str:
        return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'

    def toml_list(paths: list[Path]) -> str:
        return "[" + ", ".join(toml_str(p) for p in paths) + "]"

    lines = [
        "# Configuration machine Écurie — générée au premier lancement, modifiable.",
        "# Un chemin absent au scan est ignoré. Voir CONCEPTION.md §3.",
        "",
        '# "auto" = recommendedMaxWorkingSetSize de Metal ; sinon un nombre d\'octets.',
        f"memory_budget = {config.memory_budget!r}"
        if isinstance(config.memory_budget, str)
        else f"memory_budget = {config.memory_budget}",
        "",
        "# Volumes externes autorisés pour le tiering (`ecurie store tier`). Ils sont",
        "# aussi scannés : un volume démonté marque ses variants « froid indisponible ».",
        f"tier_volumes = {toml_list(config.tier_volumes)}",
        "",
        "# Exécution : politique de résidence mémoire (ARCHITECTURE.md §7).",
        "# Un seul modèle lourd résident à la fois ; les légers restent chauds.",
        f"max_heavy_resident = {config.max_heavy_resident}",
        f"heavy_threshold_bytes = {config.heavy_threshold_bytes}",
        "# Un worker résident se retire seul après ce temps sans travail (0 = jamais).",
        f"resident_idle_timeout_s = {config.resident_idle_timeout_s}",
        "# `ecurie pull` refuse un téléchargement qui ferait passer l'espace libre",
        "# du volume sous cette part.",
        f"free_disk_ratio = {config.free_disk_ratio}",
        "",
        "[scan]",
    ]
    for key in ("hf_hub", "ollama", "lmstudio"):
        value: Path | None = getattr(config.scan, key)
        if value is not None:
            lines.append(f"{key} = {toml_str(value)}")
        else:
            lines.append(f"# {key} : non détecté")
    lines.append(f"comfy = {toml_list(config.scan.comfy)}")
    lines.append(f"declared = {toml_list(config.scan.declared)}")
    lines.append("")
    return "\n".join(lines)
