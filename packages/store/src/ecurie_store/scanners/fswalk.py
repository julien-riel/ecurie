"""Parcours générique d'une arborescence : LM Studio, ComfyUI, chemins déclarés.

Aucun hash annoncé ici — ces gestionnaires stockent des fichiers pleins sans
adressage par contenu. L'identité de contenu viendra de la vérification
sha256 à la demande (v0.2)."""

from pathlib import Path

from ecurie_store.db import LocationRecord
from ecurie_store.scanners.common import stat_record


def scan_tree(root: Path, manager: str) -> list[LocationRecord]:
    records: list[LocationRecord] = []
    for path in sorted(root.rglob("*")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        records.append(stat_record(path, manager, root=str(root)))
    return records
