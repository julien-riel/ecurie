"""Registre des workers résidents — l'état partagé entre deux commandes de la CLI.

Un worker survit à la commande qui l'a lancé (c'est tout l'intérêt : ne pas
repayer le warmup à chaque phrase). Il faut donc un endroit où la commande
suivante retrouve ce qui est déjà chaud, et combien de mémoire c'est censé
occuper : `~/.ecurie/residents.json`, protégé par un verrou de fichier.

C'est de l'état **observé**, au sens du §1.1 de la conception : reconstructible,
jamais versionné, et toujours vérifié avant d'être cru. Une entrée dont le
processus est mort ou dont le socket a disparu est retirée à la lecture — sans
quoi le budget mémoire serait grevé par des fantômes, et le contrôle d'admission
refuserait des jobs pour faire de la place à des workers qui n'existent plus.

Les sockets ne vivent **pas** dans `~/.ecurie` : `sun_path` est limité à 104
octets sur macOS, et un dossier temporaire de test ou un répertoire personnel un
peu profond suffit à dépasser la limite. La panne qui en résulte est
particulièrement pénible — le worker meurt au `bind`, le superviseur attend un
socket qui n'arrivera jamais, et rien dans le message ne parle de longueur de
chemin. D'où un répertoire court, dédié, et un nom haché à taille fixe.
"""

import fcntl
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecurie_runtime.admission import Resident
from ecurie_runtime.worker import pid_alive

VERSION = 1


@dataclass
class ResidentEntry:
    ref: str
    pid: int
    socket: str
    peak_bytes: int
    runtime: str = ""
    env: str = ""
    loaded_at: str = ""
    last_used: float = 0.0
    pinned: bool = False
    warmup_ms: int = 0
    options: dict[str, Any] = field(default_factory=dict)
    log: str = ""
    # Empreinte du manifeste résolu avec lequel ce worker a été chargé. Le
    # manifeste peut changer sous nos pieds — une révision réépinglée, des poids
    # retéléchargés ailleurs — et le worker, lui, garde en mémoire ce qu'il a
    # chargé. Réutiliser sans comparer ferait écrire au manifeste du job une
    # révision qui n'est pas celle qui a produit la sortie.
    document: str = ""
    # Processus qui tient le worker pour un job. On retient le pid plutôt qu'un
    # simple drapeau : une commande tuée en plein job ne rend rien, et un drapeau
    # posé pour toujours rendrait le résident inévinçable jusqu'au prochain
    # redémarrage. Un pid, lui, se vérifie.
    busy_by: int = 0
    busy_since: float = 0.0

    @property
    def busy(self) -> bool:
        return bool(self.busy_by) and pid_alive(self.busy_by)

    def as_resident(self) -> Resident:
        return Resident(
            ref=self.ref,
            peak_bytes=self.peak_bytes,
            last_used=self.last_used,
            pinned=self.pinned,
            busy=self.busy,
        )

    @property
    def alive(self) -> bool:
        return pid_alive(self.pid) and Path(self.socket).exists()


SUN_PATH_MAX = 104  # macOS ; Linux est à 108. On se cale sur le plus strict.


def workers_dir(home: Path) -> Path:
    return home / "workers"


def socket_dir() -> Path:
    """Répertoire des sockets : court, privé, propre à l'utilisateur.

    `ECURIE_SOCKET_DIR` permet de le déplacer — utile si `/tmp` est monté avec
    des restrictions, et pour isoler deux exécutions de tests.
    """
    forcé = os.environ.get("ECURIE_SOCKET_DIR")
    base = Path(forcé) if forcé else Path(tempfile.gettempdir()) / f"ecurie-{os.getuid()}"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


def socket_path(home: Path, ref: str) -> Path:
    """Nom stable et court : le même variant, sous la même racine, reprend son socket."""
    empreinte = hashlib.sha256(f"{home}\0{ref}".encode()).hexdigest()[:12]
    chemin = socket_dir() / f"w-{empreinte}.sock"
    if len(str(chemin).encode()) >= SUN_PATH_MAX:
        raise OSError(
            f"chemin de socket trop long ({len(str(chemin))} octets, maximum "
            f"{SUN_PATH_MAX}) : {chemin} — déplacer avec ECURIE_SOCKET_DIR"
        )
    return chemin


def log_path(home: Path, ref: str) -> Path:
    sûr = "".join(c if c.isalnum() or c in "-_.@" else "_" for c in ref)
    return workers_dir(home) / f"{sûr}.log"


class ResidentRegistry:
    """Accès concurrent sûr au registre des résidents.

    Toute modification passe par `locked()`, qui tient un verrou exclusif du
    début à la fin : deux `ecurie run` lancés en même temps ne doivent pas
    décider chacun de leur côté qu'il y a la place pour un modèle de neuf
    gigaoctets.
    """

    def __init__(self, home: Path) -> None:
        self.home = home
        self.path = home / "residents.json"
        self.lock_path = home / "residents.lock"

    # --- lecture -------------------------------------------------------------

    def read(self) -> dict[str, ResidentEntry]:
        """Résidents réellement vivants. Ne réécrit rien : la lecture est sans effet."""
        return {ref: e for ref, e in self._read_raw().items() if e.alive}

    def stale(self) -> list[ResidentEntry]:
        return [e for e in self._read_raw().values() if not e.alive]

    def _read_raw(self) -> dict[str, ResidentEntry]:
        if not self.path.is_file():
            return {}
        try:
            doc = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(doc, dict):
            # Un JSON valide mais d'une autre forme reste un état observé
            # illisible : il se reconstruit au prochain chargement. Laisser
            # remonter l'AttributeError bloquerait toute la CLI, y compris les
            # commandes qui n'ont rien à voir avec les résidents.
            return {}
        entries = {}
        for ref, raw in (doc.get("residents") or {}).items():
            try:
                entries[ref] = ResidentEntry(**{"ref": ref, **raw})
            except TypeError:
                continue  # entrée d'une version antérieure : ignorée, pas fatale
        return entries

    # --- écriture sous verrou ------------------------------------------------

    @contextmanager
    def locked(self) -> Iterator[dict[str, ResidentEntry]]:
        """Ouvre une transaction : le dictionnaire rendu est réécrit à la sortie."""
        self.home.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "w") as verrou:
            fcntl.flock(verrou, fcntl.LOCK_EX)
            try:
                entries = {ref: e for ref, e in self._read_raw().items() if e.alive}
                yield entries
                self._write(entries)
            finally:
                fcntl.flock(verrou, fcntl.LOCK_UN)

    def _write(self, entries: dict[str, ResidentEntry]) -> None:
        payload = {
            "version": VERSION,
            "written_at": datetime.now(UTC).isoformat(),
            "residents": {
                ref: {k: v for k, v in asdict(e).items() if k != "ref"}
                for ref, e in entries.items()
            },
        }
        temporaire = self.path.with_suffix(".json.tmp")
        temporaire.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(temporaire, self.path)

    # --- opérations courantes ------------------------------------------------

    def touch(self, ref: str) -> None:
        with self.locked() as entries:
            if ref in entries:
                entries[ref].last_used = time.time()

    def forget(self, ref: str) -> ResidentEntry | None:
        with self.locked() as entries:
            return entries.pop(ref, None)

    def set_pinned(self, ref: str, pinned: bool) -> bool:
        with self.locked() as entries:
            if ref not in entries:
                return False
            entries[ref].pinned = pinned
            return True
