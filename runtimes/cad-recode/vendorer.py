"""Extrait le code d'inférence de CAD-Recode depuis le notebook amont.

    python runtimes/cad-recode/vendorer.py

**Pourquoi un script plutôt qu'une ligne de README.** Le code d'inférence de
CAD-Recode — la classe `CADRecode` et son `FourierPointEncoder` — est sous
**CC BY-NC 4.0**, comme les poids : aucune de ses lignes ne peut être committée
ici. Et contrairement à `hy3dshape` chez `runtimes/hunyuan3d/`, il n'existe pas
sous forme de module : il vit dans **une cellule de `demo.ipynb`**, mêlé à des
imports d'`open3d`, `skimage`, `matplotlib`, `scipy` et `pytorch3d` dont aucun
n'est installé ici (et dont `pytorch3d` ne publie aucune roue arm64). Un
`sparse-checkout` ne suffisait donc pas ; il fallait découper.

Ce fichier-ci ne contient **aucune ligne d'amont** : il contient la règle de
découpe. Ce qu'il produit va sous `vendor/`, que le `.gitignore` du dépôt exclut.

**Ce qu'il découpe, et pourquoi cette frontière.** La cellule commence par ses
imports, puis par `class FourierPointEncoder`. Tout ce qui précède ce marqueur
est jeté et remplacé par les quatre imports que le code conservé utilise
réellement. C'est la seule modification faite au code amont, et elle est
mécanique.

**L'empreinte est affichée, et ce n'est pas décoratif.** Le notebook amont peut
bouger sans que sa révision épinglée ici change de nom : comparer le sha256 de ce
qui a été extrait à celui qui a été éprouvé est le seul moyen de savoir si l'on
exécute encore le code qui a produit le profil du manifeste.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent

#: Le commit amont sur lequel la découpe a été éprouvée, et sous lequel le profil
#: du manifeste a été mesuré. `git clone --depth 1` en ramène un plus récent ; si
#: l'empreinte ci-dessous ne correspond plus, c'est là qu'il faut regarder.
COMMIT_EPROUVE = "03e3262119b38939feaa44b8368ad8db99243d47"

#: sha256 du fichier produit à partir de ce commit. Affiché, jamais imposé : une
#: divergence est une information, pas une raison de refuser de travailler.
SHA256_EPROUVE = "a810f52b1dde027175240b196b0d4aab67994bb47e6bf8387dca9d45566c6c01"

NOTEBOOK = RACINE / "vendor" / "cad-recode" / "demo.ipynb"
CIBLE = RACINE / "vendor" / "cad_recode_model.py"

#: Le premier symbole à conserver. Ce qui le précède dans la cellule n'est
#: qu'une liste d'imports, dont cinq pour des bibliothèques absentes d'ici.
DEBUT = "class FourierPointEncoder"

#: Les seuls imports dont le code conservé a besoin. Écrits ici parce qu'ils sont
#: le résultat de la découpe, pas une reprise de l'amont : la liste d'origine en
#: compte onze, dont `open3d`, `skimage`, `matplotlib`, `scipy` et `pytorch3d`.
EN_TETE = '''"""Code d'inférence de CAD-Recode, extrait de `demo.ipynb` par `vendorer.py`.

CE FICHIER EST SOUS CC BY-NC 4.0, comme le dépôt dont il est extrait
(github.com/filaPro/cad-recode, LICENSE.md). Il n'est pas versionné dans Écurie
et ne doit jamais l'être. Usage de recherche non commerciale uniquement.
"""

import torch
from torch import nn
from transformers import PreTrainedModel, Qwen2ForCausalLM, Qwen2Model
from transformers.modeling_outputs import CausalLMOutputWithPast


'''


def cellule(notebook: Path) -> str:
    """La cellule de code qui définit le modèle, telle quelle."""
    try:
        contenu = json.loads(notebook.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(
            f"notebook introuvable : {notebook}\n"
            "Cloner d'abord le dépôt amont — voir runtimes/cad-recode/README.md"
        ) from exc
    except ValueError as exc:
        raise SystemExit(f"{notebook} n'est pas un notebook lisible : {exc}") from exc

    trouvées = [
        "".join(c.get("source") or [])
        for c in contenu.get("cells") or []
        if c.get("cell_type") == "code" and DEBUT in "".join(c.get("source") or [])
    ]
    if len(trouvées) != 1:
        raise SystemExit(
            f"{len(trouvées)} cellule(s) contiennent « {DEBUT} », une seule était attendue — "
            "le notebook amont a changé de forme, et la découpe doit être revue avant "
            "d'exécuter quoi que ce soit"
        )
    return trouvées[0]


def decouper(source: str) -> str:
    """Le code à partir du premier symbole utile, imports d'ici en tête."""
    début = source.index(DEBUT)
    corps = source[début:].rstrip() + "\n"
    if "class CADRecode" not in corps:
        raise SystemExit(
            "la cellule ne définit pas `CADRecode` après le point de découpe — "
            "le notebook amont a changé, ne pas exécuter ce qui en sortirait"
        )
    return EN_TETE + corps


def main(argv: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parseur.add_argument("--notebook", type=Path, default=NOTEBOOK)
    parseur.add_argument("--cible", type=Path, default=CIBLE)
    args = parseur.parse_args(argv)

    module = decouper(cellule(args.notebook))
    args.cible.parent.mkdir(parents=True, exist_ok=True)
    args.cible.write_text(module, encoding="utf-8")

    empreinte = hashlib.sha256(module.encode("utf-8")).hexdigest()
    print(f"écrit  : {args.cible}")
    print(f"lignes : {len(module.splitlines())}")
    print(f"sha256 : {empreinte}")
    if SHA256_EPROUVE and empreinte != SHA256_EPROUVE:
        print(
            f"\nATTENTION — empreinte différente de celle éprouvée ({SHA256_EPROUVE}).\n"
            f"Le commit amont attendu est {COMMIT_EPROUVE}. Le profil du manifeste a été\n"
            "mesuré sur ce code-là ; relire la différence avant de s'y fier.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
