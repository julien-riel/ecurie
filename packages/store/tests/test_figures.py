"""Les trois chiffres sur le faux parc complet — valeurs attendues exactes."""

from ecurie_store.figures import compute_figures, fmt_bytes
from ecurie_store.scanners import scan_hf, scan_ollama, scan_tree


def _all_records(fake_hf, fake_ollama, fake_lmstudio):
    return scan_hf(fake_hf) + scan_ollama(fake_ollama) + scan_tree(fake_lmstudio, "lmstudio")


def test_trois_chiffres(fake_hf, fake_ollama, fake_lmstudio):
    figures = compute_figures(_all_records(fake_hf, fake_ollama, fake_lmstudio))

    assert figures.apparent_bytes == 7110
    assert figures.real_unique_bytes == 4110

    rec = figures.recoverable
    assert rec.duplication_bytes == 1000  # blob LIVE : HF + Ollama, 2 inodes
    assert rec.hf_stale_bytes == 500
    assert rec.orphan_bytes == 300  # 200 (HF) + 100 (Ollama)
    assert rec.unused_known is False
    assert rec.total_known_bytes == 1800

    assert figures.by_manager["hf"] == (1710, 4)
    assert figures.by_manager["ollama"] == (1400, 3)
    assert figures.by_manager["lmstudio"] == (4000, 2)


def test_groupes_de_duplication(fake_hf, fake_ollama, fake_lmstudio):
    figures = compute_figures(_all_records(fake_hf, fake_ollama, fake_lmstudio))
    # Un seul groupe : le blob LIVE. Les liens durs LM Studio (même inode)
    # ne sont pas une duplication récupérable.
    assert len(figures.duplicates) == 1
    group = figures.duplicates[0]
    assert group.size == 1000
    assert group.reclaimable_bytes == 1000
    assert len(group.paths) == 2


def test_fmt_bytes():
    assert fmt_bytes(1_950_000_000) == "1.95 Go"
    assert fmt_bytes(4_000_000) == "4.0 Mo"
    assert fmt_bytes(512) == "512 o"
