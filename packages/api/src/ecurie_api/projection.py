"""Du registre chargé vers les réponses de l'API.

Une seule fonction construit un `VariantOut`, et les deux routes du registre s'en
servent : `/registry/models` pour le détail, `/registry/capabilities` pour savoir
lesquels sont exécutables. Deux réponses qui se contrediraient sur la
disponibilité d'un même variant seraient pires qu'une seule qui se trompe.
"""

from dataclasses import asdict
from pathlib import Path

from ecurie_core.capabilities import CapabilityContract
from ecurie_core.config import Config
from ecurie_core.issues import Issue
from ecurie_core.models import Model, Variant
from ecurie_core.registry import Registry

from ecurie_api.readiness import inspect_variant
from ecurie_api.schemas import (
    CapabilityOut,
    IssueOut,
    LinksOut,
    ModelOut,
    ProfileOut,
    SourceOut,
    VariantOut,
)


def issues_out(issues: list[Issue]) -> list[IssueOut]:
    return [IssueOut(severity=i.severity, file=i.file, message=i.message) for i in issues]


def variant_out(root: Path, config: Config, model: Model, variant: Variant) -> VariantOut:
    ref = f"{model.id}@{variant.id}"
    état = inspect_variant(root, config, model, variant, ref)
    profil = variant.profile
    return VariantOut(
        ref=ref,
        id=variant.id,
        quantization=variant.quantization,
        tier=variant.tier,
        runtime=variant.runtime,
        env=variant.env_name,
        entrypoint=variant.entrypoint,
        source=SourceOut(**variant.source.model_dump()),
        defaults=variant.defaults,
        options=variant.options,
        caveats=variant.caveats,
        profile=ProfileOut(**profil.model_dump()) if profil else None,
        ready=état.ready,
        blockers=état.blockers,
        weights_path=état.weights_path,
        heavy=(
            None
            if profil is None
            else profil.peak_unified_memory_bytes > config.heavy_threshold_bytes
        ),
    )


def model_out(root: Path, config: Config, model: Model) -> ModelOut:
    return ModelOut(
        id=model.id,
        name=model.name,
        capability=model.capability,
        family=model.family,
        vendor=model.vendor,
        license=model.license,
        license_class=model.license_class,
        status=model.status,
        incumbent=model.incumbent,
        notes=model.notes,
        links=LinksOut(**model.links.model_dump()) if model.links else None,
        variants=[variant_out(root, config, model, v) for v in model.variants],
    )


def capability_out(
    contract: CapabilityContract, registry: Registry, projections: list[ModelOut]
) -> CapabilityOut:
    """Le contrat, plus ce que le parc en fait aujourd'hui.

    `projections` sont les modèles de cette capacité, déjà projetés : les
    reconstruire ici referait le travail d'inspection disque pour chaque variant.
    """
    titulaire = registry.incumbent_for(contract.id)
    return CapabilityOut(
        id=contract.id,
        title=contract.title,
        description=contract.document.get("description"),
        composite=contract.composite,
        steps=contract.steps,
        input=contract.input_schema,
        output=contract.output_schema,
        output_media_types=contract.output_media_types(),
        required=contract.required,
        defaults=contract.defaults(),
        models=[m.id for m in projections],
        ready_variants=[v.ref for m in projections for v in m.variants if v.ready],
        incumbent=titulaire.id if titulaire else None,
    )


def admission_fields(admission) -> dict:
    """Les champs communs d'une décision d'admission, quel que soit l'appelant."""
    champs = asdict(admission)
    champs["evict"] = list(admission.evict)
    champs["blockers"] = list(admission.blockers)
    return champs
