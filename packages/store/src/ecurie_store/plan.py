"""Plan de récupération d'espace — un fichier JSON relisible et exécutable.

Format : CONCEPTION.md §4.3. Le plan est *déclaratif* : il ne fait rien, il dit
ce qui serait fait et combien cela rendrait. Il est généré depuis l'état observé
du dernier scan, il porte son `scan_id`, et chaque action embarque l'empreinte
(taille, mtime, inode) des fichiers qu'elle touche : à l'exécution, tout écart
avec le disque annule l'action au lieu de l'appliquer à l'aveugle.

Un plan ne se lit pas seulement par la machine — c'est le document qu'on relit
avant de laisser un outil toucher trente giga-octets de poids de modèles.
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecurie_store.db import LocationRecord
from ecurie_store.figures import TRASH_REASONS, classify, unused_variants
from ecurie_store.hashing import dedup_candidates

PLAN_VERSION = 1

REASON_LABELS = {
    "duplication": "duplication inter-gestionnaires",
    "hf-stale-revision": "révision HF obsolète",
    "orphan-blob": "blob orphelin",
    "unused-variant": "variant jamais utilisé",
    "detached-snapshot": "instantané HF détaché",
}


class PlanError(RuntimeError):
    pass


def _stats(rec: LocationRecord) -> dict[str, Any]:
    """L'empreinte à retrouver à l'identique au moment d'exécuter."""
    return {"size": rec.size, "mtime": rec.mtime, "inode": rec.inode, "device": rec.device}


def generate_plan(
    records: list[LocationRecord],
    *,
    scan_id: str | None = None,
    last_runs: dict[str, str] | None = None,
    telemetry: bool = False,
    unused_after_days: int = 90,
    verified_only: bool = False,
    now: datetime | None = None,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Construit le plan à partir de l'état observé. Ne touche pas au disque."""
    files = [r for r in records if r.link_kind != "symlink"]
    par_chemin = {r.path: r for r in records}
    unused = (
        unused_variants(files, last_runs or {}, unused_after_days, now) if telemetry else set()
    )

    actions: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []

    # Les jumeaux sans hash ne peuvent pas être liés — une taille ne prouve rien —
    # mais les taire reviendrait à annoncer un parc sans doublons. Ils sortent en
    # « écartés » : c'est ce qui invite à lancer `ecurie store verify`.
    candidats = dedup_candidates(files)
    if candidats:
        ignored.append({"reason": "sans-sha256", "paths": sorted(r.path for r in candidats)})

    for group, attribution in classify(files, unused):
        for chemin, motif in attribution.skipped:
            ignored.append({"reason": motif, "paths": [chemin]})
        if attribution.dup_actions:
            if verified_only and _hash_source(group.records) != "verified":
                ignored.append(
                    {"reason": "hash-annonce-non-verifie", "paths": attribution.dup_paths}
                )
            else:
                for dup in attribution.dup_actions:
                    actions.append(
                        {
                            "kind": "hardlink",
                            "keep": dup.keep,
                            "replace": dup.replace,
                            "sha256": group.sha256,
                            "hash_source": _hash_source(group.records),
                            "reason": "duplication",
                            "bytes_reclaimed": dup.bytes_reclaimed,
                            "stats": {
                                path: _stats(par_chemin[path])
                                for path in [dup.keep, *dup.replace]
                                if path in par_chemin
                            },
                        }
                    )

        for path, octets in attribution.trash_paths:
            rec = par_chemin[path]
            actions.append(
                _trash_action(rec, TRASH_REASONS[attribution.trash_poste], octets, group.sha256)
            )

        for path, octets in attribution.unused_paths:
            rec = par_chemin[path]
            actions.append(_trash_action(rec, "unused-variant", octets, group.sha256))

    actions.extend(_detached_snapshot_actions(records))

    total = sum(action_bytes(a) for a in actions)
    by_reason: dict[str, int] = {}
    for action in actions:
        by_reason[action["reason"]] = by_reason.get(action["reason"], 0) + action_bytes(action)

    return {
        "plan_version": PLAN_VERSION,
        "plan_id": plan_id or uuid.uuid4().hex,
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "scan_id": scan_id,
        "telemetry": telemetry,
        "unused_after_days": unused_after_days,
        "actions": actions,
        "ignored": ignored,
        "by_reason": by_reason,
        "total_bytes_reclaimed": total,
    }


def action_bytes(action: dict[str, Any]) -> int:
    return int(action.get("bytes_reclaimed", 0))


def _hash_source(records: list[LocationRecord]) -> str:
    sources = {r.meta.get("hash_source") for r in records}
    return "verified" if sources == {"verified"} else "announced"


def _trash_action(
    rec: LocationRecord, reason: str, octets: int, sha256: str | None
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "kind": "trash",
        "path": rec.path,
        "reason": reason,
        "sha256": sha256,
        "bytes_reclaimed": octets,
        "stats": {rec.path: _stats(rec)},
    }
    if rec.variant_ref:
        action["variant_ref"] = rec.variant_ref
    if len(rec.variant_refs) > 1:
        # Le plan est relu par un humain avant d'être appliqué : taire qu'un
        # fichier sert à deux modèles priverait cette relecture de ce qui compte.
        action["variant_refs"] = rec.variant_refs
    return action


def _detached_snapshot_actions(records: list[LocationRecord]) -> list[dict[str, Any]]:
    """Un dossier `snapshots/<révision>` que plus aucune référence ne désigne.

    Zéro octet — ce ne sont que des liens symboliques — mais les laisser sur place
    après avoir jeté leurs blobs transforme le cache HF en champ de liens cassés.
    """
    dossiers: dict[str, str] = {}
    for rec in records:
        snapshot = rec.meta.get("snapshot")
        if rec.link_kind == "symlink" and snapshot and rec.meta.get("stale"):
            dossiers[str(snapshot)] = rec.meta.get("revision", "")
    return [
        {
            "kind": "trash",
            "path": dossier,
            "reason": "detached-snapshot",
            "sha256": None,
            "bytes_reclaimed": 0,
            "revision": revision,
            "stats": {},
        }
        for dossier, revision in sorted(dossiers.items())
    ]


def write_plan(plan: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    return path


def load_plan(path: Path) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"plan illisible : {exc}") from exc
    if not isinstance(plan, dict) or "actions" not in plan:
        raise PlanError(f"{path} n'est pas un plan Écurie")
    if plan.get("plan_version") != PLAN_VERSION:
        raise PlanError(
            f"plan de version {plan.get('plan_version')!r}, attendu {PLAN_VERSION} — "
            "régénérer avec `ecurie store plan`"
        )
    return plan


def summarize(plan: dict[str, Any]) -> dict[str, int]:
    return dict(sorted(plan.get("by_reason", {}).items(), key=lambda kv: -kv[1]))
