"""Quarantaine : déplacement, manifeste, restauration, vidage (tâche 2.3).

Tous les appels passent `home=tmp_path`. Une fixture autouse détourne en plus
`ECURIE_HOME` : même un `home` oublié quelque part ne peut pas atteindre la vraie
quarantaine de la machine.

Les deux derniers tests n'exercent pas le module mais tout le paquet : ils relisent
l'arbre syntaxique de chaque source pour interdire la suppression directe hors de
la quarantaine — c'est le livrable de la tâche 2.3.
"""

import ast
import json
import os
import re
import subprocess
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ecurie_store.trash import (
    MANIFEST_NAME,
    empty_trash,
    list_trash,
    mount_point,
    move_to_trash,
    trash_root_for,
)
from park import REV_LIVE, REV_STALE, SHA_LIVE, SHA_STALE

PAQUET = Path(__file__).parents[1] / "src" / "ecurie_store"


@pytest.fixture(autouse=True)
def home_isole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Filet de sécurité : un `home=None` oublié ne doit pas viser le vrai ~/.ecurie."""
    monkeypatch.setenv("ECURIE_HOME", str(tmp_path / "ecurie-home"))


def fichier(chemin: Path, contenu: bytes) -> Path:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(contenu)
    return chemin


def octets_occupes(racine: Path) -> int:
    """Ce que la quarantaine occupe réellement sur le disque, manifestes compris."""
    if not racine.is_dir():
        return 0
    return sum(p.stat().st_size for p in racine.rglob("*") if p.is_file() and not p.is_symlink())


def antidater(directory: Path, jours: int) -> str:
    """Recule `trashed_at` dans le manifeste : le temps ne se remonte pas en test."""
    manifest_path = directory / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest["trashed_at"] = (datetime.now(UTC) - timedelta(days=jours)).isoformat()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
    return manifest["trashed_at"]


def volume_distinct(monkeypatch: pytest.MonkeyPatch, racine: Path, device: int = 4242424) -> None:
    """Fait passer `racine` et sa descendance pour un autre volume.

    Monter un vrai volume dans une suite de tests n'est pas possible ; seul le
    `st_dev` distingue les volumes, c'est donc lui que l'on truque — le disque,
    lui, reste réel, si bien que les renommages se font pour de bon.
    """
    prefixes = (str(racine), str(racine.resolve()))
    vrais = {"stat": os.stat, "lstat": os.lstat}

    def truqueur(vrai):
        def _stat(chemin, *args, **kwargs):
            st = vrai(chemin, *args, **kwargs)
            try:
                texte = os.fsdecode(chemin)
            except TypeError:  # descripteur de fichier : hors du volume factice
                return st
            if not texte.startswith(prefixes):
                return st
            champs = list(tuple(st))
            champs[2] = device
            return os.stat_result(champs)

        return _stat

    for nom, vrai in vrais.items():
        monkeypatch.setattr(os, nom, truqueur(vrai))


# --- déplacement et manifeste ------------------------------------------------------


def test_move_to_trash_deplace_et_ecrit_un_manifeste_complet(tmp_path: Path):
    source = fichier(tmp_path / "parc" / "model.gguf", b"L" * 1000)
    avant = os.lstat(source)

    entry = move_to_trash(
        source, reason="hf-stale-revision", sha256=SHA_LIVE, plan_id="plan-42", home=tmp_path
    )

    # L'origine a disparu, le contenu est intact ailleurs : déplacé, pas copié ni effacé.
    assert not os.path.lexists(source)
    jour = datetime.now(UTC).strftime("%Y-%m-%d")
    assert entry.directory == tmp_path / "trash" / jour / SHA_LIVE[:12]
    assert entry.payload == entry.directory / "model.gguf"
    assert entry.payload.read_bytes() == b"L" * 1000
    assert os.lstat(entry.payload).st_ino == avant.st_ino  # le rename garde le même inode

    sur_disque = json.loads((entry.directory / MANIFEST_NAME).read_text())
    assert sur_disque == entry.manifest  # ce qui est rendu est ce qui est écrit
    assert sur_disque["origin"] == str(source)
    assert sur_disque["name"] == "model.gguf"
    assert sur_disque["reason"] == "hf-stale-revision"
    assert sur_disque["sha256"] == SHA_LIVE
    assert sur_disque["plan_id"] == "plan-42"
    assert sur_disque["size"] == 1000
    assert sur_disque["mtime"] == avant.st_mtime
    assert sur_disque["inode"] == avant.st_ino
    assert sur_disque["link_kind"] == "plain"

    horodatage = datetime.fromisoformat(sur_disque["trashed_at"])
    assert horodatage.tzinfo is not None  # sans fuseau, la purge à N jours compare n'importe quoi
    assert abs((datetime.now(UTC) - horodatage).total_seconds()) < 60


def test_la_commande_de_restauration_remet_vraiment_le_fichier_en_place(tmp_path: Path):
    source = fichier(tmp_path / "parc" / "model.gguf", b"G" * 2000)
    inode = os.lstat(source).st_ino
    entry = move_to_trash(source, reason="dup-inter-gestionnaires", home=tmp_path)
    assert not os.path.lexists(source)

    # La commande affichée à l'utilisateur est exécutée telle quelle : une commande
    # de restauration qui ne restaure pas serait pire qu'aucune promesse.
    subprocess.run(entry.restore_command, shell=True, check=True, capture_output=True)

    assert source.read_bytes() == b"G" * 2000
    assert os.lstat(source).st_ino == inode
    assert not entry.payload.exists()
    assert (entry.directory / MANIFEST_NAME).is_file()  # la trace du passage reste


def test_deux_fichiers_de_meme_empreinte_le_meme_jour_ne_s_ecrasent_pas(tmp_path: Path):
    # Le cas réel : le même blob dupliqué entre deux gestionnaires, mis en
    # quarantaine le même jour, sous le même nom de fichier.
    hf = fichier(tmp_path / "hf" / "model.safetensors", b"L" * 1000)
    ollama = fichier(tmp_path / "ollama" / "model.safetensors", b"L" * 1000)

    premier = move_to_trash(hf, reason="dup", sha256=SHA_LIVE, home=tmp_path)
    second = move_to_trash(ollama, reason="dup", sha256=SHA_LIVE, home=tmp_path)

    jour = datetime.now(UTC).strftime("%Y-%m-%d")
    assert premier.directory == tmp_path / "trash" / jour / SHA_LIVE[:12]
    assert second.directory == tmp_path / "trash" / jour / f"{SHA_LIVE[:12]}-1"
    assert premier.payload.read_bytes() == b"L" * 1000
    assert second.payload.read_bytes() == b"L" * 1000
    assert premier.manifest["origin"] == str(hf)
    assert second.manifest["origin"] == str(ollama)
    assert {e.origin for e in list_trash(home=tmp_path)} == {str(hf), str(ollama)}


def test_un_lien_symbolique_part_seul_et_sa_cible_reste_intacte(tmp_path: Path, fake_hf: Path):
    depot = next(fake_hf.glob("models--*"))
    lien = depot / "snapshots" / REV_LIVE / "model.safetensors"
    cible = depot / "blobs" / SHA_LIVE
    inode_cible = os.stat(cible).st_ino
    taille_lien = os.lstat(lien).st_size

    entry = move_to_trash(lien, reason="hf-stale-revision", home=tmp_path)

    assert not os.path.lexists(lien)
    assert os.path.islink(entry.payload)
    assert os.readlink(entry.payload) == f"../../blobs/{SHA_LIVE}"
    assert entry.manifest["link_kind"] == "symlink"
    assert entry.manifest["size"] == taille_lien  # la taille du lien, pas celle de sa cible
    assert entry.directory.name == f"inode-{entry.manifest['inode']}"

    # Ce qui compte : le blob de 1000 octets n'a pas été suivi ni touché.
    assert cible.read_bytes() == b"L" * 1000
    assert os.stat(cible).st_ino == inode_cible


def test_un_snapshot_hf_detache_part_en_quarantaine_avec_tout_son_dossier(
    tmp_path: Path, fake_hf: Path
):
    depot = next(fake_hf.glob("models--*"))
    snapshot = depot / "snapshots" / REV_STALE

    entry = move_to_trash(snapshot, reason="detached-snapshot", plan_id="plan-7", home=tmp_path)

    assert not snapshot.exists()
    assert [p.name for p in (depot / "snapshots").iterdir()] == [REV_LIVE]
    assert entry.payload.is_dir()
    assert entry.manifest["name"] == REV_STALE
    assert entry.manifest["link_kind"] == "plain"

    interne = entry.payload / "old.safetensors"
    assert os.path.islink(interne)
    assert os.readlink(interne) == f"../../blobs/{SHA_STALE}"
    assert (depot / "blobs" / SHA_STALE).read_bytes() == b"S" * 500  # le blob n'a pas bougé


# --- inventaire et vidage ----------------------------------------------------------


def test_list_trash_relit_les_manifestes_et_trie_du_plus_recent_au_plus_ancien(tmp_path: Path):
    entrees = {
        nom: move_to_trash(
            fichier(tmp_path / "parc" / nom, b"x" * 10), reason="dup", home=tmp_path
        )
        for nom in ("a.bin", "b.bin", "c.bin", "illisible.bin")
    }
    quand = {
        "a.bin": antidater(entrees["a.bin"].directory, 5),
        "b.bin": antidater(entrees["b.bin"].directory, 1),
        "c.bin": antidater(entrees["c.bin"].directory, 12),
    }
    # Un manifeste corrompu ne doit pas faire exploser l'inventaire, ni un dossier
    # de quarantaine à moitié écrit (interruption pendant `apply`).
    (entrees["illisible.bin"].directory / MANIFEST_NAME).write_text("{ pas du json")
    (tmp_path / "trash" / datetime.now(UTC).strftime("%Y-%m-%d") / "sans-manifeste").mkdir()

    listees = list_trash(home=tmp_path)

    assert [Path(e.origin).name for e in listees] == ["b.bin", "a.bin", "c.bin"]
    # Relu depuis le disque, pas mémorisé au moment du déplacement.
    assert [e.trashed_at for e in listees] == [quand["b.bin"], quand["a.bin"], quand["c.bin"]]
    assert all(e.reason == "dup" and e.size == 10 for e in listees)


def test_empty_trash_efface_et_rend_le_compte(tmp_path: Path):
    for nom, contenu in (("a.gguf", b"A" * 1000), ("b.gguf", b"B" * 2000)):
        move_to_trash(fichier(tmp_path / "parc" / nom, contenu), reason="dup", home=tmp_path)
    racine = tmp_path / "trash"
    avant = octets_occupes(racine)
    assert avant >= 3000

    supprimees, octets = empty_trash(home=tmp_path)

    assert supprimees == 2
    assert octets == avant  # ce qui est annoncé est ce qui a réellement disparu
    assert octets_occupes(racine) == 0
    assert list_trash(home=tmp_path) == []
    assert list(racine.iterdir()) == []  # les dossiers de date vides sont ramassés


def test_empty_trash_older_than_days_epargne_les_entrees_recentes(tmp_path: Path):
    vieux = move_to_trash(
        fichier(tmp_path / "parc" / "vieux.gguf", b"V" * 1000), reason="dup", home=tmp_path
    )
    recent = move_to_trash(
        fichier(tmp_path / "parc" / "recent.gguf", b"R" * 2000), reason="dup", home=tmp_path
    )
    antidater(vieux.directory, 60)
    octets_du_vieux = octets_occupes(vieux.directory)

    supprimees, octets = empty_trash(home=tmp_path, older_than_days=30)

    assert (supprimees, octets) == (1, octets_du_vieux)
    assert not vieux.directory.exists()
    assert recent.payload.read_bytes() == b"R" * 2000
    restantes = list_trash(home=tmp_path)
    assert [e.origin for e in restantes] == [recent.manifest["origin"]]


# --- volumes -----------------------------------------------------------------------


def test_mount_point_remonte_jusqu_au_changement_de_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    volume = tmp_path / "Volumes" / "Parc"
    cible = fichier(volume / "modeles" / "qwen" / "poids.safetensors", b"P" * 64)
    absent = volume / "modeles" / "jamais-ecrit.bin"
    volume_distinct(monkeypatch, volume)

    assert mount_point(cible) == volume.resolve()
    assert mount_point(volume / "modeles") == volume.resolve()
    assert mount_point(volume) == volume.resolve()  # la racine du volume est son propre point
    assert mount_point(absent) == volume.resolve()  # un chemin à créer se rattache déjà au volume


def test_mount_point_sur_le_disque_reel_tombe_sur_un_vrai_point_de_montage(tmp_path: Path):
    cible = fichier(tmp_path / "parc" / "poids.safetensors", b"P" * 64)

    point = mount_point(cible)

    assert os.path.ismount(point)  # oracle indépendant : os.path a son propre calcul
    assert cible.resolve().is_relative_to(point)
    assert mount_point(Path("/")) == Path("/")


def test_un_fichier_d_un_autre_volume_reste_en_quarantaine_sur_son_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    volume = tmp_path / "Volumes" / "Parc"
    cible = fichier(volume / "modeles" / "poids.safetensors", b"P" * 4096)
    volume_distinct(monkeypatch, volume)
    racine_attendue = volume.resolve() / ".ecurie-trash"

    assert trash_root_for(cible, home=tmp_path) == racine_attendue

    entry = move_to_trash(cible, reason="dup", sha256=SHA_LIVE, home=tmp_path)

    # Traverser les volumes coûterait une copie de plusieurs gigaoctets : la
    # quarantaine reste sur place, et rien n'atterrit dans ~/.ecurie/trash.
    assert entry.directory.parent.parent == racine_attendue
    assert entry.payload.read_bytes() == b"P" * 4096
    assert [e.origin for e in list_trash(roots=[racine_attendue])] == [str(cible)]
    # Ces entrées-là n'existent que si l'appelant nomme le volume : l'inventaire
    # du seul `home` les ignore, et c'est à lui de les rapatrier.
    assert list_trash(home=tmp_path) == []


# --- livrable 2.3 : aucune suppression directe possible dans le code ----------------

SUPPRESSIONS = {"remove", "removedirs", "rmdir", "rmtree", "unlink"}
FICHIERS_AUTORISES = {"trash.py", "apply.py"}
JUSTIFICATION = re.compile(r"#\s*rm-ok\s*:\s*\S")


@dataclass(frozen=True)
class Suppression:
    fichier: str
    ligne: int
    source: str
    justifiee: bool


def suppressions(source: str, fichier: str) -> list[Suppression]:
    """Repère les appels destructeurs et dit si chacun porte son « # rm-ok: »."""
    lignes = source.splitlines()
    trouvees: list[Suppression] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        appel = node.func
        if isinstance(appel, ast.Attribute):  # os.remove, shutil.rmtree, Path(p).unlink
            nom = appel.attr
        elif isinstance(appel, ast.Name):  # from os import unlink
            nom = appel.id
        else:
            continue
        if nom not in SUPPRESSIONS:
            continue
        bloc = "\n".join(lignes[node.lineno - 1 : node.end_lineno or node.lineno])
        trouvees.append(
            Suppression(fichier, node.lineno, bloc.strip(), bool(JUSTIFICATION.search(bloc)))
        )
    return trouvees


