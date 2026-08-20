"""Jointure déclaré ↔ observé : rattache les Locations aux variants du registre.

v0.1 : rattachement par dépôt HF (repo → model@variant) et par préfixe de
chemin pour les sources locales. Les sources ollama et la vérification de la
révision épinglée viendront avec les adaptateurs (v0.3).
"""

from pathlib import Path

from ecurie_core.registry import Registry

from ecurie_store.db import LocationRecord


def resolve_variant_refs(registry: Registry, records: list[LocationRecord]) -> None:
    hf_repos: dict[str, str] = {}
    local_paths: dict[str, str] = {}
    épinglées: set[tuple[str, str]] = set()  # (dépôt, révision) déclarés au registre
    for model in registry.models.values():
        for variant in model.variants:
            ref = f"{model.id}@{variant.id}"
            src = variant.source
            if src.kind == "huggingface" and src.repo:
                hf_repos[src.repo] = ref
                if src.revision:
                    épinglées.add((src.repo, src.revision))
            elif src.kind == "local" and src.path:
                local_paths[str(Path(src.path).expanduser().resolve())] = ref

    for rec in records:
        if rec.manager == "hf":
            for repo in rec.meta.get("repos", []):
                if repo in hf_repos:
                    rec.variant_ref = hf_repos[repo]
                    break
        else:
            for prefix, ref in local_paths.items():
                if rec.path == prefix or rec.path.startswith(prefix + "/"):
                    rec.variant_ref = ref
                    break

    _protege_les_revisions_epinglees(épinglées, records)
    _resolve_by_hash(records)


def _protege_les_revisions_epinglees(
    épinglées: set[tuple[str, str]], records: list[LocationRecord]
) -> None:
    """Une révision que le registre épingle n'est jamais obsolète, même sans ref.

    C'est le registre, pas `refs/main`, qui dit ce qui est en service dans le parc.
    Sans cette passe, un variant téléchargé à sa révision exacte — la règle du
    projet — se retrouverait proposé à la corbeille par son propre outil.
    """
    if not épinglées:
        return
    for rec in records:
        if not rec.meta.get("stale"):
            continue
        révisions = rec.meta.get("revisions") or []
        if "revision" in rec.meta:  # les liens d'un instantané portent la leur, au singulier
            révisions = [*révisions, rec.meta["revision"]]
        for repo in rec.meta.get("repos", []):
            if any((repo, révision) in épinglées for révision in révisions):
                rec.meta["stale"] = False
                break


def _resolve_by_hash(records: list[LocationRecord]) -> None:
    """Dernier recours (CONCEPTION.md §4.1) : un contenu identique à celui d'un
    fichier déjà rattaché appartient au même variant. C'est ce qui retrouve une
    copie migrée sur un volume de tiering, qui n'a plus ni dépôt ni chemin connu."""
    by_hash: dict[str, set[str]] = {}
    for rec in records:
        if rec.sha256 and rec.variant_ref:
            by_hash.setdefault(rec.sha256, set()).add(rec.variant_ref)
    for rec in records:
        if rec.variant_ref is None and rec.sha256:
            refs = by_hash.get(rec.sha256)
            if refs and len(refs) == 1:  # ambigu = non résolu, on ne devine pas
                rec.variant_ref = next(iter(refs))
