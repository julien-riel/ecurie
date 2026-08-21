"""Fige le schéma OpenAPI de l'API dans `apps/ui/src/api/openapi.json`.

Le front programme contre la forme des réponses de `packages/api`. Cette forme
est déjà décrite par FastAPI, et la recopier à la main en TypeScript reviendrait
à entretenir deux vérités qui divergeraient au premier champ ajouté. On fige donc
le schéma dans un fichier versionné, dont `openapi-typescript` tire les types.

Deux propriétés valent d'être dites, parce qu'elles conditionnent l'usage :

**L'export est hors ligne.** Aucun serveur n'est lancé, aucun sous-processus
n'est ouvert — le budget mémoire est injecté au lieu d'être détecté, comme dans
`packages/api/tests/conftest.py`, et le registre du dépôt est lu tel quel. La
commande tourne sans MLX, sans venv de runtime, et sans poids sur le disque.

**Le fichier produit est l'objet du test `test_openapi_fige.py`.** C'est lui qui
rend le typage du front opposable : un champ ajouté à `schemas.py` sans
régénération fait échouer la suite pytest que le développeur du serveur lance
déjà, plutôt qu'une suite front qu'il n'ouvrira peut-être jamais.

Usage : `uv run python tools/openapi_dump.py apps/ui/src/api/openapi.json`
"""

import json
import sys
from pathlib import Path

from ecurie_api.app import create_app
from ecurie_api.state import AppState
from ecurie_core.config import Config, ScanConfig
from ecurie_runtime.budget import Budget

# Le budget n'entre pas dans le schéma OpenAPI ; il n'est là que pour éviter que
# `AppState.budget` parte détecter Metal si une route venait à le lire.
BUDGET_FIGE = 16 << 30


def openapi_document(root: Path) -> dict:
    """Le schéma que sert `ecurie serve`, sans lancer ni serveur ni sous-processus."""
    state = AppState(
        root,
        Config(memory_budget=BUDGET_FIGE, scan=ScanConfig()),
        budget=Budget(BUDGET_FIGE, "budget figé pour l'export du schéma"),
    )
    return create_app(state).openapi()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage : {argv[0]} <chemin/openapi.json>", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    destination = Path(argv[1])
    destination.parent.mkdir(parents=True, exist_ok=True)
    # `sort_keys` et le saut de ligne final ne sont pas de la coquetterie : sans
    # eux, deux exports du même schéma produisent des diffs de plusieurs
    # centaines de lignes et le fichier figé devient illisible en revue.
    destination.write_text(
        json.dumps(openapi_document(root), indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"schéma écrit : {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
