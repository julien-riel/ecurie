"""État du serveur : ce qui se relit à chaque requête, et ce qui se paie une fois.

Le partage n'est pas symétrique, et c'est délibéré.

Le **registre** est de l'état déclaré qui vit dans Git (CONCEPTION.md §1.1). On
l'édite à la main, et la promesse du §4 de l'architecture — « ajouter un modèle =
ajouter un YAML, aucune ligne de front à écrire » — perdrait beaucoup s'il
fallait redémarrer le serveur pour voir le YAML qu'on vient d'écrire. Il est donc
rechargé dès que l'un des fichiers qui le composent a bougé. Le coût est d'une
quarantaine de `stat()` par requête, sans lecture ni validation tant que rien ne
change.

Le **budget mémoire** est à l'opposé : le détecter revient à lancer un
sous-processus dans le venv d'un runtime pour y interroger Metal via MLX
(`budget.py`). C'est un prix qu'on paie une fois au démarrage, jamais par
requête. Il ne change qu'au redémarrage de la machine ou à un `sysctl
iogpu.wired_limit_mb` — deux événements après lesquels relancer `ecurie serve`
n'est pas un fardeau.

Les **résidents**, enfin, ne sont ni l'un ni l'autre : ils se relisent
intégralement à chaque fois, sans cache, parce qu'un worker peut mourir entre
deux requêtes et qu'un budget calculé sur un fantôme est un budget faux.

Le **superviseur**, lui, est unique et vit aussi longtemps que le serveur (tâche
4.6). Ce n'est pas une économie : c'est là que se tiennent le tour de rôle des
jobs sur un même worker et l'occupation des résidents, deux choses qu'un objet
reconstruit à chaque requête ne peut pas savoir. Il ne fige pas le registre pour
autant — il le redemande, et le rechargement à chaud continue de valoir.
"""

import threading
from pathlib import Path

from ecurie_core.config import Config
from ecurie_core.registry import Registry, load_registry
from ecurie_runtime.budget import Budget, detect_budget
from ecurie_runtime.supervisor import SpecFactory, Supervisor
from ecurie_runtime.worker import Timeouts
from ecurie_store.db import StateDB

from ecurie_api.jobs import JobRegistry

# Ce que `load_registry` lit réellement. Surveiller `registry/` en entier ferait
# entrer les fichiers du banc d'essai et leurs images dans l'empreinte : des
# octets qui ne changent jamais la validation, et un `rglob` qui grossit avec les
# golden sets.
WATCHED = (
    ("registry/schema", "*.json"),
    ("registry/capabilities", "*.json"),
    ("registry/models", "*.yaml"),
    ("registry/measurements", "*.json"),
)


def registry_signature(root: Path) -> tuple:
    """Empreinte du registre sur le disque : chemins, tailles, dates de modification.

    La liste des fichiers en fait partie, pas seulement leur contenu : un
    manifeste supprimé doit disparaître de l'API aussi sûrement qu'un manifeste
    ajouté doit y entrer.
    """
    empreinte: list[tuple] = []
    for sous, motif in WATCHED:
        dossier = root / sous
        if not dossier.is_dir():
            empreinte.append((sous, None))
            continue
        for chemin in sorted(dossier.glob(motif)):
            try:
                stat = chemin.stat()
            except OSError:  # supprimé entre le glob et le stat : la prochaine passe le verra
                continue
            empreinte.append((str(chemin), stat.st_mtime_ns, stat.st_size))
    return tuple(empreinte)


class AppState:
    """Le contexte que toutes les routes partagent, monté sur `app.state.ecurie`."""

    def __init__(
        self,
        root: Path,
        config: Config,
        *,
        home: Path | None = None,
        timeouts: Timeouts | None = None,
        budget: Budget | None = None,
        spec_factory: SpecFactory | None = None,
    ) -> None:
        self.root = root.resolve()
        self.config = config
        self.home = home
        self.timeouts = timeouts or Timeouts()
        # Comment lancer un worker. Injecté par les tests, qui n'ont ni venv de
        # runtime ni poids : la fabrique d'essai lance `workers/fake.py`, et tout
        # le reste du chemin — admission, tour de rôle, socket, protocole — est
        # celui de production.
        self.spec_factory = spec_factory
        self._registry: Registry | None = None
        self._signature: tuple = ()
        self._registry_lock = threading.Lock()
        self._budget = budget
        # Deux verrous et non un : la détection du budget peut tenir trente
        # secondes sur un sous-processus, et une lecture du registre n'a aucune
        # raison de l'attendre.
        self._budget_lock = threading.Lock()
        self._supervisor: Supervisor | None = None
        self._supervisor_lock = threading.Lock()
        # Les jobs de cette session. Leur mémoire longue est le manifeste écrit
        # dans le dossier de chaque job, qui survit au redémarrage ; cette table
        # ne porte que ce qui se suit en direct.
        self.jobs = JobRegistry(self)

    def registry(self) -> Registry:
        empreinte = registry_signature(self.root)
        with self._registry_lock:
            if self._registry is None or empreinte != self._signature:
                self._registry = load_registry(self.root)
                self._signature = empreinte
            return self._registry

    @property
    def budget(self) -> Budget:
        with self._budget_lock:
            if self._budget is None:
                self._budget = detect_budget(self.config, repo_root=self.root)
            return self._budget

    def supervisor(self) -> Supervisor:
        """Le superviseur du processus — un seul, du démarrage à l'arrêt.

        Il en existait un par requête tant qu'il ne tenait aucun état : celui des
        résidents vivait dans `~/.ecurie/residents.json`, sous verrou de fichier.
        La tâche 4.6 y a mis deux choses qu'un objet jetable ne peut pas porter —
        le tour de rôle des jobs sur un même worker, et l'occupation de chaque
        résident. Un superviseur neuf par requête les oublierait entre la
        soumission d'un job et la question de savoir s'il tourne encore.

        Le registre, lui, n'est pas figé pour autant : il est **redemandé** à
        chaque lecture, et le rechargement à chaud du §6 de la conception
        continue de valoir.
        """
        with self._supervisor_lock:
            if self._supervisor is None:
                self._supervisor = Supervisor(
                    self.root,
                    self.registry(),
                    self.config,
                    home=self.home,
                    timeouts=self.timeouts,
                    budget=self.budget,
                    registry_provider=self.registry,
                    spec_factory=self.spec_factory,
                )
            return self._supervisor

    def close(self) -> None:
        """Le serveur s'arrête : ses workers restent, l'occupation qu'il publiait non."""
        with self._supervisor_lock:
            if self._supervisor is not None:
                self._supervisor.close()

    def open_db(self) -> StateDB:
        return StateDB(self.config.state_db)
