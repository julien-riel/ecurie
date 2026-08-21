"""Registre des résidents : l'état partagé entre deux commandes, et ce qui ment dedans.

Aucun worker n'est lancé ici — les entrées sont fabriquées à la main, avec des PID
choisis. C'est précisément ce que le registre doit savoir faire : juger des entrées
qu'il n'a pas produites, écrites par une autre commande, une autre version, ou une
session tuée au milieu d'une écriture.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from ecurie_runtime import residents
from ecurie_runtime.admission import Resident
from ecurie_runtime.residents import (
    SUN_PATH_MAX,
    ResidentEntry,
    ResidentRegistry,
    socket_dir,
    socket_path,
)

GIB = 1 << 30
INSTANT = 1_755_700_000.0  # horloge figée : `touch` doit écrire une valeur, pas « plus tard »


@pytest.fixture
def home(tmp_path: Path) -> Path:
    racine = tmp_path / "ecurie-home"
    racine.mkdir()
    return racine


@pytest.fixture
def sockets(monkeypatch) -> Iterator[Path]:
    """Répertoire de sockets court et jetable — celui de production est partagé.

    Court volontairement : `tmp_path` de pytest dépasse à lui seul les 104 octets
    de `sun_path` sur macOS, et `socket_path` refuserait d'y poser un socket.
    """
    court = Path(tempfile.mkdtemp(prefix="ec-"))
    monkeypatch.setenv("ECURIE_SOCKET_DIR", str(court))
    yield court
    shutil.rmtree(court, ignore_errors=True)


@pytest.fixture
def pid_mort() -> int:
    """Un PID qui a existé et n'existe plus : un fils lancé, terminé, puis moissonné."""
    fils = subprocess.Popen([sys.executable, "-c", ""])
    assert fils.wait() == 0  # moissonné : plus de zombie, le PID ne répond plus
    return fils.pid


def _socket(sockets: Path, ref: str) -> Path:
    chemin = sockets / f"{ref.replace('@', '-')}.sock"
    chemin.touch()
    return chemin


def _entree(sockets: Path, ref: str, **champs) -> ResidentEntry:
    """Une entrée vivante par défaut : notre propre PID, un socket qui existe."""
    champs.setdefault("pid", os.getpid())
    champs.setdefault("socket", str(_socket(sockets, ref)))
    champs.setdefault("peak_bytes", 2 * GIB)
    return ResidentEntry(ref=ref, **champs)


def _document(home: Path) -> dict:
    return json.loads((home / "residents.json").read_text(encoding="utf-8"))


# --- aller-retour sur disque -------------------------------------------------


def test_une_entree_relue_est_identique_a_celle_ecrite(home, sockets):
    registre = ResidentRegistry(home)
    entrée = ResidentEntry(
        ref="tts-test@essai",
        pid=os.getpid(),
        socket=str(_socket(sockets, "tts-test@essai")),
        peak_bytes=3 * GIB,
        runtime="mlx-audio",
        env="mlx-audio",
        loaded_at="2026-08-20T10:00:00+00:00",
        last_used=INSTANT,
        pinned=True,
        warmup_ms=2400,
        options={"voice": "af_heart", "sample_rate": 24000},
        log=str(home / "workers" / "tts-test@essai.log"),
    )

    with registre.locked() as entries:
        entries["tts-test@essai"] = entrée

    relu = registre.read()
    assert list(relu) == ["tts-test@essai"]
    assert asdict(relu["tts-test@essai"]) == asdict(entrée)
    # La référence est la clé du document : la répéter dans la valeur autoriserait
    # deux vérités sur la même entrée.
    assert "ref" not in _document(home)["residents"]["tts-test@essai"]
    assert _document(home)["version"] == residents.VERSION


def test_un_registre_absent_se_lit_comme_un_registre_vide(home):
    registre = ResidentRegistry(home)
    assert registre.read() == {}
    assert registre.stale() == []
    assert not registre.path.exists()  # lire ne crée rien


# --- ce qui n'est plus là ----------------------------------------------------


def test_une_entree_dont_le_processus_est_mort_est_ecartee_de_la_lecture(home, sockets, pid_mort):
    registre = ResidentRegistry(home)
    with registre.locked() as entries:
        entries["vivant@essai"] = _entree(sockets, "vivant@essai")
        entries["fantome@essai"] = _entree(sockets, "fantome@essai", pid=pid_mort)

    assert list(registre.read()) == ["vivant@essai"]
    assert [e.ref for e in registre.stale()] == ["fantome@essai"]
    # Son socket est toujours là : c'est bien le PID qui l'a disqualifié.
    assert Path(registre.stale()[0].socket).exists()


