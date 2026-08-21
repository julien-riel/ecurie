"""Un job réel, de la requête HTTP au fichier téléchargé (hors CI).

Le reste de la suite éprouve `/jobs` avec le worker d'essai : tout y est de
production sauf ce qui charge des poids. Ici, c'est un vrai modèle MLX derrière
la même route, sur le vrai registre et le vrai `~/.ecurie` — la seule façon de
savoir que le chemin complet tient, et le pendant côté serveur de
`test_real_run.py`.

    ecurie env sync mlx-audio
    ecurie pull qwen3-tts-1.7b@8bit-mlx
    pytest -m real

Saute avec un message qui dit quelle commande manque plutôt que d'échouer.
"""

import io
import time
import wave
from pathlib import Path

import pytest
from ecurie_api.app import create_app
from ecurie_api.state import AppState
from ecurie_core.config import load_config
from ecurie_core.registry import load_registry
from ecurie_runtime.envs import EnvError, spec_for_variant
from ecurie_runtime.supervisor import parse_ref
from ecurie_store.weights import WeightsMissing, resolve_weights
from fastapi.testclient import TestClient

pytestmark = pytest.mark.real

REPO = Path(__file__).parents[3]
REF_TTS = "qwen3-tts-1.7b@8bit-mlx"


@pytest.fixture
def client_reel():
    registre = load_registry(REPO)
    if registre.errors:
        pytest.skip(f"registre en erreur : {registre.errors[0].message}")
    try:
        model, variant, résolu = parse_ref(registre, REF_TTS)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(str(exc))

    config = load_config()
    try:
        spec_for_variant(REPO, variant, ref=résolu, capability=model.capability)
    except EnvError as exc:
        pytest.skip(str(exc))
    try:
        resolve_weights(config, variant, ref=résolu)
    except WeightsMissing as exc:
        pytest.skip(str(exc))

    state = AppState(REPO, config)
    client = TestClient(create_app(state))
    client.ecurie = state
    yield client
    state.supervisor().unload_all(force=True)


def test_un_job_tts_reel_du_post_au_fichier(client_reel):
    soumis = client_reel.post(
        "/jobs",
        json={"ref": REF_TTS, "input": {"text": "Le serveur lance ses propres jobs."}},
    )
    assert soumis.status_code == 202, soumis.text
    job_id = soumis.json()["id"]

    limite = time.monotonic() + 300
    while time.monotonic() < limite:
        corps = client_reel.get(f"/jobs/{job_id}").json()
        if corps["state"] in ("done", "failed"):
            break
        time.sleep(0.2)

    assert corps["state"] == "done", corps["error"]
    assert corps["manifest"]["revision"], "la révision épinglée doit être au manifeste"
    assert corps["metrics"].get("rtf"), "un job réel rapporte son facteur temps réel"

    fichier = client_reel.get(corps["files"]["audio"])
    assert fichier.status_code == 200
    assert fichier.headers["content-type"] == "audio/wav"

    # Le wav est jugé sur ce que la route a **servi**, pas sur ce que le worker a
    # écrit : entre les deux il y a un chemin composé par le serveur, et c'est
    # lui qu'on éprouve ici.
    with wave.open(io.BytesIO(fichier.content)) as son:
        durée = son.getnframes() / son.getframerate()
    assert durée > 0.5, "un wav d'une demi-seconde ne contient pas cette phrase"
