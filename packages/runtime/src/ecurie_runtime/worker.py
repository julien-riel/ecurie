"""Côté superviseur : dialoguer avec un worker, et savoir le tuer.

`WorkerSession` porte le dialogue (load / infer / ping / unload) sur n'importe
quel canal ; `WorkerProcess` y ajoute le sous-processus — journal d'erreurs,
terminaison escaladée, mort constatée. Cette séparation n'est pas gratuite : le
mode résident parle à un worker déjà lancé par-dessus un socket, sans processus
enfant à surveiller, et l'API du v0.4 réutilisera la même session en direct.

Règle du §5.1 de la conception, appliquée ici : un worker qui ne répond pas au
ping dans les dix secondes est tué — SIGTERM, puis SIGKILL — et son variant
cesse d'être compté comme résident. Un worker bloqué qui garde neuf gigaoctets
est pire qu'un worker mort : le budget le croit vivant et refuse les jobs suivants.
"""

import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecurie_runtime.channel import Channel, ChannelClosed, ChannelTimeout
from ecurie_runtime.envs import WorkerSpec
from ecurie_runtime.protocol import (
    EV_ERROR,
    EV_LOADED,
    EV_PONG,
    EV_PROGRESS,
    EV_RESULT,
    EV_UNLOADED,
    OP_INFER,
    OP_LOAD,
    OP_PING,
    OP_UNLOAD,
    name_of,
    op,
)

ProgressFn = Callable[[int, str], None]


@dataclass(frozen=True)
class Timeouts:
    """Délais du protocole. Le ping est celui de la conception ; les autres sont
    des garde-fous, pas des attentes : un chargement de modèle lourd depuis un
    disque externe peut légitimement prendre plusieurs minutes."""

    load_s: float = 900
    infer_s: float = 3600
    ping_s: float = 10
    unload_s: float = 60
    grace_s: float = 10  # entre SIGTERM et SIGKILL


@dataclass
class Loaded:
    warmup_ms: int
    peak_memory_bytes: int | None = None
    rss_bytes: int | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobResult:
    output: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


class WorkerFailure(RuntimeError):
    """Le worker a répondu `ev:error` — panne côté adaptateur, worker toujours vivant."""

    def __init__(self, message: str, trace: str | None = None) -> None:
        super().__init__(message)
        self.trace = trace


class WorkerTimeout(RuntimeError):
    """Le worker n'a pas répondu dans le délai. Il est à tuer, pas à attendre."""


