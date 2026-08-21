"""`/registry` — les contrats de capacité et les manifestes, tels que le disque les dit.

Deux lectures, et rien d'autre : le registre vit dans Git, l'API ne l'écrit
jamais. Un manifeste se corrige dans son YAML, se relit à la requête suivante, et
c'est la propriété qu'il faut préserver — un registre modifiable par HTTP
cesserait d'être versionné.

Un point mérite d'être dit ici plutôt que découvert : **le serveur répond même
quand le registre a des erreurs.** La CLI, elle, s'arrête (`_context` du runtime
sort en code 1), et c'est le bon comportement pour une commande qui va exécuter
quelque chose. Pour l'UI, refuser de servir quoi que ce soit parce qu'un
manifeste sur six a une révision non épinglée laisserait un écran vide sans
moyen d'apprendre lequel. Les modèles valides sont donc servis, et `issues`
transporte le reste.
"""

from fastapi import APIRouter, HTTPException, Query

from ecurie_api.deps import StateDep
from ecurie_api.projection import capability_out, issues_out, model_out
from ecurie_api.schemas import CapabilitiesResponse, ModelsResponse

router = APIRouter(prefix="/registry", tags=["registre"])


@router.get("/capabilities", response_model=CapabilitiesResponse, summary="Contrats de capacité")
def capabilities(state: StateDep) -> CapabilitiesResponse:
    """Les contrats, avec ce que le parc en fait aujourd'hui.

    C'est la source du formulaire de l'Atelier : `input` est rendu tel quel par
    RJSF, `output_media_types` choisit le visualiseur. `ready_variants` évite le
    piège d'un écran qui propose une capacité déclarée mais dont aucun variant
    n'est téléchargé.
    """
    registry = state.registry()
    par_capacité: dict[str, list] = {}
    for model in registry.models.values():
        projeté = model_out(state.root, state.config, model)
        par_capacité.setdefault(model.capability, []).append(projeté)

    return CapabilitiesResponse(
        capabilities=[
            capability_out(contract, registry, par_capacité.get(cap_id, []))
            for cap_id, contract in sorted(registry.capabilities.items())
        ],
        issues=issues_out(registry.issues),
    )


@router.get("/models", response_model=ModelsResponse, summary="Manifestes du parc")
def models(
    state: StateDep,
    capability: str | None = Query(
        default=None, description="Ne rendre que les modèles de cette capacité."
    ),
) -> ModelsResponse:
    registry = state.registry()

    if capability is not None and capability not in registry.capabilities:
        # Un filtre inconnu qui rend une liste vide se lit « aucun modèle pour
        # cette capacité », ce qui est faux et met sur une mauvaise piste.
        connues = ", ".join(sorted(registry.capabilities)) or "aucune"
        raise HTTPException(
            status_code=404,
            detail=f"capacité inconnue : {capability!r} — connues : {connues}",
        )

    retenus = [
        m
        for m in registry.models.values()
        if capability is None or m.capability == capability
    ]
    ordonnés = sorted(retenus, key=lambda m: m.id)
    return ModelsResponse(
        models=[model_out(state.root, state.config, m) for m in ordonnés],
        issues=issues_out(registry.issues),
    )
