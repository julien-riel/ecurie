"""Jointure déclaré ↔ observé : rattache les Locations aux variants du registre.

v0.1 : rattachement par dépôt HF (repo → model@variant) et par préfixe de
chemin pour les sources locales. Les sources ollama et la vérification de la
révision épinglée viennent avec les adaptateurs (v0.3).

**Un fichier peut appartenir à plusieurs variants**, et c'est le §4 de
l'architecture qui l'impose : tous les modèles d'une capacité sont
interchangeables derrière un contrat, mais rien n'interdit à un même jeu de poids
de remplir deux capacités — les mêmes poids vision-langage transcrivent un
document et décrivent une image, et le registre le déclare alors par deux
manifestes pointant le même dépôt. Le rattachement rend donc une liste, et le
premier élément sert d'étiquette. Écraser en silence, comme le faisait un simple
dictionnaire indexé par dépôt, attribuait plusieurs gigaoctets au dernier
manifeste chargé — et exposait les autres au poste « jamais utilisé » du plan de
récupération.
"""

from pathlib import Path

from ecurie_core.registry import Registry

from ecurie_store.db import LocationRecord


def resolve_variant_refs(registry: Registry, records: list[LocationRecord]) -> None:
    hf_repos: dict[str, list[str]] = {}
    local_paths: dict[str, list[str]] = {}
    épinglées: set[tuple[str, str]] = set()  # (dépôt, révision) déclarés au registre
    for model in registry.models.values():
        for variant in model.variants:
            ref = f"{model.id}@{variant.id}"
            # Toutes les sources, pas seulement les poids : le tokenizer publié
            # dans un autre dépôt occupe du disque, et un dépôt qu'aucun variant
            # ne réclame tombe au poste « jamais utilisé » du plan de
            # récupération — c'est-à-dire à la corbeille, alors qu'il est requis
            # au chargement.
            for src in variant.sources:
                if src.kind == "huggingface" and src.repo:
                    hf_repos.setdefault(src.repo, []).append(ref)
                    if src.revision:
                        épinglées.add((src.repo, src.revision))
                elif src.kind == "local" and src.path:
                    clé = str(Path(src.path).expanduser().resolve())
                    local_paths.setdefault(clé, []).append(ref)

    # Ordre stable : deux scans du même parc doivent produire la même étiquette,
    # sans quoi la télémétrie et le plan de récupération changeraient d'avis d'un
    # jour à l'autre sur des fichiers qui n'ont pas bougé.
    for table in (hf_repos, local_paths):
        for clé, refs in table.items():
            table[clé] = sorted(set(refs))

    for rec in records:
        if rec.manager == "hf":
            for repo in rec.meta.get("repos", []):
                if repo in hf_repos:
                    _attacher(rec, hf_repos[repo])
                    break
        else:
            for prefix, refs in local_paths.items():
                if rec.path == prefix or rec.path.startswith(prefix + "/"):
                    _attacher(rec, refs)
                    break

    _protege_les_revisions_epinglees(épinglées, records)
    _resolve_by_hash(records)


def _attacher(rec: LocationRecord, refs: list[str]) -> None:
    rec.variant_ref = refs[0]
    if len(refs) > 1:
        rec.meta["variant_refs"] = list(refs)
    else:
        rec.meta.pop("variant_refs", None)


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
    by_hash: dict[str, set[tuple[str, ...]]] = {}
    for rec in records:
        if rec.sha256 and rec.variant_refs:
            by_hash.setdefault(rec.sha256, set()).add(tuple(rec.variant_refs))
    for rec in records:
        if rec.variant_ref is None and rec.sha256:
            familles = by_hash.get(rec.sha256)
            if familles and len(familles) == 1:  # ambigu = non résolu, on ne devine pas
                _attacher(rec, list(next(iter(familles))))
