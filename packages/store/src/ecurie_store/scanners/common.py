import os
import re
from pathlib import Path

from ecurie_store.db import LocationRecord

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_or_none(name: str) -> str | None:
    return name if _SHA256_RE.fullmatch(name) else None


def stat_record(path: Path, manager: str, **meta) -> LocationRecord:
    """Observe un chemin. Un lien symbolique n'est jamais suivi : il est enregistré
    pour lui-même (taille nulle, cible et disponibilité en `meta`) — c'est la trace
    laissée par le tiering, et sa cible absente signifie « froid indisponible »."""
    if path.is_symlink():
        return symlink_record(path, manager, **meta)
    st = os.stat(path)
    return LocationRecord(
        path=str(path),
        manager=manager,
        size=st.st_size,
        mtime=st.st_mtime,
        device=st.st_dev,
        inode=st.st_ino,
        link_kind="hardlink" if st.st_nlink > 1 else "plain",
        # Le nombre de liens durs décide si jeter un chemin libère vraiment les
        # octets : un inode tenu par une référence hors du parc scanné ne rend rien.
        meta={**meta, "nlink": st.st_nlink},
    )


def symlink_record(path: Path, manager: str, **meta) -> LocationRecord:
    st = os.lstat(path)
    target = os.readlink(path)
    resolved = Path(os.path.normpath(os.path.join(os.path.dirname(str(path)), target)))
    available = resolved.exists()
    return LocationRecord(
        path=str(path),
        manager=manager,
        size=0,  # un lien symbolique n'occupe pas les octets de sa cible
        mtime=st.st_mtime,
        device=st.st_dev,
        inode=st.st_ino,
        link_kind="symlink",
        meta={**meta, "target": str(resolved), "available": available},
    )


def announce(record: LocationRecord, sha256: str | None) -> LocationRecord:
    """Pose un hash *annoncé* par le gestionnaire (nom de blob) — non vérifié.

    La valeur annoncée est conservée à part : quand la vérification écrira le hash
    réel dans `sha256`, c'est elle qui permettra de dire encore que le fichier ment.
    Une alarme qui s'éteint au second passage ne sert à rien.
    """
    if sha256 is not None:
        record.sha256 = sha256
        record.meta["hash_source"] = "announced"
        record.meta["announced_sha256"] = sha256
    return record
