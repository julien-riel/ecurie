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

Le verrou du registre est tenu pendant tout le chargement, y compris s'il dure
des minutes. C'est délibéré : deux commandes lancées en même temps qui
décideraient chacune de leur côté qu'il reste de la place, c'est exactement le
double chargement que le contrôle d'admission existe pour empêcher. Les lectures
(`ecurie ps`) ne prennent pas ce verrou et ne sont donc jamais bloquées.
"""

import hashlib
import json
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecurie_core.config import Config, ecurie_home
from ecurie_core.models import Model, Variant
from ecurie_core.registry import Registry
from ecurie_store.weights import WeightsLocation, resolve_weights

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


class AdmissionRefused(RuntimeError):
    """Le job ne peut pas être admis. Le message dit pourquoi et ce qui débloquerait."""

    def __init__(self, admission: Admission) -> None:
        super().__init__(admission.reason)
        self.admission = admission


class RefError(RuntimeError):
    """Référence de variant inconnue ou ambiguë."""


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


def variant_document(model: Model, variant: Variant, weights: WeightsLocation) -> dict[str, Any]:
    """Le manifeste résolu transmis au worker : tout ce qu'il lui faut, et rien de plus.

    Le worker ne lit pas le registre, ne connaît pas le cache, ne choisit pas sa
    révision. Il reçoit un chemin local déjà vérifié — c'est ce qui garantit que
    la révision exécutée est celle qui sera écrite au manifeste du job.
    """
    return {
        "ref": f"{model.id}@{variant.id}",
        "model_id": model.id,
        "variant_id": variant.id,
        "capability": model.capability,
        "runtime": variant.runtime,
        "quantization": variant.quantization,
        "weights_path": str(weights.path),
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
        """Rend la main. Un worker résident reste chargé, un éphémère est tué."""
        if self.on_release is not None:
            self.on_release()
        if self.resident:
            self.session.close()
        elif isinstance(self.session, WorkerProcess):
            try:
                self.session.unload()
            except Exception:  # noqa: BLE001 — on le tue juste après, l'échec n'apprend rien
                pass
            self.session.close()
        else:
            self.session.close()


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
    ) -> None:
        self.repo_root = repo_root
        self.registry = registry
        self.config = config
        self.home = home or ecurie_home()
        self.timeouts = timeouts or Timeouts()
        self.registry_file = ResidentRegistry(self.home)
        self._spec_factory = spec_factory or (
            lambda root, variant, ref, capability=None: spec_for_variant(
                root, variant, ref=ref, capability=capability
            )
        )
        # Le budget se détecte en lançant un sous-processus dans le venv d'un
        # runtime pour y interroger MLX : c'est bon marché une fois par commande,
        # ruineux à chaque requête HTTP. Un serveur qui reconstruit un superviseur
        # par requête le mesure donc une fois et le passe ici.
        self._budget: Budget | None = budget

    # --- lecture -------------------------------------------------------------

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
            heavy_threshold_bytes=self.config.heavy_threshold_bytes,
        )

    def residents(self) -> list[ResidentEntry]:
        return sorted(self.registry_file.read().values(), key=lambda e: -e.last_used)

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

    def simulate(self, ref: str, peak_bytes: int | None, *, measure: bool = False) -> Admission:  # noqa: E501
        """Ce que ferait `acquire`, sans rien charger — c'est ce qu'affiche `ecurie ps`."""
        residents = [e.as_resident() for e in self.registry_file.read().values()]
        return plan_admission(ref, peak_bytes, residents, self.policy, measure=measure)

    # --- chargement ----------------------------------------------------------

    def acquire(
        self,
        model: Model,
        variant: Variant,
        *,
        measure: bool = False,
        pin: bool = False,
        on_progress: ProgressFn | None = None,
        values: dict[str, Any] | None = None,
    ) -> Lease:
        """Rend un worker prêt pour ce variant, en respectant le budget mémoire.

        `values` est l'entrée résolue du job : elle sert au calcul du pic attendu
        quand le profil déclare une pente (`peak_scaling`).
        """
        ref = f"{model.id}@{variant.id}"
        weights = resolve_weights(self.config, variant, ref=ref)
        spec = self._spec_factory(self.repo_root, variant, ref, model.capability)
        document = variant_document(model, variant, weights)

        with self.registry_file.locked() as entries:
            residents = [e.as_resident() for e in entries.values()]
            admission = plan_admission(
                ref, self.peak_bytes(variant, values), residents, self.policy, measure=measure
            )
            if not admission.admitted:
                raise AdmissionRefused(admission)

            for victime in admission.evict:
                self._evict(entries, victime)

            if measure:
                return self._ephemeral(ref, spec, document, admission, on_progress)

            entrée = entries.get(ref)
            empreinte = document_fingerprint(document)
            if entrée is not None and entrée.document not in ("", empreinte):
                # Le manifeste a changé depuis le chargement : le worker en
                # mémoire n'est plus celui que le registre décrit.
                self._evict(entries, ref)
                entrée = None
            if entrée is not None:
                lease = self._reconnect(ref, entrée, admission, on_progress)
                if lease is not None:
                    entrée.last_used = time.time()
                    entrée.pinned = entrée.pinned or pin
                    self._mark_busy(entrée)
                    return lease
                # Socket mort : le worker n'est plus là malgré son entrée.
                self._evict(entries, ref)

            return self._start_resident(
                entries, ref, variant, spec, document, admission, pin, on_progress
            )

    def _ephemeral(
        self,
        ref: str,
        spec: WorkerSpec,
        document: dict[str, Any],
        admission: Admission,
        on_progress: ProgressFn | None,
    ) -> Lease:
        worker = WorkerProcess.spawn(
            spec,
            log_path=log_path(self.home, ref),
            timeouts=self.timeouts,
            on_progress=on_progress,
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
        ref: str,
        variant: Variant,
        spec: WorkerSpec,
        document: dict[str, Any],
        admission: Admission,
        pin: bool,
        on_progress: ProgressFn | None,
    ) -> Lease:
        sock_path = socket_path(self.home, ref)
        journal = log_path(self.home, ref)
        sock_path.unlink(missing_ok=True)
        pid = spawn_detached(
            spec, sock_path, journal, idle_timeout_s=self.config.resident_idle_timeout_s
        )
        try:
            wait_for_socket(sock_path, pid, timeout_s=self.timeouts.load_s, log_path=journal)
            session = self._connect(sock_path, on_progress)
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
        self._mark_busy(entrée)
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
            on_release=lambda: self._free(ref),
        )

    def _mark_busy(self, entrée: ResidentEntry) -> None:
        entrée.busy_by = os.getpid()
        entrée.busy_since = time.time()

    def _free(self, ref: str) -> None:
        """Le job est fini : le résident redevient évinçable."""
        with self.registry_file.locked() as entries:
            entrée = entries.get(ref)
            if entrée is not None and entrée.busy_by == os.getpid():
                entrée.busy_by = 0
                entrée.busy_since = 0.0
                entrée.last_used = time.time()

    def _reconnect(
        self, ref: str, entrée: ResidentEntry, admission: Admission, on_progress: ProgressFn | None
    ) -> Lease | None:
        """Se rattache à un worker résident, sans le déranger.

        Aucun ping ici, et c'est délibéré : un worker occupé par un autre job ne
        lit pas son socket, donc il ne répondrait pas — et le prendre pour un
        fantôme reviendrait à tuer un job en cours pour récupérer sa place. La
        preuve de vie est ailleurs : le processus existe (vérifié à la lecture du
        registre) et le socket accepte la connexion. Deux commandes lancées sur
        le même variant se sérialisent alors dans la file d'écoute du worker, ce
        qui est le comportement voulu — un modèle sert un job à la fois.
        """
        try:
            session = self._connect(Path(entrée.socket), on_progress)
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
            on_release=lambda: self._free(ref),
        )

    def _connect(self, sock_path: Path, on_progress: ProgressFn | None) -> ResidentSession:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(sock_path))
        return ResidentSession(sock, timeouts=self.timeouts, on_progress=on_progress)

    # --- déchargement --------------------------------------------------------

    def unload(self, ref: str, *, force: bool = False) -> bool:
        with self.registry_file.locked() as entries:
            entrée = entries.get(ref)
            if entrée is None:
                return False
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
            cibles = [ref for ref, e in entries.items() if force or not e.pinned]
            for ref in cibles:
                self._evict(entries, ref)
            return cibles

    def _evict(self, entries: dict[str, ResidentEntry], ref: str) -> None:
        """Décharge poliment, puis tue. La mémoire doit être rendue avant le prochain load."""
        entrée = entries.pop(ref, None)
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

    def health(self, *, timeout_s: float = 5) -> dict[str, bool]:
        """Interroge chaque résident : répond-il encore ?

        Ne tue rien, et c'est délibéré : de l'extérieur, un worker occupé par un
        job et un worker bloqué se ressemblent — aucun des deux ne lit son
        socket. Trancher à leur place reviendrait à tuer un job en cours pour
        cause de lenteur. On rapporte, l'utilisateur décide (`ecurie unload
        --force`). C'est la limite honnête de la règle du §5.1 de la conception
        appliquée depuis une CLI plutôt que depuis un serveur qui, lui, saura
        qu'un job tourne (v0.4).
        """
        états: dict[str, bool] = {}
        for entrée in self.registry_file.read().values():
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
            with self.registry_file.locked():
                pass  # `locked()` retire les entrées périmées en entrant
        return [e.ref for e in périmées]

    def env_problems(self, variant: Variant, ref: str) -> str | None:
        try:
            self._spec_factory(self.repo_root, variant, ref)
        except EnvError as exc:
            return str(exc)
        return None
