"""Le registre réel du dépôt : état connu et attendu au v0.1.

Les révisions placeholder des deux modèles actifs sont un défaut assumé,
à résorber à la tâche 3.5 (épinglage des vraies révisions). Ce test fige
cet état : si un manifeste régresse autrement, il casse.
"""

from ecurie_core.registry import load_registry


def test_registre_reel(repo_root):
    reg = load_registry(repo_root)

    assert set(reg.models) == {"qwen3-tts-1.7b", "hunyuan3d-2.1-shape-mlx", "trellis2"}
    assert {"text-to-speech", "image-to-mesh"} <= set(reg.capabilities)

    assert reg.incumbent_for("text-to-speech").id == "qwen3-tts-1.7b"
    assert reg.incumbent_for("image-to-mesh").id == "hunyuan3d-2.1-shape-mlx"

    # Seules erreurs attendues : les révisions placeholder des deux modèles actifs.
    pinning_errors = {i.file for i in reg.errors}
    assert pinning_errors == {
        "registry/models/qwen3-tts-1.7b.yaml",
        "registry/models/hunyuan3d-2.1-shape-mlx.yaml",
    }
    assert all("épinglée" in i.message for i in reg.errors)

    # trellis2 (candidate) ne produit qu'un avertissement pour son placeholder.
    trellis_issues = [i for i in reg.issues if i.file == "registry/models/trellis2.yaml"]
    assert trellis_issues and all(i.severity == "warning" for i in trellis_issues)
