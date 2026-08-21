"""`ecurie serve` — l'API du parc, greffée sur la CLI comme `store` et `run`.

La commande fait trois choses avant d'écouter, et chacune évite une question que
l'utilisateur se poserait au premier écran vide :

1. elle **dit sur quel dépôt** elle sert — un `ecurie serve` lancé du mauvais
   dossier rendrait un registre vide sans que rien ne l'explique ;
2. elle **valide le registre** et annonce ce qu'il a à redire. Contrairement à
   `ecurie run`, elle ne s'arrête pas pour autant : l'API sert les manifestes
   valides et transporte le reste dans `issues`, faute de quoi une révision non
   épinglée priverait l'UI de tout le parc ;
3. elle **mesure le budget mémoire** tout de suite. C'est un sous-processus dans
   le venv d'un runtime : le payer ici plutôt qu'à la première requête évite que
   le premier affichage du bandeau de ressources paraisse gelé.
"""

import ipaddress
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from ecurie_core.config import load_config
from ecurie_core.registry import find_root, load_registry
from ecurie_store.figures import fmt_bytes
from rich.console import Console

from ecurie_api.app import DEFAULT_CORS_ORIGINS, create_app
from ecurie_api.state import AppState

console = Console()

DEFAULT_PORT = 8765


def _is_local(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve_command(
    host: Annotated[str, typer.Option("--host", help="Adresse d'écoute.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port d'écoute.")] = DEFAULT_PORT,
    cors_origin: Annotated[
        list[str] | None,
        typer.Option("--cors-origin", help="Origine autorisée en plus de celles de Vite."),
    ] = None,
    expose: Annotated[
        bool,
        typer.Option(
            "--expose",
            help="Autoriser une adresse d'écoute non locale. À n'utiliser qu'en connaissance.",
        ),
    ] = False,
    log_level: Annotated[str, typer.Option("--log-level", help="Verbosité d'uvicorn.")] = "info",
) -> None:
    """Sert l'API de lecture du parc : registre, occupation disque, résidents."""
    if not _is_local(host) and not expose:
        console.print(
            f"[red]{host} n'est pas une adresse locale.[/red] L'API dit où sont les poids "
            "sur le disque, ce que la machine a en mémoire, et lancera bientôt des jobs. "
            "La publier sur un réseau donne tout cela à qui passe — "
            "[bold]--expose[/bold] si c'est bien l'intention."
        )
        raise typer.Exit(code=1)

    root = find_root(Path.cwd())
    if root is None:
        raise typer.BadParameter(
            "aucun registre Écurie trouvé depuis le dossier courant "
            "(attendu : registry/schema/model.schema.json)"
        )

    config = load_config()
    state = AppState(root, config)

    registre = load_registry(root)
    console.print(f"Dépôt : [bold]{root}[/bold]")
    console.print(
        f"Registre : {len(registre.models)} modèle(s), {len(registre.capabilities)} capacité(s), "
        f"[red]{len(registre.errors)} erreur(s)[/red], "
        f"[yellow]{len(registre.warnings)} avertissement(s)[/yellow]"
    )
    if registre.errors:
        console.print(
            "[yellow]Les manifestes en erreur ne sont pas servis ; les autres le sont. "
            "Le détail est dans `issues` de /registry/models et dans "
            "[bold]ecurie registry validate[/bold].[/yellow]"
        )

    budget = state.budget
    console.print(
        f"Budget mémoire unifiée : [bold]{fmt_bytes(budget.bytes)}[/bold] ({budget.source})"
    )

    origines = [*DEFAULT_CORS_ORIGINS, *(cors_origin or [])]
    app = create_app(state, cors_origins=origines)

    console.print(
        f"\nÉcoute sur [bold]http://{host}:{port}[/bold] — "
        f"documentation interactive : [bold]http://{host}:{port}/docs[/bold]"
    )
    console.print(f"[dim]Origines autorisées : {', '.join(origines)}[/dim]")
    uvicorn.run(app, host=host, port=port, log_level=log_level)


def register(app: typer.Typer) -> None:
    """Ajoute `ecurie serve` à la CLI racine."""
    app.command("serve")(serve_command)
