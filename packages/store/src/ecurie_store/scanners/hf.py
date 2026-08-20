"""Cache Hugging Face (~/.cache/huggingface/hub).

S'appuie sur `scan_cache_dir()` qui donne l'inventaire exact — dépôts,
révisions, refs, blobs. On enregistre les blobs (les vrais fichiers ; les
snapshots ne sont que des liens symboliques vers eux). Deux drapeaux posés
dans `meta` alimentent les postes du récupérable :

- `stale`  : blob référencé uniquement par des révisions détachées de tout ref
             (l'équivalent des « révisions obsolètes » de `hf cache delete`) ;
- `orphan` : fichier présent dans blobs/ mais référencé par aucun snapshot.

Une précaution vaut d'être dite : un dépôt téléchargé à une **révision épinglée**
n'a aucun ref — c'est le cas normal de tout le parc actif d'Écurie, qui épingle
ses révisions par sha. Prendre « sans ref » pour « obsolète » proposerait la
corbeille pour le parc entier. Un dépôt qui n'a aucun ref du tout n'a donc rien
d'obsolète : sans ref, on n'a aucune base pour désigner une révision comme la
courante. Seul un dépôt qui a des refs peut avoir des révisions distancées.
"""

from pathlib import Path

from huggingface_hub import scan_cache_dir

from ecurie_store.db import LocationRecord
from ecurie_store.scanners.common import announce, sha256_or_none, stat_record


def scan_hf(hub_dir: Path, *, warnings: list[str] | None = None) -> list[LocationRecord]:
    info = scan_cache_dir(hub_dir)
    records: dict[str, LocationRecord] = {}

    # `scan_cache_dir` écarte silencieusement tout dépôt qu'il juge corrompu — un
    # blob manquant derrière un lien cassé suffit, ce que le tiering d'Écurie
    # produit lui-même quand le volume externe est démonté. Un dépôt disparu de
    # l'inventaire sans un mot serait pire qu'une erreur : on le remonte.
    if warnings is not None:
        warnings.extend(str(avertissement) for avertissement in info.warnings)

    for repo in info.repos:
        # Sans aucun ref, aucune révision n'est « en retard » sur une autre.
        if not any(rev.refs for rev in repo.revisions):
            live_blobs = {str(f.blob_path) for rev in repo.revisions for f in rev.files}
        else:
            live_blobs = {
                str(f.blob_path) for rev in repo.revisions if rev.refs for f in rev.files
            }
        referenced: set[str] = set()
        for rev in repo.revisions:
            for f in rev.files:
                key = str(f.blob_path)
                referenced.add(key)
                rec = records.get(key)
                if rec is None:
                    if not f.blob_path.is_symlink() and not f.blob_path.is_file():
                        continue
                    rec = stat_record(
                        f.blob_path, "hf", repos=[], revisions=[], refs=[], stale=False
                    )
                    announce(rec, sha256_or_none(f.blob_path.name))
                    records[key] = rec
                if repo.repo_id not in rec.meta["repos"]:
                    rec.meta["repos"].append(repo.repo_id)
                if rev.commit_hash not in rec.meta["revisions"]:
                    rec.meta["revisions"].append(rev.commit_hash)
                for ref in sorted(rev.refs):
                    if ref not in rec.meta["refs"]:
                        rec.meta["refs"].append(ref)
                rec.meta["stale"] = key not in live_blobs

        blobs_dir = repo.repo_path / "blobs"
        if blobs_dir.is_dir():
            for blob in blobs_dir.iterdir():
                if (blob.is_file() or blob.is_symlink()) and str(blob) not in referenced:
                    rec = stat_record(blob, "hf", repos=[repo.repo_id], orphan=True)
                    announce(rec, sha256_or_none(blob.name))
                    records[str(blob)] = rec

        # Les liens symboliques d'une révision détachée : zéro octet, mais les
        # laisser en place après avoir jeté leurs blobs, c'est laisser un cache HF
        # jonché de liens cassés. Ils voyagent avec leurs blobs.
        for rev in repo.revisions:
            if rev.refs or not any(r.refs for r in repo.revisions):
                continue
            for f in rev.files:
                path = f.file_path
                if not path.is_symlink():
                    continue
                records[str(path)] = stat_record(
                    path,
                    "hf",
                    repos=[repo.repo_id],
                    revision=rev.commit_hash,
                    snapshot=str(rev.snapshot_path),
                    stale=True,
                )

    return list(records.values())
