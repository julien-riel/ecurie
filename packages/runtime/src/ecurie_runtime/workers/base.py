"""Squelette de worker : boucle de protocole, mesures, transports.

Un adaptateur concret hérite de `Worker` et implémente `load` / `infer`. Tout le
reste — cadence des messages, mesure du warmup, capture des exceptions, ping,
déchargement, socket du mode résident — est ici, une fois pour toutes.

Bibliothèque standard uniquement (voir `workers/__init__.py`).

Le canal est protégé dès le démarrage : le descripteur 1 est dupliqué pour le
protocole, puis remplacé par le descripteur 2. Une barre de progression `tqdm`,
un `print` de débogage ou un avertissement de `transformers` partent alors sur
`stderr`, où ils sont journalisés, au lieu de couper une ligne JSON en deux. Sans
cette précaution, la première bibliothèque bavarde casse le protocole, et la
panne se présente comme un « worker muet » impossible à relier à sa cause.
"""

import argparse
import json
import os
import resource
import signal
import socket
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecurie_runtime.channel import Channel, ChannelClosed, ChannelTimeout
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
    ev,
    name_of,
)

ProgressFn = Callable[[int, str], None]


@dataclass
class InferRequest:
    job_id: str
    input: dict[str, Any]
    params: dict[str, Any]
    output_dir: Path
    seed: int | None = None

    def get(self, key: str, default: Any = None) -> Any:
        """Valeur d'un paramètre, qu'il vienne de l'entrée typée ou des réglages.

        Un adaptateur n'a pas à savoir si `speed` a été rangé dans `input` par le
        contrat de capacité ou dans `params` par le variant : c'est la même
        valeur pour le modèle.
        """
        if key in self.input:
            return self.input[key]
        return self.params.get(key, default)


@dataclass
class InferResult:
    output: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


class WorkerError(RuntimeError):
    """Échec attendu, rapporté proprement en `ev:error` sans tuer le worker."""


class Worker:
    """Contrat d'un adaptateur. `load` et `infer` sont à écrire ; le reste a un défaut."""

    name = "worker"

    def load(self, variant: dict[str, Any]) -> dict[str, Any]:
        """Charge les poids. Retourne les options dynamiques (ex. `{"voices": [...]}`)."""
        raise NotImplementedError

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        """Exécute un job. Les fichiers produits vont dans `request.output_dir`."""
        raise NotImplementedError

    def unload(self) -> None:
        """Rend la mémoire. Appelé avant toute sortie, y compris sur SIGTERM."""

    def peak_memory_bytes(self) -> int | None:
        """Pic mémoire, du point de vue le plus juste du runtime.

        Le défaut est le pic RSS du processus, qui vaut pour un runtime opaque.
        MLX et PyTorch/MPS savent mieux : leurs adaptateurs redéfinissent.
        """
        return peak_rss_bytes()

    def rss_bytes(self) -> int | None:
        return current_rss_bytes()

    def ping(self) -> int | None:
        """Réponse au ping de santé. Distincte de `rss_bytes`, qui sert aussi au
        chargement : un adaptateur qui veut interroger son moteur pour prouver
        qu'il répond encore le fait ici, sans ralentir les autres opérations."""
        return self.rss_bytes()


# --- mesures mémoire ---------------------------------------------------------


def peak_rss_bytes() -> int | None:
    """Pic RSS depuis le démarrage du processus.

    `ru_maxrss` est en octets sur macOS et en kibioctets sur Linux — l'un des
    pièges les plus coûteux de `getrusage`, puisqu'un facteur 1024 sur un profil
    mémoire fausse directement le contrôle d'admission.
    """
    try:
        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError):
        return None
    return maxrss if sys.platform == "darwin" else maxrss * 1024


def current_rss_bytes() -> int | None:
    """RSS courant, via `ps` — `psutil` n'est pas dans les venv des runtimes."""
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return int(value) * 1024 if value.isdigit() else None


# --- boucle de protocole -----------------------------------------------------


