"""Chargement et validation du registre déclaré (registry/).

Deux étages :
1. Validation de chaque manifeste contre le JSON Schema — l'autorité.
2. Invariants inter-fichiers, que le schéma ne peut pas exprimer : unicité de
   l'incumbent par capacité, existence des contrats de capacité, révisions
   épinglées, environnements d'exécution.

Aucune exception pour un manifeste invalide : tout est collecté en `Issue`,
c'est l'appelant (CLI, CI, API) qui décide quoi en faire.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from ecurie_core.capabilities import (
    CAPABILITIES_DIR,
    CapabilityContract,
    check_variant_defaults,
    check_variant_options,
    load_capabilities,
)
from ecurie_core.issues import Issue, Severity
from ecurie_core.models import Model

__all__ = [
    "CAPABILITIES_DIR",
    "CapabilityContract",
    "Issue",
    "Registry",
    "Severity",
    "find_root",
    "load_registry",
]

SCHEMA_PATH = Path("registry/schema/model.schema.json")
MODELS_DIR = Path("registry/models")
MEASUREMENTS_DIR = Path("registry/measurements")
RUNTIMES_DIR = Path("runtimes")


@dataclass
class Registry:
    root: Path
    models: dict[str, Model] = field(default_factory=dict)
    capabilities: dict[str, CapabilityContract] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    def incumbent_for(self, capability: str) -> Model | None:
        for m in self.models.values():
            if m.capability == capability and m.incumbent:
                return m
        return None


def find_root(start: Path) -> Path | None:
    """Remonte depuis `start` jusqu'au dossier contenant le schéma du registre."""
    start = start.resolve()
    for candidate in [start, *start.parents]:
        if (candidate / SCHEMA_PATH).is_file():
            return candidate
    return None


def _is_placeholder(revision: str | None) -> bool:
    """Révision non épinglée : placeholder tout-zéros ou référence flottante."""
    if revision is None:
        return False
    return revision == "main" or set(revision) == {"0"}


def load_registry(root: Path) -> Registry:
    """Charge registry/ sous `root` et applique les invariants."""
    root = root.resolve()
    reg = Registry(root=root)
    issues = reg.issues

    schema_file = root / SCHEMA_PATH
    if not schema_file.is_file():
        issues.append(Issue("error", str(SCHEMA_PATH), "schéma de manifeste introuvable"))
        return reg
    validator = Draft202012Validator(json.loads(schema_file.read_text()))

    contracts, cap_issues = load_capabilities(root)
    reg.capabilities.update(contracts)
    issues.extend(cap_issues)

    models_dir = root / MODELS_DIR
    for path in sorted(models_dir.glob("*.yaml")):
        rel = str(path.relative_to(root))
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            issues.append(Issue("error", rel, f"YAML illisible : {exc}"))
            continue

        schema_errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
        for err in schema_errors:
            where = "/".join(str(p) for p in err.absolute_path) or "racine"
            issues.append(Issue("error", rel, f"schéma violé à {where} : {err.message}"))
        if schema_errors:
            continue

        try:
            model = Model.model_validate(doc)
        except ValidationError as exc:
            # Le schéma a accepté mais pas pydantic : divergence de miroir, à corriger.
            issues.append(Issue("error", rel, f"divergence pydantic/schéma : {exc}"))
            continue

        if path.stem != model.id:
            issues.append(Issue("warning", rel, f"nom de fichier ≠ id du modèle ({model.id!r})"))
        if model.id in reg.models:
            issues.append(Issue("error", rel, f"id dupliqué : {model.id!r}"))
            continue
        reg.models[model.id] = model
        _check_model(reg, model, rel)

    _check_incumbents(reg)
    return reg


