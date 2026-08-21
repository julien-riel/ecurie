"""Capture des réponses de l'API sur le vrai registre, pour les tests du front.

Le front recopie une règle du serveur : la fusion des valeurs par défaut d'un
contrat et d'un variant (`merge_defaults` de `ecurie_runtime.runner`). Il la
recopie parce que le formulaire doit s'afficher rempli au premier rendu, avant
qu'aucune question n'ait été posée au serveur — mais une recopie non vérifiée
finit toujours par diverger.

Cet outil fige donc, pour **chaque variant du parc réel**, ce que le serveur
calcule. Le test `defaults.test.ts` compare les deux, variant par variant.

Un instantané ne vaut que s'il reste vrai, et celui-ci a divergé du registre en
une demi-heure la première fois. `packages/api/tests/test_fixtures_ui.py` le
garde : il rejoue cette capture et la compare aux fichiers committés, dans la
suite pytest — celle que lance qui édite `registry/`, et non `npm test`.

Deux précautions valent d'être dites :

**Rien de ce qui sort n'est propre à cette machine.** `weights_path` est un
chemin absolu du disque de l'auteur : il est remplacé par un jeton. Le fichier
produit est committé, il ne doit rien apprendre à personne sur l'arborescence
d'un poste.

**Aucun modèle n'est chargé.** Le registre est lu, les contrats sont validés, et
c'est tout — pas de sous-processus, pas de Metal, pas de poids.

Usage : `uv run python tools/ui_fixtures.py apps/ui/src/api/__fixtures__`
"""

import json
import sys
from pathlib import Path

from ecurie_api.projection import capability_out, issues_out, model_out
from ecurie_core.config import Config, ScanConfig
from ecurie_core.registry import load_registry
from ecurie_runtime.runner import merge_defaults

JETON_POIDS = "<poids>"
BUDGET_FIGE = 16 << 30


def _anonymise(valeur):
    """Remplace tout chemin absolu par un jeton, récursivement."""
    if isinstance(valeur, dict):
        return {clé: _anonymise(v) for clé, v in valeur.items()}
    if isinstance(valeur, list):
        return [_anonymise(v) for v in valeur]
    if isinstance(valeur, str) and valeur.startswith("/"):
        return JETON_POIDS
    return valeur


def capture(root: Path) -> dict:
    config = Config(memory_budget=BUDGET_FIGE, scan=ScanConfig())
    registre = load_registry(root)

    par_capacité: dict[str, list] = {}
    modèles = []
    for model in sorted(registre.models.values(), key=lambda m: m.id):
        projeté = model_out(root, config, model)
        par_capacité.setdefault(model.capability, []).append(projeté)
        modèles.append(projeté)

    capacités = [
        capability_out(contrat, registre, par_capacité.get(cap_id, []))
        for cap_id, contrat in sorted(registre.capabilities.items())
    ]

    # Le cœur de la fixture : ce que le serveur pose dans un formulaire vide,
    # pour chaque variant du parc, contrat et manifeste fusionnés.
    défauts = {}
    for model in sorted(registre.models.values(), key=lambda m: m.id):
        contrat = registre.capabilities.get(model.capability)
        if contrat is None:
            continue
        for variant in model.variants:
            défauts[f"{model.id}@{variant.id}"] = {
                "capability": model.capability,
                "variant_defaults": variant.defaults or {},
                "merged": merge_defaults(contrat, variant),
            }

    return {
        "capabilities": {
            "capabilities": [_anonymise(c.model_dump(mode="json")) for c in capacités],
            "issues": [i.model_dump(mode="json") for i in issues_out(registre.issues)],
        },
        "models": {
            "models": [_anonymise(m.model_dump(mode="json")) for m in modèles],
            "issues": [i.model_dump(mode="json") for i in issues_out(registre.issues)],
        },
        "merged_defaults": défauts,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage : {argv[0]} <dossier/__fixtures__>", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    dossier = Path(argv[1])
    dossier.mkdir(parents=True, exist_ok=True)

    capturé = capture(root)
    for nom, contenu in capturé.items():
        chemin = dossier / f"{nom}.json"
        chemin.write_text(
            json.dumps(contenu, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"écrit : {chemin}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