class WorkerLoop:
    """Sert un `Worker` sur un canal, une opération à la fois."""

    def __init__(self, worker: Worker, *, log: Callable[[str], None] | None = None) -> None:
        self.worker = worker
        self.log = log or _stderr_log
        self.loaded = False
        self.variant: dict[str, Any] | None = None

    def serve(self, channel: Channel) -> None:
        """Traite les messages jusqu'à la fermeture du canal."""
        while True:
            try:
                message = channel.recv()
            except (ChannelClosed, ChannelTimeout):
                return
            try:
                name = name_of(message)
            except Exception as exc:  # noqa: BLE001 — message hors protocole : on le signale, on continue
                self.log(f"message ignoré : {exc}")
                continue
            self._dispatch(channel, name, message)

    def _dispatch(self, channel: Channel, name: str, message: dict[str, Any]) -> None:
        job_id = message.get("job_id")
        try:
            if name == OP_LOAD:
                channel.send(self._load(message))
            elif name == OP_INFER:
                channel.send(self._infer(channel, message))
            elif name == OP_UNLOAD:
                channel.send(self._unload())
            elif name == OP_PING:
                channel.send(ev(EV_PONG, rss_bytes=self.worker.ping()))
            else:
                channel.send(ev(EV_ERROR, job_id=job_id, message=f"opération inconnue : {name!r}"))
        except ChannelClosed:
            raise
        except Exception as exc:  # noqa: BLE001 — toute panne d'adaptateur devient un ev:error
            self.log(traceback.format_exc())
            payload = ev(
                EV_ERROR,
                job_id=job_id,
                message=f"{type(exc).__name__}: {exc}",
                trace=traceback.format_exc(limit=20),
            )
            try:
                channel.send(payload)
            except ChannelClosed:
                raise

    def _load(self, message: dict[str, Any]) -> dict[str, Any]:
        variant = message.get("variant") or {}
        started = time.monotonic()
        options = self.worker.load(variant) or {}
        warmup_ms = int((time.monotonic() - started) * 1000)
        self.loaded = True
        self.variant = variant
        return ev(
            EV_LOADED,
            warmup_ms=warmup_ms,
            peak_memory_bytes=self.worker.peak_memory_bytes(),
            rss_bytes=self.worker.rss_bytes(),
            options=options,
        )

    def _infer(self, channel: Channel, message: dict[str, Any]) -> dict[str, Any]:
        if not self.loaded:
            raise WorkerError("infer avant load — le worker n'a aucun modèle en mémoire")
        job_id = str(message.get("job_id") or "sans-id")
        output_dir = Path(message.get("output_dir") or ".")
        output_dir.mkdir(parents=True, exist_ok=True)
        request = InferRequest(
            job_id=job_id,
            input=message.get("input") or {},
            params=message.get("params") or {},
            output_dir=output_dir,
            seed=message.get("seed"),
        )

        def progress(pct: int, note: str = "") -> None:
            channel.send(
                ev(EV_PROGRESS, job_id=job_id, pct=max(0, min(100, int(pct))), note=note)
            )

        started = time.monotonic()
        result = self.worker.infer(request, progress)
        duration_ms = int((time.monotonic() - started) * 1000)
        metrics = {"duration_ms": duration_ms, **(result.metrics or {})}
        metrics.setdefault("peak_memory_bytes", self.worker.peak_memory_bytes())
        return ev(EV_RESULT, job_id=job_id, output=result.output, metrics=metrics)

    def _unload(self) -> dict[str, Any]:
        if self.loaded:
            self.worker.unload()
        self.loaded = False
        self.variant = None
        return ev(EV_UNLOADED, rss_bytes=self.worker.rss_bytes())


def _stderr_log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --- transports --------------------------------------------------------------


def protect_stdout() -> int:
    """Réserve le descripteur 1 au protocole et renvoie tout le reste sur stderr."""
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return protocol_fd


def serve_stdio(worker: Worker) -> None:
    protocol_fd = protect_stdout()
    loop = WorkerLoop(worker)
    channel = Channel(0, protocol_fd, on_noise=lambda line: loop.log(f"bruit reçu : {line[:200]}"))
    try:
        loop.serve(channel)
    finally:
        _shutdown(loop, channel)


def serve_socket(worker: Worker, path: Path, *, idle_timeout_s: float | None = None) -> None:
    """Mode résident : le worker survit aux commandes successives de la CLI.

    Le socket est créé sous un nom temporaire puis renommé : un chemin de socket
    qui existe est donc toujours un socket qui écoute, jamais un reliquat d'un
    démarrage à moitié fait qu'un client attendrait en vain.
    """
    protect_stdout()
    loop = WorkerLoop(worker)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporaire = path.with_name(f".{path.name}.{os.getpid()}")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        temporaire.unlink(missing_ok=True)
        server.bind(str(temporaire))
        server.listen(1)
        os.replace(temporaire, path)
        _install_signal_handlers()
        while True:
            server.settimeout(idle_timeout_s)
            try:
                conn, _ = server.accept()
            except TimeoutError:
                loop.log(f"aucune connexion depuis {idle_timeout_s} s — sortie")
                return
            with conn:
                channel = Channel(conn.fileno(), conn.fileno())
                try:
                    loop.serve(channel)
                except ChannelClosed:
                    # Le client est parti au milieu d'une opération — un Ctrl-C
                    # sur la CLI, une session fermée. Ce n'est pas une raison de
                    # se saborder : le modèle est chargé, il a coûté son warmup,
                    # et la commande suivante doit le retrouver chaud. On rend la
                    # connexion et on se remet à écouter.
                    loop.log("client déconnecté en cours d'opération — le modèle reste chargé")
                finally:
                    channel.close()
    finally:
        server.close()
        temporaire.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        _shutdown(loop, None)


def _shutdown(loop: WorkerLoop, channel: Channel | None) -> None:
    try:
        if loop.loaded:
            loop.worker.unload()
    except Exception:  # noqa: BLE001 — on sort de toute façon
        loop.log(traceback.format_exc())
    if channel is not None:
        channel.close()


def _install_signal_handlers() -> None:
    def stop(signum: int, _frame: object) -> None:
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)


def main(build: Callable[[], Worker], argv: list[str] | None = None) -> int:
    """Point d'entrée commun : `python -m ecurie_runtime.workers.<adaptateur>`."""
    parser = argparse.ArgumentParser(description="Worker Écurie (protocole JSON Lines).")
    parser.add_argument("--listen", type=Path, help="Servir sur ce socket Unix (mode résident).")
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=None,
        help="Sortir après N secondes sans connexion (mode résident).",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Vérifier que l'adaptateur s'instancie dans cet environnement, puis sortir.",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        worker = build()
        print(json.dumps({"ok": True, "worker": worker.name}), flush=True)
        return 0

    worker = build()
    if args.listen:
        serve_socket(worker, args.listen, idle_timeout_s=args.idle_timeout)
    else:
        serve_stdio(worker)
    return 0
