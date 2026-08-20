"""Parcours générique d'une arborescence : LM Studio, ComfyUI, chemins déclarés,
volumes de tiering.

Aucun hash annoncé ici — ces gestionnaires stockent des fichiers pleins sans
adressage par contenu. L'identité de contenu vient de `ecurie store verify`,
dont le résultat est réinjecté au scan suivant par le cache de hachage."""

from pathlib import Path

from ecurie_store.db import LocationRecord
from ecurie_store.scanners.common import stat_record


def scan_tree(root: Path, manager: str) -> list[LocationRecord]:
    records: list[LocationRecord] = []
    for path in sorted(root.rglob("*")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if not path.is_symlink() and not path.is_file():
            continue
        records.append(stat_record(path, manager, root=str(root)))
    return records
