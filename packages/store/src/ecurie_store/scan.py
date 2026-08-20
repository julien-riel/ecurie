"""Orchestration d'un scan complet : scanners → cache de hachage → résolveur → base d'état."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ecurie_core.config import Config
from ecurie_core.registry import Registry

from ecurie_store.db import LocationRecord, StateDB
from ecurie_store.resolver import resolve_variant_refs
from ecurie_store.scanners import scan_hf, scan_ollama, scan_tree


@dataclass
class ScanReport:
    counts: dict[str, int]  # manager → nb de fichiers observés
    skipped: dict[str, str]  # manager → raison (chemin absent, erreur)
    scan_id: str = ""
    cached_hashes: int = 0  # hashs vérifiés réinjectés depuis le cache
    warnings: list[str] = field(default_factory=list)  # dépôts écartés par un gestionnaire


def run_scan(config: Config, db: StateDB, registry: Registry | None = None) -> ScanReport:
    report = ScanReport(counts={}, skipped={}, scan_id=uuid.uuid4().hex)
    jobs: list[tuple[str, Path | None, object]] = [
        ("hf", config.scan.hf_hub, lambda p: scan_hf(p, warnings=report.warnings)),
        ("ollama", config.scan.ollama, scan_ollama),
        ("lmstudio", config.scan.lmstudio, lambda p: scan_tree(p, "lmstudio")),
    ]
    for i, path in enumerate(config.scan.comfy):
        jobs.append((f"comfy:{i}" if i else "comfy", path, lambda p: scan_tree(p, "comfy")))
    for i, path in enumerate(config.scan.declared):
        name = f"declared:{i}" if i else "declared"
        jobs.append((name, path, lambda p: scan_tree(p, "declared")))
    for i, path in enumerate(config.tier_volumes):
        name = f"tier:{i}" if i else "tier"
        jobs.append((name, path, lambda p: scan_tree(p, "tier")))

    by_manager: dict[str, list[LocationRecord]] = {}
    for name, path, scanner in jobs:
        if path is None:
            continue
        path = path.expanduser()
        if not path.is_dir():
            report.skipped[name] = f"chemin absent : {path}"
            continue
        try:
            records = scanner(path)
        except Exception as exc:  # un gestionnaire cassé ne doit pas bloquer les autres
            report.skipped[name] = f"échec du scan : {exc}"
            continue
        for r in records:
            by_manager.setdefault(r.manager, []).append(r)
        report.counts[name] = len(records)

    all_records = [r for records in by_manager.values() for r in records]
    report.cached_hashes = fill_from_hash_cache(db, all_records)
    if registry is not None:
        resolve_variant_refs(registry, all_records)

    for manager, records in by_manager.items():
        db.replace_manager(manager, records)
    db.set_kv("last_scan_at", datetime.now(UTC).isoformat())
    db.set_kv("scan_id", report.scan_id)
    return report


def fill_from_hash_cache(db: StateDB, records: list[LocationRecord]) -> int:
    """Réinjecte les sha256 déjà vérifiés — sans quoi chaque scan effacerait le
    travail de `ecurie store verify`.

    Un hash relu l'emporte sur le nom du blob, y compris quand les deux diffèrent :
    c'est justement le cas où le scan ne doit pas réinstaller le mensonge. La valeur
    annoncée reste dans `meta`, la divergence continue donc d'être signalée.
    Le cache est invalidé par tout changement de (taille, mtime, inode) : un fichier
    réécrit repart sans hash vérifié.
    """
    filled = 0
    for rec in records:
        if rec.link_kind == "symlink":
            continue
        cached = db.hash_cache_get(rec.path, rec.size, rec.mtime, rec.inode)
        if cached is None or (rec.sha256 == cached and rec.meta.get("hash_source") == "verified"):
            continue
        rec.sha256 = cached
        rec.meta["hash_source"] = "verified"
        filled += 1
    return filled
