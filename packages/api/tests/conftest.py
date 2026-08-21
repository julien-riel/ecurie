"""Fixtures d'API : un dépôt synthétique, une base d'état, un client HTTP.

Tout est vrai sauf les modèles : vrai JSON Schema, vrais contrats de capacité
copiés du dépôt, vrai chargement du registre, vraie base SQLite, vrai
`compute_figures`. Ce qui est simulé, c'est ce qu'on ne peut pas avoir en CI —
des poids de plusieurs gigaoctets, un venv de runtime, et un budget mémoire lu
dans Metal.

Le budget est **injecté** plutôt que détecté : `detect_budget` lance un
sous-processus dans le venv d'un runtime pour y interroger MLX, ce qu'un test ne
doit ni attendre ni voir échouer. Le chiffre est fixe, et les assertions portent
dessus.
"""

import shutil
from pathlib import Path

import pytest
import yaml
from ecurie_api.app import create_app
from ecurie_api.state import AppState
from ecurie_core.config import Config, ScanConfig
from ecurie_runtime.budget import Budget
from ecurie_store.db import LocationRecord, StateDB
from fastapi.testclient import TestClient

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
            """Un environnement de runtime. `synced` pose l'interpréteur attendu."""
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
        ) -> "Depot":
            variant: dict = {
                "id": "essai",
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
def state_factory(config: Config, ecurie_home: Path):
    def build(depot) -> AppState:
        return AppState(
            depot.root,
            config,
            home=ecurie_home,
            budget=Budget(BUDGET_TEST, "budget d'essai, injecté"),
        )

    return build


@pytest.fixture
def client_factory(state_factory):
    def build(depot) -> TestClient:
        state = state_factory(depot)
        client = TestClient(create_app(state))
        client.ecurie = state  # le test a parfois besoin de l'état, pas seulement du HTTP
        return client

    return build


@pytest.fixture
def client(client_factory, parc) -> TestClient:
    return client_factory(parc)


@pytest.fixture
def residents(ecurie_home: Path):
    """Écrit `residents.json` comme le ferait un chargement de worker.

    Le fichier est composé directement plutôt que par `ResidentRegistry.locked()` :
    la transaction écarte les entrées périmées **à l'entrée**, si bien qu'ajouter
    un résident vivant après un résident mort effacerait le second. Or c'est
    précisément le mort qu'on veut voir arriver jusqu'à l'API, dans `stale`.

    Le pid et le socket sont vrais par défaut — celui du processus de test, et un
    fichier qui existe — parce que `read()` écarte les entrées dont le processus
    est mort ou le socket disparu. Une entrée fabriquée avec un pid imaginaire ne
    serait jamais lue, et le test ne prouverait rien.
    """
    import json
    import os
    from dataclasses import asdict

    from ecurie_runtime.residents import VERSION, ResidentEntry

    entries: dict[str, ResidentEntry] = {}

    def add(
        ref: str,
        peak_bytes: int,
        *,
        pid: int | None = None,
        socket: Path | None = None,
        **champs,
    ) -> ResidentEntry:
        if socket is None:
            socket = ecurie_home / f"{ref.replace('@', '_')}.sock"
            socket.write_text("")
        entries[ref] = ResidentEntry(
            ref=ref,
            pid=os.getpid() if pid is None else pid,
            socket=str(socket),
            peak_bytes=peak_bytes,
            **champs,
        )
        (ecurie_home / "residents.json").write_text(
            json.dumps(
                {
                    "version": VERSION,
                    "written_at": "2026-08-20T12:00:00+00:00",
                    "residents": {
                        r: {k: v for k, v in asdict(e).items() if k != "ref"}
                        for r, e in entries.items()
                    },
                },
                ensure_ascii=False,
            )
        )
        return entries[ref]

    return add


@pytest.fixture
def locations(config: Config):
    """Remplit l'état observé, comme le ferait `ecurie store scan`."""

    def fill(records: list[LocationRecord], **kv: str) -> None:
        db = StateDB(config.state_db)
        try:
            par_gestionnaire: dict[str, list[LocationRecord]] = {}
            for record in records:
                par_gestionnaire.setdefault(record.manager, []).append(record)
            for manager, lot in par_gestionnaire.items():
                db.replace_manager(manager, lot)
            for clé, valeur in kv.items():
                db.set_kv(clé, valeur)
        finally:
            db.close()

    return fill
