"""Les jobs du serveur : soumettre, suivre, reprendre la sortie.

`run_job` est bloquant — il attend son tour, charge un modèle, exécute une
inférence, et cela peut durer des minutes. Une requête HTTP ne peut pas attendre
cela : `POST /jobs` rend un identifiant tout de suite, le travail part dans un
fil, et le client suit par SSE. C'est ce que le déménagement du superviseur
(tâche 4.6) a rendu possible : le processus qui reçoit la requête est celui qui
tient le worker, et il sait ce qui tourne.

Trois décisions gouvernent ce module.

**Un fil par job, pas un pool.** Un pool borné ferait attendre un job sur un
modèle libre derrière deux jobs en file sur un modèle occupé — le backlog qu'on
vient de retirer du socket, réintroduit un cran plus haut. La sérialisation a
déjà son endroit : le tour de rôle du superviseur, par variant. Ce qui reste ici
n'est qu'un garde-fou de nombre, contre un client qui s'emballe.

**L'entrée est déjà typée.** Le terminal ne connaît que des chaînes et les
convertit d'après le contrat ; un client HTTP envoie un nombre pour un nombre et
un booléen pour un booléen. `run_job(typed=True)` prend le second chemin — le
premier se tromperait sur `false`, qui deviendrait la chaîne `"False"`, donc le
booléen vrai.

**Le serveur donne l'URL des fichiers, le client ne la fabrique pas.** C'est ce
qui règle la question laissée ouverte au 4.4 : un nom de fichier ou un chemin à
plusieurs segments ? Les deux existent — `audio-separation` rend `tracks/vocals.wav`
—, et le client n'a pas à le savoir. Il reçoit `files` déjà composé et le suit.

Ce que ce module ne fait pas : durer. Les jobs terminés restent consultables le
temps de la session ; la mémoire longue est le `manifest.json` écrit dans le
dossier du job, que la Bibliothèque (4.2) indexera et qui survit au redémarrage.
"""

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from ecurie_core.capabilities import CapabilityContract
from ecurie_core.models import Model, Variant
from ecurie_runtime.runner import JobOutcome, new_job_id, run_job

if TYPE_CHECKING:  # pragma: no cover — AppState importe ce module, la flèche ne va que dans un sens
    from ecurie_api.state import AppState

JobState = Literal["queued", "running", "done", "failed"]

# Garde-fou contre un client qui s'emballe, pas une politique de parc : celle-là
# est dans le contrôle d'admission, seul juge de ce qui tient en mémoire. Un
# humain devant un écran ne soumet pas seize jobs ; une boucle mal écrite, si.
MAX_JOBS_ACTIFS = 16

# Au-delà, les plus anciens terminés sortent de la table. Leur manifeste reste
# sur le disque : ce qui est perdu, c'est le suivi en direct d'un job fini depuis
# longtemps, ce que personne ne regarde.
MAX_JOBS_RETENUS = 200


class TooManyJobs(RuntimeError):
    """Trop de jobs en cours pour en accepter un de plus."""


def _maintenant() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Job:
    """Un job du serveur, et son journal d'événements.

    Le journal est append-only et rejoué depuis le début à qui s'abonne : un
    client qui ouvre le flux après le lancement doit voir où l'on en est, pas
    seulement ce qui arrivera ensuite. Il ne grossit pas pour autant — une
    progression qui répète le même pourcentage et la même note n'est pas un
    événement.
    """

    id: str
    ref: str
    model: str
    variant: str
    capability: str
    input: dict[str, Any]
    seed: int | None = None
    state: JobState = "queued"
    submitted_at: str = field(default_factory=_maintenant)
    started_at: str | None = None
    finished_at: str | None = None
    progress: int = 0
    note: str = ""
    error: str | None = None
    reused: bool = False
    evicted: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)  # clé du contrat → chemin dans le job
    manifest: dict[str, Any] | None = None
    job_dir: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        # Le premier événement du journal est l'existence du job : un client qui
        # ouvre le flux aussitôt après la soumission voit d'abord qu'il est en
        # file, et non un flux muet jusqu'au premier chargement.
        with self._lock:
            self._emit("state")

    # --- lecture -------------------------------------------------------------

    @property
    def terminal(self) -> bool:
        return self.state in ("done", "failed")

    def snapshot(self) -> dict[str, Any]:
        """Tout ce que le client a besoin de savoir, en un objet remplaçable.

        Le même objet sert la lecture `GET /jobs/{id}` et chaque événement du
        flux : le client remplace son état plutôt que de recomposer des
        fragments, et deux chemins de code ne peuvent pas diverger.
        """
        return {
            "id": self.id,
            "ref": self.ref,
            "model": self.model,
            "variant": self.variant,
            "capability": self.capability,
            "state": self.state,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": self.progress,
            "note": self.note,
            "error": self.error,
            "reused": self.reused,
            "evicted": list(self.evicted),
            "warnings": list(self.warnings),
            "metrics": dict(self.metrics),
            "outputs": dict(self.outputs),
            "files": {clé: file_url(self.id, chemin) for clé, chemin in self.outputs.items()},
            "input": dict(self.input),
            "seed": self.seed,
        }

    def events_since(self, index: int) -> list[dict[str, Any]]:
        with self._lock:
            return self.events[index:]

    # --- transitions ---------------------------------------------------------

    def _emit(self, kind: str) -> None:
        self.events.append({"kind": kind, "job": self.snapshot()})

    def begin(self) -> None:
        with self._lock:
            self.state = "running"
            self.started_at = _maintenant()
            # Le chargement d'un modèle n'a pas de granularité : les adaptateurs
            # ne rapportent une progression qu'à l'inférence. Annoncer une barre
            # qui n'avance pas serait pire que d'écrire ce qui se passe.
            self.note = "admission et chargement du modèle"
            self._emit("state")

    def waiting_for(self, précédent: str) -> None:
        with self._lock:
            self.note = f"en file derrière le job {précédent}"
            self._emit("state")

    def advance(self, pct: int, note: str) -> None:
        with self._lock:
            pct = max(0, min(100, int(pct)))
            if pct == self.progress and note == self.note:
                return  # rien de neuf : un événement de plus n'apprendrait rien
            self.progress = pct
            self.note = note
            self._emit("progress")

    def finish(self, outcome: JobOutcome) -> None:
        with self._lock:
            self.state = "done" if outcome.ok else "failed"
            self.finished_at = _maintenant()
            self.progress = 100 if outcome.ok else self.progress
            self.note = "" if outcome.ok else (outcome.error or "")
            self.error = outcome.error
            self.reused = outcome.reused
            self.evicted = list(outcome.evicted)
            self.warnings = list(outcome.warnings)
            self.metrics = dict(outcome.result.metrics) if outcome.result else {}
            self.manifest = outcome.manifest
            self.job_dir = str(outcome.job_dir)
            self.outputs = {
                clé: str(chemin.relative_to(outcome.job_dir))
                for clé, chemin in outcome.files.items()
            }
            self._emit("state")

    def fail(self, message: str) -> None:
        """Échec survenu **avant** l'exécution : entrée refusée, fichier introuvable.

        Ceux-là ne passent pas par `run_job`, qui écrit un manifeste même quand
        le worker échoue. Il n'y a donc rien sur le disque, et le message est
        tout ce que le job laissera.
        """
        with self._lock:
            self.state = "failed"
            self.finished_at = _maintenant()
            self.error = message
            self.note = message
            self._emit("state")


