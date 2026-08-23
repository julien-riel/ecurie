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
    """Les routes que le front peut appeler, énumérées : rien de plus, rien de moins.

    La liste a grandi de quatre avec le reste de la tâche 4.1 — soumettre, suivre,
    relire, télécharger. Ce qui les retenait, le superviseur reconstruit à chaque
    requête et incapable de savoir qu'un job tourne, a été levé par la 4.6.

    La douzième est `POST /uploads`, et elle referme la note que cette liste
    portait : « aucune route de téléversement, alors que dix champs du registre
    attendent un fichier ». Ce n'est pas le partage de machine qui a cessé
    d'être vrai — le chemin rendu est toujours local — mais le raisonnement qui
    en découlait : une image choisie dans une page, une photo de la caméra et un
    son du micro n'ont jamais eu de chemin à saisir, quelle que soit la machine.

    Les deux dernières sont celles de l'écran Parc (tâche 4.5). Elles auraient pu
    manquer : `/store/summary` porte déjà les trois chiffres et l'arbre de
    duplication, et un écran s'en serait contenté. Ce qui aurait manqué avec, ce
    sont les deux moitiés de la décision — ce qu'un plan de GC propose, et ce
    qu'un déport rendrait —, c'est-à-dire tout ce qui distingue un tableau de
    bord d'un écran sur lequel on agit.
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
        "/store/plan",
        "/store/tiering",
        "/runtime/residents",
        "/runtime/admission",
        "/jobs",
        "/jobs/{job_id}",
        "/jobs/{job_id}/events",
        "/jobs/{job_id}/files/{chemin}",
        "/uploads",
    }
