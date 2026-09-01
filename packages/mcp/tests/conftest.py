"""Fixtures du serveur MCP : un dépôt synthétique, un worker qui ne charge rien.

Le même parti que les fixtures d'API — tout est vrai sauf ce qu'on ne peut pas
avoir en CI. Vrai JSON Schema, vrais contrats copiés du dépôt, vrai chargement du
registre, vraie admission, vrai protocole de worker, vrai serveur MCP. Sont
simulés les trois choses qu'une machine d'intégration n'a pas : des poids de
plusieurs gigaoctets, un venv de runtime, et un budget mémoire lu dans Metal.

Le client MCP, lui, est **réel** : `Client(serveur)` du SDK connecte l'objet
`Server` en processus et parle le protocole pour de bon — pas un appel direct aux
handlers. Ce qui est éprouvé est donc bien ce qu'un client verra, jusqu'à la
sérialisation des `Tool` et des `CallToolResult`.
"""

import shutil
import sys
from pathlib import Path

import pytest
import yaml
from ecurie_core.config import Config, ScanConfig
from ecurie_mcp.contexte import Contexte, Exposition
from ecurie_runtime.budget import Budget
from ecurie_runtime.envs import WorkerSpec
from ecurie_runtime.worker import Timeouts

REPO_ROOT = Path(__file__).parents[3]
GIB = 1 << 30
BUDGET_TEST = 16 * GIB


@pytest.fixture
def ecurie_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "ecurie-home"
    home.mkdir()
    monkeypatch.setenv("ECURIE_HOME", str(home))
    return home


@pytest.fixture
def depot(tmp_path: Path):
    """Un dépôt Écurie complet et modifiable en cours de test."""

    class Depot:
        def __init__(self) -> None:
            self.root = tmp_path / "depot"
            (self.root / "registry" / "schema").mkdir(parents=True)
            (self.root / "registry" / "models").mkdir(parents=True)
            (self.root / "registry" / "capabilities").mkdir(parents=True)
            for nom in ("model.schema.json", "capability.schema.json"):
                shutil.copy(
                    REPO_ROOT / "registry" / "schema" / nom,
                    self.root / "registry" / "schema" / nom,
                )
            self.weights = tmp_path / "poids"
            self.weights.mkdir()
            (self.weights / "model.safetensors").write_bytes(b"W" * 4096)

        def capability(self, cap_id: str) -> "Depot":
            shutil.copy(
                REPO_ROOT / "registry" / "capabilities" / f"{cap_id}.json",
                self.root / "registry" / "capabilities" / f"{cap_id}.json",
            )
            return self

        def env(self, name: str, *, synced: bool = True) -> "Depot":
            dossier = self.root / "runtimes" / name
            dossier.mkdir(parents=True, exist_ok=True)
            (dossier / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
            if synced:
                binaire = dossier / ".venv" / "bin"
                binaire.mkdir(parents=True, exist_ok=True)
                (binaire / "python").write_text("#!/bin/sh\n")
            return self

        def model(
            self,
            model_id: str = "tts-test",
            *,
            capability: str = "text-to-speech",
            runtime: str = "mlx-audio",
            peak_bytes: int | None = 2 * GIB,
            peak_scaling: dict | None = None,
            defaults: dict | None = None,
            weights: Path | None | str = None,
            incumbent: bool = True,
            status: str = "active",
            variant_id: str = "essai",
        ) -> "Depot":
            variant: dict = {
                "id": variant_id,
                "tier": "hot",
                "runtime": runtime,
                "source": {
                    "kind": "local",
                    "path": str(self.weights if weights is None else weights),
                },
            }
            if defaults:
                variant["defaults"] = defaults
            if peak_bytes is not None:
                variant["profile"] = {
                    "disk_bytes": 4096,
                    "peak_unified_memory_bytes": peak_bytes,
                    "warmup_ms": 10,
                    "measured_on": "machine d'essai",
                    "measured_at": "2026-08-20",
                    "harness_version": "0.3.0",
                }
                if peak_scaling:
                    variant["profile"]["peak_scaling"] = peak_scaling
            document = {
                "id": model_id,
                "capability": capability,
                "license": "apache-2.0",
                "status": status,
                "incumbent": incumbent,
                "variants": [variant],
            }
            (self.root / "registry" / "models" / f"{model_id}.yaml").write_text(
                yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
            )
            return self

    return Depot()


@pytest.fixture
def parc(depot):
    """Le cas courant : une capacité, un modèle prêt, son environnement synchronisé."""
    return depot.capability("text-to-speech").env("mlx-audio").model()


@pytest.fixture
def config(ecurie_home: Path) -> Config:
    return Config(memory_budget=BUDGET_TEST, scan=ScanConfig())


@pytest.fixture
def fake_spec_factory():
    """Lance `workers/fake.py` au lieu d'un runtime réel.

    Le point d'injection est celui du superviseur : tout le reste du chemin —
    admission, tour de rôle, socket, protocole, manifeste, ligne de `runs` — est
    celui de production.
    """

    def factory(root: Path, variant, ref: str, capability: str | None = None) -> WorkerSpec:
        return WorkerSpec(
            argv=[sys.executable, "-m", "ecurie_runtime.workers.fake"], env_vars={}, label=ref
        )

    return factory


@pytest.fixture
def contexte_factory(config: Config, ecurie_home: Path, fake_spec_factory):
    """Un contexte de serveur par dépôt, et aucun worker qui survit au test."""
    créés: list[Contexte] = []

    def build(depot, *, familles: frozenset[str] | None = None) -> Contexte:
        contexte = Contexte(
            depot.root,
            config,
            exposition=Exposition(familles=familles or frozenset()),
            home=ecurie_home,
            budget=Budget(BUDGET_TEST, "budget d'essai, injecté"),
            timeouts=Timeouts(load_s=30, infer_s=30, ping_s=5, grace_s=2, queue_s=30),
            spec_factory=fake_spec_factory,
        )
        créés.append(contexte)
        return contexte

    yield build

    for contexte in créés:
        if contexte._supervisor is not None:
            contexte._supervisor.unload_all(force=True)
        contexte.close()


@pytest.fixture
def serveur_factory(contexte_factory):
    """Le serveur MCP monté sur un dépôt, prêt à recevoir un client."""
    from ecurie_mcp.serveur import construire

    def build(depot, *, familles: frozenset[str] | None = None):
        contexte = contexte_factory(depot, familles=familles)
        serveur, servi = construire(contexte)
        return serveur, servi, contexte

    return build
