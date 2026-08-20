import json
import shutil
from pathlib import Path

import pytest
import yaml
from manifest_helpers import minimal_capability

REPO_ROOT = Path(__file__).parents[3]


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def registry_builder(tmp_path: Path):
    """Construit un registre synthétique avec le vrai schéma et de vrais contrats."""

    class Builder:
        def __init__(self) -> None:
            self.root = tmp_path
            schema_dst = tmp_path / "registry" / "schema"
            schema_dst.mkdir(parents=True)
            for nom in ("model.schema.json", "capability.schema.json"):
                shutil.copy(REPO_ROOT / "registry" / "schema" / nom, schema_dst / nom)
            (tmp_path / "registry" / "models").mkdir()
            (tmp_path / "registry" / "capabilities").mkdir()

        def capability(self, cap_id: str, document: dict | None = None) -> "Builder":
            """Copie le vrai contrat, ou en fabrique un minimal mais conforme."""
            dst = self.root / "registry" / "capabilities" / f"{cap_id}.json"
            src = REPO_ROOT / "registry" / "capabilities" / f"{cap_id}.json"
            if document is not None:
                dst.write_text(json.dumps(document, ensure_ascii=False))
            elif src.is_file():
                shutil.copy(src, dst)
            else:
                dst.write_text(json.dumps(minimal_capability(cap_id), ensure_ascii=False))
            return self

        def model(self, doc: dict, filename: str | None = None) -> "Builder":
            name = filename or f"{doc.get('id', 'sans-id')}.yaml"
            (self.root / "registry" / "models" / name).write_text(
                yaml.safe_dump(doc, sort_keys=False)
            )
            return self

        def runtime_env(self, env: str) -> "Builder":
            env_dir = self.root / "runtimes" / env
            env_dir.mkdir(parents=True)
            (env_dir / "pyproject.toml").write_text(f'[project]\nname = "{env}"\n')
            return self

    return Builder()
