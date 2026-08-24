"""Contrats de capacité — le pivot de l'architecture (ARCHITECTURE.md §4).

Une capacité déclare un schéma d'entrée et un schéma de sortie typés. Tous les
modèles d'une même capacité sont interchangeables derrière ce contrat, et trois
choses en découlent : l'UI est *engendrée* et non écrite, la confrontation A/B
est native, le chaînage devient vérifiable.

D'où l'exigence de ce module : le contrat n'est pas un document décoratif, c'est
un schéma exécutable. On le valide donc à trois niveaux au chargement du registre —
sa forme (contre `capability.schema.json`), sa validité en tant que JSON Schema
(un `pattern` mal écrit ne se verrait qu'au premier formulaire rendu), et sa
cohérence avec les manifestes qui s'y réfèrent.

Cette dernière vérification est celle qui rapporte le plus : un `defaults:` de
variant nommant un paramètre que le contrat ne déclare pas est silencieusement
ignoré à l'exécution. Le modèle tourne, produit un résultat plausible, et le
réglage qu'on croyait appliqué ne l'a jamais été.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

from ecurie_core.issues import Issue

CAPABILITIES_DIR = Path("registry/capabilities")
CAPABILITY_SCHEMA_PATH = Path("registry/schema/capability.schema.json")


@dataclass
class CapabilityContract:
    id: str
    document: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.document.get("title") or self.id

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.document.get("input") or {}

    @property
    def output_schema(self) -> dict[str, Any]:
        return self.document.get("output") or {}

    @property
    def input_properties(self) -> dict[str, Any]:
        return self.input_schema.get("properties") or {}

    @property
    def required(self) -> list[str]:
        return list(self.input_schema.get("required") or [])

    @property
    def human_subject(self) -> str | None:
        """Ce que la capacité fait d'une personne réelle, ou None.

        `license_class` dit ce que le droit interdit ; ce champ dit ce que la
        capacité **fait**, et les deux ne se recouvrent pas : EdgeFace est sous
        licence BSD-3 et sert pourtant à reconnaître quelqu'un qui n'a rien
        demandé. Porté par le contrat plutôt que par le variant, parce que c'est
        la capacité qui décide — tous ses modèles rendent la même chose.
        """
        return self.document.get("human_subject") or None

    @property
    def composite(self) -> bool:
        return bool(self.document.get("composite"))

    @property
    def steps(self) -> list[dict[str, Any]]:
        return list(self.document.get("steps") or [])

    def defaults(self) -> dict[str, Any]:
        """Valeurs par défaut déclarées par le contrat, tous champs confondus."""
        return {
            nom: champ["default"]
            for nom, champ in self.input_properties.items()
            if "default" in champ
        }

    def output_media_types(self) -> dict[str, str]:
        """Sorties qui sont des fichiers, et leur type de média.

        C'est ce qui choisit le visualiseur de l'UI (CONCEPTION.md §7) et ce qui
        rendra le chaînage typé vérifiable au v0.7 : une sortie `image/*` peut
        alimenter une étape qui consomme `image/*`.

        Le parcours descend dans les sorties imbriquées, avec des clés en chemin
        pointé (`tracks.vocals`) : une séparation audio rend un objet de pistes,
        et s'arrêter au premier niveau reviendrait à ne vérifier aucun des
        fichiers qu'elle produit.
        """
        trouvés: dict[str, str] = {}

        def visiter(propriétés: dict[str, Any] | None, préfixe: str) -> None:
            for nom, champ in (propriétés or {}).items():
                chemin = f"{préfixe}{nom}"
                if "contentMediaType" in champ:
                    trouvés[chemin] = champ["contentMediaType"]
                if champ.get("type") == "object":
                    visiter(champ.get("properties"), f"{chemin}.")

        visiter(self.output_schema.get("properties"), "")
        return trouvés

    def input_media_types(self) -> dict[str, str]:
        """Entrées qui sont des fichiers, et le type de média qu'elles acceptent.

        Le symétrique d'`output_media_types`, et la reconnaissance est celle que
        `runner.stage_inputs` emploie déjà pour décider quoi copier dans le
        dossier du job : un `contentMediaType`, ou un `x-ui: "file"`. Les deux,
        parce qu'un champ peut porter l'un sans l'autre — le premier dit ce que
        le worker sait ouvrir, le second ce que l'UI doit rendre.

        La valeur peut être une liste séparée par des virgules (`document-to-text`
        accepte « application/pdf,image/* ») : c'est la graphie de l'attribut
        `accept` d'un `<input type="file">`, et la garder telle quelle évite de
        traduire deux fois la même chose.
        """
        trouvés: dict[str, str] = {}

        def visiter(propriétés: dict[str, Any] | None, préfixe: str) -> None:
            for nom, champ in (propriétés or {}).items():
                chemin = f"{préfixe}{nom}"
                if "contentMediaType" in champ:
                    trouvés[chemin] = champ["contentMediaType"]
                elif champ.get("x-ui") == "file":
                    # Un champ fichier sans type déclaré n'annonce aucune
                    # restriction : « */* » est la réponse exacte, pas un défaut
                    # de repli.
                    trouvés[chemin] = "*/*"
                if champ.get("type") == "object":
                    visiter(champ.get("properties"), f"{chemin}.")

        visiter(self.input_properties, "")
        return trouvés

    def file_outputs(self) -> set[str]:
        return set(self.output_media_types())

    def validator(self) -> Draft202012Validator:
        return Draft202012Validator(self.input_schema)


def load_capabilities(root: Path) -> tuple[dict[str, CapabilityContract], list[Issue]]:
    """Charge et valide `registry/capabilities/`. Ne lève jamais : tout est en `Issue`."""
    contracts: dict[str, CapabilityContract] = {}
    issues: list[Issue] = []

    méta: Draft202012Validator | None = None
    méta_file = root / CAPABILITY_SCHEMA_PATH
    if méta_file.is_file():
        try:
            méta = Draft202012Validator(json.loads(méta_file.read_text()))
        except (json.JSONDecodeError, SchemaError) as exc:
            issues.append(
                Issue("error", str(CAPABILITY_SCHEMA_PATH), f"méta-schéma illisible : {exc}")
            )
    else:
        issues.append(
            Issue(
                "warning",
                str(CAPABILITY_SCHEMA_PATH),
                "méta-schéma des capacités absent — les contrats ne sont pas validés "
                "dans leur forme",
            )
        )

    directory = root / CAPABILITIES_DIR
    if not directory.is_dir():
        return contracts, issues

    for path in sorted(directory.glob("*.json")):
        rel = str(path.relative_to(root))
        try:
            document = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            issues.append(Issue("error", rel, f"JSON illisible : {exc}"))
            continue
        if not isinstance(document, dict):
            issues.append(Issue("error", rel, "contrat qui n'est pas un objet JSON"))
            continue

        if méta is not None:
            for err in sorted(méta.iter_errors(document), key=lambda e: list(e.absolute_path)):
                où = "/".join(str(p) for p in err.absolute_path) or "racine"
                issues.append(Issue("error", rel, f"méta-schéma violé à {où} : {err.message}"))

        contract = CapabilityContract(id=document.get("id") or path.stem, document=document)
        if contract.id != path.stem:
            issues.append(
                Issue("error", rel, f"id du contrat ({contract.id!r}) ≠ nom de fichier")
            )
        if contract.id in contracts:
            issues.append(Issue("error", rel, f"contrat en double pour {contract.id!r}"))
            continue

        # Un JSON Schema syntaxiquement correct peut être un schéma invalide. Le
        # découvrir ici plutôt qu'au premier formulaire rendu coûte une ligne.
        sous_schémas = (("input", contract.input_schema), ("output", contract.output_schema))
        for nom, sous_schéma in sous_schémas:
            try:
                Draft202012Validator.check_schema(sous_schéma)
            except SchemaError as exc:
                issues.append(
                    Issue("error", rel, f"{nom} n'est pas un JSON Schema valide : {exc.message}")
                )

        contracts[contract.id] = contract

    issues.extend(_check_composites(contracts))
    return contracts, issues


def _check_composites(contracts: dict[str, CapabilityContract]) -> list[Issue]:
    """Une étape de capacité composite doit désigner une capacité qui existe."""
    issues: list[Issue] = []
    for contract in contracts.values():
        if not contract.steps:
            continue
        rel = f"registry/capabilities/{contract.id}.json"
        for index, étape in enumerate(contract.steps):
            capacité = étape.get("capability")
            if capacité not in contracts:
                issues.append(
                    Issue("error", rel, f"étape {index} : capacité inconnue {capacité!r}")
                )
                continue
            if contracts[capacité].composite:
                issues.append(
                    Issue(
                        "error",
                        rel,
                        f"étape {index} : {capacité!r} est elle-même composite — "
                        "une seule composition est câblée (ARCHITECTURE.md §11)",
                    )
                )
    return issues


def check_variant_defaults(
    contract: CapabilityContract, defaults: dict[str, Any], ref: str, rel: str
) -> list[Issue]:
    """Croise les `defaults:` d'un variant avec le schéma d'entrée de sa capacité.

    Un défaut qui ne correspond à aucun paramètre du contrat n'est pas une
    coquille sans conséquence : il ne sera jamais transmis au worker, et le
    réglage qu'il prétend fixer restera à sa valeur d'origine sans que rien ne
    le signale.
    """
    issues: list[Issue] = []
    propriétés = contract.input_properties
    if not propriétés:
        return issues

    for nom, valeur in defaults.items():
        champ = propriétés.get(nom)
        if champ is None:
            connus = ", ".join(sorted(propriétés)) or "aucun"
            issues.append(
                Issue(
                    "error",
                    rel,
                    f"{ref} : defaults.{nom} n'existe pas dans le contrat "
                    f"{contract.id} (paramètres : {connus})",
                )
            )
            continue
        try:
            erreurs = sorted(
                Draft202012Validator(champ).iter_errors(valeur),
                key=lambda e: list(e.absolute_path),
            )
        except SchemaError:
            continue  # le champ lui-même est invalide : déjà signalé au chargement du contrat
        for err in erreurs:
            issues.append(
                Issue("error", rel, f"{ref} : defaults.{nom} = {valeur!r} — {err.message}")
            )
    return issues


def check_variant_options(
    contract: CapabilityContract, options: dict[str, Any], ref: str, rel: str
) -> list[Issue]:
    """Croise `options:` avec le contrat — en sens inverse de `defaults:`.

    `options:` est réservé aux réglages que le contrat ne connaît pas. Une clé
    qui porte le nom d'un paramètre de la capacité y est un piège silencieux :
    elle part au worker dans `params`, mais le paramètre du contrat, lui, part
    dans l'entrée typée avec sa valeur par défaut — et c'est cette dernière que
    l'adaptateur lit en premier. Le réglage n'atteint donc jamais le modèle, et
    rien ne le dit. Sa place est dans `defaults:`.
    """
    propriétés = contract.input_properties
    return [
        Issue(
            "error",
            rel,
            f"{ref} : options.{nom} porte le nom d'un paramètre de la capacité "
            f"{contract.id} — le déclarer dans defaults:, sinon il n'atteindra "
            "jamais le worker",
        )
        for nom in sorted(options)
        if nom in propriétés
    ]
