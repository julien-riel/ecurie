"""Du contrat de capacité à l'`inputSchema` d'un outil MCP.

La conversion est mécanique, et c'est tout l'intérêt : le bloc `input` d'un
contrat est déjà du JSON Schema 2020-12, un `inputSchema` MCP se déclare dans le
même dialecte (spec 2026-07-28 : « When a schema does not include a `$schema`
field, it defaults to JSON Schema 2020-12 »). C'est le pari qui engendre les
formulaires RJSF, appliqué une troisième fois — et la troisième fois qu'il tient.

Il reste trois choses à faire, et aucune n'est un détail.

**Retirer les `x-*`.** Le §6.3 dit « les extensions `x-ui` sont ignorées », ce
qui se lit de deux façons : ne pas les interpréter, ou ne pas les émettre. C'est
la seconde. Un client qui valide en AJV strict — le défaut d'AJV — refuse un
mot-clé inconnu ; ce dépôt ne s'en sort côté UI qu'en désactivant explicitement
ce mode (`apps/ui/src/form/validator.ts`). Émettre `x-ui` vers un client dont on
ne connaît pas le validateur serait parier sur sa mansuétude. Le retrait est
récursif : `properties`, `items`, et les sous-objets.

**Injecter les défauts du variant.** `merge_defaults` fait diverger le défaut
réel de celui du contrat sur plusieurs variants du parc : un schéma qui annonce
`speed: 1.0` là où le variant retenu impose `0.8` ne ment pas beaucoup, mais il
ment. Le schéma exposé porte donc les défauts **du variant qui servira**.

**Dire ce qu'un `x-options-from` ne peut pas dire.** Trois des douze contrats
portent un champ dont les valeurs ne sont connues qu'après chargement du modèle
— les voix d'un moteur TTS, les langues d'un ASR. On ne peut pas en faire un
`enum` sans charger le modèle, et un schéma sans contrainte invite le modèle à
inventer : il écrira `"alloy"`, et le job paiera le chargement complet avant de
mourir sur « voix inconnue ». La description du champ dit donc explicitement que
la valeur vient du modèle, que l'omission donne le défaut, et — quand le variant
en déclare un — lequel. C'est la seule barrière qu'un catalogue statique puisse
poser, et elle coûte une phrase.

Le SDK, lui, **ne valide rien** : `on_call_tool` reçoit les arguments tels quels,
`{"n": -5}` comme `{"inconnu": "zz"}` comme `{}`. La validation contre le schéma
est donc à faire ici, et c'est `jsonschema` qui la fait — le même validateur qui
charge le registre.
"""

from copy import deepcopy
from typing import Any

from ecurie_core.capabilities import CapabilityContract
from ecurie_core.models import Variant
from jsonschema import Draft202012Validator

# Ce qui parle à un écran et non à un agent. Le préfixe suffit : le registre n'a
# jamais employé `x-` pour autre chose, et le méta-schéma des capacités le
# réserve aux extensions d'interface.
PREFIXE_EXTENSION = "x-"


def sans_extensions(schema: Any) -> Any:
    """Le même schéma, débarrassé de ses `x-*`, à toute profondeur.

    Copie plutôt que mutation : le contrat chargé est partagé avec la validation
    du registre et avec l'API, qui, elle, **doit** garder `x-ui` — c'est ce qui
    choisit le widget de l'Atelier.
    """
    if isinstance(schema, dict):
        return {
            clé: sans_extensions(valeur)
            for clé, valeur in schema.items()
            if not clé.startswith(PREFIXE_EXTENSION)
        }
    if isinstance(schema, list):
        return [sans_extensions(élément) for élément in schema]
    return schema


def _note_des_options(champ: dict[str, Any], défaut: Any) -> str:
    """La phrase qui remplace l'`enum` qu'on ne peut pas écrire."""
    source = str(champ.get("x-options-from") or "")
    quoi = source.split(".")[-1] or "values"
    if défaut is not None:
        return (
            f" Accepted {quoi} are announced by the model when it loads and are not "
            f"listed here; omit this field to use {défaut!r}."
        )
    return (
        f" Accepted {quoi} are announced by the model when it loads and are not "
        "listed here; omit this field unless you already know a valid value — "
        "an invented one fails only after the model has finished loading."
    )


