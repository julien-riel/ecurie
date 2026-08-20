from ecurie_store.scanners import scan_hf, scan_ollama, scan_tree
from park import ETAG40, HF_REPO, REV_LIVE, REV_STALE, SHA_HORPH, SHA_LIVE, SHA_STALE


def test_scan_hf_blobs_et_drapeaux(fake_hf):
    records = {r.sha256 or r.path.rsplit("/", 1)[-1]: r for r in scan_hf(fake_hf)}
    assert len(records) == 4

    live = records[SHA_LIVE]
    assert live.size == 1000
    assert live.meta["repos"] == [HF_REPO]
    assert REV_LIVE in live.meta["revisions"]
    assert live.meta["stale"] is False

    stale = records[SHA_STALE]
    assert stale.meta["stale"] is True
    assert stale.meta["revisions"] == [REV_STALE]

    assert records[SHA_HORPH].meta["orphan"] is True

    etag = records[ETAG40]
    assert etag.sha256 is None  # etag 40 caractères : pas un sha256 annoncé
    assert etag.size == 10


def test_scan_ollama_references_et_orphelin(fake_ollama):
    records = scan_ollama(fake_ollama)
    assert len(records) == 3
    by_orphan = {r.sha256: r.meta["orphan"] for r in records}
    assert list(by_orphan.values()).count(True) == 1
    referenced = [r for r in records if not r.meta["orphan"]]
    assert all(r.meta["models"] == ["registry.ollama.ai/library/test/latest"] for r in referenced)
    assert all(r.sha256 == r.path.rsplit("sha256-", 1)[-1] for r in records)


def test_scan_tree_liens_durs_et_fichiers_caches(fake_lmstudio):
    records = scan_tree(fake_lmstudio, "lmstudio")
    assert len(records) == 2  # .cache-interne ignoré
    assert all(r.link_kind == "hardlink" for r in records)
    assert len({r.inode for r in records}) == 1
    assert all(r.sha256 is None for r in records)
