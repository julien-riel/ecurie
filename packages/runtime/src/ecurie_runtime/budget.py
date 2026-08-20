"""Budget de mémoire unifiée (ARCHITECTURE.md §7).

Sur Apple Silicon, la valeur qui compte n'est pas la mémoire installée mais
`recommendedMaxWorkingSetSize` de Metal — environ 75 % de la mémoire unifiée.
C'est elle que MLX expose, et c'est le seul chiffre qui prédit un swap.

Quatre sources, de la plus fiable à la plus grossière. Celle qui a servi est
toujours rapportée avec la valeur : un budget de 17,8 Gio lu dans Metal et un
budget de 18 Gio déduit d'une règle de trois ne méritent pas la même confiance,
et l'utilisateur doit pouvoir faire la différence dans `ecurie ps`.

MLX n'est pas — et ne doit pas être — installé dans l'environnement d'Écurie
(CONCEPTION.md §5.3). On l'interroge donc là où il est légitimement présent : le
venv d'un runtime.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ecurie_core.config import Config

from ecurie_runtime.envs import list_envs

GIB = 1 << 30
FRACTION_GPU = 0.75  # part de la mémoire unifiée que macOS réserve au GPU

# `mx.metal.device_info` est déprécié au profit de `mx.device_info` depuis mlx
# 0.32 ; l'ancien nom reste servi pour les versions antérieures. On essaie les
# deux plutôt que d'épingler une version de mlx dans un env qu'Écurie ne
# contrôle pas.
_SNIPPET = (
    "import json,mlx.core as mx;"
    "info=(mx.device_info() if hasattr(mx,'device_info') else mx.metal.device_info());"
    "print(json.dumps(info['max_recommended_working_set_size']))"
)


@dataclass(frozen=True)
class Budget:
    bytes: int
    source: str

    @property
    def measured(self) -> bool:
        """Vrai si la valeur vient de Metal, faux si elle est déduite."""
        return "metal" in self.source


def detect_budget(config: Config, *, repo_root: Path | None = None) -> Budget:
    if isinstance(config.memory_budget, int):
        return Budget(config.memory_budget, "config (~/.ecurie/config.toml)")

    valeur = _from_local_mlx()
    if valeur:
        return Budget(valeur, "metal, via le mlx de l'environnement d'Écurie")

    if repo_root is not None:
        for env in list_envs(repo_root):
            if not env.synced:
                continue
            valeur = _from_env_mlx(env.python)
            if valeur:
                return Budget(valeur, f"metal, via le mlx de runtimes/{env.name}")

    total = physical_memory_bytes()
    if total:
        return Budget(
            int(total * FRACTION_GPU),
            f"déduit : {FRACTION_GPU:.0%} de la mémoire physique (mlx introuvable)",
        )
    return Budget(8 * GIB, "défaut prudent : mémoire physique illisible")


def _from_local_mlx() -> int | None:
    try:
        import mlx.core as mx  # noqa: PLC0415 — sonde optionnelle, absente par construction
    except Exception:  # noqa: BLE001 — mlx absent ou cassé : ce n'est pas une erreur ici
        return None
    metal = getattr(mx, "metal", None)
    for sonde in (getattr(mx, "device_info", None), getattr(metal, "device_info", None)):
        if sonde is None:
            continue
        try:
            return int(sonde()["max_recommended_working_set_size"])
        except Exception:  # noqa: BLE001 — sonde absente ou renommée : on essaie la suivante
            continue
    return None


def _from_env_mlx(python: Path) -> int | None:
    try:
        out = subprocess.run(
            [str(python), "-c", _SNIPPET],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return int(json.loads(out.stdout.strip()))
    except (ValueError, json.JSONDecodeError):
        return None


def physical_memory_bytes() -> int | None:
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=False
            )
        except OSError:
            return None
        valeur = out.stdout.strip()
        return int(valeur) if valeur.isdigit() else None
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError):
        return None
