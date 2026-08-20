"""Cache Hugging Face (~/.cache/huggingface/hub).

S'appuie sur `scan_cache_dir()` qui donne l'inventaire exact — dépôts,
révisions, refs, blobs. On enregistre les blobs (les vrais fichiers ; les
snapshots ne sont que des liens symboliques vers eux). Deux drapeaux posés
dans `meta` alimentent les postes du récupérable :

- `stale`  : blob référencé uniquement par des révisions détachées de tout ref
             (l'équivalent des « révisions obsolètes » de `hf cache delete`) ;
- `orphan` : fichier présent dans blobs/ mais référencé par aucun snapshot.
"""

from pathlib import Path

from huggingface_hub import scan_cache_dir

from ecurie_store.db import LocationRecord
from ecurie_store.scanners.common import sha256_or_none, stat_record


def scan_hf(hub_dir: Path) -> list[LocationRecord]:
    info = scan_cache_dir(hub_dir)
    records: dict[str, LocationRecord] = {}

    for repo in info.repos:
        live_blobs = {str(f.blob_path) for rev in repo.revisions if rev.refs for f in rev.files}
        referenced: set[str] = set()
        for rev in repo.revisions:
            for f in rev.files:
                key = str(f.blob_path)
                referenced.add(key)
                rec = records.get(key)
                if rec is None:
                    if not f.blob_path.is_file():
                        continue
                    rec = stat_record(
                        f.blob_path, "hf", repos=[], revisions=[], refs=[], stale=False
                    )
                    rec.sha256 = sha256_or_none(f.blob_path.name)
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
                if blob.is_file() and str(blob) not in referenced:
                    rec = stat_record(blob, "hf", repos=[repo.repo_id], orphan=True)
                    rec.sha256 = sha256_or_none(blob.name)
                    records[str(blob)] = rec

    return list(records.values())
