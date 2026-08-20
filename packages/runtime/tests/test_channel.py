"""Le transport du protocole, sur de vrais descripteurs (CONCEPTION.md §5.1).

Rien n'est simulé ici : les deux montages de production sont montés tels quels —
la paire de tubes du worker en sous-processus, et la paire de sockets du worker
résident que la CLI rejoint entre deux commandes. Ce qu'on éprouve, ce sont
justement les cas que le noyau décide et qu'aucun double ne reproduit : plusieurs
messages dans un même paquet, une ligne coupée en deux, une fin de fichier.
"""

import os
import socket
import threading
import time

import pytest
from ecurie_runtime.channel import Channel, ChannelClosed, ChannelTimeout
from ecurie_runtime.protocol import MAX_LINE_BYTES, ProtocolError, encode, ev, op

DELAI = 2.0  # large : on ne l'atteint que si le test échoue
COURT = 0.05  # court : le délai qu'on demande quand on s'attend à ce qu'il expire


class Tube:
    """Un `Channel` sur une paire de tubes, et l'autre bout à l'octet près.

    C'est le montage du worker en sous-processus : le canal lit sa sortie
    standard et écrit sur son entrée standard. Écrire des octets bruts est
    indispensable — le bruit, les messages coupés et les flots sans fin de ligne
    ne se fabriquent pas avec `send`.
    """

    def __init__(self) -> None:
        lecture, self._vers_le_canal = os.pipe()
        self._depuis_le_canal, ecriture = os.pipe()
        self.canal = Channel(lecture, ecriture)
        self._ouverts = [lecture, self._vers_le_canal, self._depuis_le_canal, ecriture]

    def ecrit(self, data: bytes) -> None:
        """Un seul appel système tant que le tube a la place : ce qui part groupé arrive groupé."""
        vue = memoryview(data)
        while vue:
            vue = vue[os.write(self._vers_le_canal, vue) :]

    def n_ecrit_plus(self) -> None:
        """L'autre bout se tait : le canal doit voir une fin de fichier."""
        self._ferme(self._vers_le_canal)

    def ne_lit_plus(self) -> None:
        """L'autre bout n'écoute plus : le canal doit voir un tube rompu."""
        self._ferme(self._depuis_le_canal)

    def ferme(self) -> None:
        for fd in list(self._ouverts):
            self._ferme(fd)

    def _ferme(self, fd: int) -> None:
        if fd in self._ouverts:
            self._ouverts.remove(fd)
            os.close(fd)


@pytest.fixture
def tube():
    t = Tube()
    yield t
    t.canal.close()
    t.ferme()


@pytest.fixture
def duo():
    """Deux canaux face à face sur une paire de sockets : le worker résident."""
    a, b = socket.socketpair()
    superviseur, worker = Channel(a.fileno(), a.fileno()), Channel(b.fileno(), b.fileno())
    try:
        yield superviseur, worker
    finally:
        superviseur.close()
        worker.close()
        a.close()
        b.close()


def _deverse(tube: Tube, data: bytes) -> threading.Thread:
    """Écrit depuis un fil : un tube ne retient que quelques dizaines de kio, il
    faut que `recv` lise pendant que l'émetteur pousse."""

    def pousser() -> None:
        try:
            tube.ecrit(data)
        except OSError:
            pass  # le tube a été fermé : l'émetteur n'a plus personne à qui parler

    fil = threading.Thread(target=pousser, daemon=True)
    fil.start()
    return fil


def _ligne_maximale() -> bytes:
    """Une ligne de protocole qui pèse exactement le plafond, saut de ligne compris."""
    creux = len(encode(ev("result", job_id="j1", output={"blob": ""})))
    ligne = encode(ev("result", job_id="j1", output={"blob": "x" * (MAX_LINE_BYTES + 1 - creux)}))
    assert len(ligne) == MAX_LINE_BYTES + 1
    return ligne


