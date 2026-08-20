"""Hachage niveau 3 : sha256 complet, cache, divergences (CONCEPTION.md §1.2).

Les sha256 attendus sont écrits en clair : un test qui recalculerait le hash avec
`hashlib` ne prouverait rien du découpage en blocs ni de la lecture complète.
"""

import os
from pathlib import Path

import pytest
from ecurie_core.config import Config, ScanConfig
from ecurie_store import hashing
from ecurie_store.db import LocationRecord, StateDB
from ecurie_store.hashing import (
    CHUNK_BYTES,
    dedup_candidates,
    sha256_file,
    unverified,
    verify_location,
    verify_locations,
)
from ecurie_store.scan import run_scan
from ecurie_store.scanners import scan_hf, scan_ollama, scan_tree
from park import SHA_LIVE

VIDE = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

# Le blob « LIVE » du faux parc : 1000 octets de « L ». Son nom de fichier annonce
# SHA_LIVE, qui n'a rien à voir avec son contenu — c'est le blob qui ment.
CONTENU_L = b"L" * 1000
SHA_L = "6a98b771df7f29a4ae13bb63cd602f34833f3a1fab8e3f6642485197d3cf46d4"
CONTENU_P = b"P" * 1000
SHA_P = "725fa6ac29c02ee0afa7e17c250df09310e6a6075bd8a2470a134c8e26a13074"
CONTENU_X = b"X" * 1000
SHA_X = "bbc4de2ca238d1ec41fb622b75b5cf7d31a6d2ac92405043dd8f8220364fefc8"
CONTENU_REECRIT = CONTENU_X + b"!"
SHA_REECRIT = "14fa2edfd2ae25818bfba07bca6a955c6be22d83c83ffcdb714c6c64f099b307"

MENSONGE = "ff" * 32  # jamais le sha256 de quoi que ce soit qu'on écrit ici


@pytest.fixture
def db(tmp_path: Path) -> StateDB:
    return StateDB(tmp_path / "state.db")


def _observe(path: Path, manager: str = "lmstudio", **meta) -> LocationRecord:
    """L'enregistrement qu'un scanner produirait sur ce fichier, ici et maintenant."""
    st = os.stat(path)
    return LocationRecord(
        path=str(path),
        manager=manager,
        size=st.st_size,
        mtime=st.st_mtime,
        device=st.st_dev,
        inode=st.st_ino,
        link_kind="hardlink" if st.st_nlink > 1 else "plain",
        meta=dict(meta),
    )


def _annonce(rec: LocationRecord, sha256: str) -> LocationRecord:
    """Pose un hash annoncé, en n'écrivant que `sha256` et la provenance.

    Volontairement plus pauvre que `scanners.common.announce`, qui met aussi la
    valeur annoncée de côté : c'est l'état d'une Location relue d'une base créée
    avant cette précaution, et la divergence doit rester détectable même là.
    """
    rec.sha256 = sha256
    rec.meta["hash_source"] = "announced"
    return rec


def _enregistre(db: StateDB, *records: LocationRecord) -> None:
    manager = records[0].manager
    db.replace_manager(manager, list(records))


def _en_base(db: StateDB, path: Path | str) -> LocationRecord:
    (rec,) = [r for r in db.locations() if r.path == str(path)]
    return rec


def _fichier(path: Path, contenu: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contenu)
    return path


def _decale_mtime(path: Path, secondes: int = 60) -> None:
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + secondes * 1_000_000_000))


# --- niveau 3 brut ----------------------------------------------------------


def test_sha256_file_valeurs_exactes(tmp_path):
    assert sha256_file(_fichier(tmp_path / "vide.bin", b"")) == VIDE
    assert sha256_file(_fichier(tmp_path / "abc.bin", b"abc")) == ABC
    assert sha256_file(_fichier(tmp_path / "l.bin", CONTENU_L)) == SHA_L


