"""Fabrique de manifestes pour les tests."""


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
