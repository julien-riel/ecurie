"""Écurie — état observé du disque : scan, comptabilité, dédup, GC, tiering."""

__version__ = "0.3.0"

from ecurie_store.apply import ApplyReport, apply_plan
from ecurie_store.db import LocationRecord, StateDB
from ecurie_store.figures import Figures, compute_figures
from ecurie_store.hashing import sha256_file, verify_location
from ecurie_store.plan import generate_plan, load_plan, write_plan
from ecurie_store.pull import (
    PullError,
    PullPlan,
    PullResult,
    plan_pull,
    resolve_revision,
    run_pull,
)
from ecurie_store.tier import tier_variant
from ecurie_store.trash import empty_trash, list_trash, move_to_trash
from ecurie_store.weights import WeightsMissing, resolve_weights, variant_disk_bytes

__all__ = [
    "ApplyReport",
    "Figures",
    "LocationRecord",
    "PullError",
    "PullPlan",
    "PullResult",
    "StateDB",
    "WeightsMissing",
    "apply_plan",
    "compute_figures",
    "empty_trash",
    "generate_plan",
    "list_trash",
    "load_plan",
    "move_to_trash",
    "plan_pull",
    "resolve_revision",
    "resolve_weights",
    "run_pull",
    "sha256_file",
    "tier_variant",
    "variant_disk_bytes",
    "verify_location",
    "write_plan",
]
