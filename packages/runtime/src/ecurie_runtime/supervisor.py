"""Superviseur : qui est chargé, qui doit partir, qui peut entrer.

Il assemble les trois pièces précédentes — le registre des résidents (état), le
contrôle d'admission (décision), le worker (exécution) — et c'est le seul endroit
du projet qui a le droit de charger ou de décharger un modèle.

Deux modes, et la différence compte :

- **résident** : le worker est détaché, il survit à la commande, on le retrouve
  par son socket au job suivant. C'est ce qui évite de repayer 2,4 s de warmup à
  chaque phrase de synthèse (ARCHITECTURE.md §7) ;
- **éphémère** : worker attaché en stdio, tué à la fin. C'est le mode du banc
  d'essai, qui mesure un modèle seul dans la machine et ne doit rien laisser
  derrière lui.

**Un superviseur vit aussi longtemps que son processus** — la durée d'une
commande dans la CLI, celle du serveur dans `ecurie serve` (tâche 4.6). Deux
choses vivent alors dans sa mémoire, et nulle part ailleurs :

- **le tour de rôle par variant.** Un worker résident n'accepte qu'une connexion
  à la fois (`listen(1)`, `workers/base.py`). Deux jobs lancés sur le même modèle
  attendaient donc dans le backlog du socket : rien ne disait pourquoi, et le
  délai d'inférence du second courait déjà pendant que le premier travaillait. Un
  verrou par variant les sérialise en amont — le second attend son tour, pas une
  connexion ;
- **l'occupation.** Elle était le pid du processus détenteur, écrit dans
  `residents.json`. Un pid suffit tant qu'un processus ne tient qu'un job à la
  fois ; le serveur, lui, en tient plusieurs, et la fin du premier déclarait
  libre un worker qui ne l'était pas — un job évinçable en pleine inférence.

`residents.json` reste, en **miroir** : le superviseur y publie ce qu'il sait,
pour qu'un autre processus le lise — `ecurie ps` pendant qu'un serveur tourne —
et il continue d'y prendre un verrou exclusif le temps d'une admission, parce que
deux processus qui décideraient chacun de leur côté qu'il reste de la place,
c'est exactement le double chargement que le contrôle d'admission existe pour
empêcher. Ce qu'il n'y lit plus, c'est l'état de ses propres workers : là, c'est
la mémoire qui fait foi.

Le verrou du registre est tenu pendant tout le chargement, y compris s'il dure
des minutes, et pour la même raison. Les lectures (`ecurie ps`) ne le prennent
pas et ne sont donc jamais bloquées.
"""

import hashlib
import json
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecurie_core.config import Config, ecurie_home, resolve_heavy_threshold
from ecurie_core.models import Model, Variant
from ecurie_core.registry import Registry
from ecurie_store.weights import WeightsLocation, resolve_extra_weights, resolve_weights

from ecurie_runtime.admission import Admission, Policy, plan_admission
from ecurie_runtime.budget import Budget, detect_budget
from ecurie_runtime.channel import Channel
from ecurie_runtime.envs import EnvError, WorkerSpec, spec_for_variant
from ecurie_runtime.residents import (
    ResidentEntry,
    ResidentRegistry,
    log_path,
    socket_path,
)
from ecurie_runtime.worker import (
    DeltaFn,
    Loaded,
    Timeouts,
    WorkerProcess,
    WorkerSession,
    kill_pid,
    pid_alive,
    spawn_detached,
    wait_for_socket,
)

ProgressFn = Callable[[int, str], None]
SpecFactory = Callable[..., WorkerSpec]
RegistryProvider = Callable[[], Registry]
WaitFn = Callable[[str], None]  # prévenu du job qui précède, quand il faut attendre

SANS_IDENTIFIANT = "sans identifiant"


class AdmissionRefused(RuntimeError):
    """Le job ne peut pas être admis. Le message dit pourquoi et ce qui débloquerait."""

    def __init__(self, admission: Admission) -> None:
        super().__init__(admission.reason)
        self.admission = admission


class RefError(RuntimeError):
    """Référence de variant inconnue ou ambiguë."""