def test_une_entree_dont_le_socket_a_disparu_est_ecartee_de_la_lecture(home, sockets):
    registre = ResidentRegistry(home)
    with registre.locked() as entries:
        entries["vivant@essai"] = _entree(sockets, "vivant@essai")
        entries["sourd@essai"] = _entree(sockets, "sourd@essai")
    Path(registre.read()["sourd@essai"].socket).unlink()

    assert list(registre.read()) == ["vivant@essai"]
    assert [e.ref for e in registre.stale()] == ["sourd@essai"]
    # Le processus, lui, est bien vivant : un worker injoignable reste un fantôme.
    assert registre.stale()[0].pid == os.getpid()


def test_la_lecture_ne_reecrit_pas_le_fichier(home, sockets, pid_mort):
    """`read()` s'exécute hors du verrou : si elle purgeait le fichier, un `ecurie
    run` concurrent verrait l'entrée qu'il vient d'inscrire disparaître sous lui."""
    registre = ResidentRegistry(home)
    with registre.locked() as entries:
        entries["fantome@essai"] = _entree(sockets, "fantome@essai", pid=pid_mort)
    avant = registre.path.read_text(encoding="utf-8")

    assert registre.read() == {}
    assert len(registre.stale()) == 1
    assert registre.path.read_text(encoding="utf-8") == avant


def test_locked_purge_les_entrees_mortes_en_entrant(home, sockets, pid_mort):
    """Le dictionnaire remis au bloc sert à décider du budget mémoire : une entrée
    fantôme y réserverait des gigaoctets pour un processus qui n'existe plus."""
    registre = ResidentRegistry(home)
    with registre.locked() as entries:
        entries["fantome@essai"] = _entree(sockets, "fantome@essai", pid=pid_mort)
        entries["vivant@essai"] = _entree(sockets, "vivant@essai")

    with registre.locked() as entries:
        assert list(entries) == ["vivant@essai"]
    assert registre.stale() == []  # purgé du fichier, et pas seulement de la vue


# --- transactions : le bloc `locked()` réécrit même quand on en sort par un return ---


def test_forget_retire_l_entree_du_fichier_malgre_le_return(home, sockets):
    """`forget` quitte le bloc `locked()` par un `return` : la réécriture doit avoir
    lieu quand même, sinon le résident oublié réapparaît à la commande suivante."""
    registre = ResidentRegistry(home)
    with registre.locked() as entries:
        entries["a@essai"] = _entree(sockets, "a@essai")
        entries["b@essai"] = _entree(sockets, "b@essai")

    oubliée = registre.forget("a@essai")
    assert oubliée is not None
    assert oubliée.ref == "a@essai"
    assert list(_document(home)["residents"]) == ["b@essai"]
    assert registre.forget("a@essai") is None


def test_set_pinned_persiste_l_epinglage_malgre_le_return(home, sockets):
    registre = ResidentRegistry(home)
    with registre.locked() as entries:
        entries["a@essai"] = _entree(sockets, "a@essai")

    assert registre.set_pinned("a@essai", True) is True
    assert registre.read()["a@essai"].pinned is True
    assert registre.set_pinned("a@essai", False) is True
    assert registre.read()["a@essai"].pinned is False


def test_set_pinned_sur_une_reference_absente_rend_faux(home, sockets):
    registre = ResidentRegistry(home)
    with registre.locked() as entries:
        entries["a@essai"] = _entree(sockets, "a@essai")

    assert registre.set_pinned("jamais-charge@essai", True) is False
    assert list(registre.read()) == ["a@essai"]


def test_touch_met_a_jour_la_derniere_utilisation(home, sockets, monkeypatch):
    registre = ResidentRegistry(home)
    with registre.locked() as entries:
        entries["a@essai"] = _entree(sockets, "a@essai", last_used=0.0)
    monkeypatch.setattr(residents, "time", SimpleNamespace(time=lambda: INSTANT))

    registre.touch("a@essai")
    assert registre.read()["a@essai"].last_used == INSTANT

    registre.touch("jamais-charge@essai")  # référence inconnue : sans effet, sans erreur
    assert list(registre.read()) == ["a@essai"]


# --- un fichier qu'on n'a pas écrit ------------------------------------------


@pytest.mark.parametrize(
    "contenu",
    ['{"version": 1, "residents": {"a@essai": {"pid": 4', "", "ceci n'est pas du JSON"],
    ids=["tronqué", "vide", "illisible"],
)
def test_un_registre_illisible_se_lit_vide_au_lieu_de_lever(home, sockets, contenu):
    """État observé, donc reconstructible : une écriture interrompue par un disque
    plein ne doit pas empêcher la commande suivante de lancer un worker."""
    registre = ResidentRegistry(home)
    registre.path.write_text(contenu, encoding="utf-8")

    assert registre.read() == {}
    assert registre.stale() == []

    with registre.locked() as entries:
        entries["a@essai"] = _entree(sockets, "a@essai")
    assert list(registre.read()) == ["a@essai"]