def file_url(job_id: str, chemin: str) -> str:
    """L'URL d'un fichier de sortie, composée par le serveur.

    `quote` laisse les barres obliques : une sortie imbriquée (`tracks/vocals.wav`)
    est un chemin à plusieurs segments, et la route l'accepte comme tel.
    """
    return f"/jobs/{job_id}/files/{quote(chemin)}"


class JobRegistry:
    """Les jobs de cette session, et les fils qui les exécutent."""

    def __init__(self, state: "AppState") -> None:
        self.state = state
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def actifs(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if not job.terminal)

    def submit(
        self,
        model: Model,
        variant: Variant,
        contract: CapabilityContract,
        ref: str,
        entrée: dict[str, Any],
        *,
        seed: int | None = None,
    ) -> Job:
        job = Job(
            id=new_job_id(),
            ref=ref,
            model=model.id,
            variant=variant.id,
            capability=model.capability,
            input=dict(entrée),
            seed=seed,
        )
        with self._lock:
            actifs = sum(1 for autre in self._jobs.values() if not autre.terminal)
            if actifs >= MAX_JOBS_ACTIFS:
                raise TooManyJobs(
                    f"{actifs} jobs déjà en cours ou en attente, c'est le maximum — "
                    "un modèle sert un job à la fois, et le parc n'en tient pas "
                    "davantage ; attendre qu'ils finissent"
                )
            self._jobs[job.id] = job
            self._purger()

        fil = threading.Thread(
            target=self._executer,
            args=(job, model, variant, contract),
            # Un job d'une heure ne doit pas retenir l'arrêt du serveur. Ce qu'on
            # perd en le tuant : le manifeste de ce job. Ce qu'on ne perd pas :
            # le worker, qui est détaché et survit de toute façon.
            daemon=True,
            name=f"ecurie-job-{job.id}",
        )
        fil.start()
        return job

    def _purger(self) -> None:
        """Retire les plus anciens jobs terminés. Appelé sous verrou."""
        surplus = len(self._jobs) - MAX_JOBS_RETENUS
        for job_id in [j.id for j in self._jobs.values() if j.terminal][: max(0, surplus)]:
            self._jobs.pop(job_id, None)

    def _executer(
        self, job: Job, model: Model, variant: Variant, contract: CapabilityContract
    ) -> None:
        job.begin()
        # La base s'ouvre **dans le fil du job** : une connexion sqlite3
        # appartient au fil qui l'a créée, et la partager en lèverait une erreur
        # au premier `record_run`.
        db = self.state.open_db()
        try:
            outcome = run_job(
                self.state.supervisor(),
                model,
                variant,
                contract,
                job.input,
                typed=True,
                seed=job.seed,
                db=db,
                job_id=job.id,
                on_progress=job.advance,
                on_wait=job.waiting_for,
            )
        except Exception as exc:  # noqa: BLE001 — entrée refusée, fichier absent : le job le porte
            job.fail(f"{type(exc).__name__}: {exc}")
        else:
            job.finish(outcome)
        finally:
            db.close()
