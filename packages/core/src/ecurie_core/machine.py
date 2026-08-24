"""Identité de la machine locale — ce qui distingue un relevé d'un autre.

Un profil mesuré ne vaut pas partout de la même façon. `disk_bytes` et
`peak_unified_memory_bytes` se transportent d'un Mac à l'autre : ce sont les
poids et les activations, et ils ne dépendent pas de qui les charge. `warmup_ms`,
`latency_ms_p50`, `throughput` et toute pente de `peak_scaling` ajustée sous un
budget donné, non — ils décrivent la machine autant que le modèle.

C'est la raison d'être de ce module : `registry/measurements/` range désormais un
fichier **par machine**, et il faut savoir nommer la machine. Deux formes, pour
deux usages :

- `machine_id()` — « Mac17,4 24 Gio », le matériel seul, lisible ;
- `machine_slug()` — la même identité réduite à un nom de fichier stable ;
- `describe_machine()` — l'identité complète, matériel, système et versions des
  bibliothèques, telle qu'elle est inscrite dans `measured_on`.

Le slug ne retient que le matériel, sans le système ni les versions : une mise à
jour de macOS ou de mlx ne doit pas créer un second fichier pour la même machine,
elle doit remplacer le relevé qui s'y trouve. C'est `measured_on`, à l'intérieur
du fichier, qui dit sous quelles versions la mesure a été prise.
"""

import platform
import re
import subprocess
from typing import Any

MACHINE_INCONNUE = "machine-inconnue"


def machine_id(hardware: str | None = None) -> str:
    """« Mac17,4 24 Gio » — le matériel, sans le système ni les bibliothèques."""
    if hardware is not None:
        return hardware
    modèle = _sysctl("hw.model") or platform.machine() or MACHINE_INCONNUE
    mémoire = _sysctl("hw.memsize")
    if mémoire and mémoire.isdigit():
        return f"{modèle} {int(mémoire) / (1 << 30):.0f} Gio"
    return modèle


def machine_slug(identité: str | None = None) -> str:
    """`mac17-4-24gio` — l'identité matérielle réduite à un nom de fichier.

    Tout ce qui n'est ni lettre ni chiffre devient un tiret : `Mac17,4` porte une
    virgule, et un nom de fichier qui en contient survit mal aux outils qui le
    liront. Le résultat est committé, donc il doit être stable — c'est pourquoi
    il se dérive du matériel seul, jamais de l'horloge ni du chemin courant.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (identité or machine_id()).lower()).strip("-")
    return slug or MACHINE_INCONNUE


def describe_machine(versions: dict[str, Any] | None = None) -> str:
    """« Mac17,4 24 Gio / macOS 26.5.2 / mlx 0.32.1 » — un profil sans contexte
    est ininterprétable, et c'est ce champ qui dit sur quoi il vaut."""
    morceaux = [machine_id()]
    if platform.system() == "Darwin":
        version = platform.mac_ver()[0]
        morceaux.append(f"macOS {version}" if version else "macOS")
    else:
        morceaux.append(f"{platform.system()} {platform.release()}")
    for nom, valeur in sorted((versions or {}).items()):
        morceaux.append(f"{nom} {valeur}")
    return " / ".join(morceaux)


def hardware_of(measured_on: str) -> str:
    """Le segment matériel d'un `measured_on`, pour retrouver le nom du fichier.

    `write_measurement` nomme le fichier d'après le rapport qu'il écrit, et non
    d'après la machine qui l'exécute : les deux coïncident en pratique, mais
    faire dépendre un chemin d'un `sysctl` rendrait la fonction inéprouvable.
    """
    return measured_on.split(" / ")[0].strip() or MACHINE_INCONNUE


def _sysctl(clé: str) -> str | None:
    try:
        out = subprocess.run(["sysctl", "-n", clé], capture_output=True, text=True, check=False)
    except OSError:
        return None
    valeur = out.stdout.strip()
    return valeur or None
