"""Adaptateur `mlx-lm`, chemin **appel d'outils**.

Il sert la capacité `tool-use`, qui juge si un modèle est utilisable dans un
harnais : choisir le bon outil parmi ceux qu'on lui déclare, et en remplir les
arguments. **Aucun outil n'est exécuté ici, et ne le sera pas** — Écurie mesure
le choix et le remplissage, pas leur effet. La boucle d'agent est hors périmètre
(ARCHITECTURE.md §11).

Le point délicat est l'extraction. Les modèles n'ont pas de format commun : les
uns entourent leur appel de `<tool_call>…</tool_call>`, d'autres rendent un objet
JSON nu, d'autres encore un tableau. Un extracteur qui n'en connaîtrait qu'un
seul noterait zéro un modèle qui a parfaitement choisi son outil — la capacité
mesurerait alors la conformité à un format, pas la compétence. D'où un
extracteur délibérément tolérant, dont ce qu'il a dû faire pour arriver à ses
fins est rapporté dans les métriques : `parse_strategy` dit si l'appel était
balisé, nu, ou noyé dans du texte.

Rien de mlx n'est importé au niveau du module (voir `workers/__init__.py`).
"""

import json
import re
from typing import Any

from ecurie_runtime.workers.base import (
    InferRequest,
    InferResult,
    ProgressFn,
    WorkerError,
    main,
)
from ecurie_runtime.workers.mlx_lm import Consigne, MlxLmBase

OUTPUT_CALLS = "calls.json"
OUTPUT_TEXT = "text.txt"

BALISES = (
    ("tool_call", re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)),
    ("tool_calls", re.compile(r"<tool_calls>\s*(.*?)\s*</tool_calls>", re.DOTALL)),
    ("function", re.compile(r"<function\b[^>]*>\s*(.*?)\s*</function>", re.DOTALL)),
    ("json_fence", re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)),
)

SYSTEME_REPLI = (
    "Tu disposes des outils suivants. Pour en appeler un, réponds UNIQUEMENT par "
    "un objet JSON de la forme {\"name\": \"...\", \"arguments\": {...}}, sans "
    "texte autour. Si aucun outil ne convient, réponds en clair.\n\nOutils :\n"
)

FORCE = (
    "Tu dois appeler un outil. Ne réponds pas en clair : rends l'appel et rien d'autre."
)
INTERDIT = (
    "N'appelle aucun outil. Réponds en clair, en français."
)


def _decoder(fragment: str) -> list[dict[str, Any]]:
    """Un fragment JSON → une liste d'appels, quelle que soit sa forme.

    Un objet seul, un tableau d'objets, ou un objet à la mode OpenAI dont l'appel
    est niché sous `function` : les trois se rencontrent, et les trois disent la
    même chose.
    """
    try:
        valeur = json.loads(fragment)
    except (json.JSONDecodeError, TypeError):
        return []
    éléments = valeur if isinstance(valeur, list) else [valeur]
    appels: list[dict[str, Any]] = []
    for élément in éléments:
        if not isinstance(élément, dict):
            continue
        fonction = élément.get("function") if isinstance(élément.get("function"), dict) else élément
        nom = fonction.get("name")
        if not isinstance(nom, str) or not nom:
            continue
        arguments = fonction.get("arguments", fonction.get("parameters", {}))
        if isinstance(arguments, str):
            # Certains modèles sérialisent les arguments une seconde fois.
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"_brut": arguments}
        appels.append({"name": nom, "arguments": arguments if isinstance(arguments, dict) else {}})
    return appels


def _objets_nus(texte: str) -> list[str]:
    """Fragments JSON équilibrés trouvés dans du texte libre.

    Un `json.loads` sur toute la réponse échoue dès qu'une phrase l'entoure ;
    une expression régulière sur `{.*}` avale tout jusqu'à la dernière accolade.
    On compte donc les accolades, en ignorant celles qui sont dans une chaîne.
    """
    fragments: list[str] = []
    profondeur = 0
    début = -1
    dans_chaine = False
    échappé = False
    for index, caractère in enumerate(texte):
        if dans_chaine:
            if échappé:
                échappé = False
            elif caractère == "\\":
                échappé = True
            elif caractère == '"':
                dans_chaine = False
            continue
        if caractère == '"':
            dans_chaine = True
        elif caractère == "{":
            if profondeur == 0:
                début = index
            profondeur += 1
        elif caractère == "}":
            profondeur -= 1
            if profondeur == 0 and début >= 0:
                fragments.append(texte[début : index + 1])
                début = -1
            elif profondeur < 0:
                profondeur = 0
    return fragments


def extraire_appels(texte: str) -> tuple[list[dict[str, Any]], str, str]:
    """Rend (appels, texte restant, stratégie employée)."""
    for nom, motif in BALISES:
        blocs = motif.findall(texte)
        if not blocs:
            continue
        appels = [appel for bloc in blocs for appel in _decoder(bloc)]
        if appels:
            reste = motif.sub("", texte).strip()
            return appels, reste, nom

    for fragment in _objets_nus(texte):
        appels = _decoder(fragment)
        if appels:
            reste = texte.replace(fragment, "").strip()
            return appels, reste, "json_nu" if texte.strip() == fragment.strip() else "json_noye"

    return [], texte.strip(), "aucun"


