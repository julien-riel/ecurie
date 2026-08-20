"""Écurie — état observé du disque : scan, comptabilité, dédup, GC, tiering."""

__version__ = "0.2.0"

from ecurie_store.apply import ApplyReport, apply_plan
from ecurie_store.db import LocationRecord, StateDB
from ecurie_store.figures import Figures, compute_figures
from ecurie_store.hashing import sha256_file, verify_location
from ecurie_store.plan import generate_plan, load_plan, write_plan
from ecurie_store.tier import tier_variant
from ecurie_store.trash import empty_trash, list_trash, move_to_trash

__all__ = [
    "ApplyReport",
    "Figures",
    "LocationRecord",
    "StateDB",
    "apply_plan",
    "compute_figures",
    "empty_trash",
    "generate_plan",
    "list_trash",
    "load_plan",
    "move_to_trash",
    "sha256_file",
    "tier_variant",
    "verify_location",
    "write_plan",
]
