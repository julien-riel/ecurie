import json
import shutil
from pathlib import Path

import pytest
import yaml

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
            shutil.copy(
                REPO_ROOT / "registry" / "schema" / "model.schema.json",
                schema_dst / "model.schema.json",
            )
            (tmp_path / "registry" / "models").mkdir()
            (tmp_path / "registry" / "capabilities").mkdir()

        def capability(self, cap_id: str) -> "Builder":
            src = REPO_ROOT / "registry" / "capabilities" / f"{cap_id}.json"
            dst = self.root / "registry" / "capabilities" / f"{cap_id}.json"
            if src.is_file():
                shutil.copy(src, dst)
            else:
                dst.write_text(json.dumps({"id": cap_id, "input": {}, "output": {}}))
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


def make_manifest(
    model_id: str = "test-tts",
    capability: str = "text-to-speech",
    status: str = "active",
    revision: str = "abc1234",
    incumbent: bool = False,
    tier: str = "absent",
    **extra,
) -> dict:
    return {
        "id": model_id,
        "capability": capability,
        "license": "apache-2.0",
        "status": status,
        "incumbent": incumbent,
        "variants": [
            {
                "id": "8bit-mlx",
                "tier": tier,
                "runtime": "mlx-audio",
                "source": {
                    "kind": "huggingface",
                    "repo": f"test/{model_id}",
                    "revision": revision,
                },
            }
        ],
        **extra,
    }
