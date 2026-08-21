"""Les fixtures du front disent-elles encore ce que le registre dit ?

`apps/ui/src/api/__fixtures__/` porte trois instantanés du **vrai** registre,
capturés par `tools/ui_fixtures.py`. La suite du front s'en sert pour prouver que
sa fusion des valeurs par défaut égale celle du serveur, que ses trois états de
capacité existent bel et bien dans le parc, et que sa recopie des chemins de
sortie est exacte.

Un instantané n'a de valeur que s'il reste vrai. Sans cette garde, ajouter un
modèle au registre ou changer un `defaults:` dans un manifeste laisserait la
suite du front comparer sa logique à un parc qui n'existe plus — au vert, et
pour rien. Le cas s'est produit avant même que ce test soit écrit : deux des
trois fixtures ont divergé du registre en une demi-heure, sans que rien ne le
signale.

Le test vit ici, dans la suite pytest, pour la même raison que
`test_openapi_fige.py` : c'est celui qui édite `registry/` qui doit être averti,
et il ne lance pas `npm test`.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[3]
DOSSIER = REPO_ROOT / "apps" / "ui" / "src" / "api" / "__fixtures__"
NOMS = ("capabilities", "models", "merged_defaults")


def _capture(root: Path) -> dict:
    """La capture, obtenue par l'outil même qui écrit les fichiers figés."""
    spec = importlib.util.spec_from_file_location(
        "ecurie_ui_fixtures", root / "tools" / "ui_fixtures.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.capture(root)


@pytest.mark.parametrize("nom", NOMS)
def test_les_fixtures_du_front_refletent_le_registre(nom: str):
    chemin = DOSSIER / f"{nom}.json"
    if not chemin.exists():
        pytest.skip("apps/ui absent : le front n'est pas installé dans cette copie")

    attendu = _capture(REPO_ROOT)[nom]
    figé = json.loads(chemin.read_text(encoding="utf-8"))

    assert figé == attendu, (
        f"la fixture {nom}.json du front ne reflète plus le registre.\n"
        "Régénérer et committer : uv run python tools/ui_fixtures.py "
        "apps/ui/src/api/__fixtures__"
    )


def test_aucun_chemin_de_cette_machine_ne_fuite_dans_les_fixtures():
    """Ces fichiers sont versionnés : ils ne doivent rien dire du disque de l'auteur.

    `weights_path` est un chemin absolu, remplacé par un jeton à la capture. La
    vérification porte sur le contenu committé, pas sur l'intention de l'outil.
    """
    if not DOSSIER.exists():
        pytest.skip("apps/ui absent : le front n'est pas installé dans cette copie")

    for nom in NOMS:
        texte = (DOSSIER / f"{nom}.json").read_text(encoding="utf-8")
        assert '"/Users/' not in texte, f"{nom}.json porte un chemin absolu"
        assert '"/home/' not in texte, f"{nom}.json porte un chemin absolu"