def test_sha256_file_lit_en_flux_sans_changer_de_resultat(tmp_path):
    fichier = _fichier(tmp_path / "l.bin", CONTENU_L)
    assert CHUNK_BYTES > len(CONTENU_L)  # le défaut lit ce fichier d'une traite
    # Un bloc qui ne divise pas la taille : dernier bloc partiel, boucle correcte.
    assert sha256_file(fichier, chunk_bytes=7) == SHA_L
    assert sha256_file(fichier, chunk_bytes=1) == SHA_L
    assert sha256_file(fichier, chunk_bytes=1000) == SHA_L


# --- verify : calcul, persistance, cache ------------------------------------


def test_verify_calcule_persiste_dans_le_cache_et_dans_la_location(db, tmp_path):
    fichier = _fichier(tmp_path / "modele.gguf", CONTENU_L)
    rec = _observe(fichier)
    _enregistre(db, rec)

    result = verify_location(db, rec)
    assert result.sha256 == SHA_L
    assert result.from_cache is False
    assert result.changed is False
    assert result.mismatch is False
    assert result.ok is True

    st = os.stat(fichier)
    assert db.hash_cache_get(str(fichier), st.st_size, st.st_mtime, st.st_ino) == SHA_L
    assert db.hash_cache_size() == 1

    stocke = _en_base(db, fichier)
    assert stocke.sha256 == SHA_L
    assert stocke.meta["hash_source"] == "verified"


def test_le_cache_ressert_sans_relire_le_fichier(db, tmp_path, monkeypatch):
    fichier = _fichier(tmp_path / "modele.gguf", CONTENU_L)
    rec = _observe(fichier)
    _enregistre(db, rec)
    verify_location(db, rec)

    def interdit(*args, **kwargs):
        raise AssertionError("le contenu a été relu alors que le cache était valide")

    monkeypatch.setattr(hashing, "sha256_file", interdit)
    result = verify_location(db, rec)
    assert result.from_cache is True
    assert result.sha256 == SHA_L


def test_le_cache_est_invalide_par_une_reecriture(db, tmp_path):
    fichier = _fichier(tmp_path / "modele.gguf", CONTENU_L)
    rec = _observe(fichier)
    _enregistre(db, rec)
    verify_location(db, rec)
    ancien = os.stat(fichier)

    fichier.write_bytes(CONTENU_REECRIT)  # taille et contenu changés
    result = verify_location(db, rec)
    assert result.from_cache is False
    assert result.sha256 == SHA_REECRIT
    assert result.changed is True  # le fichier n'est plus celui du scan

    # L'ancienne entrée ne peut plus ressortir, la nouvelle est en place.
    assert db.hash_cache_get(str(fichier), ancien.st_size, ancien.st_mtime, ancien.st_ino) is None
    st = os.stat(fichier)
    assert db.hash_cache_get(str(fichier), st.st_size, st.st_mtime, st.st_ino) == SHA_REECRIT
    assert _en_base(db, fichier).sha256 == SHA_REECRIT


def test_le_cache_est_invalide_par_le_seul_mtime(db, tmp_path):
    fichier = _fichier(tmp_path / "modele.gguf", CONTENU_L)
    rec = _observe(fichier)
    _enregistre(db, rec)
    verify_location(db, rec)

    _decale_mtime(fichier)  # taille, inode et contenu inchangés
    result = verify_location(db, rec)
    assert result.from_cache is False  # une date qui bouge est un fichier suspect
    assert result.sha256 == SHA_L
    assert result.changed is True


def test_le_cache_est_invalide_par_le_seul_inode(db, tmp_path):
    fichier = _fichier(tmp_path / "modele.gguf", CONTENU_L)
    rec = _observe(fichier)
    _enregistre(db, rec)
    verify_location(db, rec)
    ancien = os.stat(fichier)

    # Remplacement atomique par un fichier de contenu, taille et mtime identiques :
    # seul l'inode change. C'est exactement ce que fait une dédup mal conduite.
    remplacant = _fichier(tmp_path / "remplacant.tmp", CONTENU_L)
    os.utime(remplacant, ns=(ancien.st_atime_ns, ancien.st_mtime_ns))
    os.replace(remplacant, fichier)
    st = os.stat(fichier)
    assert (st.st_size, st.st_mtime) == (ancien.st_size, ancien.st_mtime)
    assert st.st_ino != ancien.st_ino

    result = verify_location(db, rec)
    assert result.from_cache is False
    assert result.sha256 == SHA_L