class WorkerSession:
    def __init__(
        self,
        channel: Channel,
        *,
        timeouts: Timeouts | None = None,
        on_progress: ProgressFn | None = None,
        on_noise: Callable[[str], None] | None = None,
    ) -> None:
        self.channel = channel
        self.timeouts = timeouts or Timeouts()
        self.on_progress = on_progress
        self.noise: list[str] = []
        channel.on_noise = on_noise or self._note_noise

    def _note_noise(self, line: str) -> None:
        # Conservé, pas remonté : c'est un indice de diagnostic quand un adaptateur
        # se met à écrire sur le canal, pas un motif d'échec du job.
        if len(self.noise) < 50:
            self.noise.append(line)

    # --- opérations ----------------------------------------------------------

    def load(self, variant_doc: dict[str, Any]) -> Loaded:
        self.channel.send(op(OP_LOAD, variant=variant_doc))
        message = self._await(EV_LOADED, self.timeouts.load_s, what="chargement")
        return Loaded(
            warmup_ms=int(message.get("warmup_ms") or 0),
            peak_memory_bytes=message.get("peak_memory_bytes"),
            rss_bytes=message.get("rss_bytes"),
            options=message.get("options") or {},
        )

    def infer(
        self,
        job_id: str,
        *,
        input: dict[str, Any],
        params: dict[str, Any],
        output_dir: Path,
        seed: int | None = None,
        timeout_s: float | None = None,
    ) -> JobResult:
        self.channel.send(
            op(
                OP_INFER,
                job_id=job_id,
                input=input,
                params=params,
                output_dir=str(output_dir),
                seed=seed,
            )
        )
        message = self._await(
            EV_RESULT, timeout_s or self.timeouts.infer_s, what="exécution", job_id=job_id
        )
        return JobResult(output=message.get("output") or {}, metrics=message.get("metrics") or {})

    def ping(self) -> int | None:
        self.channel.send(op(OP_PING))
        message = self._await(EV_PONG, self.timeouts.ping_s, what="ping")
        return message.get("rss_bytes")

    def unload(self) -> None:
        self.channel.send(op(OP_UNLOAD))
        self._await(EV_UNLOADED, self.timeouts.unload_s, what="déchargement")

    def close(self) -> None:
        self.channel.close()

    # --- attente d'un événement terminal -------------------------------------

    def _await(
        self, expected: str, timeout_s: float, *, what: str, job_id: str | None = None
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            restant = deadline - time.monotonic()
            if restant <= 0:
                raise WorkerTimeout(f"{what} : pas de réponse en {timeout_s:g} s")
            try:
                message = self.channel.recv(timeout=restant)
            except ChannelTimeout as exc:
                raise WorkerTimeout(f"{what} : pas de réponse en {timeout_s:g} s") from exc
            except ChannelClosed as exc:
                raise WorkerTimeout(f"{what} : le worker a fermé le canal") from exc

            name = name_of(message)
            if name == EV_PROGRESS:
                if self.on_progress is not None:
                    self.on_progress(int(message.get("pct") or 0), str(message.get("note") or ""))
                # La progression relance le compte à rebours : un job qui avance
                # n'est pas un job bloqué, quelle que soit sa durée totale.
                deadline = time.monotonic() + timeout_s
                continue
            if name == EV_ERROR:
                raise WorkerFailure(
                    str(message.get("message") or "erreur sans message"),
                    trace=message.get("trace"),
                )
            if name == expected:
                if job_id and message.get("job_id") not in (None, job_id):
                    continue  # réponse d'un job précédent : on continue d'attendre le nôtre
                return message
            # Événement inattendu mais valide (un `pong` retardé, par exemple) :
            # on le laisse passer plutôt que d'échouer sur du protocole correct.


class WorkerProcess(WorkerSession):
    """Session attachée à un sous-processus lancé par nos soins."""

    def __init__(
        self,
        process: subprocess.Popen,
        channel: Channel,
        *,
        log_path: Path | None = None,
        stderr_file: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(channel, **kwargs)
        self.process = process
        self._log_path = log_path
        self._stderr_file = stderr_file

    @classmethod
    def spawn(
        cls,
        spec: WorkerSpec,
        *,
        log_path: Path | None = None,
        timeouts: Timeouts | None = None,
        on_progress: ProgressFn | None = None,
    ) -> "WorkerProcess":
        """Lance le worker et branche le canal sur son stdio."""
        env = {**os.environ, **spec.env_vars}
        stderr = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            stderr = open(log_path, "ab", buffering=0)  # noqa: SIM115 — fermé dans close()
        process = subprocess.Popen(
            spec.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr or subprocess.DEVNULL,
            env=env,
            cwd=str(spec.cwd) if spec.cwd else None,
        )
        assert process.stdin is not None and process.stdout is not None
        channel = Channel(process.stdout.fileno(), process.stdin.fileno())
        return cls(
            process,
            channel,
            log_path=log_path,
            stderr_file=stderr,
            timeouts=timeouts,
            on_progress=on_progress,
        )

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def healthy(self) -> bool:
        """Vivant *et* qui répond. Un worker bloqué passe le premier test, pas le second."""
        if not self.alive:
            return False
        try:
            self.ping()
        except (WorkerTimeout, WorkerFailure, ChannelClosed, ChannelTimeout, OSError):
            return False
        return True

    def terminate(self, *, grace_s: float | None = None) -> int | None:
        """SIGTERM, puis SIGKILL si le worker s'accroche. Retourne le code de sortie."""
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=grace_s if grace_s is not None else self.timeouts.grace_s)
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        return self.process.returncode

    def close(self, *, terminate: bool = True) -> None:
        """Ferme proprement : fin de canal, puis fin de processus s'il s'attarde.

        La fermeture de stdin est un signal de fin normale pour un worker stdio ;
        `terminate` n'intervient que s'il ne la comprend pas.
        """
        try:
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
        except OSError:
            pass
        self.channel.close()
        if terminate:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.terminate()
        for stream in (self.process.stdout, self._stderr_file):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except OSError:
                pass

    def stderr_tail(self, lines: int = 20) -> str:
        """Fin du journal du worker — ce qu'il faut coller dans un rapport d'erreur."""
        if self._log_path is None or not self._log_path.is_file():
            return ""
        contenu = self._log_path.read_text(errors="replace").splitlines()
        return "\n".join(contenu[-lines:])


def spawn_detached(
    spec: WorkerSpec, socket_path: Path, log_path: Path, *, idle_timeout_s: int | None = None
) -> int:
    """Lance un worker résident, détaché de la commande en cours.

    Le worker doit survivre à la commande qui le crée : c'est ce qui permet à un
    `ecurie run` de trouver le modèle déjà chaud, sans repayer le warmup à chaque
    phrase (ARCHITECTURE.md §7). Détaché veut dire session propre : Ctrl-C sur la
    CLI ne doit pas emporter un modèle de neuf gigaoctets fraîchement chargé.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **spec.env_vars}
    argv = [*spec.argv, "--listen", str(socket_path)]
    if idle_timeout_s:
        # Filet de sécurité : un worker qu'on oublie finit par rendre sa mémoire
        # de lui-même. Sans ce délai, un modèle de neuf gigaoctets chargé une
        # fois reste résident jusqu'au prochain redémarrage de la machine.
        argv += ["--idle-timeout", str(idle_timeout_s)]
    with open(log_path, "ab", buffering=0) as journal:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=journal,
            stderr=journal,
            env=env,
            cwd=str(spec.cwd) if spec.cwd else None,
            start_new_session=True,
        )
    return process.pid


def wait_for_socket(
    path: Path, pid: int, *, timeout_s: float = 120, log_path: Path | None = None
) -> None:
    """Attend que le worker écoute — ou qu'il meure, ce qui se voit tout de suite."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        if not pid_alive(pid):
            raise WorkerTimeout(
                "le worker est mort avant d'écouter" + _journal(log_path)
            )
        time.sleep(0.05)
    raise WorkerTimeout(
        f"le worker n'écoute toujours pas après {timeout_s:g} s" + _journal(log_path)
    )


def _journal(log_path: Path | None, lines: int = 8) -> str:
    if log_path is None or not log_path.is_file():
        return ""
    fin = log_path.read_text(errors="replace").splitlines()[-lines:]
    return (" — son journal dit :\n  " + "\n  ".join(fin)) if fin else ""


def pid_alive(pid: int) -> bool:
    """Vivant *et* pas zombie.

    Un processus détaché reste notre enfant tant que nous vivons ; s'il meurt et
    que personne ne le moissonne, il devient zombie et `kill(pid, 0)` réussit
    encore. Sans ce second contrôle, un worker mort au démarrage passerait pour
    en train de démarrer, et l'attente irait jusqu'au bout du délai de chargement
    au lieu de rapporter la panne tout de suite.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # un autre utilisateur : vivant, et ce n'est pas à nous de juger
    état = process_state(pid)
    if état is None:
        # Disparu entre les deux relevés — le zombie a été moissonné pendant qu'on
        # regardait. « Introuvable » veut dire mort, jamais « pas zombie » : c'est
        # exactement l'inversion qui ferait réserver de la mémoire à un fantôme.
        return False
    return not état.startswith("Z")


def process_state(pid: int) -> str | None:
    """État POSIX du processus (`R`, `S`, `Z`…), ou None s'il n'existe plus."""
    try:
        out = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "?"  # `ps` indisponible : on ne conclut rien, donc surtout pas « mort »
    état = out.stdout.strip()
    return état or None


def kill_pid(pid: int, *, grace_s: float = 10) -> None:
    """SIGTERM puis SIGKILL sur un worker résident, dont nous ne sommes pas le parent."""
    if not pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