def input_schema(
    contract: CapabilityContract,
    variant: Variant | None = None,
    *,
    descriptions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """L'`inputSchema` exposé pour cette capacité, servie par ce variant.

    `descriptions` remplace le texte des champs par sa version anglaise
    (tâche 1.5) : les contrats parlent français à l'Atelier, l'outil parle
    anglais à l'agent, et les deux lisent le même schéma.
    """
    schéma = sans_extensions(deepcopy(contract.input_schema))
    if not schéma:
        # Un contrat sans bloc `input` reste un objet vide et non `{}` : la spec
        # exige `type: "object"` à la racine d'un inputSchema.
        return {"type": "object", "properties": {}}
    schéma.setdefault("type", "object")
    schéma.setdefault("properties", {})

    propriétés: dict[str, Any] = schéma["properties"]
    défauts = dict(variant.defaults or {}) if variant is not None else {}

    for nom, champ in propriétés.items():
        original = contract.input_properties.get(nom) or {}
        if nom in défauts:
            champ["default"] = défauts[nom]
        if descriptions and nom in descriptions:
            champ["description"] = descriptions[nom]
        if original.get("x-options-from"):
            champ["description"] = (champ.get("description") or "").rstrip() + _note_des_options(
                original, champ.get("default")
            )
    return schéma


def valider(schéma: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    """Ce que le schéma reproche à ces arguments. Liste vide = rien à redire.

    Le SDK MCP ne valide pas les arguments d'un outil — vérifié sur 2.1.1, dans
    les deux modes de connexion : `{"n": -5}` sur un schéma qui exige `minimum: 1`
    atteint le handler. Sans cette fonction, une entrée hors domaine partirait au
    worker, chargerait le modèle, et échouerait deux minutes plus tard sur un
    message écrit pour un humain.

    Les reproches sont rendus **tous ensemble** et non le premier : un agent qui
    corrige un champ à la fois consomme un aller-retour par erreur, et c'est le
    même raisonnement que `inspect_variant`, qui rend toutes les causes.
    """
    validateur = Draft202012Validator(schéma)
    reproches: list[str] = []
    for erreur in sorted(validateur.iter_errors(arguments), key=lambda e: list(e.absolute_path)):
        où = ".".join(str(p) for p in erreur.absolute_path)
        reproches.append(f"{où}: {erreur.message}" if où else erreur.message)
    return reproches


def output_schema(contract: CapabilityContract) -> dict[str, Any]:
    """Le schéma de ce que l'outil rend — l'enveloppe, pas la sortie nue.

    La spec est catégorique : « If an output schema is provided, servers MUST
    provide structured results that conform to this schema. » Déclarer ici le
    seul bloc `output` du contrat serait donc faux, parce que ce n'est pas ce
    qu'on rend : un agent a besoin de savoir quel variant a servi et ce que
    l'admission a décidé, et ces deux réponses ne sont pas dans le contrat de
    capacité — elles sont la valeur propre d'Écurie.

    La sortie du contrat vit sous `output`, à sa forme exacte. Le reste est
    l'enveloppe, identique pour les douze outils.

    **Et parce qu'elle est identique, elle ne se décrit pas ici.** L'enveloppe
    documentée champ par champ pesait 2 188 caractères, répétés douze fois :
    26 261 caractères, soit **59 % du catalogue** pour dire douze fois la même
    chose. Le budget de contexte est la contrainte dimensionnante du §6.3, et
    une prose qu'on paie à chaque outil pour l'apprendre une fois est exactement
    ce qu'il faut retirer. La structure reste — un client qui valide en a besoin
    —, l'explication part dans les `instructions` du serveur, lues une seule
    fois par session. Mesuré après : 6 565 jetons de catalogue économisés.
    """
    return {
        "type": "object",
        "required": ["ok", "capability", "ref", "job_id", "output"],
        "properties": {
            "ok": {"type": "boolean"},
            "capability": {"type": "string"},
            "ref": {"type": "string"},
            "job_id": {"type": "string"},
            "output": sans_extensions(deepcopy(contract.output_schema)) or {"type": "object"},
            "files": {"type": "object", "additionalProperties": {"type": "string"}},
            "admission": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "reused": {"type": "boolean"},
                    "evicted": {"type": "array", "items": {"type": "string"}},
                    "headroom_bytes": {"type": "integer"},
                },
            },
            "duration_ms": {"type": "integer"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }
