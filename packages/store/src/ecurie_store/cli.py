"""Commandes `ecurie store …` — greffées sur la CLI de ecurie-core."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer
from ecurie_core.config import load_config
from ecurie_core.registry import find_root, load_registry
from rich.console import Console
from rich.table import Table

from ecurie_store.db import StateDB
from ecurie_store.figures import Figures, compute_figures, fmt_bytes
from ecurie_store.scan import run_scan

store_app = typer.Typer(no_args_is_help=True, help="Comptabilité disque du parc.")
console = Console()


@store_app.command("scan")
def store_scan() -> None:
    """Scanne les gestionnaires configurés et met à jour l'état observé."""
    config = load_config()
    registry = None
    root = find_root(Path.cwd())
    if root is not None:
        registry = load_registry(root)
    else:
        console.print(
            "[yellow]registre introuvable depuis le dossier courant — "
            "scan sans rattachement aux variants[/yellow]"
        )

    db = StateDB(config.state_db)
    try:
        report = run_scan(config, db, registry)
    finally:
        db.close()

    table = Table(pad_edge=False)
    table.add_column("Gestionnaire")
    table.add_column("Fichiers", justify="right")
    for manager, count in sorted(report.counts.items()):
        table.add_row(manager, str(count))
    for manager, reason in sorted(report.skipped.items()):
        table.add_row(manager, f"[yellow]{reason}[/yellow]")
    console.print(table)
    if not report.counts:
        console.print(
            "[yellow]aucun gestionnaire scanné — vérifier [scan] dans "
            "~/.ecurie/config.toml[/yellow]"
        )


@store_app.command("status")
def store_status(
    json_out: Annotated[
        bool, typer.Option("--json", help="Sortie machine (pour les agents de veille).")
    ] = False,
) -> None:
    """Les trois chiffres : apparent, réel unique, récupérable."""
    config = load_config()
    db = StateDB(config.state_db)
    try:
        records = db.locations()
        last_scan = db.get_kv("last_scan_at")
    finally:
        db.close()

    if not records:
        console.print("aucun état observé — lancer d'abord [bold]ecurie store scan[/bold]")
        raise typer.Exit(code=1)

    figures = compute_figures(records)
    if json_out:
        payload = asdict(figures)
        payload["last_scan_at"] = last_scan
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    _render(figures, last_scan)


def _render(figures: Figures, last_scan: str | None) -> None:
    rec = figures.recoverable
    main = Table(title="Trois chiffres", pad_edge=False, show_header=False)
    main.add_column(style="bold")
    main.add_column(justify="right")
    main.add_row("Apparent (somme naïve)", fmt_bytes(figures.apparent_bytes))
    main.add_row("Réel unique (par contenu)", fmt_bytes(figures.real_unique_bytes))
    main.add_row("Récupérable (connu)", fmt_bytes(rec.total_known_bytes))
    main.add_row("  duplication inter-gestionnaires", fmt_bytes(rec.duplication_bytes))
    main.add_row("  révisions HF obsolètes", fmt_bytes(rec.hf_stale_bytes))
    main.add_row("  blobs orphelins", fmt_bytes(rec.orphan_bytes))
    main.add_row(
        "  variants jamais utilisés",
        fmt_bytes(rec.unused_bytes) if rec.unused_known else "inconnu (télémétrie au v0.2)",
    )
    console.print(main)

    managers = Table(title="Par gestionnaire", pad_edge=False)
    managers.add_column("Gestionnaire")
    managers.add_column("Apparent", justify="right")
    managers.add_column("Fichiers", justify="right")
    for manager, (size, count) in figures.by_manager.items():
        managers.add_row(manager, fmt_bytes(size), str(count))
    console.print(managers)

    if figures.duplicates:
        dups = Table(title="Duplications (10 premières)", pad_edge=False)
        dups.add_column("Contenu")
        dups.add_column("Taille", justify="right")
        dups.add_column("Récupérable", justify="right")
        dups.add_column("Chemins")
        for group in figures.duplicates[:10]:
            dups.add_row(
                group.sha256[:12] + "…",
                fmt_bytes(group.size),
                fmt_bytes(group.reclaimable_bytes),
                "\n".join(group.paths),
            )
        console.print(dups)

    console.print(
        f"Hors registre : {figures.unresolved_count} fichier(s), "
        f"{fmt_bytes(figures.unresolved_bytes)}"
        + (f" — dernier scan : {last_scan}" if last_scan else "")
    )
