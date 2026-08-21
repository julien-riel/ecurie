"""Le schéma figé pour le front est-il encore celui que l'API sert ?

Le front d'`apps/ui` ne réécrit pas la forme des réponses à la main : il tire ses
types d'`apps/ui/src/api/openapi.json`, figé par `tools/openapi_dump.py`, dont
`openapi-typescript` engendre `openapi.gen.ts`. Un champ ajouté à `schemas.py`
sans régénération laisserait le front programmer contre une forme périmée.

Ce test est ce qui rend le typage **opposable**, et sa place ici n'est pas
accessoire : il tourne dans la suite pytest que le développeur du serveur lance
déjà, sans Node, sans `npm install`. Une garde qui n'aurait vécu que dans la
suite du front n'aurait averti personne — c'est exactement ainsi qu'un endpoint
promis par la conception (`/runtime/residents/{ref}/options`) a pu ne jamais
exister sans que rien ne le signale.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[3]
FIGE = REPO_ROOT / "apps" / "ui" / "src" / "api" / "openapi.json"


def openapi_document(root: Path) -> dict:
    """Le schéma que sert l'API, obtenu par l'outil même qui écrit le fichier figé.

    Chargé par chemin plutôt qu'importé : `tools/` n'est pas un paquet du
    workspace, et en faire un ferait entrer des scripts d'export dans les
    dépendances de `ecurie-api`. Passer par l'outil réel, plutôt que de
    reconstruire l'application ici, garantit que le test compare bien ce que la
    commande de régénération produirait.
    """
    spec = importlib.util.spec_from_file_location(
        "ecurie_openapi_dump", root / "tools" / "openapi_dump.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.openapi_document(root)


def test_le_schema_fige_du_front_est_celui_que_l_api_sert():
    if not FIGE.exists():
        pytest.skip("apps/ui absent : le front n'est pas installé dans cette copie")

    servi = openapi_document(REPO_ROOT)
    figé = json.loads(FIGE.read_text(encoding="utf-8"))

    assert figé == servi, (
        "le schéma OpenAPI figé pour le front a divergé de celui que l'API sert.\n"
        "Régénérer et committer : uv run python tools/openapi_dump.py "
        "apps/ui/src/api/openapi.json, puis (dans apps/ui) npm run gen:api"
    )


def test_les_routes_figees_sont_celles_que_le_front_appelle():
    """Le front n'a que sept fonctions d'appel : elles doivent toutes exister.

    Ce test dit aussi ce qui n'existe PAS encore, et c'est le point : `POST
    /jobs`, le flux SSE et les fichiers de sortie attendent délibérément le
    déménagement du superviseur dans le processus de l'API (tâche 4.6). Le jour
    où ils arriveront, cette liste changera, et le front pourra les appeler.
    """
    if not FIGE.exists():
        pytest.skip("apps/ui absent : le front n'est pas installé dans cette copie")

    figé = json.loads(FIGE.read_text(encoding="utf-8"))
    assert set(figé["paths"]) == {
        "/",
        "/healthz",
        "/registry/capabilities",
        "/registry/models",
        "/store/summary",
        "/runtime/residents",
        "/runtime/admission",
    }
    assert "/jobs" not in figé["paths"]
