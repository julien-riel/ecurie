"""Commandes `ecurie store …` — greffées sur la CLI de ecurie-core."""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from ecurie_core.config import Config, load_config
from ecurie_core.registry import find_root, load_registry
from rich.console import Console
from rich.table import Table

from ecurie_store.apply import APPLIED, FAILED, apply_plan
from ecurie_store.db import StateDB
from ecurie_store.figures import (
    Figures,
    compute_figures,
    fmt_bytes,
    telemetry_is_conclusive,
)
from ecurie_store.hashing import dedup_candidates, verify_locations
from ecurie_store.plan import REASON_LABELS, generate_plan, load_plan, write_plan
from ecurie_store.scan import run_scan
from ecurie_store.tier import TierError, tier_variant, yaml_patch
from ecurie_store.trash import empty_trash, list_trash, mount_point

store_app = typer.Typer(no_args_is_help=True, help="Comptabilité disque du parc.")
trash_app = typer.Typer(no_args_is_help=True, help="Quarantaine — rien ne s'efface tout seul.")
store_app.add_typer(trash_app, name="trash")
console = Console()


def _open_db(config: Config) -> StateDB:
    return StateDB(config.state_db)


def _trash_roots(config: Config) -> list[Path]:
    """Toutes les quarantaines possibles, pas seulement celle de ~/.ecurie.

    Un fichier sur un volume externe est mis en quarantaine sur ce volume (sans
    quoi le déplacement serait une copie de plusieurs gigaoctets) : une commande
    `trash` qui ne regarde que la racine principale déclarerait vide une
    quarantaine qui retient des octets.
    """
    principale = config.trash_dir
    principale.mkdir(parents=True, exist_ok=True)
    device = principale.stat().st_dev

    racines = [principale]
    chemins = [config.scan.hf_hub, config.scan.ollama, config.scan.lmstudio]
    chemins += [*config.scan.comfy, *config.scan.declared, *config.tier_volumes]
    for chemin in chemins:
        if chemin is None:
            continue
        chemin = chemin.expanduser()
        if not chemin.exists():  # volume démonté : rien à y chercher, et pas une erreur
            continue
        if chemin.stat().st_dev != device:
            # Même règle que `trash_root_for`, sans quoi la CLI chercherait la
            # quarantaine ailleurs que là où le module la pose.
            racines.append(mount_point(chemin) / ".ecurie-trash")
    vues: list[Path] = []
    for racine in racines:
        if racine not in vues:
            vues.append(racine)
    return vues


def _observed(config: Config):
    """État observé du dernier scan, ou sortie nette si la base est vide."""
    db = _open_db(config)
    records = db.locations()
    if not records:
        db.close()
        console.print("aucun état observé — lancer d'abord [bold]ecurie store scan[/bold]")
        raise typer.Exit(code=1)
    return db, records


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

    db = _open_db(config)
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
    if report.cached_hashes:
        console.print(f"{report.cached_hashes} hash vérifié(s) réinjecté(s) du cache")
    for avertissement in report.warnings:
        console.print(
            f"[yellow]dépôt écarté par le gestionnaire, absent de la comptabilité : "
            f"{avertissement}[/yellow]"
        )
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
    unused_after_days: Annotated[
        int, typer.Option("--unused-after-days", help="Seuil du poste « jamais utilisé ».")
    ] = 90,
) -> None:
    """Les trois chiffres : apparent, réel unique, récupérable."""
    config = load_config()
    db, records = _observed(config)
    try:
        last_scan = db.get_kv("last_scan_at")
        last_apply = db.get_kv("last_apply_at")
        premier_run = db.first_run_at()
        last_runs = db.last_run_by_variant()
    finally:
        db.close()

    telemetry = telemetry_is_conclusive(premier_run, unused_after_days)

    périmé = bool(last_apply and last_scan and last_apply > last_scan)

    figures = compute_figures(
        records,
        last_runs=last_runs,
        telemetry=telemetry,
        unused_after_days=unused_after_days,
    )
    if json_out:
        payload = asdict(figures)
        # Les propriétés calculées ne sortent pas d'asdict : les agents de veille
        # (v0.6) lisent ce JSON, ils ne doivent pas avoir à refaire l'addition.
        payload["recoverable"]["total_known_bytes"] = figures.recoverable.total_known_bytes
        payload["cold_unavailable"] = [asdict(c) for c in figures.cold_unavailable]
        payload["last_scan_at"] = last_scan
        payload["stale"] = périmé
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if périmé:
        console.print(
            f"[yellow]Un plan a été appliqué depuis le dernier scan ({last_apply[:19]}) : "
            "ces chiffres décrivent le disque d'avant. Relancer "
            "[bold]ecurie store scan[/bold].[/yellow]"
        )
    _render(figures, last_scan, premier_run)


