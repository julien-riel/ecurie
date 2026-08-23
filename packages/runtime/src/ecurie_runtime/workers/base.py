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
import re
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
    CANAL_RAISONNEMENT,
    CANAL_REPONSE,
    CANAUX,
    EV_DELTA,
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

    # Posé par la boucle avant chaque `infer`, remis à None après. Un adaptateur
    # ne l'appelle jamais directement : il passe par `stream()`, qui ne fait rien
    # quand personne n'écoute — en test, au banc d'essai, ou dans un worker qui
    # ne sait pas produire son texte au fur et à mesure.
    _emettre_delta: Callable[[str, str], None] | None = None

    def stream(self, texte: str, canal: str = CANAL_REPONSE) -> None:
        """Émet un fragment de texte pendant qu'il se produit. Facultatif.

        Ce qui traverse ce canal n'est **pas** le résultat : le `result` final
        reste seul à faire foi, et il est écrit en fichiers comme avant. Un
        fragment perdu ne change donc rien à ce que le job produit — il change ce
        que l'on voit pendant qu'il travaille, et sur un modèle qui met deux
        minutes à répondre, c'est toute la différence entre attendre et lire.

        `canal` sépare le brouillon de raisonnement de la réponse. La séparation
        se fait ici, à la source, parce qu'elle est irréversible ensuite :
        personne ne peut redécouper après coup un flux de caractères où les deux
        auraient été mêlés.
        """
        émettre = self._emettre_delta
        if émettre is None or not texte:
            return
        émettre(texte, canal if canal in CANAUX else CANAL_REPONSE)

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


# Un motif par convention, l'ouverture restant facultative : un gabarit qui
# l'amorce lui-même ne la fait pas réémettre au modèle.
RAISONNEMENTS = tuple(
    (fermeture, re.compile(rf"(?:{re.escape(ouverture)})?(.*?){re.escape(fermeture)}", re.DOTALL))
    for ouverture, fermeture in (("<think>", "</think>"), ("<|channel>thought", "<channel|>"))
)


def sans_raisonnement(texte: str) -> tuple[str, str]:
    """Sépare la réponse du raisonnement à voix haute. Rend (réponse, raisonnement).

    Un modèle en mode « thinking » émet son brouillon avant sa réponse. Ce
    brouillon n'est pas une sortie : il précède le texte demandé, et tout ce qui
    lit la réponse — la note d'une traduction, l'extracteur d'un appel d'outil,
    les coordonnées d'une boîte, le fichier déposé dans la Bibliothèque — le
    prendrait pour elle.

    Ici plutôt que dans un adaptateur : le mode « thinking » n'appartient ni à
    une famille de modèles ni à un runtime. Il est arrivé par Qwen3.6 sur
    `mlx-vlm`, il touche déjà sept capacités du parc, et le prochain modèle qui
    l'apportera ne préviendra pas.

    Un raisonnement laissé ouvert, faute de jetons pour le refermer, est rendu
    tel quel plutôt que dérobé : la réponse est vide, et c'est exactement ce
    qu'il faut voir. La masquer donnerait un succès silencieux sur un job qui a
    manqué de place pour répondre.
    """
    for fermeture, motif in RAISONNEMENTS:
        if fermeture not in texte:
            continue
        correspondance = motif.search(texte)
        if correspondance is None:
            continue
        raisonnement = correspondance.group(1).strip()
        réponse = texte[correspondance.end() :].strip()
        return réponse, raisonnement
    return texte, ""


# Les façons connues de baliser un raisonnement à voix haute. Il n'y a pas de
# convention commune, et l'écart n'est pas cosmétique : chercher `<think>` dans
# une sortie de Gemma 4 ne trouve rien, et tout le brouillon part alors sur le
# canal de la réponse — exactement ce que ce module existe pour éviter.
#
#   Qwen3, Qwen3.6, et la plupart des dérivés   <think> … </think>
#   Gemma 4                                     <|channel>thought … <channel|>
#
# L'ouverture est facultative des deux côtés : un gabarit qui l'amorce lui-même
# ne la fait pas réémettre au modèle, dont la sortie commence directement par le
# brouillon. C'est la fermeture qui fait foi.
BALISES_RAISONNEMENT = (
    ("<think>", "</think>"),
    ("<|channel>thought", "<channel|>"),
)

