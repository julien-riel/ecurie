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
"""

import threading
from pathlib import Path

from ecurie_core.config import Config
from ecurie_core.registry import Registry, load_registry
from ecurie_runtime.budget import Budget, detect_budget
from ecurie_runtime.supervisor import Supervisor
from ecurie_runtime.worker import Timeouts
from ecurie_store.db import StateDB

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
    ) -> None:
        self.root = root.resolve()
        self.config = config
        self.home = home
        self.timeouts = timeouts or Timeouts()
        self._registry: Registry | None = None
        self._signature: tuple = ()
        self._registry_lock = threading.Lock()
        self._budget = budget
        # Deux verrous et non un : la détection du budget peut tenir trente
        # secondes sur un sous-processus, et une lecture du registre n'a aucune
        # raison de l'attendre.
        self._budget_lock = threading.Lock()

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
        """Un superviseur neuf par requête, sur le registre courant et le budget mesuré.

        Neuf, parce qu'il porte le registre : le garder en vie ferait servir à
        l'admission un manifeste que le rechargement a déjà remplacé. Le
        superviseur lui-même ne tient aucun état — celui des résidents vit dans
        `~/.ecurie/residents.json`, sous verrou de fichier. Ce n'est plus vrai à
        partir de la tâche 4.6, où il déménage dans le processus de l'API.
        """
        return Supervisor(
            self.root,
            self.registry(),
            self.config,
            home=self.home,
            timeouts=self.timeouts,
            budget=self.budget,
        )

    def open_db(self) -> StateDB:
        return StateDB(self.config.state_db)