def test_force_ignore_le_cache_et_le_corrige(db, tmp_path):
    fichier = _fichier(tmp_path / "modele.gguf", CONTENU_L)
    rec = _observe(fichier)
    _enregistre(db, rec)
    st = os.stat(fichier)
    db.hash_cache_put(str(fichier), st.st_size, st.st_mtime, st.st_ino, MENSONGE)

    tel_quel = verify_location(db, rec)
    assert (tel_quel.from_cache, tel_quel.sha256) == (True, MENSONGE)

    force = verify_location(db, rec, force=True)
    assert force.from_cache is False
    assert force.sha256 == SHA_L
    assert db.hash_cache_get(str(fichier), st.st_size, st.st_mtime, st.st_ino) == SHA_L
    assert _en_base(db, fichier).sha256 == SHA_L


def test_persist_false_ne_touche_ni_le_cache_ni_la_location(db, tmp_path):
    fichier = _fichier(tmp_path / "modele.gguf", CONTENU_L)
    rec = _observe(fichier)
    _enregistre(db, rec)

    result = verify_location(db, rec, persist=False)
    assert result.sha256 == SHA_L
    assert db.hash_cache_size() == 0
    assert _en_base(db, fichier).sha256 is None


# --- divergence entre hash annoncé et contenu réel --------------------------


def test_divergence_entre_hash_annonce_et_contenu(db, tmp_path):
    # Un blob HF nommé par un sha256 qui n'est pas celui de son contenu.
    blob = _fichier(tmp_path / "blobs" / SHA_LIVE, CONTENU_L)
    rec = _annonce(_observe(blob, "hf"), SHA_LIVE)
    _enregistre(db, rec)

    result = verify_location(db, rec)
    assert result.announced == SHA_LIVE
    assert result.sha256 == SHA_L
    assert result.mismatch is True
    assert result.ok is False


def test_hash_annonce_conforme_ne_diverge_pas(db, tmp_path):
    # Blob honnête : le nom du fichier est bien le sha256 du contenu.
    blob = _fichier(tmp_path / "blobs" / SHA_L, CONTENU_L)
    rec = _annonce(_observe(blob, "hf"), SHA_L)
    _enregistre(db, rec)

    result = verify_location(db, rec)
    assert result.announced == SHA_L
    assert result.sha256 == SHA_L
    assert result.mismatch is False
    assert result.ok is True
    assert _en_base(db, blob).meta["hash_source"] == "verified"


def test_la_divergence_est_encore_signalee_a_la_verification_suivante(db, tmp_path):
    blob = _fichier(tmp_path / "blobs" / SHA_LIVE, CONTENU_L)
    rec = _annonce(_observe(blob, "hf"), SHA_LIVE)
    _enregistre(db, rec)
    assert verify_location(db, rec).mismatch is True

    # Une alarme ne s'éteint pas d'elle-même : le fichier ment toujours, et
    # `store verify` doit encore sortir en erreur au second passage.
    relu = verify_location(db, _en_base(db, blob))
    assert relu.sha256 == SHA_L
    assert relu.mismatch is True
    assert relu.ok is False


def test_un_hash_relu_du_cache_marque_la_location_verifiee(db, tmp_path):
    # État après « scan, verify, rescan » sur un blob honnête : la Location est
    # retombée en « announced » alors que le contenu a bel et bien été lu.
    blob = _fichier(tmp_path / "blobs" / SHA_L, CONTENU_L)
    rec = _annonce(_observe(blob, "hf"), SHA_L)
    _enregistre(db, rec)
    st = os.stat(blob)
    db.hash_cache_put(str(blob), st.st_size, st.st_mtime, st.st_ino, SHA_L)

    result = verify_location(db, rec)
    assert result.from_cache is True
    # Sans cette marque, `store plan` refuse éternellement de déduper (plan.py
    # exige hash_source == "verified") et `store verify` ne converge jamais.
    stocke = _en_base(db, blob)
    assert stocke.meta["hash_source"] == "verified"
    assert unverified([stocke]) == []