def _ouvert(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


# --- échange -----------------------------------------------------------------


def test_un_message_fait_l_aller_et_le_retour(duo):
    superviseur, worker = duo

    superviseur.send(op("infer", job_id="j1", input={"text": "où va-t-on ?"}, seed=42))
    assert worker.recv(timeout=DELAI) == {
        "op": "infer",
        "job_id": "j1",
        "input": {"text": "où va-t-on ?"},
        "seed": 42,
    }

    worker.send(ev("result", job_id="j1", output={"audio": "audio.wav"}, metrics={"rtf": 0.11}))
    assert superviseur.recv(timeout=DELAI) == {
        "ev": "result",
        "job_id": "j1",
        "output": {"audio": "audio.wav"},
        "metrics": {"rtf": 0.11},
    }


def test_plusieurs_messages_arrives_dans_un_meme_paquet(tube):
    tube.ecrit(encode(op("ping")) + encode(op("unload")) + encode(op("ping")))

    assert [tube.canal.recv(timeout=DELAI) for _ in range(3)] == [
        {"op": "ping"},
        {"op": "unload"},
        {"op": "ping"},
    ]
    with pytest.raises(ChannelTimeout):
        tube.canal.recv(timeout=COURT)  # rien n'est resté collé au fond du tampon


def test_le_tampon_du_canal_et_l_attente_voient_les_memes_octets(tube):
    """Le canal lit avec `os.read` et tient son tampon lui-même, au lieu d'un
    `readline()` sur un objet fichier bufferisé. Les trois messages arrivent dans
    un seul paquet : dès le premier `recv`, le noyau n'a plus rien à signaler.
    Une implémentation qui attendrait avec `select()` avant de lire resterait
    bloquée sur des octets qu'elle a déjà — les deux derniers messages
    expireraient ici, sur un canal pourtant plein."""
    tube.ecrit(b"".join(encode(ev("progress", job_id="j1", pct=p)) for p in (10, 50, 90)))

    lus = [tube.canal.recv(timeout=COURT) for _ in range(3)]
    assert [m["pct"] for m in lus] == [10, 50, 90]


def test_un_message_coupe_en_deux_ecritures_est_recolle(tube):
    """La coupure tombe au milieu d'un « é » : recoller à l'octet est la seule
    façon de ne pas transformer une écriture en deux fois en erreur de décodage."""
    ligne = encode(ev("error", job_id="j1", message="échec du démarrage"))
    coupe = ligne.index("é".encode()) + 1

    tube.ecrit(ligne[:coupe])
    with pytest.raises(ChannelTimeout):
        tube.canal.recv(timeout=COURT)  # une demi-ligne n'est pas un message

    tube.ecrit(ligne[coupe:])
    assert tube.canal.recv(timeout=DELAI) == {
        "ev": "error",
        "job_id": "j1",
        "message": "échec du démarrage",
    }


def test_un_message_de_taille_maximale_traverse_le_canal(tube):
    """Le plafond du protocole et celui du tampon du canal doivent tomber au même
    endroit : sinon `encode` accepte une ligne que `recv` refusera à l'arrivée."""
    ligne = _ligne_maximale()
    fil = _deverse(tube, ligne)
    try:
        message = tube.canal.recv(timeout=DELAI)
    finally:
        fil.join(timeout=DELAI)

    assert message["ev"] == "result"
    assert len(encode(message)) == MAX_LINE_BYTES + 1
    assert not fil.is_alive()


# --- délai et fermeture ------------------------------------------------------


def test_delai_depasse_sans_message(tube):
    debut = time.monotonic()
    with pytest.raises(ChannelTimeout, match="délai dépassé"):
        tube.canal.recv(timeout=COURT)

    assert time.monotonic() - debut >= COURT  # il a bien attendu, il n'a pas rendu la main


def test_l_autre_bout_qui_ferme_leve_channel_closed(tube):
    tube.n_ecrit_plus()

    with pytest.raises(ChannelClosed, match="a fermé"):
        tube.canal.recv(timeout=DELAI)
    # Un canal mort le reste : sans ce verrou, chaque `recv` suivant attendrait le
    # délai complet sur un worker dont on sait déjà qu'il est sorti.
    debut = time.monotonic()
    with pytest.raises(ChannelClosed):
        tube.canal.recv(timeout=DELAI)
    assert time.monotonic() - debut < COURT


def test_une_ligne_tronquee_puis_fermeture_ne_produit_pas_de_message(tube):
    """Un worker tué au milieu d'une écriture : la demi-ligne ne doit ressortir ni
    comme un message, ni comme du bruit à rapporter."""
    bruits = []
    tube.canal.on_noise = bruits.append
    tube.ecrit(encode(ev("result", job_id="j1", output={"audio": "audio.wav"}))[:20])
    tube.n_ecrit_plus()

    with pytest.raises(ChannelClosed):
        tube.canal.recv(timeout=DELAI)
    assert bruits == []


def test_envoi_sur_un_tube_ferme_leve_channel_closed(tube):
    tube.ne_lit_plus()

    with pytest.raises(ChannelClosed, match="à l'écriture"):
        tube.canal.send(op("ping"))


# --- bruit -------------------------------------------------------------------


def test_une_ligne_hors_protocole_est_ignoree_et_rapportee(tube):
    """Une bibliothèque bavarde sur la sortie standard du worker ne doit pas tuer
    un job qui se passait bien — mais elle ne doit pas non plus disparaître."""
    bruits = []
    tube.canal.on_noise = bruits.append
    tube.ecrit(
        b"Downloading shards:  40%|####      |\n"
        b"\n"
        b'{"level":"info","msg":"tokenizer pret"}\n'
        b"\xff\xfe pas de l'UTF-8\n" + encode(ev("pong", rss_bytes=3_100_000_000))
    )

    assert tube.canal.recv(timeout=DELAI) == {"ev": "pong", "rss_bytes": 3_100_000_000}
    assert bruits == [
        "Downloading shards:  40%|####      |",
        "",
        '{"level":"info","msg":"tokenizer pret"}',
        "�� pas de l'UTF-8",  # les octets illisibles sont remplacés, pas perdus
    ]


def test_sans_on_noise_une_ligne_hors_protocole_est_une_erreur(tube):
    """Réglage par défaut : rien n'est ignoré en silence. La ligne fautive est tout
    de même consommée — l'erreur porte sur elle, pas sur le canal, et le message
    utile qui suivait reste lisible."""
    tube.ecrit(b"Traceback (most recent call last):\n" + encode(ev("pong", rss_bytes=1)))

    with pytest.raises(ProtocolError) as exc:
        tube.canal.recv(timeout=DELAI)
    assert not isinstance(exc.value, ChannelClosed | ChannelTimeout)
    assert tube.canal.recv(timeout=DELAI) == {"ev": "pong", "rss_bytes": 1}


def test_le_bruit_ne_prolonge_pas_le_delai_d_attente(tube):
    """L'échéance est posée une fois, à l'entrée de `recv`. Si chaque ligne reçue la
    repoussait, un worker bavard rendrait tous les délais du superviseur
    inopérants : celui-ci attendrait un `result` aussi longtemps que la
    bibliothèque du runtime écrit sa barre de progression."""
    bruits = []
    tube.canal.on_noise = bruits.append
    tube.ecrit(b"[INFO] chargement du tokenizer\n" * 3)
    arret = threading.Event()

    def bavarder() -> None:
        for _ in range(400):  # de quoi bavarder quelques secondes, sans jamais rien dire
            if arret.is_set():
                return
            try:
                tube.ecrit(b"[INFO] shard 3/8\n")
            except OSError:
                return
            arret.wait(0.01)

    fil = threading.Thread(target=bavarder, daemon=True)
    fil.start()
    debut = time.monotonic()
    try:
        with pytest.raises(ChannelTimeout):
            tube.canal.recv(timeout=COURT)
        ecoule = time.monotonic() - debut
    finally:
        arret.set()
        fil.join(timeout=DELAI)

    assert ecoule < 1.5
    assert len(bruits) >= 3  # le bruit a bien été absorbé pendant l'attente


def test_un_flot_sans_fin_de_ligne_est_coupe_au_plafond(tube):
    """Un worker qui déverse un binaire sur le canal : le tampon du superviseur ne
    doit pas gonfler sans limite en attendant un saut de ligne qui ne viendra pas.
    Ce n'est pas du bruit, et `on_noise` ne doit pas l'absorber."""
    bruits = []
    tube.canal.on_noise = bruits.append
    fil = _deverse(tube, b"x" * (MAX_LINE_BYTES + 4096))
    try:
        with pytest.raises(ProtocolError, match="fin de ligne") as exc:
            tube.canal.recv(timeout=DELAI)
    finally:
        fil.join(timeout=DELAI)

    assert not isinstance(exc.value, ChannelClosed | ChannelTimeout)
    assert bruits == []
    assert not fil.is_alive()


# --- propriété des descripteurs ----------------------------------------------


def test_par_defaut_le_canal_ne_ferme_pas_les_descripteurs_qu_on_lui_prete(tube):
    """Le superviseur construit son canal sur `sock.fileno()` : si `close()` fermait
    ce descripteur, le socket le refermerait à son tour — et ce numéro, entre-temps
    réattribué, désignerait le fichier de quelqu'un d'autre."""
    tube.canal.close()

    assert [_ouvert(tube.canal.read_fd), _ouvert(tube.canal.write_fd)] == [True, True]


def test_close_fds_ferme_les_descripteurs_confies():
    lecture, ecriture = os.pipe()
    canal = Channel(lecture, ecriture, close_fds=True)
    canal.send(op("ping"))  # les deux descripteurs sont bien vivants

    canal.close()
    assert [_ouvert(lecture), _ouvert(ecriture)] == [False, False]


def test_recv_apres_close_leve_channel_closed(tube):
    """`close()` ferme aussi le sélecteur : sans le verrou posé au même moment,
    `recv` irait attendre sur un sélecteur mort et lèverait tout autre chose
    qu'une erreur de protocole."""
    tube.canal.close()

    with pytest.raises(ChannelClosed):
        tube.canal.recv(timeout=DELAI)