FERMETURES = tuple(fermeture for _, fermeture in BALISES_RAISONNEMENT)


class FluxRaisonnement:
    """Aiguille un texte qui arrive par fragments vers le bon canal, au fil de l'eau.

    `sans_raisonnement` fait le même partage, mais après coup, sur un texte
    complet. Ici il faut décider **avant** d'avoir vu la suite, et deux pièges
    s'ensuivent.

    Le premier : `</think>` peut arriver à cheval sur deux fragments. Un modèle
    produit `</th` puis `ink>`, et chercher la balise dans chaque fragment pris
    isolément ne la trouverait jamais — le raisonnement ne se refermerait pas, et
    la réponse entière partirait sur le canal du brouillon. On garde donc en
    réserve la fin d'un fragment tant qu'elle peut être le début de la balise.

    Le second : l'ouverture est facultative. Un gabarit qui amorce `<think>`
    lui-même ne la fait pas réémettre au modèle, dont la sortie commence
    directement par le brouillon. C'est le premier fragment non vide qui tranche,
    et il ne se relit pas ensuite.
    """

    def __init__(self, balises: tuple[tuple[str, str], ...] = BALISES_RAISONNEMENT) -> None:
        self.balises = balises
        self.en_raisonnement: bool | None = None
        self.termine = False
        self.fermeture = balises[0][1] if balises else "</think>"
        self._reserve = ""

    def pousser(self, fragment: str) -> list[tuple[str, str]]:
        """Un fragment brut → les couples (texte, canal) qu'on peut émettre sûrement."""
        if not fragment:
            return []
        texte = self._reserve + fragment
        self._reserve = ""

        if self.termine:
            return self._sortir(texte, CANAL_REPONSE)

        if self.en_raisonnement is None:
            dépouillé = texte.lstrip()
            if not dépouillé:
                self._reserve = texte
                return []
            for ouverture, fermeture in self.balises:
                if dépouillé.startswith(ouverture):
                    self.en_raisonnement = True
                    self.fermeture = fermeture
                    texte = dépouillé[len(ouverture) :]
                    break
            else:
                if any(o.startswith(dépouillé) for o, _ in self.balises):
                    # Peut encore devenir une ouverture : on attend la suite
                    # plutôt que d'envoyer un début de balise sur le canal de la
                    # réponse, où il resterait à jamais.
                    self._reserve = texte
                    return []
                self.en_raisonnement = False

        if not self.en_raisonnement:
            return self._sortir(texte, CANAL_REPONSE)

        avant, séparateur, après = texte.partition(self.fermeture)
        if not séparateur:
            return self._sortir(texte, CANAL_RAISONNEMENT)
        self.en_raisonnement = False
        self.termine = True
        sorties = [(avant, CANAL_RAISONNEMENT)] if avant else []
        return sorties + ([(après.lstrip(), CANAL_REPONSE)] if après.strip() else [])

    def vider(self) -> list[tuple[str, str]]:
        """Ce qui restait en réserve, à la fin du flux. Rien ne se perd."""
        reste, self._reserve = self._reserve, ""
        if not reste:
            return []
        canal = CANAL_RAISONNEMENT if self.en_raisonnement else CANAL_REPONSE
        return [(reste, canal)]

    def _sortir(self, texte: str, canal: str) -> list[tuple[str, str]]:
        """Émet, en gardant en réserve ce qui pourrait amorcer `</think>`."""
        if canal == CANAL_RAISONNEMENT:
            for taille in range(len(self.fermeture) - 1, 0, -1):
                if texte.endswith(self.fermeture[:taille]):
                    self._reserve = texte[-taille:]
                    texte = texte[:-taille]
                    break
        return [(texte, canal)] if texte else []


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

        def émettre_delta(texte: str, canal: str) -> None:
            channel.send(ev(EV_DELTA, job_id=job_id, text=texte, channel=canal))

        started = time.monotonic()
        # Le canal n'existe que le temps du job : un adaptateur qui garderait la
        # référence écrirait sur un socket dont le job d'après ne veut rien savoir.
        self.worker._emettre_delta = émettre_delta  # noqa: SLF001 — même module, injection prévue
        try:
            result = self.worker.infer(request, progress)
        finally:
            self.worker._emettre_delta = None  # noqa: SLF001
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