def test_aucune_suppression_directe_hors_de_la_quarantaine():
    sources = sorted(PAQUET.rglob("*.py"))
    assert len(sources) >= 10, f"paquet introuvable sous {PAQUET} — l'analyse porterait sur rien"

    trouvees = [
        s
        for chemin in sources
        for s in suppressions(chemin.read_text(), str(chemin.relative_to(PAQUET)))
    ]
    # `empty_trash` est le seul droit d'effacer du paquet : s'il n'efface plus rien,
    # l'analyse ne mord plus sur rien non plus et ce test deviendrait décoratif.
    assert [s for s in trouvees if s.fichier == "trash.py"]

    ailleurs = [s for s in trouvees if s.fichier not in FICHIERS_AUTORISES]
    assert ailleurs == [], (
        "suppression directe hors de la quarantaine : "
        + "; ".join(f"{s.fichier}:{s.ligne} {s.source}" for s in ailleurs)
    )
    sans_motif = [s for s in trouvees if not s.justifiee]
    assert sans_motif == [], (
        "suppression sans « # rm-ok: <motif> » sur sa ligne : "
        + "; ".join(f"{s.fichier}:{s.ligne} {s.source}" for s in sans_motif)
    )


def test_l_analyse_attrape_une_suppression_ajoutee_demain():
    ajout = textwrap.dedent(
        """
        import os
        import shutil
        from pathlib import Path

        def nettoyer(p):
            os.remove(p)
            shutil.rmtree(p)
            Path(p).unlink()
            Path(p).rmdir()
            os.rename(p, f"{p}.bak")
            os.replace(p, f"{p}.bak")
        """
    )
    trouvees = suppressions(ajout, "scan.py")
    assert len(trouvees) == 4  # ni rename ni replace : déplacer n'est pas effacer
    assert not any(s.justifiee for s in trouvees)

    multiligne = "import shutil\nshutil.rmtree(\n    dossier,\n)  # rm-ok: vidage explicite\n"
    assert suppressions(multiligne, "trash.py")[0].justifiee
    sans_motif = "import shutil\nshutil.rmtree(dossier)  # rm-ok:\n"
    assert not suppressions(sans_motif, "trash.py")[0].justifiee
