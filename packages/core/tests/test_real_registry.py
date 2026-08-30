"""Le registre réel du dépôt : les invariants qui doivent tenir à chaque ajout.

Ce fichier ne fige pas un inventaire — le parc est fait pour grandir, et un test
qui liste ses modèles casserait à chaque ajout sans rien apprendre. Il fige les
règles : zéro erreur, révisions épinglées sur ce qui est installé, un contrat par
capacité employée, un titulaire au plus par capacité.

Le seuil qui compte : `ecurie run` refuse de s'exécuter sur un registre en
erreur, et la CI du v0.6 en fera un motif de rejet de PR.
"""

from ecurie_core.registry import load_registry, measurement_records

FONDATEURS = {"qwen3-tts-1.7b", "hunyuan3d-2.1-shape-mlx", "trellis2"}


def test_le_registre_reel_ne_produit_aucune_erreur(repo_root):
    reg = load_registry(repo_root)
    assert reg.errors == []
    assert FONDATEURS <= set(reg.models), "un modèle du parc initial a disparu"


def test_un_titulaire_au_plus_par_capacite(repo_root):
    """« Au plus » est un plafond ; son plancher est zéro, et zéro est un état légal.

    `_check_incumbents` (registry.py) ne lève une erreur que pour *plusieurs*
    titulaires sur une même capacité, jamais pour aucun, et la projection de l'API
    lit déjà le résultat comme nullable (`titulaire.id if titulaire else None`,
    projection.py:105). Ce test était le seul endroit du dépôt à exiger un
    titulaire : il assertait `image-to-mesh` → `hunyuan3d-2.1-shape-mlx` jusqu'à ce
    que la décision de pivot n°8 du 2026-08-29 coupe la filière 3D et retire ce
    statut du manifeste. C'était un inventaire déguisé en règle — ce que l'en-tête
    de ce fichier refuse — et il a cassé au premier changement de parc.

    Restent les deux règles, qui elles ne dépendent d'aucun inventaire : le plafond
    d'un titulaire par capacité, et `incumbent_for` qui rend le manifeste marqué ou
    rien — jamais un modèle pris au hasard parmi ceux de la capacité. La nuance
    compte ici : `image-to-mesh` a deux manifestes (hunyuan3d, trellis2) et aucun
    titulaire, donc la fonction doit dire None là où elle a le choix.
    """
    reg = load_registry(repo_root)
    titulaires = [m for m in reg.models.values() if m.incumbent]
    par_capacité = {m.capability: m.id for m in titulaires}
    assert len(par_capacité) == len(titulaires), (
        "deux manifestes titulaires sur une même capacité : "
        + ", ".join(sorted(m.id for m in titulaires))
    )
    for capacité in sorted(set(reg.capabilities) | set(par_capacité)):
        trouvé = reg.incumbent_for(capacité)
        assert (trouvé.id if trouvé else None) == par_capacité.get(capacité), (
            f"{capacité} : incumbent_for ne suit pas le champ incumbent des manifestes"
        )
    # L'unique titulaire du parc, et donc le seul point d'appui d'un A/B : les
    # quarante autres capacités n'ont aucune référence désignée (PLAN.md 5.6).
    assert reg.incumbent_for("text-to-speech").id == "qwen3-tts-1.7b"


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


def test_chaque_capacite_a_au_moins_un_modele(repo_root):
    """La réciproque du test suivant, et elle n'allait pas de soi.

    Un contrat sans modèle est parfaitement valide — il dit ce que le parc
    pourrait faire —, et l'Atelier lui réservait même un groupe. Ce qu'il coûte
    se voit à l'usage : sur vingt-cinq capacités, six proposaient un formulaire
    dont aucun bouton *Lancer* ne pouvait partir, et rien à l'écran ne disait
    quel modèle irait là. Le registre est aussi une liste de courses ; une case
    vide n'en est pas une.

    Ce test ne demande pas que la capacité soit **exécutable** : télécharger
    quinze gigaoctets de poids vidéo n'est pas une condition pour décrire le
    modèle qui les porte. Il demande qu'un manifeste existe, avec sa source
    épinglée, sa licence et ses caveats — de quoi savoir ce qu'un `ecurie pull`
    apporterait.
    """
    reg = load_registry(repo_root)
    pourvues = {m.capability for m in reg.models.values()}
    orphelines = sorted(set(reg.capabilities) - pourvues)
    assert orphelines == [], (
        "capacité(s) sans aucun modèle au registre : "
        + ", ".join(orphelines)
        + " — ajouter un manifeste dans registry/models/, même en status: candidate"
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

    Les fichiers de measurements/ sont l'autorité — un par machine ayant mesuré ;
    un bloc `profile:` sans aucun d'eux est une estimation déguisée, et le
    contrôle d'admission s'y fierait.
    """
    reg = load_registry(repo_root)
    mesurés = [
        (m.id, v.id)
        for m in reg.models.values()
        for v in m.variants
        if v.profile is not None
    ]
    for model_id, variant_id in mesurés:
        relevés, _ = measurement_records(repo_root, f"{model_id}@{variant_id}")
        assert relevés, f"{model_id}@{variant_id} : profil sans mesure"
    assert not any("sans mesure correspondante" in i.message for i in reg.warnings)