class QueueTimeout(RuntimeError):
    """Le tour n'est jamais venu : un job occupe ce worker depuis trop longtemps.

    Attendre est normal — un modèle sert un job à la fois. Attendre plus long-
    temps qu'un job entier ne l'est pas : c'est le signe d'un bail qu'on n'a pas
    rendu, et un blocage sans fin ne se diagnostique pas.
    """


class ReentrantJob(RuntimeError):
    """Le même fil demande deux fois le même worker sans avoir rendu son bail.

    Sans ce constat, l'appelant s'attendrait lui-même : le tour de rôle n'a
    aucune raison de distinguer un second job d'un fil qui s'est oublié, et le
    symptôme serait un serveur figé sans une ligne de journal.
    """


def parse_ref(registry: Registry, text: str) -> tuple[Model, Variant, str]:
    """« model@variant », ou « model » quand le choix est évident.

    Sans variant explicite, on ne devine que si le modèle n'en a qu'un ou qu'un
    seul est présent sur le disque (`tier: hot`). Deux variants téléchargés, deux
    profils mémoire différents : deviner serait choisir le budget à la place de
    l'utilisateur.
    """
    model_id, _, variant_id = text.partition("@")
    model = registry.models.get(model_id)
    if model is None:
        connus = ", ".join(sorted(registry.models)) or "aucun modèle au registre"
        raise RefError(f"modèle inconnu : {model_id!r} — connus : {connus}")

    if variant_id:
        try:
            variant = model.variant(variant_id)
        except KeyError as exc:
            choix = ", ".join(v.id for v in model.variants)
            raise RefError(f"{model_id} : variant inconnu {variant_id!r} — {choix}") from exc
        return model, variant, f"{model.id}@{variant.id}"

    if len(model.variants) == 1:
        variant = model.variants[0]
    else:
        présents = [v for v in model.variants if v.tier in ("hot", "cold")]
        if len(présents) != 1:
            choix = ", ".join(f"{model.id}@{v.id}" for v in model.variants)
            raise RefError(f"{model_id} a plusieurs variants — préciser lequel : {choix}")
        variant = présents[0]
    return model, variant, f"{model.id}@{variant.id}"


