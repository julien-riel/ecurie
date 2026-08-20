"""Parc Ollama (~/.ollama/models).

Les manifestes (JSON de style OCI, un fichier par tag) référencent des blobs
adressés par contenu : `blobs/sha256-<hex>`. Un blob présent mais référencé
par aucun manifeste est orphelin — reliquat d'un `ollama rm` incomplet ou
d'un téléchargement interrompu.
"""

import json
from pathlib import Path

from ecurie_store.db import LocationRecord
from ecurie_store.scanners.common import announce, sha256_or_none, stat_record


def scan_ollama(models_dir: Path) -> list[LocationRecord]:
    manifests_dir = models_dir / "manifests"
    blobs_dir = models_dir / "blobs"

    referenced: dict[str, list[str]] = {}  # nom de blob → modèles qui l'utilisent
    if manifests_dir.is_dir():
        for manifest_path in sorted(manifests_dir.rglob("*")):
            if not manifest_path.is_file():
                continue
            try:
                doc = json.loads(manifest_path.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            model_name = "/".join(manifest_path.relative_to(manifests_dir).parts)
            digests = [layer.get("digest") for layer in doc.get("layers", [])]
            digests.append(doc.get("config", {}).get("digest"))
            for digest in digests:
                if digest:
                    blob_name = digest.replace(":", "-")
                    referenced.setdefault(blob_name, []).append(model_name)

    records: list[LocationRecord] = []
    if blobs_dir.is_dir():
        for blob in sorted(blobs_dir.iterdir()):
            if not blob.is_file() and not blob.is_symlink():
                continue
            models = referenced.get(blob.name, [])
            rec = stat_record(blob, "ollama", models=models, orphan=not models)
            announce(rec, sha256_or_none(blob.name.removeprefix("sha256-")))
            records.append(rec)
    return records
