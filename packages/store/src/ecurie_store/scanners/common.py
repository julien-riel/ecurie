import os
import re
from pathlib import Path

from ecurie_store.db import LocationRecord

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_or_none(name: str) -> str | None:
    return name if _SHA256_RE.fullmatch(name) else None


def stat_record(path: Path, manager: str, **meta) -> LocationRecord:
    st = os.stat(path)
    return LocationRecord(
        path=str(path),
        manager=manager,
        size=st.st_size,
        mtime=st.st_mtime,
        device=st.st_dev,
        inode=st.st_ino,
        link_kind="hardlink" if st.st_nlink > 1 else "plain",
        meta=meta,
    )