def document_fingerprint(document: dict[str, Any]) -> str:
    """Empreinte du manifeste résolu transmis au worker."""
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def variant_document(
    model: Model,
    variant: Variant,
    weights: WeightsLocation,
    extra_paths: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Le manifeste résolu transmis au worker : tout ce qu'il lui faut, et rien de plus.

    Le worker ne lit pas le registre, ne connaît pas le cache, ne choisit pas sa
    révision. Il reçoit un chemin local déjà vérifié — c'est ce qui garantit que
    la révision exécutée est celle qui sera écrite au manifeste du job.

    `extra_paths` porte les dépôts secondaires, par rôle. Il est toujours présent,
    vide pour l'immense majorité des variants : un adaptateur qui en a besoin le
    lit sans avoir à tester si la clé existe.
    """
    return {
        "ref": f"{model.id}@{variant.id}",
        "model_id": model.id,
        "variant_id": variant.id,
        "capability": model.capability,
        "runtime": variant.runtime,
        "quantization": variant.quantization,
        "weights_path": str(weights.path),
        "extra_paths": {rôle: str(chemin) for rôle, chemin in (extra_paths or {}).items()},
        "repo": variant.source.repo,
        "revision": variant.source.revision,
        "defaults": variant.defaults or {},
        "options": variant.options or {},
        "entrypoint": variant.entrypoint,
    }


@dataclass
class Lease:
    """Un worker à disposition, le temps d'un job."""

    ref: str
    session: WorkerSession
    admission: Admission
    resident: bool
    loaded: Loaded | None = None
    entry: ResidentEntry | None = None
    evicted: tuple[str, ...] = ()
    reused: bool = False
    warnings: list[str] = field(default_factory=list)
    on_release: Callable[[], None] | None = None

    @property
    def options(self) -> dict[str, Any]:
        return self.loaded.options if self.loaded else (self.entry.options if self.entry else {})

    def release(self) -> None:
        """Rend la main. Un worker résident reste chargé, un éphémère est tué.

        La connexion se ferme **avant** que le tour ne soit rendu, et l'ordre
        n'est pas cosmétique : le worker n'accepte qu'une connexion à la fois, et
        rendre le tour d'abord laisserait le job suivant en ouvrir une seconde
        pendant que la nôtre est encore là — le backlog que le tour de rôle
        existe pour éviter.
        """
        try:
            if not self.resident and isinstance(self.session, WorkerProcess):
                try:
                    self.session.unload()
                except Exception:  # noqa: BLE001 — on le tue juste après, l'échec n'apprend rien
                    pass
            self.session.close()
        finally:
            if self.on_release is not None:
                self.on_release()


class ResidentSession(WorkerSession):
    """Session sur le socket d'un worker déjà lancé."""

    def __init__(self, sock: socket.socket, **kwargs: Any) -> None:
        super().__init__(Channel(sock.fileno(), sock.fileno()), **kwargs)
        self.sock = sock

    def close(self) -> None:
        super().close()
        try:
            self.sock.close()
        except OSError:
            pass


@dataclass
class WorkerHandle:
    """Ce que **ce** processus sait d'un worker, et le tour de rôle qui va avec.

    Un handle existe dès qu'un variant a été demandé, même si son worker n'a
    jamais été chargé : c'est lui qui porte le verrou, et il faut le prendre
    avant de savoir s'il y a un worker à retrouver. Il n'est jamais retiré de la
    table — treize variants au parc réel, et un verrou qu'on jetterait pendant
    qu'un autre fil l'attend serait un verrou pour rien.
    """

    ref: str
    gate: threading.Lock = field(default_factory=threading.Lock)
    # Le résident tel que nous le connaissons. `None` veut dire « pas de worker
    # vivant de notre fait » : jamais chargé, déchargé, ou tué par ailleurs.
    entry: ResidentEntry | None = None
    job: str | None = None  # identifiant du job en cours ; l'occupation, en mémoire
    since: float = 0.0
    holder: int | None = None  # fil qui tient le tour, pour reconnaître un appel réentrant
    waiting: int = 0

    @property
    def busy(self) -> bool:
        return self.job is not None

    def publish(self) -> None:
        """Reporte l'occupation dans l'entrée qui part au miroir.

        Le pid publié est le nôtre : c'est ce qu'un autre processus peut vérifier
        — un détenteur mort ne retient rien —, et c'est tout ce dont il a besoin.
        Quel job précisément occupe le worker ne regarde que nous.
        """
        if self.entry is None:
            return
        self.entry.busy_by = os.getpid() if self.job else 0
        self.entry.busy_since = self.since if self.job else 0.0


class Supervisor:
    def __init__(
        self,
        repo_root: Path,
        registry: Registry,
        config: Config,
        *,
        home: Path | None = None,
        timeouts: Timeouts | None = None,
        spec_factory: SpecFactory | None = None,
        budget: Budget | None = None,
        registry_provider: RegistryProvider | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.config = config
        self.home = home or ecurie_home()
        self.timeouts = timeouts or Timeouts()
        self.registry_file = ResidentRegistry(self.home)
        # Le registre se recharge à chaud côté serveur (CONCEPTION.md §6) et le
        # superviseur, lui, vit désormais aussi longtemps que le processus. Il ne
        # le fige donc pas : il le redemande.
        self._registry_provider = registry_provider or (lambda: registry)
        self._spec_factory = spec_factory or (
            lambda root, variant, ref, capability=None: spec_for_variant(
                root, variant, ref=ref, capability=capability
            )
        )
        # Le budget se détecte en lançant un sous-processus dans le venv d'un
        # runtime pour y interroger MLX : c'est bon marché une fois par commande,
        # ruineux à chaque requête HTTP. Un serveur qui construit son superviseur
        # une fois le mesure donc une fois et le passe ici.
        self._budget: Budget | None = budget
        self._live: dict[str, WorkerHandle] = {}
        # Protège la table et les compteurs, jamais tenu pendant une E/S : c'est
        # le verrou de fichier, et lui seul, qui sérialise les admissions.
        self._mutex = threading.RLock()

    # --- lecture -------------------------------------------------------------

    @property
    def registry(self) -> Registry:
        return self._registry_provider()

    @property
    def budget(self) -> Budget:
        if self._budget is None:
            self._budget = detect_budget(self.config, repo_root=self.repo_root)
        return self._budget

    @property
    def policy(self) -> Policy:
        return Policy(
            budget_bytes=self.budget.bytes,
            max_heavy_resident=self.config.max_heavy_resident,
            heavy_threshold_bytes=resolve_heavy_threshold(self.config, self.budget.bytes),
        )

    def residents(self) -> list[ResidentEntry]:
        return sorted(self._entries().values(), key=lambda e: -e.last_used)

    def _entries(self) -> dict[str, ResidentEntry]:
        """Vue autoritaire : le miroir pour les workers des autres, la mémoire pour les nôtres.

        Ne réécrit rien, et ne retire rien de la mémoire : une lecture est sans
        effet, y compris sur nos propres tables. Un worker que le miroir ne
        connaît plus sera oublié à la prochaine transaction, où il y a de quoi
        conclure.
        """
        entrées = self.registry_file.read()
        with self._mutex:
            for ref, poignée in self._live.items():
                if poignée.entry is None or ref not in entrées:
                    continue
                poignée.publish()
                entrées[ref] = poignée.entry
        return entrées

    def peak_bytes(
        self, variant: Variant, values: dict[str, Any] | None = None
    ) -> int | None:
        """Pic attendu pour cette entrée — pas seulement pour ce variant.

        Le coût de certains modèles dépend de ce qu'on leur demande : trente
        secondes de musique coûtent le double de quinze. Décider sans regarder
        l'entrée reviendrait à refuser tous les jobs courts, ou à laisser passer
        les longs.
        """
        if variant.profile is None:
            return None
        attendu, _ = variant.profile.expected_peak(values)
        return attendu

    def peak_note(self, variant: Variant, values: dict[str, Any] | None = None) -> str | None:
        if variant.profile is None:
            return None
        return variant.profile.expected_peak(values)[1]

    def simulate(
        self,
        ref: str,
        peak_bytes: int | None,
        *,
        measure: bool = False,
        overcommit: bool = False,
    ) -> Admission:
        """Ce que ferait `acquire`, sans rien charger — c'est ce qu'affiche `ecurie ps`."""
        residents = [e.as_resident() for e in self._entries().values()]
        return plan_admission(
            ref, peak_bytes, residents, self.policy, measure=measure, overcommit=overcommit
        )

    # --- tour de rôle --------------------------------------------------------

    def _handle(self, ref: str) -> WorkerHandle:
        with self._mutex:
            poignée = self._live.get(ref)
            if poignée is None:
                poignée = self._live[ref] = WorkerHandle(ref=ref)
            return poignée

    def _enter(self, poignée: WorkerHandle, job_id: str, on_wait: WaitFn | None = None) -> None:
        """Prend le tour de ce variant, en attendant le job qui le précède.

        Une attente qui ne se dit pas est indiscernable d'un blocage : `on_wait`
        est prévenu quand le tour n'est pas libre, avec le job qui l'occupe.
        C'est ce qui distingue « en file derrière le job de l'Atelier » d'un
        `ecurie run` qui semble avoir cessé de répondre.
        """
        moi = threading.get_ident()
        with self._mutex:
            if poignée.holder == moi:
                raise ReentrantJob(
                    f"{poignée.ref} : ce fil tient déjà un bail sur ce worker "
                    f"(job {poignée.job}) — le relâcher avant d'en prendre un autre"
                )
            poignée.waiting += 1
            devant = poignée.waiting
        try:
            obtenu = poignée.gate.acquire(blocking=False)
            if not obtenu:
                if on_wait is not None:
                    on_wait(poignée.job or SANS_IDENTIFIANT)
                obtenu = poignée.gate.acquire(timeout=self.timeouts.queue_s)
        finally:
            with self._mutex:
                poignée.waiting -= 1
        if not obtenu:
            raise QueueTimeout(
                f"{poignée.ref} : le tour n'est pas venu en {self.timeouts.queue_s:g} s — "
                f"un job occupe ce worker depuis plus longtemps qu'un job entier "
                f"({devant} en attendaient la fin) ; ecurie ps --ping dit s'il répond encore"
            )
        with self._mutex:
            poignée.holder = moi
            poignée.job = job_id
            poignée.since = time.time()

    def _leave(self, poignée: WorkerHandle) -> None:
        """Rend le tour. Un second appel ne casse rien : il n'a plus rien à rendre."""
        with self._mutex:
            tenu = poignée.holder is not None
            poignée.holder = None
            poignée.job = None
            poignée.since = 0.0
            poignée.publish()
        if tenu:
            poignée.gate.release()

    def _release(self, poignée: WorkerHandle) -> None:
        """Le job est fini : le résident redevient évinçable, et le miroir le dit."""
        try:
            with self._mutex:
                poignée.job = None
                if poignée.entry is not None:
                    poignée.entry.last_used = time.time()
            with self.registry_file.locked() as entries:
                self._sync(entries)
        finally:
            self._leave(poignée)

    def _sync(self, entries: dict[str, ResidentEntry]) -> None:
        """Rapproche le miroir de la mémoire, dans les deux sens.

        Nos workers s'y écrivent — c'est ainsi qu'un autre processus apprend
        qu'un job tourne. Ceux que le miroir ne connaît plus sortent de la
        mémoire : `locked()` écarte à l'entrée les entrées dont le processus est
        mort ou le socket disparu, et un autre superviseur a pu évincer le nôtre
        pour faire de la place. Le tour de rôle, lui, reste : le job qui parlait
        à ce worker doit apprendre sa mort par le canal, pas par une table qu'on
        lui retire sous les pieds.
        """
        with self._mutex:
            for ref, poignée in self._live.items():
                if poignée.entry is None:
                    continue
                if ref not in entries:
                    poignée.entry = None
                    continue
                poignée.publish()
                entries[ref] = poignée.entry

    # --- chargement ----------------------------------------------------------

    def acquire(
        self,
        model: Model,
        variant: Variant,
        *,
        measure: bool = False,
        pin: bool = False,
        overcommit: bool = False,
        on_progress: ProgressFn | None = None,
        on_delta: DeltaFn | None = None,
        values: dict[str, Any] | None = None,
        job_id: str | None = None,
        on_wait: WaitFn | None = None,
    ) -> Lease:
        """Rend un worker prêt pour ce variant, en respectant le budget mémoire.

        `overcommit` assume un dépassement du budget plutôt que de le refuser :
        le parc est alors vidé, et la décision suit le job jusqu'à son manifeste.
        Voir `admission._hors_budget` pour ce qu'elle coûte.

        `values` est l'entrée résolue du job : elle sert au calcul du pic attendu
        quand le profil déclare une pente (`peak_scaling`). `job_id` nomme
        l'occupation — un worker occupé se lit mieux quand on sait par quoi.

        L'appel **attend son tour** si un job tourne déjà sur ce variant. C'est le
        seul endroit du projet où l'on patiente volontairement : un modèle sert
        un job à la fois, et le savoir ici vaut mieux que de le découvrir dans le
        backlog d'un socket.
        """
        ref = f"{model.id}@{variant.id}"
        weights = resolve_weights(self.config, variant, ref=ref)
        extra = resolve_extra_weights(self.config, variant, ref=ref)
        spec = self._spec_factory(self.repo_root, variant, ref, model.capability)
        document = variant_document(model, variant, weights, extra)

        if measure:
            # Le banc d'essai ne prend pas de tour : il ne réutilise jamais un
            # résident, il vide le parc et lance un worker qui n'appartient qu'à
            # lui. L'attendre reviendrait à attendre un worker qu'on va tuer.
            with self.registry_file.locked() as entries:
                self._sync(entries)
                admission = self._plan(ref, variant, values, entries, measure=True)
                if not admission.admitted:
                    raise AdmissionRefused(admission)
                for victime in admission.evict:
                    self._evict(entries, victime)
                return self._ephemeral(ref, spec, document, admission, on_progress, on_delta)

        poignée = self._handle(ref)
        self._enter(poignée, job_id or SANS_IDENTIFIANT, on_wait)
        try:
            with self.registry_file.locked() as entries:
                self._sync(entries)
                admission = self._plan(ref, variant, values, entries, overcommit=overcommit)
                if not admission.admitted:
                    raise AdmissionRefused(admission)

                for victime in admission.evict:
                    self._evict(entries, victime)

                entrée = entries.get(ref)
                empreinte = document_fingerprint(document)
                if entrée is not None and entrée.document not in ("", empreinte):
                    # Le manifeste a changé depuis le chargement : le worker en
                    # mémoire n'est plus celui que le registre décrit.
                    self._evict(entries, ref)
                    entrée = None
                if entrée is not None:
                    lease = self._reconnect(ref, entrée, admission, poignée, on_progress, on_delta)
                    if lease is not None:
                        entrée.last_used = time.time()
                        entrée.pinned = entrée.pinned or pin
                        with self._mutex:
                            poignée.entry = entrée
                            poignée.publish()
                        entries[ref] = entrée
                        return lease
                    # Socket mort : le worker n'est plus là malgré son entrée.
                    self._evict(entries, ref)

                return self._start_resident(
                    entries,
                    poignée,
                    ref,
                    variant,
                    spec,
                    document,
                    admission,
                    pin,
                    on_progress,
                    on_delta,
                )
        except BaseException:
            self._leave(poignée)
            raise

    def _plan(
        self,
        ref: str,
        variant: Variant,
        values: dict[str, Any] | None,
        entries: dict[str, ResidentEntry],
        *,
        measure: bool = False,
        overcommit: bool = False,
    ) -> Admission:
        residents = [e.as_resident() for e in entries.values()]
        return plan_admission(
            ref,
            self.peak_bytes(variant, values),
            residents,
            self.policy,
            measure=measure,
            overcommit=overcommit,
        )

    def _ephemeral(
        self,
        ref: str,
        spec: WorkerSpec,
        document: dict[str, Any],
        admission: Admission,
        on_progress: ProgressFn | None,
        on_delta: DeltaFn | None,
    ) -> Lease:
        worker = WorkerProcess.spawn(
            spec,
            log_path=log_path(self.home, ref),
            timeouts=self.timeouts,
            on_progress=on_progress,
            on_delta=on_delta,
        )
        try:
            loaded = worker.load(document)
        except Exception:
            worker.close()
            raise
        return Lease(
            ref=ref,
            session=worker,
            admission=admission,
            resident=False,
            loaded=loaded,
            evicted=admission.evict,
        )

    def _start_resident(
        self,
        entries: dict[str, ResidentEntry],
        poignée: WorkerHandle,
        ref: str,
        variant: Variant,
        spec: WorkerSpec,
        document: dict[str, Any],
        admission: Admission,
        pin: bool,
        on_progress: ProgressFn | None,
        on_delta: DeltaFn | None,
    ) -> Lease:
        sock_path = socket_path(self.home, ref)
        journal = log_path(self.home, ref)
        sock_path.unlink(missing_ok=True)
        pid = spawn_detached(
            spec, sock_path, journal, idle_timeout_s=self.config.resident_idle_timeout_s
        )
        try:
            wait_for_socket(sock_path, pid, timeout_s=self.timeouts.load_s, log_path=journal)
            session = self._connect(sock_path, on_progress, on_delta)
            loaded = session.load(document)
        except BaseException:
            # `BaseException` et non `Exception` : un Ctrl-C pendant le
            # chargement passe par `KeyboardInterrupt`. Sans cette prise, le
            # worker détaché survivrait à la commande qui l'a lancé, chargé,
            # sans entrée au registre — de la mémoire réservée que plus rien ne
            # connaît et que plus personne ne déchargera.
            kill_pid(pid)
            sock_path.unlink(missing_ok=True)
            raise

        annoncé = self.peak_bytes(variant)
        mesuré = loaded.peak_memory_bytes or 0
        entrée = ResidentEntry(
            ref=ref,
            pid=pid,
            socket=str(sock_path),
            # On retient le plus grand des deux : le profil du manifeste engage le
            # budget, mais si le worker rapporte davantage, c'est lui qui a raison
            # sur ce qui est réellement en mémoire à cet instant.
            peak_bytes=max(annoncé or 0, mesuré),
            runtime=variant.runtime,
            env=variant.env_name,
            loaded_at=datetime.now(UTC).isoformat(),
            last_used=time.time(),
            pinned=pin,
            warmup_ms=loaded.warmup_ms,
            options=loaded.options,
            log=str(journal),
            document=document_fingerprint(document),
        )
        with self._mutex:
            poignée.entry = entrée
            poignée.publish()
        entries[ref] = entrée
        avertissements = []
        if annoncé and mesuré and mesuré > annoncé * 1.15:
            avertissements.append(
                f"{ref} : pic rapporté au chargement ({mesuré}) supérieur de plus de 15 % "
                f"au profil du manifeste ({annoncé}) — un ecurie bench s'impose"
            )
        return Lease(
            ref=ref,
            session=session,
            admission=admission,
            resident=True,
            loaded=loaded,
            entry=entrée,
            evicted=admission.evict,
            warnings=avertissements,
            on_release=lambda: self._release(poignée),
        )

    def _reconnect(
        self,
        ref: str,
        entrée: ResidentEntry,
        admission: Admission,
        poignée: WorkerHandle,
        on_progress: ProgressFn | None,
        on_delta: DeltaFn | None,
    ) -> Lease | None:
        """Se rattache à un worker résident, sans le déranger.

        Aucun ping ici, et c'est délibéré : la preuve de vie est que le processus
        existe (vérifié à la lecture du registre) et que le socket accepte la
        connexion. Un worker que **nous** occupons ne peut pas se présenter ici —
        le tour de rôle l'interdit ; un worker qu'un autre processus occupe, si,
        et les deux jobs se sérialiseront alors dans la file d'écoute du worker.
        C'est le comportement voulu : un modèle sert un job à la fois, quel que
        soit le nombre de processus qui le lui demandent.
        """
        try:
            session = self._connect(Path(entrée.socket), on_progress, on_delta)
        except OSError:  # socket absent ou refusant la connexion : worker fantôme
            return None
        return Lease(
            ref=ref,
            session=session,
            admission=admission,
            resident=True,
            entry=entrée,
            evicted=admission.evict,
            reused=True,
            on_release=lambda: self._release(poignée),
        )

    def _connect(
        self, sock_path: Path, on_progress: ProgressFn | None, on_delta: DeltaFn | None = None
    ) -> ResidentSession:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(sock_path))
        return ResidentSession(
            sock, timeouts=self.timeouts, on_progress=on_progress, on_delta=on_delta
        )

    # --- déchargement --------------------------------------------------------

    def unload(self, ref: str, *, force: bool = False) -> bool:
        with self.registry_file.locked() as entries:
            self._sync(entries)
            entrée = entries.get(ref)
            if entrée is None:
                return False
            if entrée.busy and not force:
                # Le refus vaut pour nos jobs comme pour ceux d'un autre
                # processus : après `_sync`, le miroir dit la même chose que la
                # mémoire, et c'est tout l'intérêt de le tenir à jour.
                raise AdmissionRefused(
                    Admission(
                        admitted=False,
                        reason=(
                            f"{ref} : un job est en cours dessus (pid {entrée.busy_by}) — "
                            f"le décharger détruirait ce travail ; attendre, ou "
                            f"ecurie unload {ref} --force"
                        ),
                        blockers=(ref,),
                    )
                )
            if entrée.pinned and not force:
                raise AdmissionRefused(
                    Admission(
                        admitted=False,
                        reason=f"{ref} est épinglé — ecurie unload {ref} --force pour passer outre",
                        blockers=(ref,),
                    )
                )
            self._evict(entries, ref)
            return True

    def unload_all(self, *, force: bool = False) -> list[str]:
        with self.registry_file.locked() as entries:
            self._sync(entries)
            cibles = [ref for ref, e in entries.items() if force or not (e.pinned or e.busy)]
            for ref in cibles:
                self._evict(entries, ref)
            return cibles

    def _evict(self, entries: dict[str, ResidentEntry], ref: str) -> None:
        """Décharge poliment, puis tue. La mémoire doit être rendue avant le prochain load."""
        entrée = entries.pop(ref, None)
        with self._mutex:
            poignée = self._live.get(ref)
            if poignée is not None:
                entrée = entrée or poignée.entry
                poignée.entry = None
        if entrée is None:
            return
        try:
            session = self._connect(Path(entrée.socket), None)
            # Délai court : ici on attend un worker qu'on s'apprête à tuer de
            # toute façon. Lui laisser le temps nominal ferait patienter une
            # minute avant un chargement, par politesse envers un processus qui
            # ne répond plus. Depuis que l'admission épargne les résidents
            # occupés, un évincé qui tarde est un évincé bloqué.
            session.timeouts = replace(self.timeouts, unload_s=min(self.timeouts.unload_s, 5))
        except OSError:
            session = None
        if session is not None:
            try:
                session.unload()
            except Exception:  # noqa: BLE001 — un worker qui refuse de décharger est tué
                pass
            finally:
                session.close()
        kill_pid(entrée.pid, grace_s=self.timeouts.grace_s)
        Path(entrée.socket).unlink(missing_ok=True)

    def close(self) -> None:
        """Le processus s'arrête : ses workers restent, son occupation non.

        Un résident survit délibérément à qui l'a lancé — c'est ce qui évite de
        repayer le warmup. Ce qui ne doit pas lui survivre, c'est la ligne qui dit
        qu'un job tourne dessus : le pid mort finirait par la démentir, mais
        seulement à qui pense à le vérifier.
        """
        with self._mutex:
            for poignée in self._live.values():
                poignée.job = None
                poignée.since = 0.0
                poignée.holder = None
        try:
            with self.registry_file.locked() as entries:
                self._sync(entries)
        except OSError:  # le home a disparu sous nos pieds : il n'y a plus de miroir à tenir
            pass

    def health(self, *, timeout_s: float = 5) -> dict[str, bool]:
        """Interroge chaque résident : répond-il encore ?

        Ne tue rien, et c'est délibéré : de l'extérieur, un worker occupé par un
        job et un worker bloqué se ressemblent — aucun des deux ne lit son
        socket. Trancher à leur place reviendrait à tuer un job en cours pour
        cause de lenteur. On rapporte, l'utilisateur décide (`ecurie unload
        --force`).

        La limite s'est déplacée avec la tâche 4.6 : les jobs que **nous** tenons,
        nous les connaissons. On ne les interroge pas — le ping attendrait la fin
        du job dans le backlog du socket pour apprendre ce que la mémoire dit
        déjà. Reste le cas honnêtement indécidable : un worker qu'un autre
        processus occupe.
        """
        états: dict[str, bool] = {}
        for entrée in self._entries().values():
            poignée = self._live.get(entrée.ref)
            if poignée is not None and poignée.busy:
                états[entrée.ref] = True
                continue
            try:
                session = self._connect(Path(entrée.socket), None)
            except OSError:
                états[entrée.ref] = False
                continue
            session.timeouts = replace(self.timeouts, ping_s=timeout_s)
            try:
                session.ping()
                états[entrée.ref] = True
            except Exception:  # noqa: BLE001 — muet ou occupé : de l'extérieur, c'est pareil
                états[entrée.ref] = False
            finally:
                session.close()
        return états

    def prune(self) -> list[str]:
        """Nettoie les entrées périmées — en tuant ce qui doit l'être.

        Deux cas se cachent derrière « périmée ». Le processus est mort : il n'y
        a que l'entrée à retirer. Ou le processus vit mais son socket a disparu :
        c'est un worker devenu injoignable, qui tient toujours ses gigaoctets. Le
        retirer du registre sans le tuer ferait pire que le mal — plus personne
        ne saurait qu'il existe, et le budget mémoire compterait de la place qui
        n'est pas libre.
        """
        périmées = self.registry_file.stale()
        for entrée in périmées:
            if pid_alive(entrée.pid):
                kill_pid(entrée.pid, grace_s=self.timeouts.grace_s)
                Path(entrée.socket).unlink(missing_ok=True)
        if périmées:
            with self.registry_file.locked() as entries:
                # `locked()` retire les entrées périmées en entrant ; `_sync` en
                # tire les conséquences côté mémoire.
                self._sync(entries)
        return [e.ref for e in périmées]

    def env_problems(self, variant: Variant, ref: str) -> str | None:
        try:
            self._spec_factory(self.repo_root, variant, ref)
        except EnvError as exc:
            return str(exc)
        return None
