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
