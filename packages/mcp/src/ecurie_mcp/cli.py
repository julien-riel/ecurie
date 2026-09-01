"""`ecurie mcp` — le serveur MCP, greffé sur la CLI comme `serve` et `store`.

La commande n'affiche rien sur la sortie standard, et c'est sa contrainte
première : stdout est le canal JSON-RPC. Un dépôt annoncé, un budget mesuré, un
avertissement de registre — tout ce que `ecurie serve` imprime pour rassurer
l'utilisateur casserait ici la première trame. Le même besoin existe pourtant :
un `ecurie mcp` lancé du mauvais dossier servirait un registre vide sans que rien
ne l'explique. Tout part donc sur **stderr**, que les clients MCP rangent dans
leurs journaux et que l'utilisateur retrouve quand il se demande pourquoi son
outil manque.

Le budget mémoire se mesure **avant** de servir. C'est un sous-processus dans le
venv d'un runtime, de l'ordre de deux dixièmes de seconde : le payer ici plutôt
qu'au premier appel évite qu'un outil paraisse lent pour une raison qui ne le
regarde pas.
"""

import sys
from pathlib import Path
from typing import Annotated

import anyio
import typer
from ecurie_core.config import load_config
from ecurie_core.format import fmt_memory
from ecurie_core.registry import find_root, load_registry
from mcp.server.stdio import stdio_server
from rich.console import Console

from ecurie_mcp.catalogue import FAMILLES
from ecurie_mcp.contexte import Contexte, Exposition
from ecurie_mcp.serveur import construire, options

# Tout sur stderr : stdout appartient au protocole.
console = Console(stderr=True)


def mcp_command(
    tools: Annotated[
        list[str] | None,
        typer.Option(
            "--tools",
            help=(
                "Familles de capacités à exposer en plus des douze : "
                f"{', '.join(sorted(FAMILLES))}, ou « all ». Les capacités qui portent un "
                "human_subject sont exclues sans cela — du catalogue comme d'ecurie_run."
            ),
        ),
    ] = None,
) -> None:
    """Sert le parc à un agent, en MCP sur stdio."""
    root = find_root(Path.cwd())
    if root is None:
        console.print(
            "[red]Aucun registre Écurie trouvé depuis le dossier courant[/red] "
            "(attendu : registry/schema/model.schema.json). "
            "Lancer la commande depuis un clone du dépôt."
        )
        raise typer.Exit(code=1)

    demandées = {f.strip() for arg in (tools or []) for f in arg.split(",") if f.strip()}
    inconnues = demandées - set(FAMILLES) - {"all"}
    if inconnues:
        console.print(
            f"[red]Famille inconnue : {', '.join(sorted(inconnues))}[/red] — "
            f"connues : {', '.join(sorted(FAMILLES))}, all"
        )
        raise typer.Exit(code=1)

    config = load_config()
    contexte = Contexte(root, config, exposition=Exposition(familles=frozenset(demandées)))

    registre = load_registry(root)
    console.print(f"Dépôt : [bold]{root}[/bold]")
    console.print(
        f"Registre : {len(registre.models)} modèle(s), {len(registre.capabilities)} capacité(s), "
        f"[red]{len(registre.errors)} erreur(s)[/red], "
        f"[yellow]{len(registre.warnings)} avertissement(s)[/yellow]"
    )

    budget = contexte.budget
    console.print(
        f"Budget mémoire unifiée : [bold]{fmt_memory(budget.bytes)}[/bold] ({budget.source})"
    )

    serveur, servi = construire(contexte)
    déclarés = servi.declarer()
    console.print(
        f"Outils servis : [bold]{len(déclarés)}[/bold] "
        f"({', '.join(t.name for t in déclarés)})"
    )
    if demandées:
        # Une famille rouverte n'ajoute un outil que si sa capacité a un texte
        # rédigé, et aucune n'en a : le catalogue est éditorial, pas engendré du
        # registre. L'opt-in ouvre donc `ecurie_run` et rien d'autre — le dire
        # vaut mieux que laisser compter les outils pour s'en apercevoir.
        console.print(
            f"[dim]--tools {', '.join(sorted(demandées))} : ces capacités deviennent "
            "exécutables par ecurie_run. Elles n'ont pas d'outil dédié — le catalogue "
            "des douze est rédigé à la main, pas engendré du registre.[/dim]"
        )
    if servi.muets:
        for capacité, cause in sorted(servi.muets.items()):
            console.print(f"[yellow]Non servie — {capacité} : {cause}[/yellow]")
        console.print(
            "[dim]ecurie_catalog en donne le détail et la commande qui répare.[/dim]"
        )
    if registre.errors:
        # Le compte seul n'a jamais aidé personne à trouver le fichier fautif, et
        # ces erreurs sont précisément ce qui fait taire un outil.
        for issue in registre.errors[:5]:
            console.print(f"[red]registre — {issue.file} : {issue.message}[/red]")
        if len(registre.errors) > 5:
            console.print(
                f"[dim]… et {len(registre.errors) - 5} autre(s) — "
                "ecurie registry validate les donne toutes.[/dim]"
            )

    try:
        anyio.run(_servir, serveur, contexte)
    except KeyboardInterrupt:  # pragma: no cover — l'arrêt du client passe par là
        pass
    finally:
        contexte.close()


async def _servir(serveur, contexte: Contexte) -> None:
    async with stdio_server() as (lecture, écriture):
        await serveur.run(lecture, écriture, options(serveur))


def register(app: typer.Typer) -> None:
    """Ajoute `ecurie mcp` à la CLI racine."""
    app.command("mcp")(mcp_command)


def main() -> None:
    """Point d'entrée de `python -m ecurie_mcp`, pour les tests de bout en bout."""
    typer.run(mcp_command)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