def _pourquoi_inconnu(premier_run: str | None) -> str:
    if premier_run is None:
        return "inconnu (aucune exécution notée)"
    return f"inconnu (télémétrie trop jeune, depuis le {premier_run[:10]})"


def _render(figures: Figures, last_scan: str | None, premier_run: str | None = None) -> None:
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
        fmt_bytes(rec.unused_bytes) if rec.unused_known else _pourquoi_inconnu(premier_run),
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

    if figures.cold:
        cold = Table(title="Variants déportés (tiering)", pad_edge=False)
        cold.add_column("Lien")
        cold.add_column("Cible")
        cold.add_column("État")
        for link in figures.cold:
            état = "[green]disponible[/green]" if link.available else "[red]volume absent[/red]"
            cold.add_row(link.path, link.target, état)
        console.print(cold)

    if figures.mismatched:
        console.print(
            f"[red]{len(figures.mismatched)} fichier(s) dont le contenu ne correspond pas "
            "au hash annoncé par leur gestionnaire — ne rien planifier dessus :[/red]\n  "
            + "\n  ".join(figures.mismatched[:5])
        )
    if figures.unverified_count:
        console.print(
            f"Sans sha256 : {figures.unverified_count} fichier(s), "
            f"{fmt_bytes(figures.unverified_bytes)} — comptés chacun pour eux-mêmes "
            "([bold]ecurie store verify[/bold] tranche)"
        )
    if figures.announced_bytes:
        console.print(
            f"Hash annoncé non relu : {fmt_bytes(figures.announced_bytes)} — "
            "suffisant pour compter, jamais pour effacer"
        )
    console.print(
        f"Hors registre : {figures.unresolved_count} fichier(s), "
        f"{fmt_bytes(figures.unresolved_bytes)}"
        + (f" — dernier scan : {last_scan}" if last_scan else "")
    )


