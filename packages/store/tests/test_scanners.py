import os
from pathlib import Path

from ecurie_store.scanners import scan_hf, scan_ollama, scan_tree
from park import ETAG40, HF_REPO, REV_LIVE, REV_STALE, SHA_HORPH, SHA_LIVE, SHA_STALE


def test_scan_hf_blobs_et_drapeaux(fake_hf):
    records = {r.sha256 or r.path.rsplit("/", 1)[-1]: r for r in scan_hf(fake_hf)}
    # 4 blobs + le lien symbolique de la révision détachée.
    assert len(records) == 5

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
    assert live.meta["hash_source"] == "announced"  # nom de blob, jamais relu

    # La révision détachée laisse aussi des liens symboliques : ils voyagent avec
    # leurs blobs, sinon le cache HF se retrouve jonché de liens cassés.
    lien = records["old.safetensors"]
    assert lien.link_kind == "symlink"
    assert lien.size == 0  # un lien n'occupe pas les octets de sa cible
    assert lien.meta["stale"] is True
    assert lien.meta["snapshot"].endswith(f"snapshots/{REV_STALE}")
    assert lien.meta["available"] is True


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


def test_un_depot_sans_aucun_ref_n_a_rien_d_obsolete(tmp_path):
    """Le cas normal du parc Écurie : les variants sont téléchargés à une révision
    épinglée, donc sans ref. Prendre « sans ref » pour « obsolète » proposerait la
    corbeille pour le parc actif tout entier."""
    hub = tmp_path / "hub"
    repo = hub / "models--vendeur--modele"
    blobs = repo / "blobs"
    blobs.mkdir(parents=True)
    (blobs / SHA_LIVE).write_bytes(b"L" * 1000)
    snapshot = repo / "snapshots" / REV_LIVE
    snapshot.mkdir(parents=True)
    os.symlink(Path("..") / ".." / "blobs" / SHA_LIVE, snapshot / "model.safetensors")
    (repo / "refs").mkdir()  # aucun ref à l'intérieur : révision épinglée par sha

    records = scan_hf(hub)
    assert [r.meta.get("stale") for r in records] == [False]
    assert not any(r.link_kind == "symlink" for r in records)
