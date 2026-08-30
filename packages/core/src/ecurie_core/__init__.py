"""Écurie — noyau : modèles de données, contrats de capacité, registre, CLI."""

__version__ = "0.4.0"

from ecurie_core.capabilities import CapabilityContract, load_capabilities
from ecurie_core.issues import Issue
from ecurie_core.models import Model, Profile, Source, Variant
from ecurie_core.registry import Registry, load_registry

__all__ = [
    "CapabilityContract",
    "Issue",
    "Model",
    "Profile",
    "Registry",
    "Source",
    "Variant",
    "load_capabilities",
    "load_registry",
]
