"""État observé, SQLite (~/.ecurie/state.db). Voir CONCEPTION.md §1.1.

Ce n'est pas une source de vérité : tout est reconstructible par un scan.
Le hash d'une Location est soit annoncé par le gestionnaire (nom de blob),
soit vérifié par lecture du contenu — `hash_cache.verified_at` fait la
différence. En v0.1 seul l'annoncé existe ; la vérification arrive en v0.2.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    sha256     TEXT PRIMARY KEY,
    size       INTEGER NOT NULL,
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS locations (
    path        TEXT PRIMARY KEY,
    manager     TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    device      INTEGER NOT NULL,
    inode       INTEGER NOT NULL,
    link_kind   TEXT NOT NULL,          -- plain | hardlink | symlink
    sha256      TEXT,                   -- annoncé ou vérifié, NULL si inconnu
    variant_ref TEXT,                   -- "model@variant" si résolu au registre
    meta        TEXT NOT NULL DEFAULT '{}',
    seen_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS locations_manager ON locations (manager);
CREATE INDEX IF NOT EXISTS locations_sha256 ON locations (sha256);
CREATE TABLE IF NOT EXISTS hash_cache (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    inode       INTEGER NOT NULL,
    quick_hash  TEXT,
    sha256      TEXT,
    verified_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    variant_ref TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    duration_ms INTEGER,
    job_dir     TEXT,
    ok          INTEGER
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class LocationRecord:
    path: str
    manager: str
    size: int
    mtime: float
    device: int
    inode: int
    link_kind: str
    sha256: str | None = None
    variant_ref: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class StateDB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def replace_manager(self, manager: str, records: list[LocationRecord]) -> None:
        """Remplace l'état observé d'un gestionnaire par le résultat d'un scan."""
        now = datetime.now(UTC).isoformat()
        with self.conn:
            self.conn.execute("DELETE FROM locations WHERE manager = ?", (manager,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO locations"
                " (path, manager, size, mtime, device, inode, link_kind,"
                "  sha256, variant_ref, meta, seen_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r.path,
                        r.manager,
                        r.size,
                        r.mtime,
                        r.device,
                        r.inode,
                        r.link_kind,
                        r.sha256,
                        r.variant_ref,
                        json.dumps(r.meta),
                        now,
                    )
                    for r in records
                ],
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO artifacts (sha256, size, first_seen) VALUES (?, ?, ?)",
                [(r.sha256, r.size, now) for r in records if r.sha256],
            )

    def locations(self) -> list[LocationRecord]:
        rows = self.conn.execute(
            "SELECT path, manager, size, mtime, device, inode, link_kind,"
            " sha256, variant_ref, meta FROM locations ORDER BY path"
        ).fetchall()
        return [
            LocationRecord(
                path=row[0],
                manager=row[1],
                size=row[2],
                mtime=row[3],
                device=row[4],
                inode=row[5],
                link_kind=row[6],
                sha256=row[7],
                variant_ref=row[8],
                meta=json.loads(row[9]),
            )
            for row in rows
        ]

    # --- cache de hachage (CONCEPTION.md §1.2) --------------------------------
    #
    # La clé de validité est le quadruplet (path, size, mtime, inode) : si l'un
    # des quatre a bougé, l'entrée est périmée et le sha256 doit être recalculé.

    def hash_cache_get(self, path: str, size: int, mtime: float, inode: int) -> str | None:
        row = self.conn.execute(
            "SELECT sha256 FROM hash_cache"
            " WHERE path = ? AND size = ? AND mtime = ? AND inode = ? AND sha256 IS NOT NULL",
            (path, size, mtime, inode),
        ).fetchone()
        return row[0] if row else None

    def hash_cache_put(
        self,
        path: str,
        size: int,
        mtime: float,
        inode: int,
        sha256: str,
        verified_at: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO hash_cache"
                " (path, size, mtime, inode, quick_hash, sha256, verified_at)"
                " VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (path, size, mtime, inode, sha256, verified_at or datetime.now(UTC).isoformat()),
            )

    def hash_cache_size(self) -> int:
        return self.conn.execute("SELECT count(*) FROM hash_cache").fetchone()[0]

    def set_location_hash(self, path: str, sha256: str, source: str = "verified") -> None:
        """Écrit le hash vérifié dans la Location et trace sa provenance.

        Ce qu'annonçait le gestionnaire est mis de côté avant d'être recouvert :
        sans cette trace, un blob dont le nom ment ne serait signalé qu'une fois,
        et la vérification suivante le déclarerait conforme.
        """
        row = self.conn.execute(
            "SELECT meta, size, sha256 FROM locations WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            return
        meta = json.loads(row[0])
        if meta.get("hash_source") == "announced" and row[2] and "announced_sha256" not in meta:
            meta["announced_sha256"] = row[2]
        meta["hash_source"] = source
        now = datetime.now(UTC).isoformat()
        with self.conn:
            self.conn.execute(
                "UPDATE locations SET sha256 = ?, meta = ? WHERE path = ?",
                (sha256, json.dumps(meta), path),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO artifacts (sha256, size, first_seen) VALUES (?, ?, ?)",
                (sha256, row[1], now),
            )

    # --- télémétrie d'usage (poste « variants jamais utilisés ») --------------

    def record_run(
        self,
        run_id: str,
        variant_ref: str,
        started_at: str | None = None,
        duration_ms: int | None = None,
        job_dir: str | None = None,
        ok: bool | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO runs"
                " (id, variant_ref, started_at, duration_ms, job_dir, ok)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    variant_ref,
                    started_at or datetime.now(UTC).isoformat(),
                    duration_ms,
                    job_dir,
                    None if ok is None else int(ok),
                ),
            )

    def runs_count(self) -> int:
        return self.conn.execute("SELECT count(*) FROM runs").fetchone()[0]

    def first_run_at(self) -> str | None:
        """Depuis quand la télémétrie observe. Une télémétrie de la veille ne peut
        pas conclure qu'un variant n'a pas servi depuis trois mois."""
        row = self.conn.execute("SELECT min(started_at) FROM runs").fetchone()
        return row[0] if row and row[0] else None

    def last_run_by_variant(self) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT variant_ref, max(started_at) FROM runs GROUP BY variant_ref"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def set_variant_refs(self, refs: dict[str, str | None]) -> None:
        with self.conn:
            self.conn.executemany(
                "UPDATE locations SET variant_ref = ? WHERE path = ?",
                [(ref, path) for path, ref in refs.items()],
            )

    def set_kv(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    def get_kv(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
