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
    scan: ScanConfig = Field(default_factory=ScanConfig)

    @property
    def state_db(self) -> Path:
        return ecurie_home() / "state.db"


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
