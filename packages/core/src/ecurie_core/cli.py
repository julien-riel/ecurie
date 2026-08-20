"""CLI Écurie. v0.1 : `ecurie registry validate`, `ecurie store scan|status`."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from ecurie_core.registry import find_root, load_registry

app = typer.Typer(no_args_is_help=True, help="Écurie — parc de modèles locaux.")
registry_app = typer.Typer(no_args_is_help=True, help="Registre déclaré (registry/).")
app.add_typer(registry_app, name="registry")

console = Console()


def _find_root(start: Path) -> Path:
    root = find_root(start)
    if root is None:
        raise typer.BadParameter(
            f"aucun registre Écurie trouvé depuis {start} "
            "(attendu : registry/schema/model.schema.json)"
        )
    return root


@registry_app.command("validate")
def registry_validate(
    root: Annotated[
        Path,
        typer.Argument(help="Racine du dépôt (défaut : détectée depuis le dossier courant)."),
    ] = Path("."),
) -> None:
    """Valide les manifestes contre le schéma et les invariants du parc."""
    reg = load_registry(_find_root(root.resolve()))

    if reg.issues:
        table = Table(show_lines=False, pad_edge=False)
        table.add_column("Gravité", no_wrap=True)
        table.add_column("Fichier", no_wrap=True)
        table.add_column("Problème")
        for issue in reg.issues:
            style = "red" if issue.severity == "error" else "yellow"
            label = "erreur" if issue.severity == "error" else "avert."
            table.add_row(f"[{style}]{label}[/{style}]", issue.file, issue.message)
        console.print(table)

    n_err, n_warn = len(reg.errors), len(reg.warnings)
    summary = (
        f"{len(reg.models)} modèle(s), {len(reg.capabilities)} capacité(s), "
        f"[red]{n_err} erreur(s)[/red], [yellow]{n_warn} avertissement(s)[/yellow]"
    )
    console.print(summary)
    if n_err:
        raise typer.Exit(code=1)


# Le paquet store est optionnel pour ecurie-core ; ses commandes se greffent
# quand il est installé (toujours le cas dans le workspace).
try:
    from ecurie_store.cli import store_app
except ImportError:
    pass
else:
    app.add_typer(store_app, name="store")


if __name__ == "__main__":
    app()
