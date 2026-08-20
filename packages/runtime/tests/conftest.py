"""Fixtures d'exécution : un parc synthétique et des workers qui ne chargent rien.

Tout ce qui est testé ici l'est en CI, sur des machines sans Apple Silicon, sans
poids et sans venv de runtime. C'est ce que permet `workers/fake.py` : un worker
qui parle le protocole intégralement, et dont on demande les pannes par
variables d'environnement.
"""

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from ecurie_core.config import Config, ScanConfig
from ecurie_core.registry import load_registry
from ecurie_runtime.envs import WorkerSpec
from ecurie_runtime.supervisor import Supervisor
from ecurie_runtime.worker import Timeouts

REPO_ROOT = Path(__file__).parents[3]
GIB = 1 << 30

FAKE_ARGV = [sys.executable, "-m", "ecurie_runtime.workers.fake"]


class _DumperIndenté(yaml.SafeDumper):
    """Indente les tirets de liste comme les manifestes du dépôt.

    PyYAML colle les tirets à la marge de leur clé ; les manifestes de
    `registry/models/` les indentent. Les clés d'un variant se retrouvent donc à
    quatre espaces au lieu de deux — exactement l'indentation que suppose le
    patch de `ecurie bench`. Un test qui écrirait ses manifestes autrement
    validerait un collage qui échoue sur les vrais fichiers.
    """

    def increase_indent(self, flow: bool = False, indentless: bool = False):
        return super().increase_indent(flow, False)


def dump_manifeste(document: dict) -> str:
    return yaml.dump(document, Dumper=_DumperIndenté, sort_keys=False, allow_unicode=True)


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def ecurie_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "ecurie-home"
    home.mkdir()
    monkeypatch.setenv("ECURIE_HOME", str(home))
    return home


@pytest.fixture
def parc(tmp_path: Path):
    """Construit un registre synthétique valide, poids locaux inclus.

    Les poids sont de vrais fichiers : `resolve_weights` refuse de lancer un
    worker sur un chemin absent, et c'est une garantie qu'on ne veut pas
    contourner dans les tests — sinon on éprouverait un chemin de code qui
    n'existe pas en production.
    """

    class Parc:
        def __init__(self) -> None:
            self.root = tmp_path / "depot"
            (self.root / "registry" / "schema").mkdir(parents=True)
            (self.root / "registry" / "models").mkdir(parents=True)
            (self.root / "registry" / "capabilities").mkdir(parents=True)
            for nom in ("model.schema.json", "capability.schema.json"):
                shutil.copy(REPO_ROOT / "registry" / "schema" / nom,
                            self.root / "registry" / "schema" / nom)
            self.weights = tmp_path / "poids"
            self.weights.mkdir()
            (self.weights / "model.safetensors").write_bytes(b"W" * 4096)

        def capability(self, cap_id: str = "text-to-speech") -> "Parc":
            shutil.copy(
                REPO_ROOT / "registry" / "capabilities" / f"{cap_id}.json",
                self.root / "registry" / "capabilities" / f"{cap_id}.json",
            )
            return self

        def model(
            self,
            model_id: str = "tts-test",
            *,
            capability: str = "text-to-speech",
            peak_bytes: int | None = 2 * GIB,
            defaults: dict | None = None,
            options: dict | None = None,
            weights: Path | None = None,
            incumbent: bool = False,
        ) -> "Parc":
            variant: dict = {
                "id": "essai",
                "tier": "hot",
                "runtime": "mlx-audio",
                "source": {"kind": "local", "path": str(weights or self.weights)},
            }
            if defaults:
                variant["defaults"] = defaults
            if options:
                variant["options"] = options
            if peak_bytes is not None:
                variant["profile"] = {
                    "disk_bytes": 4096,
                    "peak_unified_memory_bytes": peak_bytes,
                    "warmup_ms": 10,
                    "measured_on": "machine d'essai",
                    "measured_at": "2026-08-20",
                    "harness_version": "0.3.0",
                }
            document = {
                "id": model_id,
                "capability": capability,
                "license": "apache-2.0",
                "status": "active",
                "incumbent": incumbent,
                "variants": [variant],
            }
            (self.root / "registry" / "models" / f"{model_id}.yaml").write_text(
                dump_manifeste(document)
            )
            return self

        def mesure(self, ref: str, profile: dict) -> "Parc":
            dossier = self.root / "registry" / "measurements"
            dossier.mkdir(parents=True, exist_ok=True)
            (dossier / f"{ref}.json").write_text(
                json.dumps({"ref": ref, "profile": profile, "harness_version": "0.3.0"})
            )
            return self

        def load(self):
            return load_registry(self.root)

    return Parc()


@pytest.fixture
def config(ecurie_home: Path) -> Config:
    """Budget fixe : les tests d'admission comparent des chiffres, pas la machine."""
    return Config(memory_budget=8 * GIB, scan=ScanConfig(), max_heavy_resident=1,
                  heavy_threshold_bytes=6 * GIB)


@pytest.fixture
def fake_spec_factory():
    """Fabrique de `WorkerSpec` qui lance le worker d'essai au lieu d'un runtime réel.

    Le point d'injection est celui du superviseur lui-même : tout le reste du
    chemin — admission, registre des résidents, socket, protocole, éviction — est
    exactement celui de production.
    """

    def build(env_vars: dict[str, str] | None = None):
        def factory(root: Path, variant, ref: str, capability: str | None = None) -> WorkerSpec:
            return WorkerSpec(argv=list(FAKE_ARGV), env_vars=dict(env_vars or {}), label=ref)

        return factory

    return build


@pytest.fixture
def supervisor_factory(config: Config, fake_spec_factory, ecurie_home: Path):
    def build(parc, *, env_vars: dict[str, str] | None = None, timeouts: Timeouts | None = None):
        registry = parc.load()
        assert not registry.errors, registry.errors
        return Supervisor(
            parc.root,
            registry,
            config,
            home=ecurie_home,
            timeouts=timeouts or Timeouts(load_s=30, infer_s=30, ping_s=5, grace_s=2),
            spec_factory=fake_spec_factory(env_vars),
        )

    return build
