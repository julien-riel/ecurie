"""Transport du protocole worker : lignes JSON sur une paire de descripteurs.

Un seul transport pour les deux usages (CONCEPTION.md §5.1) : le tube stdio d'un
sous-processus, et le socket Unix d'un worker résident entre deux commandes de la
CLI. Les deux sont des descripteurs POSIX, `os.read` et `selectors` suffisent.

Le tampon de lignes est tenu ici, à la main, plutôt que par `BufferedReader` :
`select()` ne voit que le noyau, pas le tampon de Python. Un `readline()`
bufferisé mêlé à une attente `select()` reste bloqué sur des octets déjà lus mais
pas encore rendus — panne rare, non reproductible, et impossible à diagnostiquer
depuis les journaux. Bibliothèque standard uniquement : ce module est importé
dans les venv isolés.
"""

import errno
import os
import selectors
import time
from collections.abc import Callable
from typing import Any

from ecurie_runtime.protocol import MAX_LINE_BYTES, ProtocolError, decode, encode


class ChannelClosed(ProtocolError):
    """L'autre bout a fermé — worker sorti, tué, ou socket rompu."""


class ChannelTimeout(ProtocolError):
    """Rien n'est arrivé dans le délai imparti."""


class Channel:
    """Canal bidirectionnel de messages sur deux descripteurs.

    `on_noise` reçoit les lignes qui ne sont pas du protocole. Un worker `custom`
    tiers peut écrire n'importe quoi sur sa sortie standard ; les ignorer en les
    comptant vaut mieux que tuer un job qui se passait bien à cause d'une
    bibliothèque bavarde.
    """

    def __init__(
        self,
        read_fd: int,
        write_fd: int,
        *,
        on_noise: Callable[[str], None] | None = None,
        close_fds: bool = False,
    ) -> None:
        self.read_fd = read_fd
        self.write_fd = write_fd
        self.on_noise = on_noise
        self._close_fds = close_fds
        self._buffer = bytearray()
        self._closed = False
        self._selector = selectors.DefaultSelector()
        self._selector.register(read_fd, selectors.EVENT_READ)

    # --- émission ------------------------------------------------------------

    def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            # Sans ce contrôle, un envoi après `close()` réussit tant que les
            # descripteurs appartiennent à quelqu'un d'autre : le message part
            # dans un canal que plus personne ne lit, et l'appelant attend une
            # réponse qui ne viendra jamais.
            raise ChannelClosed("canal fermé")
        payload = encode(message)
        view = memoryview(payload)
        while view:
            try:
                written = os.write(self.write_fd, view)
            except BrokenPipeError as exc:
                raise ChannelClosed("canal fermé à l'écriture") from exc
            except OSError as exc:
                if exc.errno == errno.EPIPE:
                    raise ChannelClosed("canal fermé à l'écriture") from exc
                raise
            view = view[written:]

    # --- réception -----------------------------------------------------------

    def recv(self, timeout: float | None = None) -> dict[str, Any]:
        """Prochain message valide. Lève sur délai dépassé ou canal fermé.

        Le délai couvre l'attente d'un message *utile* : les lignes de bruit ne
        rallongent pas l'échéance, sinon un worker bavard rendrait tout timeout
        inopérant.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            line = self._pop_line()
            if line is None:
                self._fill(deadline)
                continue
            try:
                return decode(line)
            except ProtocolError:
                if self.on_noise is None:
                    raise
                self.on_noise(line.decode("utf-8", "replace").rstrip("\n"))

    def _pop_line(self) -> bytes | None:
        index = self._buffer.find(b"\n")
        if index < 0:
            if len(self._buffer) > MAX_LINE_BYTES:
                raise ProtocolError(
                    f"ligne de plus de {MAX_LINE_BYTES} octets sans fin de ligne — "
                    "le canal ne transporte pas de contenu binaire"
                )
            return None
        line = bytes(self._buffer[:index])
        del self._buffer[: index + 1]
        return line

    def _fill(self, deadline: float | None) -> None:
        if self._closed:
            raise ChannelClosed("canal fermé")
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        # `remaining == 0` n'est pas « trop tard » mais « sans attendre » : un
        # sondage non bloquant doit quand même regarder si des octets attendent
        # dans le tube, sinon `recv(timeout=0)` déclare vide un canal plein.
        if not self._selector.select(remaining):
            raise ChannelTimeout("délai dépassé sans message")
        try:
            chunk = os.read(self.read_fd, 65536)
        except OSError as exc:
            raise ChannelClosed(f"lecture impossible : {exc}") from exc
        if not chunk:
            self._closed = True
            raise ChannelClosed("l'autre bout a fermé le canal")
        self._buffer.extend(chunk)

    # --- fermeture -----------------------------------------------------------

    def close(self) -> None:
        try:
            self._selector.close()
        except Exception:  # noqa: BLE001 — la fermeture ne doit jamais masquer l'erreur d'origine
            pass
        if self._close_fds:
            for fd in {self.read_fd, self.write_fd}:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._closed = True

    def __enter__(self) -> "Channel":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
