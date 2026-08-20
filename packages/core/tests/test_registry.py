"""Invariants du registre sur des fixtures synthétiques."""

from conftest import make_manifest
from ecurie_core.registry import load_registry


def test_manifeste_valide_sans_probleme(registry_builder):
    registry_builder.capability("text-to-speech").model(make_manifest())
    reg = load_registry(registry_builder.root)
    assert list(reg.models) == ["test-tts"]
    assert reg.issues == []


def test_schema_viole_champ_requis_manquant(registry_builder):
    doc = make_manifest()
    del doc["license"]
    registry_builder.capability("text-to-speech").model(doc)
    reg = load_registry(registry_builder.root)
    assert reg.models == {}
    assert any("license" in i.message for i in reg.errors)


def test_placeholder_sur_actif_est_une_erreur(registry_builder):
    registry_builder.capability("text-to-speech").model(make_manifest(revision="0000000"))
    reg = load_registry(registry_builder.root)
    assert [i.severity for i in reg.issues if "épinglée" in i.message] == ["error"]


def test_placeholder_sur_candidat_est_un_avertissement(registry_builder):
    registry_builder.capability("text-to-speech").model(
        make_manifest(status="candidate", revision="0000000")
    )
    reg = load_registry(registry_builder.root)
    assert [i.severity for i in reg.issues if "épinglée" in i.message] == ["warning"]


def test_deux_incumbents_meme_capacite(registry_builder):
    registry_builder.capability("text-to-speech")
    registry_builder.model(make_manifest("tts-a", incumbent=True))
    registry_builder.model(make_manifest("tts-b", incumbent=True))
    reg = load_registry(registry_builder.root)
    assert any("plusieurs incumbents" in i.message for i in reg.errors)


def test_un_incumbent_par_capacite_est_accepte(registry_builder):
    registry_builder.capability("text-to-speech").capability("image-to-mesh")
    registry_builder.model(make_manifest("tts-a", incumbent=True))
    registry_builder.model(make_manifest("mesh-a", capability="image-to-mesh", incumbent=True))
    reg = load_registry(registry_builder.root)
    assert not any("incumbents" in i.message for i in reg.errors)


def test_contrat_de_capacite_manquant(registry_builder):
    registry_builder.model(make_manifest())  # pas de .capability()
    reg = load_registry(registry_builder.root)
    assert any("aucun contrat" in i.message for i in reg.errors)


def test_runtime_env_absent_pour_variant_present(registry_builder):
    registry_builder.capability("text-to-speech").model(make_manifest(tier="hot"))
    reg = load_registry(registry_builder.root)
    assert any(i.severity == "warning" and "runtimes/mlx-audio" in i.message for i in reg.issues)


def test_runtime_env_present_pas_d_avertissement(registry_builder):
    registry_builder.capability("text-to-speech").runtime_env("mlx-audio")
    registry_builder.model(make_manifest(tier="hot"))
    reg = load_registry(registry_builder.root)
    assert not any("runtimes/" in i.message for i in reg.issues)


def test_custom_sans_entrypoint(registry_builder):
    doc = make_manifest()
    doc["variants"][0]["runtime"] = "custom"
    registry_builder.capability("text-to-speech").model(doc)
    reg = load_registry(registry_builder.root)
    assert any("custom sans entrypoint" in i.message for i in reg.errors)


def test_custom_sans_entrypoint_sur_candidat_est_un_avertissement(registry_builder):
    doc = make_manifest(status="candidate", revision="abc1234")
    doc["variants"][0]["runtime"] = "custom"
    registry_builder.capability("text-to-speech").model(doc)
    reg = load_registry(registry_builder.root)
    assert not reg.errors
    assert any("custom sans entrypoint" in i.message for i in reg.warnings)


def test_nom_de_fichier_different_de_l_id(registry_builder):
    registry_builder.capability("text-to-speech").model(make_manifest(), filename="autre.yaml")
    reg = load_registry(registry_builder.root)
    assert any(i.severity == "warning" and "nom de fichier" in i.message for i in reg.issues)


def test_id_duplique(registry_builder):
    registry_builder.capability("text-to-speech")
    registry_builder.model(make_manifest(), filename="a.yaml")
    registry_builder.model(make_manifest(), filename="b.yaml")
    reg = load_registry(registry_builder.root)
    assert any("id dupliqué" in i.message for i in reg.errors)


def test_revision_main_refusee_par_le_schema(registry_builder):
    registry_builder.capability("text-to-speech").model(make_manifest(revision="main"))
    reg = load_registry(registry_builder.root)
    # 'main' viole déjà le motif du schéma : le manifeste entier est rejeté.
    assert reg.models == {}
    assert reg.errors