def valider(appels: list[dict[str, Any]], outils: list[dict[str, Any]]) -> list[str]:
    """Reproches faits aux appels, sans exécuter quoi que ce soit.

    Volontairement superficiel : on vérifie que l'outil existe et que ses
    arguments obligatoires sont là. La validation complète contre le JSON Schema
    déclaré relève de `ecurie eval`, qui dispose de `jsonschema` — pas d'un
    worker, dont l'environnement doit rester minimal.
    """
    connus = {str(outil.get("name")): outil for outil in outils if outil.get("name")}
    reproches: list[str] = []
    for index, appel in enumerate(appels):
        nom = appel.get("name")
        outil = connus.get(str(nom))
        if outil is None:
            reproches.append(f"appel {index} : outil inconnu {nom!r}")
            continue
        schéma = outil.get("parameters") or {}
        requis = schéma.get("required") or []
        arguments = appel.get("arguments") or {}
        manquants = [clé for clé in requis if clé not in arguments]
        if manquants:
            reproches.append(f"appel {index} ({nom}) : argument(s) manquant(s) {manquants}")
    return reproches


class MlxLmToolsWorker(MlxLmBase):
    """Appel d'outils : le modèle choisit et remplit, Écurie n'exécute rien."""

    name = "mlx-lm-tools"

    def annonce(self) -> dict[str, Any]:
        return {"tool_choices": ["auto", "required", "none"], "executes_tools": False}

    def infer(self, request: InferRequest, progress: ProgressFn) -> InferResult:
        tâche = str(request.get("task") or "").strip()
        if not tâche:
            raise WorkerError("aucune demande : le champ `task` est vide")
        outils = request.get("tools") or []
        if not isinstance(outils, list) or not outils:
            raise WorkerError("aucun outil déclaré : le champ `tools` est vide")

        choix = str(self.reglage(request, "tool_choice", "auto"))
        parallèle = bool(self.reglage(request, "parallel_calls", False))
        système = self.reglage(request, "system", None)

        progress(5, "préparation")
        consigne = Consigne(system=système, user=self._demande(tâche, choix, parallèle))

        réponse, gabarit_outils = self.engendrer(
            consigne.messages(),
            progress=progress,
            max_tokens=int(self.reglage(request, "max_tokens", 1024)),
            temperature=float(self.reglage(request, "temperature", 0.0)),
            top_p=1.0,
            seed=request.seed,
            tools=None if choix == "none" else outils,
            etape="choix d'outil",
        )

        if not gabarit_outils and choix != "none":
            # Le gabarit du modèle ignore les outils : ils sont décrits dans le
            # message système. C'est un repli, pas un équivalent, et la métrique
            # le dit pour qu'aucune comparaison ne le prenne pour du natif.
            consigne.system = (système or "") + "\n\n" + SYSTEME_REPLI + _lister(outils)
            réponse, _ = self.engendrer(
                consigne.messages(),
                progress=progress,
                max_tokens=int(self.reglage(request, "max_tokens", 1024)),
                temperature=float(self.reglage(request, "temperature", 0.0)),
                top_p=1.0,
                seed=request.seed,
                etape="choix d'outil (repli)",
            )

        appels, reste, stratégie = extraire_appels(réponse.text)
        if not parallèle and len(appels) > 1:
            appels = appels[:1]
        if choix == "none":
            appels = []

        progress(92, "écriture")
        # Toujours écrit, même vide : un fichier absent et une liste vide ne
        # disent pas la même chose, et le contrat exige `calls`.
        (request.output_dir / OUTPUT_CALLS).write_text(
            json.dumps(appels, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        sorties: dict[str, Any] = {
            "calls": OUTPUT_CALLS,
            "call_names": [str(appel.get("name")) for appel in appels],
            "finish_reason": réponse.finish_reason,
        }
        if reste:
            (request.output_dir / OUTPUT_TEXT).write_text(reste, encoding="utf-8")
            sorties["text"] = OUTPUT_TEXT

        return InferResult(
            output=sorties,
            metrics={
                "tools_declared": len(outils),
                "calls": len(appels),
                "parse_strategy": stratégie,
                "template_tools": gabarit_outils,
                "complaints": valider(appels, outils),
                "prompt_tokens": réponse.prompt_tokens,
                "generation_tokens": réponse.generation_tokens,
                "tokens_per_second": réponse.tokens_per_second,
                "peak_memory_bytes": self.peak_memory_bytes(),
            },
        )

    def _demande(self, tâche: str, choix: str, parallèle: bool) -> str:
        lignes = [tâche]
        if choix == "required":
            lignes.append(FORCE)
        elif choix == "none":
            lignes.append(INTERDIT)
        if parallèle:
            lignes.append("Tu peux appeler plusieurs outils à la fois si c'est utile.")
        else:
            lignes.append("N'appelle qu'un seul outil.")
        return "\n\n".join(lignes)


def _lister(outils: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- {outil.get('name')} : {outil.get('description', '')} "
        f"— arguments : {json.dumps(outil.get('parameters') or {}, ensure_ascii=False)}"
        for outil in outils
    )


if __name__ == "__main__":
    raise SystemExit(main(MlxLmToolsWorker))
