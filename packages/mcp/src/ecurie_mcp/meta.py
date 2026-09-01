"""Les trois méta-outils : découvrir, exécuter n'importe quoi, regarder la machine.

Ils sont toujours présents, quel que soit l'opt-in, et ils sont préfixés du nom
du serveur là où les douze ne le sont pas : un outil qui porte le nom d'une
capacité fait le travail, un outil qui porte le nom du serveur parle du serveur.

**`ecurie_catalog` rend une liste courte, et c'est une contrainte mesurée.** La
projection complète du registre — quarante et un contrats, soixante-douze
manifestes, cent variants, avec leurs schémas d'entrée — pèse 160 ko de JSON,
environ quarante mille jetons : plus du double du catalogue de quarante outils
qui saturait déjà le modèle mesuré en août. Rendre cela à un agent qui demande
« que sais-tu faire ? » remplirait son contexte avant sa première question. La
réponse par défaut tient donc en une ligne par capacité, et le détail — le schéma
d'entrée, les variants, les profils — se demande capacité par capacité.

**`ecurie_status` ne dit que ce qu'il sait.** Les résidents d'Ollama et de LM
Studio sont la tâche 2.6, au jalon J2 : ils ne sont pas lus. Le champ le dit en
toutes lettres plutôt que de rendre une liste vide, qui se lirait « rien d'autre
n'occupe la mémoire » — ce qui serait faux et exactement l'inverse de ce que
l'outil promet. De même, les trois chiffres du disque supposent un `store scan` :
sans lui, la réponse est « inconnu » et porte la commande qui la remplit.
"""

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import mcp_types as types
from ecurie_core.format import fmt_memory
from ecurie_runtime.readiness import inspect_variant
from ecurie_store.figures import compute_figures

from ecurie_mcp import catalogue, execution
from ecurie_mcp.choix import choisir_dans, variants_de
from ecurie_mcp.contexte import Contexte

if TYPE_CHECKING:  # pragma: no cover
    from mcp.server.context import ServerRequestContext

    from ecurie_mcp.serveur import Serveur


def declarer(contexte: Contexte, textes: dict[str, dict], annotations) -> list[types.Tool]:
    """Les trois outils, dans l'ordre où un agent les découvre."""
    return [
        types.Tool(
            name=catalogue.CATALOGUE_OUTIL,
            title=textes[catalogue.CATALOGUE_OUTIL]["title"],
            description=textes[catalogue.CATALOGUE_OUTIL]["description"],
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "capability": {
                        "type": "string",
                        "description": "Return the full contract for this one capability — "
                        "its input schema, its variants and their measured profiles. "
                        "Omit for the one-line-per-capability list.",
                    },
                    "ready_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "List only capabilities that can run right now.",
                    },
                },
            },
            annotations=annotations(lecture_seule=True),
        ),
        types.Tool(
            name=catalogue.RUN_OUTIL,
            title=textes[catalogue.RUN_OUTIL]["title"],
            description=textes[catalogue.RUN_OUTIL]["description"],
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["capability", "input"],
                "properties": {
                    "capability": {
                        "type": "string",
                        "description": "Contract id, as listed by ecurie_catalog "
                        "(e.g. 'document-to-text').",
                    },
                    "input": {
                        "type": "object",
                        "description": "Arguments matching that contract's input schema. "
                        "Call ecurie_catalog with the capability first to read it.",
                    },
                    "variant": {
                        "type": "string",
                        "description": "Force a specific variant, as model@variant. Écurie "
                        "picks one on its own; pass this only to take up a 'variant' option "
                        "from a refused admission.",
                    },
                },
            },
            annotations=annotations(),
        ),
        types.Tool(
            name=catalogue.STATUS_OUTIL,
            title=textes[catalogue.STATUS_OUTIL]["title"],
            description=textes[catalogue.STATUS_OUTIL]["description"],
            input_schema={"type": "object", "additionalProperties": False, "properties": {}},
            annotations=annotations(lecture_seule=True),
        ),
    ]


async def appeler(
    servi: "Serveur", ctx: "ServerRequestContext", nom: str, arguments: dict[str, Any]
) -> types.CallToolResult:
    if nom == catalogue.CATALOGUE_OUTIL:
        return _resultat(catalogue_reponse(servi, arguments))
    if nom == catalogue.STATUS_OUTIL:
        return _resultat(status_reponse(servi.contexte))
    return await _run(servi, ctx, arguments)