def _check_model(reg: Registry, model: Model, rel: str) -> None:
    issues = reg.issues

    contract = reg.capabilities.get(model.capability)
    if contract is None:
        issues.append(
            Issue(
                "error",
                rel,
                f"aucun contrat registry/capabilities/{model.capability}.json pour cette capacité",
            )
        )
    elif contract.composite:
        issues.append(
            Issue(
                "error",
                rel,
                f"{model.capability} est une capacité composite : elle s'exécute en enchaînant "
                "d'autres capacités, aucun modèle ne la remplit directement",
            )
        )

    if model.incumbent and model.status != "active":
        issues.append(
            Issue("warning", rel, f"incumbent avec status {model.status!r} — attendu active")
        )

    seen_variants: set[str] = set()
    for v in model.variants:
        ref = f"{model.id}@{v.id}"
        if v.id in seen_variants:
            issues.append(Issue("error", rel, f"variant dupliqué : {v.id!r}"))
        seen_variants.add(v.id)

        if _is_placeholder(v.source.revision):
            severity: Severity = "error" if model.status == "active" else "warning"
            issues.append(
                Issue(
                    severity,
                    rel,
                    f"{ref} : révision non épinglée ({v.source.revision!r}) — "
                    "un profil mesuré sur une révision flottante est caduc sans préavis",
                )
            )
        elif v.source.kind == "huggingface" and v.source.revision is None:
            issues.append(Issue("error", rel, f"{ref} : source huggingface sans révision"))
        if v.source.kind == "huggingface" and not v.source.repo:
            issues.append(Issue("error", rel, f"{ref} : source huggingface sans repo"))

        if v.runtime == "custom" and not v.entrypoint:
            severity = "error" if model.status == "active" else "warning"
            issues.append(Issue(severity, rel, f"{ref} : runtime custom sans entrypoint"))

        if contract is not None and v.defaults:
            issues.extend(check_variant_defaults(contract, v.defaults, ref, rel))
        if contract is not None and v.options:
            issues.extend(check_variant_options(contract, v.options, ref, rel))

        if v.profile is not None:
            issues.extend(_check_profile(reg, model, v, ref, rel))

        if v.tier != "absent":
            env_dir = reg.root / RUNTIMES_DIR / v.env_name
            if not (env_dir / "pyproject.toml").is_file():
                issues.append(
                    Issue(
                        "warning",
                        rel,
                        f"{ref} : environnement runtimes/{v.env_name}/ absent (attendu au v0.3)",
                    )
                )


def _check_profile(reg: Registry, model: Model, variant, ref: str, rel: str) -> list[Issue]:
    """Un bloc `profile:` doit être la copie d'une mesure, jamais une estimation.

    C'est la règle non négociable du §3 de l'architecture : le contrôle
    d'admission mémoire décide de charger ou de décharger sur la foi de ce
    chiffre. Un profil saisi à la main est un profil faux, et il se paie en swap.
    Le fichier de `measurements/` est l'autorité ; le manifeste en est la copie
    committée par un humain, et cette copie peut diverger — on le dit.
    """
    issues: list[Issue] = []
    fichier = reg.root / MEASUREMENTS_DIR / f"{ref}.json"
    if not fichier.is_file():
        issues.append(
            Issue(
                "warning",
                rel,
                f"{ref} : bloc profile: sans mesure correspondante "
                f"({MEASUREMENTS_DIR}/{ref}.json) — le mesurer avec ecurie bench {ref}",
            )
        )
        return issues

    try:
        mesure = json.loads(fichier.read_text())
    except json.JSONDecodeError as exc:
        issues.append(
            Issue("error", str(MEASUREMENTS_DIR / f"{ref}.json"), f"JSON illisible : {exc}")
        )
        return issues

    mesuré = mesure.get("profile") or {}
    for champ in ("peak_unified_memory_bytes", "disk_bytes"):
        attendu, déclaré = mesuré.get(champ), getattr(variant.profile, champ)
        if attendu is not None and déclaré != attendu:
            issues.append(
                Issue(
                    "warning",
                    rel,
                    f"{ref} : profile.{champ} = {déclaré} alors que la mesure dit {attendu} — "
                    "le manifeste a divergé de measurements/",
                )
            )
    if variant.profile.harness_version and mesure.get("harness_version"):
        if variant.profile.harness_version != mesure["harness_version"]:
            issues.append(
                Issue(
                    "warning",
                    rel,
                    f"{ref} : profil mesuré par le banc {mesure['harness_version']}, "
                    f"manifeste annonçant {variant.profile.harness_version}",
                )
            )
    return issues


def _check_incumbents(reg: Registry) -> None:
    by_capability: dict[str, list[Model]] = {}
    for m in reg.models.values():
        if m.incumbent:
            by_capability.setdefault(m.capability, []).append(m)
    for capability, holders in sorted(by_capability.items()):
        if len(holders) > 1:
            ids = ", ".join(sorted(m.id for m in holders))
            reg.issues.append(
                Issue(
                    "error",
                    f"registry/models/ ({capability})",
                    f"plusieurs incumbents pour {capability} : {ids}",
                )
            )
