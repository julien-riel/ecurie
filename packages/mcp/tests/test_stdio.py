"""Le montage réellement livré : un sous-processus, stdin et stdout.

Les autres tests connectent l'objet `Server` en processus — rapide, et suffisant
pour la logique. Celui-ci est le seul qui prouve quelque chose sur ce que
`claude mcp add ecurie -- ecurie mcp` lancera : il échoue si **quoi que ce soit**
dans l'arbre d'imports ou dans le démarrage écrit une ligne sur la sortie
standard. C'est le mode de panne propre au transport stdio, et il est
silencieux — le client ne voit qu'une trame illisible et referme.

Il est marqué `real` parce qu'il lance un vrai processus sur le vrai dépôt :
c'est le parc de la machine qui répond, avec ses poids et ses environnements. La
CI, elle, s'arrête aux tests en processus.
"""

import sys

import pytest
from mcp import StdioServerParameters
from mcp.client import Client

pytestmark = [pytest.mark.anyio, pytest.mark.real]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_le_serveur_repond_a_tools_list_par_stdio():
    """Le parcours du critère de sortie, jusqu'au transport."""
    paramètres = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ecurie_mcp"],
        cwd=str(__import__("pathlib").Path(__file__).parents[3]),
    )
    async with Client(paramètres) as client:
        outils = (await client.list_tools()).tools

    noms = {o.name for o in outils}
    assert "ecurie_catalog" in noms
    assert "ecurie_status" in noms
    # Le catalogue reste petit : c'est la contrainte dimensionnante du §6.3.
    assert len(outils) <= 20, f"catalogue trop large : {len(outils)} outils"


async def test_rien_ne_pollue_la_sortie_standard():
    """Le budget mesuré, le registre annoncé, les avertissements : tout part sur stderr."""
    paramètres = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ecurie_mcp"],
        cwd=str(__import__("pathlib").Path(__file__).parents[3]),
    )
    # Si une ligne parasite précédait le JSON-RPC, l'initialisation elle-même
    # échouerait : c'est exactement ce qu'on veut éprouver.
    async with Client(paramètres) as client:
        résultat = await client.call_tool("ecurie_status", {})

    assert not résultat.is_error
