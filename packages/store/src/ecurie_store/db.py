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
    ok          INTEGER,
    source      TEXT
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

    @property
    def variant_refs(self) -> list[str]:
        """Tous les variants auxquels ce fichier appartient, pas seulement le premier.

        Un même dépôt Hugging Face peut servir deux capacités — les mêmes poids
        vision-langage transcrivent un document et décrivent une image — et le
        registre le déclare alors par deux manifestes. Les octets, eux, n'existent
        qu'une fois. `variant_ref` en nomme un pour que tout le code qui n'a
        besoin que d'une étiquette continue de marcher ; la liste complète vit
        dans `meta`, et c'est elle qui compte dès qu'on décide d'effacer :
        proposer à la corbeille des poids « jamais utilisés » par le premier
        variant alors que le second s'en sert tous les jours serait une perte de
        données, pas une récupération d'espace.
        """
        multiples = self.meta.get("variant_refs")
        if isinstance(multiples, list) and multiples:
            return [str(r) for r in multiples]
        return [self.variant_ref] if self.variant_ref else []


class StateDB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Rattrape les colonnes ajoutées après coup à une base déjà sur le disque.

        `_SCHEMA` n'est qu'une suite de `CREATE TABLE IF NOT EXISTS` : sur une
        base existante, ajouter une colonne au DDL ne fait **rien**, et le
        silence est total. Les tests, qui partent d'un `tmp_path` neuf, passent
        au vert pendant que la seule machine qui compte lève « no such column »
        au premier job. C'est la base de qui utilise Écurie depuis le v0.1 qui
        décide ici, pas la base de la suite de tests.

        Un `ALTER TABLE ADD COLUMN` sans défaut coûte O(1) en SQLite et laisse
        les lignes existantes à NULL — ce qui est la vérité : on ignore par où
        sont passés les jobs d'avant.

        **Et deux ouvertures simultanées courent l'une contre l'autre.** Lire le
        `PRAGMA` puis décider n'est pas atomique : deux connexions — deux appels
        d'outils MCP que l'agent lance en parallèle, ou `ecurie serve` et
        `ecurie mcp` qui démarrent ensemble — voient toutes deux la colonne
        absente, la première l'ajoute, la seconde meurt sur « duplicate column
        name ». Mesuré : dix-neuf paires sur vingt, dans la forme exacte du
        serveur. La fenêtre ne dure que le temps d'un `ALTER`, mais elle tombe
        pile au premier lancement après la mise à jour — c'est-à-dire à la seule
        occasion où cette migration sert à quelque chose.

        La garde est donc l'exception plutôt qu'un verrou : SQLite tranche
        lui-même, et le perdant vérifie que la colonne existe bien avant de se
        taire. Un `ALTER` qui échoue pour une autre raison remonte.
        """
        existantes = {row[1] for row in self.conn.execute("PRAGMA table_info(runs)")}
        if "source" in existantes:
            return
        try:
            with self.conn:
                self.conn.execute("ALTER TABLE runs ADD COLUMN source TEXT")
        except sqlite3.OperationalError:
            colonnes = {row[1] for row in self.conn.execute("PRAGMA table_info(runs)")}
            if "source" not in colonnes:
                raise

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
        source: str | None = None,
    ) -> None:
        """Inscrit un usage. `source` dit par quelle porte il est entré.

        La colonne est née d'une exigence du plan et non d'un besoin du GC : la
        gate du mois 1 (tâche 3.4) demande « des jobs MCP au moins cinq jours sur
        sept », et une table qui compte les jobs sans dire d'où ils viennent
        laisserait un `ecurie bench` du dimanche tenir lieu de preuve d'usage. Le
        poste « jamais utilisé » du plan de récupération, lui, continue de
        compter toutes les portes — un variant servi par l'Atelier a servi.
        """
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO runs"
                " (id, variant_ref, started_at, duration_ms, job_dir, ok, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    variant_ref,
                    started_at or datetime.now(UTC).isoformat(),
                    duration_ms,
                    job_dir,
                    None if ok is None else int(ok),
                    source,
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
