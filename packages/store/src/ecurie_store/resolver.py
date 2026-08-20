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
    for model in registry.models.values():
        for variant in model.variants:
            ref = f"{model.id}@{variant.id}"
            src = variant.source
            if src.kind == "huggingface" and src.repo:
                hf_repos[src.repo] = ref
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