def test_un_registre_de_forme_inattendue_se_lit_vide_au_lieu_de_lever(home):
    registre = ResidentRegistry(home)
    registre.path.write_text(json.dumps([{"ref": "a@essai"}]), encoding="utf-8")
    assert registre.read() == {}


def test_une_entree_d_une_autre_version_est_ignoree_sans_emporter_les_autres(home, sockets):
    """Un champ inconnu vient d'une version antérieure ou postérieure d'Écurie : on
    perd cette entrée-là, jamais le fichier entier — sinon une mise à jour d'Écurie
    rendrait invisible d'un coup tout ce qui est chaud en mémoire."""
    registre = ResidentRegistry(home)
    with registre.locked() as entries:
        entries["bonne@essai"] = _entree(sockets, "bonne@essai")

    doc = _document(home)
    doc["residents"]["ancienne@essai"] = {**doc["residents"]["bonne@essai"], "gpu_layers": 30}
    registre.path.write_text(json.dumps(doc), encoding="utf-8")

    assert list(registre.read()) == ["bonne@essai"]
    assert registre.stale() == []  # ni vivante, ni périmée : simplement inconnue


# --- sockets : le nom, sa stabilité, sa longueur ------------------------------


def test_le_socket_est_stable_pour_un_meme_couple_racine_reference(home, sockets):
    assert socket_path(home, "tts-test@essai") == socket_path(home, "tts-test@essai")
    assert socket_path(home, "tts-test@essai") != socket_path(home, "tts-test@autre")
    # Deux racines = deux installations : leurs workers ne doivent pas se répondre.
    assert socket_path(home, "tts-test@essai") != socket_path(home.parent, "tts-test@essai")


def test_le_repertoire_des_sockets_suit_ecurie_socket_dir(home, sockets):
    assert socket_dir() == sockets
    assert socket_path(home, "tts-test@essai").parent == sockets
    assert socket_dir().is_dir()


def test_le_chemin_du_socket_reste_sous_la_limite_de_sun_path(tmp_path, monkeypatch):
    """104 octets, c'est la taille de `sun_path` sur macOS. Au-delà, le `bind` du
    worker échoue, le superviseur attend un socket qui n'arrivera jamais, et rien
    dans le message ne parle de longueur de chemin — la panne coûte une soirée.

    Un nom dérivé de la racine (`~/.ecurie/<ref>.sock`) grandit avec elle ; un nom
    haché de taille fixe, posé dans un répertoire court, ne bouge pas.
    """
    monkeypatch.delenv("ECURIE_SOCKET_DIR", raising=False)
    profonde = tmp_path.joinpath(*[f"un-dossier-au-nom-interminable-{i}" for i in range(6)])
    profonde.mkdir(parents=True)
    assert len(str(profonde).encode()) > SUN_PATH_MAX  # la racine seule dépasse déjà

    chemin = socket_path(profonde, "text-to-speech-un-modele-au-nom-tres-long@variant-tres-long")
    assert len(str(chemin).encode()) < SUN_PATH_MAX
    assert chemin.name.startswith("w-")
    assert chemin.suffix == ".sock"


def test_un_repertoire_de_sockets_trop_long_est_refuse_avec_la_marche_a_suivre(
    tmp_path, monkeypatch
):
    trop_long = tmp_path / ("s" * 80)
    monkeypatch.setenv("ECURIE_SOCKET_DIR", str(trop_long))

    with pytest.raises(OSError) as exc:
        socket_path(tmp_path, "tts-test@essai")
    assert "ECURIE_SOCKET_DIR" in str(exc.value)
    assert str(SUN_PATH_MAX) in str(exc.value)


# --- passage au contrôle d'admission ------------------------------------------


def test_as_resident_transporte_le_pic_et_l_epinglage():
    """Les quatre champs sur lesquels l'admission décide qui part : un `pinned`
    perdu en route ferait évincer un résident que l'utilisateur a épinglé."""
    entrée = ResidentEntry(
        ref="mesh@shape",
        pid=os.getpid(),
        socket="/tmp/w-abcdef.sock",
        peak_bytes=9 * GIB,
        last_used=INSTANT,
        pinned=True,
    )

    assert entrée.as_resident() == Resident(
        ref="mesh@shape", peak_bytes=9 * GIB, last_used=INSTANT, pinned=True
    )
    assert entrée.as_resident().heavy() is True