# --- refus : lien symbolique, fichier disparu -------------------------------


def test_lien_symbolique_refuse_sans_etre_hache(db, tmp_path):
    cible = _fichier(tmp_path / "blobs" / "poids.bin", CONTENU_L)
    lien = tmp_path / "snapshots" / "model.safetensors"
    lien.parent.mkdir(parents=True)
    os.symlink(cible, lien)
    rec = LocationRecord(
        path=str(lien),
        manager="hf",
        size=0,
        mtime=os.lstat(lien).st_mtime,
        device=os.lstat(lien).st_dev,
        inode=os.lstat(lien).st_ino,
        link_kind="symlink",
        meta={"target": str(cible), "available": True},
    )
    _enregistre(db, rec)

    result = verify_location(db, rec)
    assert result.sha256 is None
    assert result.ok is False
    assert "lien symbolique" in result.error
    # Rien n'a été lu : le hash de la cible n'a pas à être attribué au lien,
    # sinon le lien compterait comme un doublon de son propre blob.
    assert db.hash_cache_size() == 0
    assert _en_base(db, lien).sha256 is None


def test_fichier_disparu(db, tmp_path):
    fichier = _fichier(tmp_path / "modele.gguf", CONTENU_L)
    rec = _observe(fichier)
    _enregistre(db, rec)
    fichier.unlink()

    result = verify_location(db, rec)
    assert result.missing is True
    assert result.sha256 is None
    assert result.ok is False
    assert result.error
    assert db.hash_cache_size() == 0
    assert _en_base(db, fichier).sha256 is None


def test_verify_locations_va_au_bout_malgre_un_fichier_manquant(db, tmp_path):
    bon = _fichier(tmp_path / "a" / "bon.gguf", CONTENU_L)
    disparu = _fichier(tmp_path / "a" / "disparu.gguf", CONTENU_P)
    autre = _fichier(tmp_path / "a" / "autre.gguf", CONTENU_X)
    records = [_observe(p) for p in (bon, disparu, autre)]
    _enregistre(db, *records)
    disparu.unlink()

    vus = []
    résultats = verify_locations(db, records, on_result=vus.append)
    assert [r.path for r in résultats] == [str(bon), str(disparu), str(autre)]
    assert [r.sha256 for r in résultats] == [SHA_L, None, SHA_X]
    assert résultats[1].missing is True
    assert vus == résultats


# --- candidats à la dédup ---------------------------------------------------


def test_dedup_candidates_rien_a_relire_sur_le_faux_parc(fake_hf, fake_ollama, fake_lmstudio):
    records = scan_hf(fake_hf) + scan_ollama(fake_ollama) + scan_tree(fake_lmstudio, "lmstudio")
    # Les deux blobs de 1000 o portent déjà un hash annoncé ; les deux .gguf de
    # 2000 o partagent leur inode ; toutes les autres tailles sont uniques.
    assert dedup_candidates(records) == []


def test_dedup_candidates_retient_le_fichier_sans_hash_de_la_meme_taille(
    fake_hf, fake_ollama, fake_lmstudio
):
    jumeau = _fichier(fake_lmstudio / "editeur" / "depot" / "c.gguf", CONTENU_P)
    records = scan_hf(fake_hf) + scan_ollama(fake_ollama) + scan_tree(fake_lmstudio, "lmstudio")

    candidats = dedup_candidates(records)
    # Seul le .gguf est à relire : ses voisins de 1000 o s'annoncent déjà.
    assert [r.path for r in candidats] == [str(jumeau)]
    assert candidats[0].size == 1000


