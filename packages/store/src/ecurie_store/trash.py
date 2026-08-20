"""Quarantaine : rien ne s'efface, tout se déplace (CONCEPTION.md §1.3).

Aucune action du plan de GC ne supprime. Elle *déménage* vers
`<racine>/<date>/<empreinte>/`, avec un `manifest.json` qui dit d'où le fichier
vient, pourquoi il est parti et sous quel plan — de quoi le remettre en place à
la main d'une seule commande `mv`.

La racine est `~/.ecurie/trash/` pour tout ce qui vit sur le volume de départ.
Un fichier sur un autre volume est mis en quarantaine *sur son propre volume*
(`<point de montage>/.ecurie-trash/`) : sans cela le déplacement serait une
copie de plusieurs gigaoctets, et le coût nul de la quarantaine — son seul
argument — disparaîtrait.

`empty` est le seul endroit de tout le paquet qui a le droit d'effacer.
"""

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecurie_core.config import ecurie_home

MANIFEST_NAME = "manifest.json"


class TrashError(RuntimeError):
    pass


def trash_root(home: Path | None = None) -> Path:
    return (home or ecurie_home()) / "trash"


def mount_point(path: Path) -> Path:
    """Remonte jusqu'au changement de `st_dev` — la racine du volume portant `path`."""
    path = path.resolve() if path.exists() else path.absolute()
    device = os.stat(path if path.exists() else path.parent).st_dev
    current = path if path.is_dir() else path.parent
    while current != current.parent:
        parent = current.parent
        try:
            if os.stat(parent).st_dev != device:
                return current
        except OSError:
            return current
        current = parent
    return current


def trash_root_for(path: Path, home: Path | None = None) -> Path:
    """Racine de quarantaine du volume qui porte `path` : le déplacement reste gratuit."""
    principale = trash_root(home)
    principale.mkdir(parents=True, exist_ok=True)
    try:
        même_volume = os.stat(principale).st_dev == os.lstat(path).st_dev
    except OSError as exc:
        raise TrashError(f"{path} : introuvable ({exc})") from exc
    if même_volume:
        return principale
    return mount_point(path) / ".ecurie-trash"


@dataclass
class TrashEntry:
    directory: Path
    manifest: dict[str, Any]

    @property
    def origin(self) -> str:
        return self.manifest["origin"]

    @property
    def size(self) -> int:
        return int(self.manifest.get("size", 0))

    @property
    def reason(self) -> str:
        return self.manifest.get("reason", "?")

    @property
    def trashed_at(self) -> str:
        return self.manifest.get("trashed_at", "")

    @property
    def payload(self) -> Path:
        return self.directory / self.manifest["name"]

    @property
    def restore_command(self) -> str:
        return f'mv "{self.payload}" "{self.origin}"'


def move_to_trash(
    path: str | Path,
    *,
    reason: str,
    sha256: str | None = None,
    plan_id: str | None = None,
    home: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> TrashEntry:
    """Déplace `path` en quarantaine. Le rename est atomique et gratuit."""
    path = Path(path)
    st = os.lstat(path)  # un lien symbolique part tel quel, on ne suit pas la cible
    racine = trash_root_for(path, home)

    jour = datetime.now(UTC).strftime("%Y-%m-%d")
    empreinte = (sha256 or "")[:12] or f"inode-{st.st_ino}"
    base = racine / jour / empreinte
    directory = base
    suffixe = 1
    while directory.exists():
        directory = base.with_name(f"{base.name}-{suffixe}")
        suffixe += 1
    directory.mkdir(parents=True)

    destination = directory / path.name
    try:
        os.rename(path, destination)
    except OSError as exc:  # même volume garanti par trash_root_for : sinon c'est un bug
        raise TrashError(f"{path} : mise en quarantaine impossible ({exc})") from exc

    manifest = {
        "origin": str(path),
        "name": path.name,
        "reason": reason,
        "sha256": sha256,
        "plan_id": plan_id,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "inode": st.st_ino,
        "link_kind": "symlink" if os.path.islink(destination) else "plain",
        "trashed_at": datetime.now(UTC).isoformat(),
        **(extra or {}),
    }
    (directory / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    return TrashEntry(directory=directory, manifest=manifest)


def list_trash(home: Path | None = None, roots: list[Path] | None = None) -> list[TrashEntry]:
    entries: list[TrashEntry] = []
    for racine in roots or [trash_root(home)]:
        if not racine.is_dir():
            continue
        for manifest_path in sorted(racine.glob(f"*/*/{MANIFEST_NAME}")):
            try:
                manifest = json.loads(manifest_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            entries.append(TrashEntry(directory=manifest_path.parent, manifest=manifest))
    entries.sort(key=lambda e: e.trashed_at, reverse=True)
    return entries


def empty_trash(
    home: Path | None = None,
    older_than_days: int | None = None,
    roots: list[Path] | None = None,
) -> tuple[int, int]:
    """Efface pour de bon. Retourne (entrées supprimées, octets libérés)."""
    limite = (
        datetime.now(UTC) - timedelta(days=older_than_days)
        if older_than_days is not None
        else None
    )
    supprimées = octets = 0
    for entry in list_trash(home, roots):
        if limite is not None:
            try:
                when = datetime.fromisoformat(entry.trashed_at)
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            if when >= limite:
                continue
        taille = _directory_size(entry.directory)
        shutil.rmtree(entry.directory)  # rm-ok: vidage explicite de la quarantaine
        supprimées += 1
        octets += taille
    for racine in roots or [trash_root(home)]:
        _prune_empty_days(racine)
    return supprimées, octets


def _directory_size(directory: Path) -> int:
    total = 0
    for child in directory.rglob("*"):
        if child.is_file() and not child.is_symlink():
            total += child.stat().st_size
    return total


def _prune_empty_days(racine: Path) -> None:
    if not racine.is_dir():
        return
    for jour in sorted(racine.iterdir()):
        if jour.is_dir() and not any(jour.iterdir()):
            jour.rmdir()  # rm-ok: dossier de date vidé, il ne reste rien à préserver
