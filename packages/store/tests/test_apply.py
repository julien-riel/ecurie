"""Exécution d'un plan (CONCEPTION.md §4.3) — sur de vrais fichiers et de vrais inodes.

C'est le seul module du dépôt qui déplace des octets. Les tests travaillent donc
sur des fichiers réels dans `tmp_path`, créent de vrais liens durs, et vérifient
après chaque action que le disque dit exactement ce que le rapport prétend.

`home` est toujours passé explicitement : aucun test ne doit pouvoir écrire dans
le `~/.ecurie` de la machine.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from ecurie_store.apply import APPLIED, SKIPPED, apply_plan

CONTENU = b"P" * 4096
AUTRE = b"Q" * 4096  # même longueur : seule la relecture du contenu peut les distinguer


# --- outillage local ---------------------------------------------------------------


def _sha(path: Path) -> str:
    """Hachage indépendant du module testé — sinon un bug de hachage passerait deux fois."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _empreinte(path: Path) -> dict[str, Any]:
    st = os.lstat(path)
    return {"size": st.st_size, "mtime": st.st_mtime, "inode": st.st_ino, "device": st.st_dev}


def _action_hardlink(keep: Path, replace: list[Path], **surcharges: Any) -> dict[str, Any]:
    action: dict[str, Any] = {
        "kind": "hardlink",
        "keep": str(keep),
        "replace": [str(p) for p in replace],
        "sha256": _sha(keep),
        "reason": "duplication",
        "bytes_reclaimed": sum(os.lstat(p).st_size for p in replace),
        "stats": {str(p): _empreinte(p) for p in [keep, *replace]},
    }
    action.update(surcharges)
    return action


def _action_trash(path: Path, *, reason: str = "orphan-blob", **surcharges: Any) -> dict[str, Any]:
    dossier = path.is_dir() and not path.is_symlink()
    action: dict[str, Any] = {
        "kind": "trash",
        "path": str(path),
        "reason": reason,
        "sha256": None if dossier else _sha(path),
        "bytes_reclaimed": 0 if dossier else os.lstat(path).st_size,
        "stats": {} if dossier else {str(path): _empreinte(path)},
    }
    action.update(surcharges)
    return action


def _plan(*actions: dict[str, Any], plan_id: str = "plan-de-test") -> dict[str, Any]:
    return {
        "plan_version": 1,
        "plan_id": plan_id,
        "scan_id": "scan-de-test",
        "actions": list(actions),
    }


def _inode(path: Path) -> int:
    return os.lstat(path).st_ino


def _reecrire_sur_place(path: Path, contenu: bytes) -> None:
    """Ment sur le contenu sans rien changer de l'empreinte : taille, mtime et inode tenus.

    C'est le scénario que seul un re-hachage à l'exécution peut attraper.
    """
    st = os.lstat(path)
    assert len(contenu) == st.st_size
    with open(path, "r+b") as fichier:
        fichier.seek(0)
        fichier.write(contenu)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    apres = os.lstat(path)
    assert (apres.st_size, apres.st_mtime, apres.st_ino) == (st.st_size, st.st_mtime, st.st_ino)


def _journal(home: Path, plan_id: str = "plan-de-test") -> list[dict[str, Any]]:
    chemin = home / "journal" / f"{plan_id}.jsonl"
    return [json.loads(ligne) for ligne in chemin.read_text(encoding="utf-8").splitlines()]


def _quarantaine(home: Path) -> list[Path]:
    """Les répertoires d'entrée sous <home>/trash/<date>/<empreinte>/."""
    return sorted(p.parent for p in (home / "trash").glob("*/*/manifest.json"))


@pytest.fixture
def paire(tmp_path: Path) -> tuple[Path, Path]:
    """Deux copies distinctes du même contenu : le cas nominal de la déduplication."""
    parc = tmp_path / "parc"
    parc.mkdir()
    a, b = parc / "a.gguf", parc / "b.gguf"
    a.write_bytes(CONTENU)
    b.write_bytes(CONTENU)
    assert _inode(a) != _inode(b)
    return a, b