def test_dedup_candidates_prefiltre_par_taille_et_ignore_liens_et_vides(tmp_path):
    gros_a = _fichier(tmp_path / "gros_a.bin", b"A" * 4096)
    gros_b = _fichier(tmp_path / "gros_b.bin", b"B" * 4096)
    petit_a = _fichier(tmp_path / "petit_a.bin", b"a" * 300)
    petit_b = _fichier(tmp_path / "petit_b.bin", b"b" * 300)
    unique = _fichier(tmp_path / "unique.bin", b"u" * 777)
    vide_a = _fichier(tmp_path / "vide_a.bin", b"")
    vide_b = _fichier(tmp_path / "vide_b.bin", b"")
    dur_a = _fichier(tmp_path / "dur_a.bin", b"D" * 512)
    dur_b = tmp_path / "dur_b.bin"
    os.link(dur_a, dur_b)

    lien = tmp_path / "lien.bin"
    os.symlink(gros_a, lien)
    rec_lien = _observe(gros_a)  # même taille apparente, mais link_kind symlink
    rec_lien.path, rec_lien.link_kind, rec_lien.size = str(lien), "symlink", 0

    records = [
        _observe(p)
        for p in (gros_a, gros_b, petit_a, petit_b, unique, vide_a, vide_b, dur_a, dur_b)
    ] + [rec_lien]

    # Tri : les plus gros d'abord (le gain se mesure en octets), puis le chemin.
    assert [r.path for r in dedup_candidates(records)] == [
        str(gros_a),
        str(gros_b),
        str(petit_a),
        str(petit_b),
    ]


def test_dedup_candidates_ignore_les_fichiers_deja_haches(tmp_path):
    a = _fichier(tmp_path / "a.bin", CONTENU_L)
    b = _fichier(tmp_path / "b.bin", CONTENU_P)
    rec_a, rec_b = _observe(a), _observe(b)
    assert len(dedup_candidates([rec_a, rec_b])) == 2

    rec_a.sha256 = SHA_L
    assert [r.path for r in dedup_candidates([rec_a, rec_b])] == [str(b)]
    rec_b.sha256 = SHA_P
    assert dedup_candidates([rec_a, rec_b]) == []


# --- survie au scan suivant -------------------------------------------------


def test_le_hash_verifie_survit_au_scan_suivant(tmp_path):
    arbre = tmp_path / "declares"
    fichier = _fichier(arbre / "modele.gguf", CONTENU_P)
    config = Config(scan=ScanConfig(declared=[arbre]))
    db = StateDB(tmp_path / "state.db")

    run_scan(config, db)
    rec = _en_base(db, fichier)
    assert rec.sha256 is None  # aucun hash annoncé sur un arbre quelconque

    assert verify_location(db, rec).sha256 == SHA_P

    rapport = run_scan(config, db)
    assert rapport.cached_hashes == 1
    apres = _en_base(db, fichier)
    assert apres.sha256 == SHA_P
    assert apres.meta["hash_source"] == "verified"

    # Fichier réécrit : le cache est périmé, le hash disparaît plutôt que de mentir.
    fichier.write_bytes(CONTENU_X)
    rapport = run_scan(config, db)
    assert rapport.cached_hashes == 0
    assert _en_base(db, fichier).sha256 is None


def test_la_divergence_verifiee_survit_au_scan_suivant(tmp_path, fake_hf):
    config = Config(scan=ScanConfig(hf_hub=fake_hf))
    db = StateDB(tmp_path / "state.db")
    run_scan(config, db)

    (blob,) = [r for r in db.locations() if r.sha256 == SHA_LIVE and r.manager == "hf"]
    assert verify_location(db, blob).mismatch is True

    # Le scan reconstruit l'état observé à partir des noms de blobs : s'il
    # réinstalle le hash annoncé par-dessus le hash lu, la divergence est effacée
    # et le prochain plan destructif rebâtit sur le mensonge.
    run_scan(config, db)
    apres = _en_base(db, blob.path)
    assert apres.sha256 == SHA_L
    assert apres.meta["hash_source"] == "verified"