@store_app.command("verify")
def store_verify(
    paths: Annotated[
        list[str] | None, typer.Argument(help="Chemins précis (défaut : les candidats à la dédup).")
    ] = None,
    all_files: Annotated[
        bool, typer.Option("--all", help="Relire tout le parc — coûteux, mais sans angle mort.")
    ] = False,
    variant: Annotated[
        str | None, typer.Option("--variant", help="Restreindre à un variant « model@variant ».")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Ignorer le cache de hachage.")] = False,
) -> None:
    """Calcule les sha256 manquants et confirme les hash annoncés."""
    sélecteurs = [bool(paths), bool(variant), all_files]
    if sum(sélecteurs) > 1:
        # Se taire sur le sélecteur ignoré ferait croire à une relecture complète
        # alors qu'un seul fichier aurait été lu.
        raise typer.BadParameter("choisir un seul de : chemins, --variant, --all")

    config = load_config()
    db, records = _observed(config)
    try:
        if paths:
            voulus = {str(Path(p).resolve()) for p in paths} | set(paths)
            cibles = [r for r in records if r.path in voulus]
            inconnus = voulus - {r.path for r in cibles}
        elif variant:
            cibles = [r for r in records if variant in r.variant_refs]
            inconnus = set()
        elif all_files:
            cibles = [r for r in records if r.link_kind != "symlink"]
            inconnus = set()
        else:
            cibles = dedup_candidates(records)
            inconnus = set()

        if inconnus:
            console.print(f"[yellow]hors état observé, ignoré(s) : {', '.join(sorted(inconnus))}")
        if not cibles:
            if variant:
                console.print(f"{variant} : aucun fichier observé pour ce variant")
            elif paths or all_files:
                console.print("rien à vérifier")
            else:
                console.print(
                    "rien à vérifier — aucun fichier sans hash ne partage sa taille avec "
                    "un autre, donc aucun doublon possible à départager"
                )
            return

        total = sum(r.size for r in cibles)
        console.print(f"{len(cibles)} fichier(s), {fmt_bytes(total)} à relire…")
        résultats = verify_locations(db, cibles, force=force)
    finally:
        db.close()

    table = Table(pad_edge=False)
    table.add_column("État", no_wrap=True)
    table.add_column("sha256", no_wrap=True)
    table.add_column("Fichier")
    problèmes = 0
    for r in résultats:
        if r.mismatch:
            état, problèmes = "[red]DIVERGENT[/red]", problèmes + 1
        elif r.missing or r.error:
            état, problèmes = f"[yellow]{r.error}[/yellow]", problèmes + 1
        elif r.changed:
            état = "[yellow]bougé depuis le scan[/yellow]"
        elif r.from_cache:
            état = "[dim]du cache[/dim]"
        else:
            état = "[green]vérifié[/green]"
        table.add_row(état, (r.sha256 or "—")[:16], r.path)
    console.print(table)
    calculés = sum(1 for r in résultats if r.sha256 and not r.from_cache)
    console.print(
        f"{calculés} hash calculé(s), {len(résultats) - calculés} relu(s) du cache, "
        f"{problèmes} problème(s)"
    )
    if any(r.mismatch for r in résultats):
        console.print(
            "[red]Un hash annoncé ne correspond pas au contenu : ne rien déduire de ces "
            "fichiers avant d'avoir compris pourquoi.[/red]"
        )
        raise typer.Exit(code=1)


@store_app.command("plan")
def store_plan(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Où écrire le plan (défaut : ~/.ecurie/plans/)."),
    ] = None,
    unused_after_days: Annotated[int, typer.Option("--unused-after-days")] = 90,
    verified_only: Annotated[
        bool,
        typer.Option("--verified-only", help="Ne dédupliquer que sur des sha256 relus."),
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Écrire le plan sur la sortie.")] = False,
) -> None:
    """Génère un plan de récupération — affiché, écrit, jamais exécuté."""
    config = load_config()
    db, records = _observed(config)
    try:
        plan = generate_plan(
            records,
            scan_id=db.get_kv("scan_id"),
            last_runs=db.last_run_by_variant(),
            telemetry=telemetry_is_conclusive(db.first_run_at(), unused_after_days),
            unused_after_days=unused_after_days,
            verified_only=verified_only,
        )
    finally:
        db.close()

    destination = output or (
        config.plans_dir / f"plan-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json"
    )
    write_plan(plan, destination)

    if json_out:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return

    table = Table(title="Gain par poste", pad_edge=False)
    table.add_column("Poste")
    table.add_column("Actions", justify="right")
    table.add_column("Gain", justify="right")
    par_poste: dict[str, int] = {}
    for action in plan["actions"]:
        par_poste[action["reason"]] = par_poste.get(action["reason"], 0) + 1
    for reason, octets in sorted(plan["by_reason"].items(), key=lambda kv: -kv[1]):
        table.add_row(
            REASON_LABELS.get(reason, reason), str(par_poste.get(reason, 0)), fmt_bytes(octets)
        )
    console.print(table)
    for ignoré in plan["ignored"]:
        console.print(f"[yellow]écarté ({ignoré['reason']}) : {len(ignoré['paths'])} chemin(s)")
    console.print(
        f"Total récupérable : [bold]{fmt_bytes(plan['total_bytes_reclaimed'])}[/bold]\n"
        f"Plan écrit : {destination}\n"
        f"Exécuter : [bold]ecurie store apply {destination}[/bold]"
    )


@store_app.command("apply")
def store_apply(
    plan_path: Annotated[Path, typer.Argument(help="Plan produit par `ecurie store plan`.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Tout vérifier, ne rien déplacer.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Ne pas demander confirmation.")] = False,
) -> None:
    """Exécute un plan : chaque action est revérifiée sur le disque avant d'agir."""
    plan = load_plan(plan_path)
    total = plan.get("total_bytes_reclaimed", 0)
    console.print(
        f"Plan {plan['plan_id'][:8]}… — {len(plan['actions'])} action(s), "
        f"{fmt_bytes(total)} annoncés"
    )
    if not dry_run and not yes:
        typer.confirm("Appliquer ce plan ?", abort=True)

    report = apply_plan(plan, dry_run=dry_run)

    table = Table(pad_edge=False)
    table.add_column("État", no_wrap=True)
    table.add_column("Action", no_wrap=True)
    table.add_column("Gain", justify="right")
    table.add_column("Détail")
    for outcome in report.outcomes:
        couleur = {APPLIED: "green", FAILED: "red"}.get(outcome.status, "yellow")
        table.add_row(
            f"[{couleur}]{outcome.status}[/{couleur}]",
            f"{outcome.kind} · {outcome.reason}",
            fmt_bytes(outcome.bytes_reclaimed) if outcome.bytes_reclaimed else "—",
            outcome.message,
        )
    console.print(table)
    console.print(
        f"Récupéré : [bold]{fmt_bytes(report.applied_bytes)}[/bold] sur {fmt_bytes(total)} annoncés"
        + (f" — journal : {report.journal_path}" if report.journal_path else " (à blanc)")
    )
    if report.quarantined_bytes:
        console.print(
            f"  dont {fmt_bytes(report.freed_bytes)} rendus au disque et "
            f"{fmt_bytes(report.quarantined_bytes)} en quarantaine — cette part n'est "
            "libérée qu'après [bold]ecurie store trash empty[/bold]"
        )
    if report.measured_bytes is not None:
        écart = report.measured_bytes - report.freed_bytes
        marge = abs(écart) <= max(report.freed_bytes // 100, 1 << 20)
        couleur = "green" if marge else "yellow"
        console.print(
            f"  place libre relevée sur les volumes : [{couleur}]"
            f"{fmt_bytes(report.measured_bytes)}[/{couleur}] — mesure, non déduction"
            + ("" if marge else " ; l'écart mérite un coup d'œil (clone APFS ? autre écriture ?)")
        )
    if not dry_run:
        # L'état observé décrit maintenant un disque qui n'existe plus : on le note,
        # pour que `status` ne présente pas d'anciens chiffres avec assurance.
        db = _open_db(load_config())
        try:
            db.set_kv("last_apply_at", datetime.now(UTC).isoformat())
        finally:
            db.close()
        console.print("Relancer [bold]ecurie store scan[/bold] pour rafraîchir l'état observé.")


@store_app.command("tier")
def store_tier(
    ref: Annotated[str, typer.Argument(help="Variant « model@variant » à déporter.")],
    dest: Annotated[Path, typer.Argument(help="Volume de destination, ex. /Volumes/Parc.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Afficher sans rien déplacer.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Ne pas demander confirmation.")] = False,
) -> None:
    """Déporte un variant sur un volume externe et laisse un lien symbolique."""
    config = load_config()
    if config.tier_volumes:
        autorisé = any(
            str(dest.resolve()).startswith(str(Path(v).expanduser().resolve()))
            for v in config.tier_volumes
        )
        if not autorisé:
            console.print(
                f"[red]{dest} n'est pas dans `tier_volumes` de ~/.ecurie/config.toml[/red]"
            )
            raise typer.Exit(code=1)
    else:
        console.print(
            "[yellow]aucun `tier_volumes` en config : l'ajouter fera scanner ce volume "
            "et signalera les variants froids quand il est démonté.[/yellow]"
        )

    db, records = _observed(config)
    try:
        cibles = [r for r in records if ref in r.variant_refs and r.link_kind != "symlink"]
        octets = sum(r.size for r in cibles)
        console.print(f"{ref} : {len(cibles)} fichier(s), {fmt_bytes(octets)}")
        if not dry_run and not yes and cibles:
            typer.confirm(f"Déporter vers {dest} ?", abort=True)
        résultats = tier_variant(records, ref, dest, db=db, dry_run=dry_run)
    except TierError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        db.close()

    table = Table(pad_edge=False)
    table.add_column("Source")
    table.add_column("Destination")
    table.add_column("Taille", justify="right")
    for r in résultats:
        table.add_row(r.source, r.dest + (" [dim](déjà là)[/dim]" if r.reused else ""),
                      fmt_bytes(r.size))
    console.print(table)
    if dry_run:
        console.print("[yellow]à blanc — rien n'a bougé[/yellow]")
        return
    console.print(
        "Patch à committer dans le registre (l'outil ne touche pas au manifeste) :\n"
        f"[bold]{yaml_patch(ref)}[/bold]"
    )


@trash_app.command("list")
def trash_list(
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Ce qui attend en quarantaine, et comment le remettre en place."""
    entries = list_trash(roots=_trash_roots(load_config()))
    if json_out:
        print(json.dumps([e.manifest for e in entries], ensure_ascii=False, indent=2))
        return
    if not entries:
        console.print("quarantaine vide")
        return
    table = Table(pad_edge=False)
    table.add_column("Date", no_wrap=True)
    table.add_column("Motif", no_wrap=True)
    table.add_column("Taille", justify="right")
    table.add_column("Origine")
    for entry in entries:
        table.add_row(entry.trashed_at[:19], entry.reason, fmt_bytes(entry.size), entry.origin)
    console.print(table)
    console.print(
        f"{len(entries)} entrée(s), {fmt_bytes(sum(e.size for e in entries))}.\n"
        f"Restaurer à la main : [bold]{entries[0].restore_command}[/bold]"
    )


@trash_app.command("empty")
def trash_empty(
    older_than: Annotated[
        int | None,
        typer.Option("--older-than", help="Ne vider que les entrées de plus de N jours."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Ne pas demander confirmation.")] = False,
) -> None:
    """Efface définitivement la quarantaine. Le seul endroit qui supprime."""
    racines = _trash_roots(load_config())
    entries = list_trash(roots=racines)
    if not entries:
        console.print("quarantaine vide")
        return
    console.print(f"{len(entries)} entrée(s), {fmt_bytes(sum(e.size for e in entries))}")
    if not yes:
        typer.confirm("Effacer définitivement ? Rien ne sera restaurable.", abort=True)
    supprimées, octets = empty_trash(older_than_days=older_than, roots=racines)
    console.print(f"{supprimées} entrée(s) effacée(s), {fmt_bytes(octets)} libérés")