@pytest.fixture
def trio(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Un survivant et deux copies du même contenu, sur trois inodes distincts."""
    parc = tmp_path / "parc"
    parc.mkdir()
    chemins = tuple(parc / nom for nom in ("keep.bin", "c1.bin", "c2.bin"))
    for chemin in chemins:
        chemin.write_bytes(CONTENU)
    assert len({_inode(p) for p in chemins}) == 3
    return chemins


@pytest.fixture
def snapshot_detache(tmp_path: Path) -> tuple[Path, Path]:
    """Un dossier `snapshots/<révision>` qui ne contient que des liens symboliques."""
    hub = tmp_path / "hub"
    blobs = hub / "blobs"
    blobs.mkdir(parents=True)
    blob = blobs / "blob-obsolete"
    blob.write_bytes(b"S" * 500)
    snapshot = hub / "snapshots" / ("d" * 40)
    snapshot.mkdir(parents=True)
    os.symlink(Path("..") / ".." / "blobs" / blob.name, snapshot / "old.safetensors")
    return snapshot, blob


# --- déduplication par lien dur ------------------------------------------------------


def test_hardlink_fusionne_les_inodes_sans_perdre_un_octet(paire, tmp_path):
    a, b = paire
    report = apply_plan(_plan(_action_hardlink(a, [b])), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == APPLIED
    assert outcome.bytes_reclaimed == 4096  # la taille de la copie remplacée, pas celle du groupe
    assert report.applied_bytes == 4096

    assert a.exists() and b.exists()
    assert _inode(a) == _inode(b)
    assert a.read_bytes() == CONTENU
    assert b.read_bytes() == CONTENU
    assert os.lstat(a).st_nlink == 2
    # Le nom temporaire du remplacement atomique ne doit pas survivre au rename.
    assert sorted(p.name for p in a.parent.iterdir()) == ["a.gguf", "b.gguf"]


def test_hardlink_refuse_une_copie_dont_le_contenu_a_change(paire, tmp_path):
    """Empreinte intacte, contenu différent : seul le re-hachage à l'exécution le voit."""
    a, b = paire
    action = _action_hardlink(a, [b])
    _reecrire_sur_place(b, AUTRE)

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert outcome.bytes_reclaimed == 0
    assert str(b) in outcome.message
    assert _inode(a) != _inode(b)
    assert a.read_bytes() == CONTENU
    assert b.read_bytes() == AUTRE  # rien n'a été écrasé : le contenu divergent survit


def test_hardlink_refuse_quand_le_fichier_garde_a_change(paire, tmp_path):
    """Le survivant est haché lui aussi — sans quoi la dédup propagerait un contenu faux."""
    a, b = paire
    action = _action_hardlink(a, [b])
    _reecrire_sur_place(a, AUTRE)

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert "contenu changé depuis le scan" in outcome.message
    assert _inode(a) != _inode(b)
    assert b.read_bytes() == CONTENU


def _changer_la_taille(path: Path) -> None:
    with open(path, "ab") as fichier:
        fichier.write(b"!")


def _changer_le_mtime(path: Path) -> None:
    st = os.lstat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def _changer_l_inode(path: Path) -> None:
    """Réécriture par rename : même taille, même mtime restauré, autre inode."""
    st = os.lstat(path)
    neuf = path.with_name(path.name + ".neuf")
    neuf.write_bytes(path.read_bytes())
    os.replace(neuf, path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.lstat(path).st_ino != st.st_ino


@pytest.mark.parametrize(
    ("derive", "motif"),
    [
        (_changer_la_taille, "taille changée depuis le scan"),
        (_changer_le_mtime, "date de modification changée depuis le scan"),
        (_changer_l_inode, "inode changé depuis le scan"),
    ],
    ids=["taille", "mtime", "inode"],
)
def test_hardlink_refuse_toute_derive_d_empreinte(paire, tmp_path, derive, motif):
    a, b = paire
    action = _action_hardlink(a, [b])
    derive(b)

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert outcome.bytes_reclaimed == 0
    assert outcome.message.startswith(f"{b} : ")
    assert motif in outcome.message
    assert _inode(a) != _inode(b)


def test_hardlink_refuse_un_fichier_disparu(paire, tmp_path):
    a, b = paire
    action = _action_hardlink(a, [b])
    b.unlink()

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert "disparu" in outcome.message
    assert a.read_bytes() == CONTENU


def test_hardlink_ignore_deux_chemins_deja_sur_le_meme_inode(paire, tmp_path):
    a, b = paire
    b.unlink()
    os.link(a, b)
    assert _inode(a) == _inode(b)
    action = _action_hardlink(a, [b])

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert outcome.bytes_reclaimed == 0  # rien à gagner deux fois sur le même inode
    assert report.applied_bytes == 0
    assert _inode(a) == _inode(b)
    assert a.read_bytes() == CONTENU
    assert b.read_bytes() == CONTENU


def test_hardlink_refuse_deux_volumes_differents(paire, tmp_path, monkeypatch):
    """Un lien dur ne traverse pas un point de montage — vérifié sur `st_dev`, pas sur le plan.

    Aucun second volume n'est nécessaire : on ment sur le `st_dev` de la copie.
    """
    a, b = paire
    action = _action_hardlink(a, [b])
    vrai_lstat = os.lstat

    class _AutreVolume:
        def __init__(self, st: os.stat_result) -> None:
            self._st = st
            self.st_dev = st.st_dev + 1

        def __getattr__(self, nom: str) -> Any:
            return getattr(self._st, nom)

    def lstat_truque(chemin, *args, **kwargs):
        st = vrai_lstat(chemin, *args, **kwargs)
        return _AutreVolume(st) if os.fspath(chemin) == str(b) else st

    monkeypatch.setattr(os, "lstat", lstat_truque)
    report = apply_plan(_plan(action), home=tmp_path)
    monkeypatch.undo()

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert "autre volume" in outcome.message
    assert _inode(a) != _inode(b)
    assert b.read_bytes() == CONTENU


def test_hardlink_sans_sha256_refuse_l_action(paire, tmp_path):
    """Deux tailles égales ne prouvent rien : sans hash au plan, on ne touche à rien."""
    a, b = paire
    report = apply_plan(_plan(_action_hardlink(a, [b], sha256=None)), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert "sha256" in outcome.message
    assert _inode(a) != _inode(b)


def test_hardlink_refuse_un_lien_symbolique(paire, tmp_path):
    """Dédupliquer un lien symbolique le remplacerait par une vraie copie : refus."""
    a, b = paire
    b.unlink()
    os.symlink(a, b)
    action = _action_hardlink(a, [b])

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert "lien symbolique" in outcome.message
    assert b.is_symlink()


def test_hardlink_compte_le_gain_par_inode_et_non_par_chemin(trio, tmp_path):
    """Deux chemins du même inode ne rendent qu'une fois leurs octets."""
    keep, copie1, copie2 = trio
    copie2.unlink()
    os.link(copie1, copie2)  # les deux copies sont déjà un seul et même fichier
    assert _inode(copie1) == _inode(copie2) != _inode(keep)
    action = _action_hardlink(keep, [copie1, copie2])

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == APPLIED
    assert outcome.bytes_reclaimed == 4096  # une seule copie physique disparaît, pas deux
    assert _inode(copie1) == _inode(copie2) == _inode(keep)
    assert os.lstat(keep).st_nlink == 3
    assert copie1.read_bytes() == CONTENU
    assert copie2.read_bytes() == CONTENU


def test_hardlink_refuse_si_un_autre_chemin_tient_encore_le_contenu(trio, tmp_path):
    """Un inode dont un seul chemin sur deux passe au lien dur ne libère rien.

    Annoncer le gain quand même, c'est faire croire à des octets qui n'arrivent jamais.
    """
    keep, copie1, copie2 = trio
    copie2.unlink()
    os.link(copie1, copie2)
    action = _action_hardlink(keep, [copie1])  # copie2 reste hors du plan

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert outcome.bytes_reclaimed == 0
    assert "rien ne serait libéré" in outcome.message
    assert _inode(copie1) == _inode(copie2) != _inode(keep)
    assert copie1.read_bytes() == CONTENU


def test_une_action_est_atomique_un_refus_tardif_ne_fusionne_rien(trio, tmp_path):
    """Une action à plusieurs copies dont l'une dérive est refusée en bloc.

    Fusionner la première puis refuser la seconde laisserait le disque modifié
    derrière un verdict `skipped` à 0 octet : le journal deviendrait faux, donc
    la reprise impossible.
    """
    keep, copie1, copie2 = trio
    action = _action_hardlink(keep, [copie1, copie2])
    action["stats"][str(copie2)]["size"] += 1  # dérive sur la seconde copie seulement

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert outcome.bytes_reclaimed == 0
    assert len({_inode(keep), _inode(copie1), _inode(copie2)}) == 3  # aucune fusion
    assert copie1.read_bytes() == CONTENU
    assert _journal(tmp_path)[1]["bytes_reclaimed"] == 0


# --- quarantaine ---------------------------------------------------------------------


def test_trash_deplace_en_quarantaine_et_le_contenu_reste_recuperable(tmp_path):
    orphelin = tmp_path / "parc" / "orphelin.bin"
    orphelin.parent.mkdir()
    orphelin.write_bytes(b"O" * 200)
    action = _action_trash(orphelin)

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == APPLIED
    assert outcome.bytes_reclaimed == 200
    assert not orphelin.exists()

    (entree,) = _quarantaine(tmp_path)
    assert entree.parent.parent == tmp_path / "trash"  # <home>/trash/<date>/<empreinte>/
    assert entree.name == action["sha256"][:12]
    assert (entree / "orphelin.bin").read_bytes() == b"O" * 200
    manifest = json.loads((entree / "manifest.json").read_text())
    assert manifest["origin"] == str(orphelin)
    assert manifest["reason"] == "orphan-blob"
    assert manifest["plan_id"] == "plan-de-test"


def test_trash_refuse_un_sha256_qui_ne_correspond_plus(tmp_path):
    fichier = tmp_path / "parc" / "blob.bin"
    fichier.parent.mkdir()
    fichier.write_bytes(CONTENU)
    action = _action_trash(fichier)
    _reecrire_sur_place(fichier, AUTRE)

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert outcome.bytes_reclaimed == 0
    assert "contenu changé depuis le scan" in outcome.message
    assert fichier.read_bytes() == AUTRE  # toujours en place, intouché
    assert _quarantaine(tmp_path) == []


def test_trash_refuse_une_derive_d_empreinte(tmp_path):
    fichier = tmp_path / "parc" / "blob.bin"
    fichier.parent.mkdir()
    fichier.write_bytes(CONTENU)
    action = _action_trash(fichier)
    _changer_la_taille(fichier)

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert "taille changée depuis le scan" in outcome.message
    assert fichier.exists()
    assert _quarantaine(tmp_path) == []


def test_trash_accepte_un_snapshot_detache_de_liens_seulement(snapshot_detache, tmp_path):
    snapshot, blob = snapshot_detache
    action = _action_trash(snapshot, reason="detached-snapshot")

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == APPLIED
    assert outcome.bytes_reclaimed == 0  # un dossier de liens ne rend aucun octet
    assert not snapshot.exists()
    (entree,) = _quarantaine(tmp_path)
    assert (entree / snapshot.name / "old.safetensors").is_symlink()
    assert blob.exists()  # la cible n'est pas emportée par le déménagement du lien


def test_trash_refuse_un_snapshot_contenant_un_vrai_fichier(snapshot_detache, tmp_path):
    snapshot, _ = snapshot_detache
    intrus = snapshot / "poids.safetensors"
    intrus.write_bytes(b"W" * 1234)
    action = _action_trash(snapshot, reason="detached-snapshot")

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert "autre chose que des liens" in outcome.message
    assert intrus.read_bytes() == b"W" * 1234
    assert (snapshot / "old.safetensors").is_symlink()
    assert _quarantaine(tmp_path) == []


def test_trash_refuse_un_dossier_sous_un_autre_motif(tmp_path):
    """Seul `detached-snapshot` autorise un dossier — ailleurs c'est un plan corrompu."""
    dossier = tmp_path / "parc" / "un-dossier"
    dossier.mkdir(parents=True)
    (dossier / "poids.bin").write_bytes(b"W" * 10)
    action = _action_trash(dossier, reason="orphan-blob")

    report = apply_plan(_plan(action), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert "dossier inattendu" in outcome.message
    assert (dossier / "poids.bin").exists()


# --- dry-run, journal, robustesse ------------------------------------------------------


def test_dry_run_ne_touche_a_rien_et_rend_les_memes_verdicts(paire, tmp_path):
    a, b = paire
    orphelin = tmp_path / "parc" / "orphelin.bin"
    orphelin.write_bytes(b"O" * 200)
    perdu = tmp_path / "parc" / "perdu.bin"
    perdu.write_bytes(CONTENU)
    action_perdue = _action_trash(perdu)
    _reecrire_sur_place(perdu, AUTRE)  # une action qui doit être refusée dans les deux modes
    plan = _plan(_action_hardlink(a, [b]), _action_trash(orphelin), action_perdue)

    avant = {p: (_inode(p), p.read_bytes()) for p in (a, b, orphelin, perdu)}
    blanc = apply_plan(plan, dry_run=True, home=tmp_path)

    assert blanc.dry_run is True
    assert blanc.journal_path is None
    assert not (tmp_path / "journal").exists()
    assert not (tmp_path / "trash").exists()
    assert {p: (_inode(p), p.read_bytes()) for p in (a, b, orphelin, perdu)} == avant

    reel = apply_plan(plan, home=tmp_path)
    verdicts = [(o.kind, o.status, o.bytes_reclaimed, o.message) for o in reel.outcomes]
    assert [(o.kind, o.status, o.bytes_reclaimed, o.message) for o in blanc.outcomes] == verdicts
    assert blanc.applied_bytes == reel.applied_bytes == 4296
    assert blanc.counts == reel.counts == {APPLIED: 2, SKIPPED: 1}


def test_le_journal_consigne_le_depart_chaque_action_et_la_fin(paire, tmp_path):
    a, b = paire
    absent = tmp_path / "parc" / "absent.bin"
    absent.write_bytes(b"z" * 12)
    action_refusee = _action_trash(absent)
    _changer_la_taille(absent)
    plan = _plan(_action_hardlink(a, [b]), action_refusee)

    report = apply_plan(plan, home=tmp_path)

    assert report.journal_path == tmp_path / "journal" / "plan-de-test.jsonl"
    lignes = _journal(tmp_path)
    assert [ligne["event"] for ligne in lignes] == ["start", "action", "action", "end"]

    depart, dedup, refus, fin = lignes
    assert depart["plan_id"] == "plan-de-test"
    assert depart["scan_id"] == "scan-de-test"
    assert depart["dry_run"] is False
    assert depart["at"]

    assert (dedup["index"], dedup["kind"], dedup["status"]) == (0, "hardlink", APPLIED)
    assert dedup["bytes_reclaimed"] == 4096
    assert sorted(dedup["paths"]) == sorted([str(a), str(b)])

    assert (refus["index"], refus["kind"], refus["status"]) == (1, "trash", SKIPPED)
    assert "taille changée depuis le scan" in refus["message"]  # le motif du refus est consigné

    assert fin["applied_bytes"] == 4096
    assert fin["counts"] == {APPLIED: 1, SKIPPED: 1}


def test_une_action_de_type_inconnu_est_ignoree_sans_rien_executer(paire, tmp_path):
    a, b = paire
    inconnue = {"kind": "supprimer", "path": str(b), "reason": "?", "bytes_reclaimed": 4096}

    report = apply_plan(_plan(inconnue), home=tmp_path)

    (outcome,) = report.outcomes
    assert outcome.status == SKIPPED
    assert "action inconnue" in outcome.message
    assert outcome.bytes_reclaimed == 0
    assert report.applied_bytes == 0
    assert b.read_bytes() == CONTENU
    assert _inode(a) != _inode(b)
    assert _journal(tmp_path)[1]["kind"] == "supprimer"  # ignorée, mais journalisée


def test_rejouer_le_meme_plan_ne_detruit_rien_et_n_invente_aucun_gain(paire, tmp_path):
    a, b = paire
    orphelin = tmp_path / "parc" / "orphelin.bin"
    orphelin.write_bytes(b"O" * 200)
    plan = _plan(_action_hardlink(a, [b]), _action_trash(orphelin))

    premier = apply_plan(plan, home=tmp_path)
    assert premier.applied_bytes == 4296

    second = apply_plan(plan, home=tmp_path)

    assert second.applied_bytes == 0
    assert [o.status for o in second.outcomes] == [SKIPPED, SKIPPED]
    # La copie porte désormais l'empreinte du fichier gardé : le plan ne la reconnaît plus.
    assert "depuis le scan" in second.outcomes[0].message
    assert second.outcomes[1].message == "déjà disparu"

    assert a.read_bytes() == CONTENU
    assert b.read_bytes() == CONTENU
    assert _inode(a) == _inode(b)
    assert len(_quarantaine(tmp_path)) == 1  # une seule entrée, pas de doublon de quarantaine

    lignes = _journal(tmp_path)
    assert [ligne["event"] for ligne in lignes].count("start") == 2
    assert lignes[-1]["applied_bytes"] == 0  # la seconde passe ne réclame aucun octet


def test_la_place_libre_est_mesuree_et_non_seulement_deduite(paire, tmp_path):
    """Le critère de sortie du jalon parle d'espace *récupéré*, pas d'espace annoncé.
    Un clone APFS, un fichier creux, une écriture concurrente : rien de tout cela ne
    se déduit d'un inventaire. Le relevé de place libre, lui, ne se discute pas."""
    a, b = paire
    plan = _plan(_action_hardlink(a, [b]))

    report = apply_plan(plan, home=tmp_path / "home")

    assert report.measured_bytes is not None
    fin = _journal(tmp_path / "home")[-1]
    assert fin["event"] == "end"
    assert fin["measured_bytes"] == report.measured_bytes

    # À blanc, il n'y a rien à mesurer : annoncer un relevé serait un mensonge.
    à_blanc = apply_plan(_plan(_action_hardlink(a, [b])), home=tmp_path / "home", dry_run=True)
    assert à_blanc.measured_bytes is None


def test_le_journal_est_ecrit_au_fil_de_l_eau(paire, tmp_path):
    """Une interruption au milieu d'un plan doit laisser la trace de ce qui a déjà
    bougé : le journal se relit donc pendant l'exécution, pas seulement après."""
    a, b = paire
    home = tmp_path / "home"
    vues: list[int] = []

    def pendant(_outcome):
        vues.append(len(_journal(home)))

    apply_plan(_plan(_action_hardlink(a, [b])), home=home, on_outcome=pendant)

    assert vues == [2]  # la ligne « start » et celle de l'action, déjà sur le disque