def _resultat(charge: dict[str, Any]) -> types.CallToolResult:
    """Emballe une réponse de méta-outil, `isError` compris.

    Le drapeau se déduit du payload plutôt que de l'appelant : une capacité
    inconnue demandée à `ecurie_catalog` est la même faute que demandée à
    `ecurie_run`, et la rendre « réussie » d'un côté et « en erreur » de l'autre
    apprendrait à l'agent que le succès dépend de la porte. La spec veut qu'une
    erreur d'exécution revienne dans le résultat pour qu'il puisse se corriger ;
    encore faut-il qu'elle soit marquée.
    """
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text", text=json.dumps(charge, ensure_ascii=False, default=str)
            )
        ],
        structured_content=charge,
        is_error="error" in charge,
    )


# --- ecurie_catalog -----------------------------------------------------------


def catalogue_reponse(servi: "Serveur", arguments: dict[str, Any]) -> dict[str, Any]:
    contexte = servi.contexte
    registry = contexte.registry()
    demandée = arguments.get("capability")
    if demandée:
        return _detail(contexte, registry, str(demandée))

    ouvertes = catalogue.capacites_ouvertes(registry, contexte.exposition.familles)
    ready_only = bool(arguments.get("ready_only"))
    lignes: list[dict[str, Any]] = []
    for identifiant in sorted(registry.capabilities):
        retenu = choisir_dans(contexte, identifiant)
        if ready_only and retenu is None:
            continue
        ligne: dict[str, Any] = {
            "capability": identifiant,
            "ready": retenu is not None,
            "models": sum(
                1 for m in registry.models.values() if m.capability == identifiant
            ),
        }
        outil = next((o for o in servi.exposes if o.capability == identifiant), None)
        if outil is not None and servi.retenus.get(identifiant) is not None:
            ligne["tool"] = outil.nom
        if identifiant not in ouvertes:
            ligne["excluded"] = "human_subject"
        lignes.append(ligne)

    return {
        "capabilities": lignes,
        "note": "Capabilities with a 'tool' key have a dedicated tool in this session. "
        "The rest run through ecurie_run. Call this tool with a capability id to get "
        "its input schema before running it.",
    }


def _detail(contexte: Contexte, registry, identifiant: str) -> dict[str, Any]:
    from ecurie_mcp import schemas

    contract = registry.capabilities.get(identifiant)
    if contract is None:
        return {
            "error": "unknown_capability",
            "capability": identifiant,
            "known": sorted(registry.capabilities),
        }

    # Le schéma rendu est celui du variant **qui servira**, pas celui du contrat
    # nu. Pour les vingt-neuf capacités sans outil dédié, c'est le seul schéma
    # qu'un agent verra jamais — la description d'`ecurie_run` lui dit d'ailleurs
    # de le lire ici avant d'appeler. Rendre les défauts du contrat quand le
    # variant en impose d'autres lui ferait composer une entrée qui n'est pas
    # celle qui s'exécutera, sans rien pour l'en avertir.
    retenu = choisir_dans(contexte, identifiant)

    variants: list[dict[str, Any]] = []
    for model, variant in variants_de(registry, identifiant):
        ref = f"{model.id}@{variant.id}"
        état = inspect_variant(contexte.root, contexte.config, model, variant, ref)
        entrée: dict[str, Any] = {
            "ref": ref,
            "ready": état.ready,
            "incumbent": bool(model.incumbent),
            "status": model.status,
        }
        if variant.profile is not None:
            entrée["peak_bytes"] = variant.profile.peak_unified_memory_bytes
            entrée["measured_on"] = variant.profile.measured_on
        if not état.ready:
            entrée["blockers"] = list(état.blockers)
        variants.append(entrée)

    détail: dict[str, Any] = {
        "capability": identifiant,
        "input_schema": schemas.input_schema(
            contract, retenu.variant if retenu else None
        ),
        "output_schema": schemas.sans_extensions(contract.output_schema),
        "human_subject": contract.human_subject,
        "variants": variants,
    }
    if retenu is not None:
        # Quel variant ce schéma décrit : sans lui, les défauts rendus n'ont pas
        # d'auteur, et deux machines répondraient différemment sans le dire.
        détail["serves"] = retenu.ref
    return détail


# --- ecurie_run ---------------------------------------------------------------


