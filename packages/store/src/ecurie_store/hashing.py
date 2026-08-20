"""Hachage niveau 3 : sha256 complet, à la demande, mis en cache (CONCEPTION.md §1.2).

Le hash annoncé par un gestionnaire (nom de blob HF ou Ollama) suffit à la
comptabilité, jamais à une opération destructive. Tout ce qui hache dans Écurie
passe par ici — le scan, lui, ne fait que du `stat`.

Le cache est indexé par `(path, size, mtime, inode)` : le moindre changement de
l'un des quatre invalide l'entrée. Un hash n'y entre que s'il a été calculé en
lisant le fichier — jamais un hash annoncé, dont c'est justement l'objet.
"""

import hashlib
import os
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ecurie_store.db import LocationRecord, StateDB

CHUNK_BYTES = 4 * 1024 * 1024


def sha256_file(path: str | Path, *, chunk_bytes: int = CHUNK_BYTES) -> str:
    """sha256 du contenu, en flux — jamais le fichier entier en mémoire."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk_bytes):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class VerifyResult:
    path: str
    sha256: str | None
    announced: str | None  # ce que le gestionnaire prétendait
    from_cache: bool = False
    changed: bool = False  # (size, mtime, inode) ≠ état observé au scan
    missing: bool = False
    error: str | None = None

    @property
    def mismatch(self) -> bool:
        return bool(self.announced and self.sha256 and self.announced != self.sha256)

    @property
    def ok(self) -> bool:
        return self.sha256 is not None and not self.mismatch and not self.changed


def stat_of(path: str | Path) -> os.stat_result | None:
    try:
        return os.stat(path)
    except OSError:
        return None


def announced_sha256(rec: LocationRecord) -> str | None:
    """Ce que le gestionnaire prétendait, même après qu'on ait écrit le hash réel."""
    annoncé = rec.meta.get("announced_sha256")
    if isinstance(annoncé, str):
        return annoncé
    return rec.sha256 if rec.meta.get("hash_source") == "announced" else None


def verify_location(
    db: StateDB, rec: LocationRecord, *, force: bool = False, persist: bool = True
) -> VerifyResult:
    """Calcule (ou relit du cache) le sha256 d'une Location et le persiste."""
    announced = announced_sha256(rec)

    if rec.link_kind == "symlink":
        return VerifyResult(
            path=rec.path,
            sha256=None,
            announced=announced,
            error="lien symbolique : rien à hacher ici",
        )

    st = stat_of(rec.path)
    if st is None:
        return VerifyResult(rec.path, None, announced, missing=True, error="fichier disparu")

    changed = not (
        st.st_size == rec.size and st.st_mtime == rec.mtime and st.st_ino == rec.inode
    )

    if not force:
        cached = db.hash_cache_get(rec.path, st.st_size, st.st_mtime, st.st_ino)
        if cached:
            _adopte(db, rec, cached, persist)
            return VerifyResult(rec.path, cached, announced, from_cache=True, changed=changed)

    try:
        digest = sha256_file(rec.path)
    except OSError as exc:
        return VerifyResult(rec.path, None, announced, changed=changed, error=str(exc))

    now = datetime.now(UTC).isoformat()
    if persist:
        db.hash_cache_put(rec.path, st.st_size, st.st_mtime, st.st_ino, digest, verified_at=now)
    _adopte(db, rec, digest, persist)
    return VerifyResult(rec.path, digest, announced, changed=changed)


def _adopte(db: StateDB, rec: LocationRecord, digest: str, persist: bool) -> None:
    """Le contenu a été lu : la Location le sait, en base comme en mémoire.

    Marquer « vérifié » même quand le hash coïncide avec l'annoncé n'est pas
    cosmétique — sans cette marque, `store verify` ne converge jamais et le plan
    prudent (`--verified-only`) refuse éternellement de dédupliquer un fichier
    dont le contenu a pourtant bien été relu.
    """
    rec.sha256 = digest
    rec.meta["hash_source"] = "verified"
    if persist:
        db.set_location_hash(rec.path, digest)


def verify_locations(
    db: StateDB,
    records: Iterable[LocationRecord],
    *,
    force: bool = False,
    on_result: Callable[[VerifyResult], None] | None = None,
) -> list[VerifyResult]:
    results = []
    for rec in records:
        result = verify_location(db, rec, force=force)
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


def dedup_candidates(records: Iterable[LocationRecord]) -> list[LocationRecord]:
    """Fichiers sans sha256 dont la taille exacte coïncide avec un autre fichier.

    Le pré-filtre est la taille en octets (CONCEPTION.md §1.2) : deux fichiers de
    tailles différentes ne sont jamais comparés, donc jamais lus. Un candidat dont
    le jumeau a déjà un hash annoncé compte aussi : c'est ainsi qu'un `.gguf` de LM
    Studio se révèle être le blob Ollama d'à côté.
    """
    par_taille: dict[int, list[LocationRecord]] = defaultdict(list)
    for rec in records:
        if rec.link_kind == "symlink" or rec.size == 0:
            continue
        par_taille[rec.size].append(rec)

    candidats: list[LocationRecord] = []
    for group in par_taille.values():
        if len(group) < 2:
            continue
        inodes = {(r.device, r.inode) for r in group}
        if len(inodes) < 2:  # déjà un seul et même fichier, rien à départager
            continue
        candidats.extend(r for r in group if r.sha256 is None)
    return sorted(candidats, key=lambda r: (-r.size, r.path))


def unverified(records: Iterable[LocationRecord]) -> list[LocationRecord]:
    return [
        r
        for r in records
        if r.link_kind != "symlink" and r.meta.get("hash_source") != "verified"
    ]
