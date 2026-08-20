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
from typing import Any, Literal

import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from ecurie_core.models import Model

Severity = Literal["error", "warning"]

SCHEMA_PATH = Path("registry/schema/model.schema.json")
MODELS_DIR = Path("registry/models")
CAPABILITIES_DIR = Path("registry/capabilities")
RUNTIMES_DIR = Path("runtimes")


@dataclass(frozen=True)
class Issue:
    severity: Severity
    file: str
    message: str


@dataclass
class Registry:
    root: Path
    models: dict[str, Model] = field(default_factory=dict)
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
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

    capabilities_dir = root / CAPABILITIES_DIR
    if capabilities_dir.is_dir():
        for cap_file in sorted(capabilities_dir.glob("*.json")):
            reg.capabilities[cap_file.stem] = json.loads(cap_file.read_text())

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

    if model.capability not in reg.capabilities:
        issues.append(
            Issue(
                "error",
                rel,
                f"aucun contrat registry/capabilities/{model.capability}.json pour cette capacité",
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
