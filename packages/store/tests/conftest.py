"""Fixtures : un faux parc complet (cache HF, blobs Ollama, LM Studio).

Les tailles sont choisies pour que les trois chiffres attendus soient exacts :

apparent = HF (1000+10+500+200) + Ollama (1000+300+100) + LM Studio (2000+2000) = 7110
réel     = 1000+10+500+200+300+100+2000                                          = 4110
récupérable : duplication 1000 (blob LIVE présent dans HF et Ollama),
              révisions HF obsolètes 500 (STALE), orphelins 300 (HF 200 + Ollama 100)
"""

import json
import os
from pathlib import Path

import pytest
from park import (  # noqa: F401
    ETAG40,
    HF_REPO,
    REV_LIVE,
    REV_STALE,
    SHA_CONF,
    SHA_HORPH,
    SHA_LIVE,
    SHA_OORPH,
    SHA_STALE,
)

REPO_ROOT = Path(__file__).parents[3]


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def fake_hf(tmp_path: Path) -> Path:
    hub = tmp_path / "hf-hub"
    repo = hub / "models--mlx-community--Qwen3-TTS-1.7B-8bit"
    blobs = repo / "blobs"
    blobs.mkdir(parents=True)
    (blobs / SHA_LIVE).write_bytes(b"L" * 1000)
    (blobs / ETAG40).write_bytes(b"c" * 10)
    (blobs / SHA_STALE).write_bytes(b"S" * 500)
    (blobs / SHA_HORPH).write_bytes(b"O" * 200)

    snap_live = repo / "snapshots" / REV_LIVE
    snap_live.mkdir(parents=True)
    os.symlink(Path("..") / ".." / "blobs" / SHA_LIVE, snap_live / "model.safetensors")
    os.symlink(Path("..") / ".." / "blobs" / ETAG40, snap_live / "config.json")

    snap_stale = repo / "snapshots" / REV_STALE
    snap_stale.mkdir(parents=True)
    os.symlink(Path("..") / ".." / "blobs" / SHA_STALE, snap_stale / "old.safetensors")

    refs = repo / "refs"
    refs.mkdir()
    (refs / "main").write_text(REV_LIVE)
    return hub


@pytest.fixture
def fake_ollama(tmp_path: Path) -> Path:
    models = tmp_path / "ollama-models"
    manifest_dir = models / "manifests" / "registry.ollama.ai" / "library" / "test"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "latest").write_text(
        json.dumps(
            {
                "layers": [{"digest": f"sha256:{SHA_LIVE}"}],
                "config": {"digest": f"sha256:{SHA_CONF}"},
            }
        )
    )
    blobs = models / "blobs"
    blobs.mkdir()
    (blobs / f"sha256-{SHA_LIVE}").write_bytes(b"L" * 1000)
    (blobs / f"sha256-{SHA_CONF}").write_bytes(b"C" * 300)
    (blobs / f"sha256-{SHA_OORPH}").write_bytes(b"o" * 100)
    return models


@pytest.fixture
def fake_lmstudio(tmp_path: Path) -> Path:
    models = tmp_path / "lmstudio-models"
    repo = models / "editeur" / "depot"
    repo.mkdir(parents=True)
    (repo / "a.gguf").write_bytes(b"G" * 2000)
    os.link(repo / "a.gguf", repo / "b.gguf")
    (models / ".cache-interne").mkdir()
    (models / ".cache-interne" / "ignore.bin").write_bytes(b"x" * 999)
    return models
