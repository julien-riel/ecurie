"""L'état du serveur MCP — ce qui se relit, ce qui se paie une fois.

Le jumeau d'`ecurie_api.state.AppState`, et il n'en est pas une copie par
paresse : le pivot du 29 août fait du serveur MCP le produit, et un adoptant qui
n'installe que lui n'a aucune raison de tirer `fastapi`, `uvicorn` et
`python-multipart` pour trois lectures de disque. Ce paquet ne dépend donc que de
`core`, `store` et `runtime`. Ce que les deux états partagent — le rechargement à
chaud du registre, le budget payé une fois, le superviseur unique — est un
raisonnement identique appliqué deux fois, pas du code recopié : la version HTTP
sert des requêtes concurrentes, celle-ci sert un agent qui appelle ses outils
l'un après l'autre.

Trois décisions, et chacune se paie si on la manque.

**Le budget se mesure au démarrage, pas au premier outil.** Le détecter lance un
sous-processus dans le venv d'un runtime pour interroger Metal — de l'ordre de la
seconde. Un client MCP démarre son serveur au lancement de la session et attend
la réponse à `tools/list` avant de rendre la main ; payer la mesure là plutôt
qu'au premier appel évite qu'un `text_to_speech` paraisse gelé pour une raison
qui n'a rien à voir avec lui.

**Le registre se recharge à chaud, comme côté HTTP.** Une session d'agent dure
des heures, et « ajouter un modèle = ajouter un YAML » perdrait beaucoup s'il
fallait redémarrer Claude Code pour voir le manifeste qu'on vient d'écrire. Le
coût est d'une quarantaine de `stat()`, sans lecture ni validation tant que rien
ne bouge. Le catalogue d'outils, lui, ne bouge pas en cours de session — voir
`catalogue.py` : c'est une liste éditoriale, pas une projection du registre.

**Rien n'est écrit sur stdout.** C'est le canal JSON-RPC. Un `print` de
débogage, une barre de progression `rich`, un avertissement d'une bibliothèque
tierce, et le client ne parle plus au serveur — il lit une trame illisible et
ferme. Tout ce que ce paquet a à dire part sur stderr, que les clients MCP
capturent dans leurs journaux.
"""

import threading
from dataclasses import dataclass
from pathlib import Path

from ecurie_core.config import Config
from ecurie_core.registry import Registry, load_registry
from ecurie_runtime.budget import Budget, detect_budget
from ecurie_runtime.supervisor import Supervisor
from ecurie_runtime.worker import Timeouts
from ecurie_store.db import StateDB

# Ce que `load_registry` lit réellement, repris de `ecurie_api.state` : surveiller
# `registry/` en entier ferait entrer les images du banc d'essai dans l'empreinte.
WATCHED = (
    ("registry/schema", "*.json"),
    ("registry/capabilities", "*.json"),
    ("registry/models", "*.yaml"),
    ("registry/measurements", "**/*.json"),
)


def registry_signature(root: Path) -> tuple:
    """Empreinte du registre sur le disque : chemins, tailles, dates de modification."""
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


@dataclass(frozen=True)
class Exposition:
    """Ce que le serveur accepte de montrer et d'exécuter.

    Deux réglages distincts, et les confondre serait un contresens. `familles`
    élargit le **catalogue** — les outils déclarés dans `tools/list`. Le second
    décide ce qu'`ecurie_run` accepte d'exécuter, et il porte sur une propriété du
    contrat, pas sur une liste de noms : `human_subject` dit ce qu'une capacité
    fait d'une personne réelle. Une capacité qui identifie quelqu'un ne devient
    pas acceptable parce qu'elle est passée par l'échappatoire plutôt que par le
    catalogue.
    """

    familles: frozenset[str] = frozenset()

    @property
    def toutes(self) -> bool:
        return "all" in self.familles

    def famille_ouverte(self, nom: str) -> bool:
        return self.toutes or nom in self.familles


class Contexte:
    """Ce que tous les outils partagent, monté par le `lifespan` du serveur."""

    def __init__(
        self,
        root: Path,
        config: Config,
        *,
        exposition: Exposition | None = None,
        home: Path | None = None,
        timeouts: Timeouts | None = None,
        budget: Budget | None = None,
        spec_factory=None,
    ) -> None:
        self.root = root.resolve()
        self.config = config
        self.exposition = exposition or Exposition()
        self.home = home
        self.timeouts = timeouts or Timeouts()
        self.spec_factory = spec_factory
        self._registry: Registry | None = None
        self._signature: tuple = ()
        self._registry_lock = threading.Lock()
        self._budget = budget
        self._budget_lock = threading.Lock()
        self._supervisor: Supervisor | None = None
        self._supervisor_lock = threading.Lock()

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

        Il tient le tour de rôle par variant et l'occupation des résidents. Un
        second processus qui sert le même parc — un `ecurie serve` laissé ouvert,
        un `ecurie run` dans un terminal — se voit par `residents.json` et par le
        verrou de fichier qui sérialise les admissions : c'est ce que la tâche
        4.6 a mis en place, et le serveur MCP en hérite sans rien y ajouter.
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

    def open_db(self) -> StateDB:
        return StateDB(self.config.state_db)

    def close(self) -> None:
        """La session d'agent se termine : les workers restent, l'occupation non."""
        with self._supervisor_lock:
            if self._supervisor is not None:
                self._supervisor.close()
                self._supervisor = None
