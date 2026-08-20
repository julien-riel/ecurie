"""Le registre réel du dépôt : les invariants qui doivent tenir à chaque ajout.

Ce fichier ne fige pas un inventaire — le parc est fait pour grandir, et un test
qui liste ses modèles casserait à chaque ajout sans rien apprendre. Il fige les
règles : zéro erreur, révisions épinglées sur ce qui est installé, un contrat par
capacité employée, un titulaire au plus par capacité.

Le seuil qui compte : `ecurie run` refuse de s'exécuter sur un registre en
erreur, et la CI du v0.6 en fera un motif de rejet de PR.
"""

from ecurie_core.registry import load_registry

FONDATEURS = {"qwen3-tts-1.7b", "hunyuan3d-2.1-shape-mlx", "trellis2"}


def test_le_registre_reel_ne_produit_aucune_erreur(repo_root):
    reg = load_registry(repo_root)
    assert reg.errors == []
    assert FONDATEURS <= set(reg.models), "un modèle du parc initial a disparu"


def test_un_titulaire_au_plus_par_capacite(repo_root):
    reg = load_registry(repo_root)
    assert reg.incumbent_for("text-to-speech").id == "qwen3-tts-1.7b"
    assert reg.incumbent_for("image-to-mesh").id == "hunyuan3d-2.1-shape-mlx"
    titulaires = [m.id for m in reg.models.values() if m.incumbent]
    assert len(titulaires) == len({m.capability for m in reg.models.values() if m.incumbent})


def test_tout_ce_qui_est_installe_pointe_une_revision_de_commit(repo_root):
    """`tier: hot` veut dire « les poids sont là » : on doit savoir lesquels.

    Une révision flottante rendrait caduc sans préavis le profil mesuré sur ces
    poids-là — et le contrôle d'admission décide à partir de ce profil.
    """
    reg = load_registry(repo_root)
    for model in reg.models.values():
        for variant in model.variants:
            if variant.tier == "absent" or variant.source.kind != "huggingface":
                continue
            révision = variant.source.revision
            assert révision and len(révision) == 40 and set(révision) != {"0"}, (
                f"{model.id}@{variant.id} : révision {révision!r}"
            )


def test_les_contrats_de_capacite_couvrent_les_modeles(repo_root):
    """Aucun modèle ne peut déclarer une capacité sans contrat : c'est ce contrat
    qui engendre son formulaire, valide son entrée et nomme ses sorties."""
    reg = load_registry(repo_root)
    for model in reg.models.values():
        contrat = reg.capabilities.get(model.capability)
        assert contrat is not None, f"{model.id} : capacité {model.capability} sans contrat"
        assert contrat.input_properties and contrat.output_media_types()


def test_un_profil_committe_a_sa_mesure(repo_root):
    """« profile est rempli par le banc d'essai, pas à la main » (ARCHITECTURE.md §3).

    Le fichier de measurements/ est l'autorité ; un bloc `profile:` sans lui est
    une estimation déguisée, et le contrôle d'admission s'y fierait.
    """
    reg = load_registry(repo_root)
    mesurés = [
        (m.id, v.id)
        for m in reg.models.values()
        for v in m.variants
        if v.profile is not None
    ]
    for model_id, variant_id in mesurés:
        mesure = repo_root / "registry" / "measurements" / f"{model_id}@{variant_id}.json"
        assert mesure.is_file(), f"{model_id}@{variant_id} : profil sans mesure"
    assert not any("sans mesure correspondante" in i.message for i in reg.warnings)
