"""Fabrique les assets des charges types du banc d'essai.

    uv run --project runtimes/<env> python tools/bench_assets.py <famille>

Chaque famille de capacités a sa recette sous `tools/assets/` et déclare
l'environnement dans lequel elle tourne : la série temporelle n'a besoin que de
numpy, la scène satellite veut rasterio, le nuage de points ne veut rien du tout.
Lancé sans argument, l'outil liste ce qu'il sait faire et dans quel env — il ne
tente pas d'importer une recette dont les dépendances manquent, ce qui ferait
échouer la fabrication d'un asset pour l'absence d'une bibliothèque servant à un
autre.

Le banc est **append-only** : un asset déjà là n'est jamais réécrit sans
`--force`. Une charge type modifiée détruit la comparabilité de toutes les
mesures prises avant elle, et le fichier de mesure ne dit pas contre quelle
version d'une image il a été relevé.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
ASSETS = RACINE / "registry" / "evals" / "bench" / "assets"

sys.path.insert(0, str(Path(__file__).resolve().parent))


def recettes() -> dict[str, str]:
    """Les familles connues → le nom de leur module. Sans les importer."""
    import assets as paquet

    return {
        info.name: f"assets.{info.name}"
        for info in pkgutil.iter_modules(paquet.__path__)
        if not info.name.startswith("_")
    }


def main(argv: list[str] | None = None) -> int:
    connues = recettes()
    parseur = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parseur.add_argument(
        "familles",
        nargs="*",
        help=f"Recettes à exécuter. Connues : {', '.join(sorted(connues)) or '(aucune)'}",
    )
    parseur.add_argument(
        "--force",
        action="store_true",
        help="Réécrire un asset existant. Une charge type figée ne se corrige pas : "
        "à n'utiliser que sur un asset qu'aucune mesure n'a encore employé.",
    )
    args = parseur.parse_args(argv)

    if not args.familles:
        print("Recettes disponibles :")
        for nom, module in sorted(connues.items()):
            try:
                env = getattr(importlib.import_module(module), "ENV", None)
            except ImportError as exc:  # dépendance absente de cet env
                env = f"? ({exc})"
            print(f"  {nom:24s} env : {env or 'aucun'}")
        return 0

    inconnues = [f for f in args.familles if f not in connues]
    if inconnues:
        print(f"famille(s) inconnue(s) : {', '.join(inconnues)}", file=sys.stderr)
        return 2

    ASSETS.mkdir(parents=True, exist_ok=True)
    total = 0
    for famille in args.familles:
        module = importlib.import_module(connues[famille])
        print(f"{famille} :")
        écrits = module.produire(ASSETS, force=args.force)
        total += len(écrits)
    print(f"{total} fichier(s) écrit(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
