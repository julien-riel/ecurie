"""run_scan de bout en bout : config → scanners → résolveur → SQLite."""

from pathlib import Path

from ecurie_core.config import Config, ScanConfig
from ecurie_core.registry import load_registry
from ecurie_store.db import StateDB
from ecurie_store.figures import compute_figures
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
    assert report.counts == {"hf": 4, "ollama": 3, "lmstudio": 2}
    assert "declared" in report.skipped

    records = db.locations()
    figures = compute_figures(records)
    assert figures.apparent_bytes == 7110
    assert figures.real_unique_bytes == 4110
    assert db.get_kv("last_scan_at") is not None

    # Résolution : les blobs HF du dépôt du manifeste réel sont rattachés.
    hf_live = [r for r in records if r.sha256 == SHA_LIVE and r.manager == "hf"]
    assert hf_live[0].meta["repos"] == [HF_REPO]
    assert hf_live[0].variant_ref == "qwen3-tts-1.7b@8bit-mlx"
    # 9 fichiers au total, 2 rattachés (les blobs vivants du dépôt HF connu).
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
