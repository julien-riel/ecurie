"""Orchestration d'un scan complet : scanners → résolveur → base d'état."""

from dataclasses import dataclass
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


def run_scan(config: Config, db: StateDB, registry: Registry | None = None) -> ScanReport:
    jobs: list[tuple[str, Path | None, object]] = [
        ("hf", config.scan.hf_hub, scan_hf),
        ("ollama", config.scan.ollama, scan_ollama),
        ("lmstudio", config.scan.lmstudio, lambda p: scan_tree(p, "lmstudio")),
    ]
    for i, path in enumerate(config.scan.comfy):
        jobs.append((f"comfy:{i}" if i else "comfy", path, lambda p: scan_tree(p, "comfy")))
    for i, path in enumerate(config.scan.declared):
        name = f"declared:{i}" if i else "declared"
        jobs.append((name, path, lambda p: scan_tree(p, "declared")))

    report = ScanReport(counts={}, skipped={})
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
    if registry is not None:
        resolve_variant_refs(registry, all_records)

    for manager, records in by_manager.items():
        db.replace_manager(manager, records)
    db.set_kv("last_scan_at", datetime.now(UTC).isoformat())
    return report