async def _run(
    servi: "Serveur", ctx: "ServerRequestContext", arguments: dict[str, Any]
) -> types.CallToolResult:
    """L'échappatoire : n'importe quelle capacité par son contrat.

    Le filtre `human_subject` s'applique **ici aussi**. Le §6.3 dit d'un même
    souffle que les `face-*` sont exclues « application du champ, pas une
    opinion du serveur » et que les capacités restantes sont « exécutables par
    ecurie_run » — les deux ensemble videraient le champ de son sens, puisque
    l'échappatoire rouvrirait ce que le catalogue ferme. Le champ gagne : il dit
    ce qu'une capacité fait d'une personne réelle, et cela ne dépend pas de la
    porte par laquelle on l'appelle. `--tools faces` ouvre les deux à la fois.

    **`variant` existe pour rendre exécutable l'option d'un refus.** Le payload
    d'admission propose « prends `sdxl-base@8bit-mps`, il tient » et les
    instructions du serveur disent à l'agent d'en choisir une ; il n'avait aucun
    levier pour le faire, et l'option la mieux chiffrée du payload était une
    impasse. Une option qu'on ne peut pas exécuter n'est pas une option — c'est
    la règle de la CLI, où chaque erreur porte la commande qui répare.
    """
    contexte = servi.contexte
    identifiant = str(arguments.get("capability") or "")
    entrée = arguments.get("input")
    if not isinstance(entrée, dict):
        return execution.resultat_refus(
            {"error": "invalid_input", "problems": ["input: must be an object"]}
        )

    registry = contexte.registry()
    if identifiant not in registry.capabilities:
        return execution.resultat_refus(
            {
                "error": "unknown_capability",
                "capability": identifiant,
                "known": sorted(registry.capabilities),
            }
        )

    ouvertes = catalogue.capacites_ouvertes(registry, contexte.exposition.familles)
    if identifiant not in ouvertes:
        contract = registry.capabilities[identifiant]
        # La commande nomme la famille **de cette capacité**, pas une famille au
        # hasard : `voice-clone` n'est pas un visage, et lui proposer
        # `--tools faces` aurait été une commande qui ne répare rien — dans le
        # seul champ du payload qu'un humain va exécuter tel quel.
        famille = catalogue.famille_de(identifiant) or "all"
        return execution.resultat_refus(
            {
                "error": "capability_excluded",
                "capability": identifiant,
                "human_subject": contract.human_subject,
                "reason": (
                    f"this capability {contract.human_subject} a real person, and is excluded "
                    "from this session. It is a human decision, not one you can take: relay "
                    "the command below to your human if the use is legitimate."
                ),
                "options": [
                    {
                        "kind": "human_command",
                        "command": f"ecurie mcp --tools {famille}",
                        "why": "restarts the server with that family enabled",
                    }
                ],
            }
        )

    demandé = arguments.get("variant")
    return await servi.executer_capacite(
        ctx, identifiant, entrée, variant_impose=str(demandé) if demandé else None
    )


# --- ecurie_status ------------------------------------------------------------


def status_reponse(contexte: Contexte) -> dict[str, Any]:
    """Résidents, budget, disque. En lecture seule, et sans rien charger."""
    supervisor = contexte.supervisor()
    budget = contexte.budget
    residents = supervisor.residents()

    occupé = sum(e.peak_bytes for e in residents)
    charge: dict[str, Any] = {
        "budget": {
            "bytes": budget.bytes,
            "human": fmt_memory(budget.bytes),
            "source": budget.source,
            "measured": budget.measured,
        },
        "residents": [
            {
                "variant": e.ref,
                "peak_bytes": e.peak_bytes,
                "human": fmt_memory(e.peak_bytes),
                "pinned": e.pinned,
                "busy": e.busy,
            }
            for e in residents
        ],
        "free_bytes": budget.bytes - occupé,
        "foreign_residents": (
            "not read yet: Ollama and LM Studio residency lands in a later milestone. "
            "Memory they hold is not counted in free_bytes, so a job admitted here can "
            "still meet a busy machine."
        ),
        "disk": _disque(contexte),
    }
    return charge


def _disque(contexte: Contexte) -> dict[str, Any]:
    """Les trois chiffres — ou l'aveu qu'on ne les a pas.

    `compute_figures` est une fonction pure sur ce que la base contient : elle ne
    lit pas le disque et ne hache rien. Sa réponse ne vaut donc que ce que vaut
    le dernier scan, et une base vide ne donne pas zéro gigaoctet — elle ne donne
    rien du tout. Rendre trois zéros ferait croire à un parc vide.
    """
    db = contexte.open_db()
    try:
        records = db.locations()
        if not records:
            return {
                "known": False,
                "reason": "no scan recorded yet",
                "command": "ecurie store scan",
            }
        figures = compute_figures(records, last_runs=db.last_run_by_variant(), telemetry=True)
    finally:
        db.close()

    récupérable = asdict(figures.recoverable)
    return {
        "known": True,
        "apparent_bytes": figures.apparent_bytes,
        "real_unique_bytes": figures.real_unique_bytes,
        "recoverable": récupérable,
        "human": {
            "apparent": fmt_memory(figures.apparent_bytes),
            "real_unique": fmt_memory(figures.real_unique_bytes),
        },
    }
