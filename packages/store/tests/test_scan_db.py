"""run_scan de bout en bout : config → scanners → résolveur → SQLite."""

from pathlib import Path

from ecurie_core.config import Config, ScanConfig
from ecurie_core.models import Model
from ecurie_core.registry import Registry, load_registry
from ecurie_store.db import LocationRecord, StateDB
from ecurie_store.figures import compute_figures
from ecurie_store.resolver import resolve_variant_refs
from ecurie_store.scan import run_scan
from park import HF_REPO, SHA_LIVE


def test_scan_complet_vers_sqlite(tmp_path, fake_hf, fake_ollama, fake_lmstudio, repo_root):
    config = Config(
        scan=ScanConfig(
            hf_hub=fake_hf,
            ollama=fake_ollama,
            lmstudio=fake_lmstudio,
            declared=[tmp_path / "inexistant"],
        )
    )
    registry = load_registry(repo_root)
    db = StateDB(tmp_path / "state.db")

    report = run_scan(config, db, registry)
    assert report.counts == {"hf": 5, "ollama": 3, "lmstudio": 2}  # hf : 4 blobs + 1 lien
    assert "declared" in report.skipped
    assert report.scan_id

    records = db.locations()
    figures = compute_figures(records)
    assert figures.apparent_bytes == 7110
    assert figures.real_unique_bytes == 4110
    assert db.get_kv("last_scan_at") is not None

    # Résolution : les blobs HF du dépôt du manifeste réel sont rattachés, et le
    # blob Ollama de contenu identique l'est par hash — c'est la même duplication.
    hf_live = [r for r in records if r.sha256 == SHA_LIVE and r.manager == "hf"]
    assert hf_live[0].meta["repos"] == [HF_REPO]
    assert hf_live[0].variant_ref == "qwen3-tts-1.7b@8bit-mlx"
    ollama_live = [r for r in records if r.sha256 == SHA_LIVE and r.manager == "ollama"]
    assert ollama_live[0].variant_ref == "qwen3-tts-1.7b@8bit-mlx"
    resolved = [r for r in records if r.variant_ref]
    assert all(r.variant_ref == "qwen3-tts-1.7b@8bit-mlx" for r in resolved)
    assert figures.unresolved_count == len(records) - len(resolved)


def test_rescan_remplace_l_etat(tmp_path, fake_ollama):
    config = Config(scan=ScanConfig(ollama=fake_ollama))
    db = StateDB(tmp_path / "state.db")
    run_scan(config, db)
    before = len(db.locations())

    orphan = fake_ollama / "blobs" / f"sha256-{SHA_LIVE.replace('1a', 'ff')}"
    orphan.write_bytes(b"n" * 50)
    run_scan(config, db)
    assert len(db.locations()) == before + 1

    Path(orphan).unlink()
    run_scan(config, db)
    assert len(db.locations()) == before


def test_une_revision_epinglee_par_le_registre_n_est_jamais_obsolete():
    """C'est le registre, pas `refs/main`, qui dit ce qui est en service. Un variant
    téléchargé à sa révision exacte — la règle du projet — ne doit pas se retrouver
    proposé à la corbeille par son propre outil."""
    modele = Model.model_validate(
        {
            "id": "m",
            "capability": "text-to-speech",
            "license": "apache-2.0",
            "status": "active",
            "variants": [
                {
                    "id": "v",
                    "runtime": "mlx-audio",
                    "source": {
                        "kind": "huggingface",
                        "repo": "vendeur/modele",
                        "revision": "d" * 40,
                    },
                }
            ],
        }
    )
    registry = Registry(root=Path("/"), models={"m": modele})
    epingle = LocationRecord(
        path="/hub/blobs/aa",
        manager="hf",
        size=1000,
        mtime=1.0,
        device=1,
        inode=1,
        link_kind="plain",
        meta={"repos": ["vendeur/modele"], "revisions": ["d" * 40], "stale": True},
    )
    distancee = LocationRecord(
        path="/hub/blobs/bb",
        manager="hf",
        size=500,
        mtime=1.0,
        device=1,
        inode=2,
        link_kind="plain",
        meta={"repos": ["vendeur/modele"], "revisions": ["c" * 40], "stale": True},
    )

    resolve_variant_refs(registry, [epingle, distancee])

    assert epingle.meta["stale"] is False
    assert epingle.variant_ref == "m@v"
    assert distancee.meta["stale"] is True  # celle-là est vraiment distancée
