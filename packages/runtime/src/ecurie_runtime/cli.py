"""Commandes d'exécution : `ecurie env`, `run`, `ps`, `unload`, `bench`.

Greffées sur la CLI de `ecurie-core`, comme celles de `ecurie store`.

Un principe traverse ces commandes : **rien ne se charge sans que l'utilisateur
puisse savoir ce que ça déchargera**. `ecurie ps` montre le budget et ce que le
prochain job coûterait, `ecurie run` annonce ses évictions, `ecurie bench` dit
qu'il vide le parc. Le mécanisme est dans `admission.py` ; ici, c'est sa mise en
mots.
"""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer
from ecurie_core.capabilities import CapabilityContract
from ecurie_core.config import Config, load_config
from ecurie_core.format import fmt_memory
from ecurie_core.models import Model, Variant
from ecurie_core.registry import Registry, find_root, load_registry
from ecurie_store.db import StateDB
from ecurie_store.figures import fmt_bytes
from ecurie_store.pull import (
    PullError,
    plan_pull,
    resolve_revision,
    revision_patch,
    run_pull,
)
from rich.console import Console
from rich.table import Table

from ecurie_runtime.admission import Admission
from ecurie_runtime.bench import run_bench, write_measurement, yaml_patch
from ecurie_runtime.budget import detect_budget
from ecurie_runtime.envs import EnvError, env_for, list_envs, sync_command, sync_env
from ecurie_runtime.runner import InputError, parse_assignments, run_job
from ecurie_runtime.supervisor import AdmissionRefused, RefError, Supervisor, parse_ref
from ecurie_runtime.worker import Timeouts

env_app = typer.Typer(no_args_is_help=True, help="Environnements isolés des runtimes.")
console = Console()


class _SansStatut:
    """Substitut muet de `console.status` pour les sorties machine.

    Une commande `--json` ne doit émettre que du JSON : un fil d'avancement en
    français mêlé au flux le rend inexploitable par ce qui le lit — un script,
    un agent de veille, l'API du v0.4.
    """

    def __enter__(self) -> "_SansStatut":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def update(self, *args: object, **kwargs: object) -> None:
        return None


def _statut(message: str, *, muet: bool):
    return _SansStatut() if muet else console.status(message)


# --- socle commun ------------------------------------------------------------


def _root() -> Path:
    root = find_root(Path.cwd())
    if root is None:
        raise typer.BadParameter(
            "aucun registre Écurie trouvé depuis le dossier courant "
            "(attendu : registry/schema/model.schema.json)"
        )
    return root


def _context() -> tuple[Path, Registry, Config]:
    root = _root()
    registry = load_registry(root)
    if registry.errors:
        console.print(
            f"[red]{len(registry.errors)} erreur(s) dans le registre — "
            "corriger avant d'exécuter ([bold]ecurie registry validate[/bold])[/red]"
        )
        raise typer.Exit(code=1)
    return root, registry, load_config()


def _supervisor(root: Path, registry: Registry, config: Config) -> Supervisor:
    return Supervisor(root, registry, config, timeouts=Timeouts())


def _resolve(registry: Registry, ref: str) -> tuple[Model, Variant, str, CapabilityContract]:
    try:
        model, variant, résolu = parse_ref(registry, ref)
    except RefError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    contract = registry.capabilities.get(model.capability)
    if contract is None:
        console.print(f"[red]{résolu} : aucun contrat pour la capacité {model.capability}[/red]")
        raise typer.Exit(code=1)
    return model, variant, résolu, contract


# --- ecurie env --------------------------------------------------------------


@env_app.command("list")
def env_list() -> None:
    """État des environnements isolés déclarés sous runtimes/."""
    root = _root()
    envs = list_envs(root)
    if not envs:
        console.print(
            "aucun environnement sous runtimes/ — en créer un par famille de runtime "
            "(CONCEPTION.md §5.3)"
        )
        return
    table = Table(pad_edge=False)
    table.add_column("Environnement")
    table.add_column("État")
    table.add_column("Interpréteur")
    table.add_column("À lire")
    notices = False
    for env in envs:
        couleur = "green" if env.synced else "yellow"
        notice = env.readme
        notices = notices or notice is not None
        table.add_row(
            env.name,
            f"[{couleur}]{env.status}[/{couleur}]",
            str(env.python) if env.synced else env.repair_hint,
            str(notice.relative_to(root)) if notice else "",
        )
    console.print(table)
    if notices:
        # « prêt » ne parle que du venv. Un env qui porte une notice a des étapes
        # que `uv sync` ne fait pas — du code amont à copier, par exemple.
        console.print(
            "[yellow]Un environnement qui porte une notice demande des étapes que "
            "`ecurie env sync` ne fait pas : la lire avant de lancer un job.[/yellow]"
        )


@env_app.command("sync")
def env_sync(
    names: Annotated[
        list[str] | None, typer.Argument(help="Environnements à synchroniser (défaut : tous).")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Afficher les commandes sans les lancer.")
    ] = False,
) -> None:
    """Reconstruit les venv des runtimes avec `uv sync`. Demande du réseau."""
    root = _root()
    cibles = [env_for(root, n) for n in names] if names else list_envs(root)
    if not cibles:
        console.print("rien à synchroniser")
        return

    for env in cibles:
        if not env.declared:
            console.print(f"[red]{env.name} : {env.repair_hint}[/red]")
            continue
        commande = " ".join(sync_command(env))
        if dry_run:
            console.print(f"{env.name} : [bold]{commande}[/bold]")
            continue
        console.print(f"{env.name} : {commande}")
        with console.status(f"synchronisation de {env.name}…"):
            résultat = sync_env(env, repo_root=root)
        if résultat.returncode == 0:
            console.print(f"  [green]prêt[/green] — {env.python}")
        else:
            # La sortie d'uv dit précisément quelle résolution a échoué ; la
            # résumer ferait perdre la seule information utile.
            console.print(f"  [red]échec (code {résultat.returncode})[/red]")
            console.print(résultat.stderr.strip()[-2000:] or résultat.stdout.strip()[-2000:])


# --- ecurie ps ---------------------------------------------------------------


def ps_command(
    json_out: Annotated[bool, typer.Option("--json", help="Sortie machine.")] = False,
    ref: Annotated[
        str | None,
        typer.Option("--for", help="Simuler l'admission de ce variant sans rien charger."),
    ] = None,
    ping: Annotated[
        bool,
        typer.Option("--ping", help="Interroger chaque résident pour voir s'il répond encore."),
    ] = False,
) -> None:
    """Modèles résidents, budget mémoire, et ce que coûterait le prochain job.

    Sans `--ping`, on ne dérange personne : un worker en plein job ne lit pas son
    socket, et l'interroger ferait attendre dix secondes pour apprendre qu'il
    travaille. `--ping` sert quand un résident est soupçonné d'être bloqué —
    il occupe alors du budget sans rien rendre, et `ecurie unload --force` tranche.
    """
    root = _root()
    registry = load_registry(root)
    config = load_config()
    supervisor = _supervisor(root, registry, config)
    orphelins = supervisor.prune()
    résidents = supervisor.residents()
    budget = supervisor.budget
    occupé = sum(e.peak_bytes for e in résidents)
    santé = supervisor.health() if ping else {}

    simulation: Admission | None = None
    if ref:
        model, variant, résolu, _ = _resolve(registry, ref)
        simulation = supervisor.simulate(résolu, supervisor.peak_bytes(variant))

    if json_out:
        payload: dict[str, Any] = {
            "budget_bytes": budget.bytes,
            "budget_source": budget.source,
            "used_bytes": occupé,
            "free_bytes": budget.bytes - occupé,
            "residents": [asdict(e) for e in résidents],
            "reaped": orphelins,
        }
        if santé:
            payload["healthy"] = santé
        if simulation is not None:
            payload["admission"] = asdict(simulation)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    console.print(
        f"Budget mémoire unifiée : [bold]{fmt_memory(budget.bytes)}[/bold] "
        f"({budget.source}) — occupé {fmt_memory(occupé)}, "
        f"libre [bold]{fmt_memory(budget.bytes - occupé)}[/bold]"
    )
    if not résidents:
        console.print("aucun modèle résident")
    else:
        table = Table(pad_edge=False)
        table.add_column("Variant")
        table.add_column("Pic", justify="right")
        table.add_column("PID", justify="right")
        table.add_column("Warmup", justify="right")
        table.add_column("Chargé depuis", no_wrap=True)
        table.add_column("État")
        for e in résidents:
            if e.busy:
                # Sans cette ligne, un job refusé « parce qu'un résident est
                # occupé » renverrait vers un `ps` qui n'en dit rien.
                état = f"[yellow]job en cours[/yellow] (pid {e.busy_by})"
            elif e.pinned:
                état = "[cyan]épinglé[/cyan]"
            else:
                état = "libérable"
            if santé.get(e.ref) is False:
                état = f"[red]ne répond pas[/red] — {état}"
            table.add_row(
                e.ref,
                fmt_memory(e.peak_bytes),
                str(e.pid),
                f"{e.warmup_ms} ms",
                e.loaded_at[:19],
                état,
            )
        console.print(table)

    if orphelins:
        console.print(
            f"[yellow]{len(orphelins)} worker(s) devenu(s) injoignable(s), arrêté(s) et "
            f"retiré(s) du budget : {', '.join(orphelins)}[/yellow]"
        )
    if santé and not all(santé.values()):
        muets = ", ".join(ref for ref, ok in santé.items() if not ok)
        console.print(
            f"[red]{muets} : aucune réponse. Un job en cours l'explique ; sinon le worker "
            f"est bloqué et retient sa mémoire — ecurie unload {muets.split(',')[0]} --force"
            "[/red]"
        )
    if simulation is not None:
        _render_admission(ref or "", simulation)


def _render_admission(ref: str, admission: Admission) -> None:
    if admission.admitted:
        détail = f" — déchargerait : {', '.join(admission.evict)}" if admission.evict else ""
        console.print(
            f"[green]{ref} passerait[/green] : {admission.reason}{détail} "
            f"(marge résiduelle {fmt_memory(max(0, admission.headroom_bytes))})"
        )
    else:
        console.print(f"[red]{ref} ne passerait pas[/red] : {admission.reason}")


# --- ecurie unload -----------------------------------------------------------


def unload_command(
    ref: Annotated[str | None, typer.Argument(help="Variant à décharger.")] = None,
    all_residents: Annotated[bool, typer.Option("--all", help="Tout décharger.")] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Décharger même ce qui est épinglé ou en plein job."),
    ] = False,
) -> None:
    """Rend la mémoire d'un modèle résident.

    Un modèle sur lequel un job tourne n'est pas déchargé sans `--force` : le
    tuer ne rendrait pas la mémoire tout de suite, cela détruirait un travail en
    cours — celui d'un autre terminal, ou celui de l'Atelier.
    """
    root = _root()
    registry = load_registry(root)
    supervisor = _supervisor(root, registry, load_config())

    if all_residents:
        déchargés = supervisor.unload_all(force=force)
        détail = f" : {', '.join(déchargés)}" if déchargés else ""
        console.print(f"{len(déchargés)} modèle(s) déchargé(s){détail}")
        for restant in supervisor.residents():
            # Dire ce qui a été épargné, et pourquoi : « 0 modèle déchargé » sur
            # un parc plein laisserait croire à une commande sans effet.
            raison = f"job en cours (pid {restant.busy_by})" if restant.busy else "épinglé"
            console.print(f"[yellow]{restant.ref} gardé — {raison}[/yellow]")
        return
    if not ref:
        raise typer.BadParameter("préciser un variant, ou --all")

    cible = ref if "@" in ref else _resolve(registry, ref)[2]
    try:
        if supervisor.unload(cible):
            console.print(f"{cible} déchargé")
        else:
            console.print(f"{cible} n'était pas résident")
    except AdmissionRefused as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


# --- ecurie run --------------------------------------------------------------


def run_command(
    ref: Annotated[str, typer.Argument(help="Variant « model@variant », ou « model ».")],
    params: Annotated[
        list[str] | None,
        typer.Option("-p", "--param", help="Paramètre du contrat : clé=valeur (répétable)."),
    ] = None,
    seed: Annotated[
        int | None, typer.Option("--seed", help="Graine, si la capacité en a une.")
    ] = None,
    pin: Annotated[
        bool, typer.Option("--pin", help="Garder ce modèle résident : il ne sera pas évincé.")
    ] = False,
    json_out: Annotated[
        bool, typer.Option("--json", help="Manifeste du job sur la sortie.")
    ] = False,
    show: Annotated[
        bool, typer.Option("--show/--no-show", help="Ouvrir la sortie à la fin (macOS).")
    ] = False,
) -> None:
    """Exécute un job : admission mémoire, worker, sortie en fichiers, ligne de télémétrie."""
    root, registry, config = _context()
    model, variant, résolu, contract = _resolve(registry, ref)
    supervisor = _supervisor(root, registry, config)

    try:
        assignations = parse_assignments(params)
    except InputError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    db = StateDB(config.state_db)
    try:
        with _statut(f"{résolu} : admission…", muet=json_out) as statut:

            def progression(pct: int, note: str) -> None:
                statut.update(f"{résolu} : {pct} % {note}")

            def en_file(job: str) -> None:
                # Un modèle sert un job à la fois. Sans cette ligne, une commande
                # lancée pendant qu'un job tourne ailleurs — l'Atelier, un autre
                # terminal — paraîtrait avoir cessé de répondre.
                statut.update(f"{résolu} : un job occupe déjà ce modèle ({job}) — en file")

            try:
                outcome = run_job(
                    supervisor,
                    model,
                    variant,
                    contract,
                    assignations,
                    seed=seed,
                    db=db,
                    pin=pin,
                    on_progress=progression,
                    on_wait=en_file,
                )
            except InputError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
            except AdmissionRefused as exc:
                console.print(f"[red]admission refusée : {exc}[/red]")
                raise typer.Exit(code=1) from exc
            except Exception as exc:  # noqa: BLE001 — env absent, poids manquants, worker mort
                console.print(f"[red]{type(exc).__name__} : {exc}[/red]")
                raise typer.Exit(code=1) from exc
    finally:
        db.close()

    if json_out:
        print(json.dumps(outcome.manifest, ensure_ascii=False, indent=2))
        if not outcome.ok:
            raise typer.Exit(code=1)
        return

    if outcome.evicted:
        console.print(f"déchargé pour faire de la place : {', '.join(outcome.evicted)}")
    if outcome.reused:
        console.print("[dim]modèle déjà résident — warmup non repayé[/dim]")
    for avertissement in outcome.warnings:
        console.print(f"[yellow]{avertissement}[/yellow]")

    if not outcome.ok:
        console.print(f"[red]échec : {outcome.error}[/red]")
        console.print(f"journal du job : {outcome.job_dir / 'manifest.json'}")
        raise typer.Exit(code=1)

    table = Table(pad_edge=False, show_header=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Job", outcome.job_id)
    table.add_row("Durée", f"{outcome.duration_ms / 1000:.1f} s")
    for clé, chemin in outcome.files.items():
        table.add_row(clé, str(chemin))
    for clé, valeur in (outcome.result.metrics if outcome.result else {}).items():
        if clé != "duration_ms":
            table.add_row(clé, str(valeur))
    console.print(table)
    console.print(f"Manifeste (rejouable) : {outcome.job_dir / 'manifest.json'}")

    if show:
        _open_files(list(outcome.files.values()))


def _open_files(paths: list[Path]) -> None:
    import subprocess  # noqa: PLC0415 — confort local, jamais sur un chemin critique

    for chemin in paths:
        if chemin.exists():
            subprocess.run(["open", str(chemin)], check=False)


# --- ecurie bench ------------------------------------------------------------


def bench_command(
    ref: Annotated[str, typer.Argument(help="Variant à mesurer.")],
    write: Annotated[
        bool, typer.Option("--write/--no-write", help="Écrire registry/measurements/.")
    ] = True,
    json_out: Annotated[
        bool, typer.Option("--json", help="Mesure complète sur la sortie.")
    ] = False,
) -> None:
    """Mesure un variant, parc vidé, et affiche le profil à committer."""
    root, registry, config = _context()
    model, variant, résolu, contract = _resolve(registry, ref)
    supervisor = _supervisor(root, registry, config)

    db = StateDB(config.state_db)
    try:
        records = db.locations()
    finally:
        db.close()

    if not json_out:
        console.print(
            f"[yellow]Le banc d'essai décharge tout le parc avant de mesurer {résolu} — "
            "un profil pris en concurrence ne mesure pas le modèle.[/yellow]"
        )
    try:
        with _statut(f"{résolu} : chargement…", muet=json_out) as statut:

            def progression(pct: int, note: str) -> None:
                statut.update(f"{résolu} : {pct} % {note}")

            report = run_bench(
                supervisor,
                model,
                variant,
                contract,
                records=records,
                on_progress=progression,
            )
    except AdmissionRefused as exc:
        console.print(f"[red]admission refusée : {exc}[/red]")
        raise typer.Exit(code=1) from exc
    except (EnvError, Exception) as exc:  # noqa: BLE001
        console.print(f"[red]{type(exc).__name__} : {exc}[/red]")
        raise typer.Exit(code=1) from exc

    if json_out:
        print(json.dumps(report.document(), ensure_ascii=False, indent=2))
        if not report.ok:
            raise typer.Exit(code=1)
        return

    table = Table(title=f"Profil mesuré — {résolu}", pad_edge=False, show_header=False)
    table.add_column(style="bold")
    table.add_column(justify="right")
    table.add_row("Disque", fmt_bytes(report.profile.get("disk_bytes", 0)))
    table.add_row(
        "Pic mémoire unifiée", fmt_memory(report.profile.get("peak_unified_memory_bytes", 0))
    )
    table.add_row("Warmup", f"{report.profile.get('warmup_ms', 0)} ms")
    for clé in ("latency_ms_p50", "rtf", "throughput"):
        if clé in report.profile:
            table.add_row(clé, str(report.profile[clé]))
    table.add_row("Mesuré sur", report.measured_on)
    console.print(table)

    cas = Table(title="Charge type", pad_edge=False)
    cas.add_column("Cas")
    cas.add_column("Durée", justify="right")
    cas.add_column("État")
    for c in report.cases:
        cas.add_row(
            c.id,
            f"{c.duration_ms / 1000:.2f} s",
            "[green]ok[/green]" if c.ok else f"[red]{c.error}[/red]",
        )
    console.print(cas)
    for avertissement in report.warnings:
        console.print(f"[yellow]{avertissement}[/yellow]")

    if write and report.ok:
        destination = write_measurement(root, report)
        console.print(f"Mesure écrite : {destination}")
    elif write:
        console.print("[yellow]mesure non écrite : au moins un cas a échoué[/yellow]")

    console.print(
        "Patch à committer dans le manifeste (l'outil n'y touche pas) :\n"
        f"[bold]{yaml_patch(report)}[/bold]"
    )
    if not report.ok:
        raise typer.Exit(code=1)


# --- ecurie pull -------------------------------------------------------------


def pull_command(
    ref: Annotated[str, typer.Argument(help="Variant « model@variant » à télécharger.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Tout chiffrer, ne rien écrire.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Retélécharger un variant déjà complet.")
    ] = False,
    ignore_disk_guard: Annotated[
        bool,
        typer.Option("--ignore-disk-guard", help="Passer outre la garde d'espace libre."),
    ] = False,
    resolve: Annotated[
        bool,
        typer.Option(
            "--resolve",
            help="Ne rien télécharger : résoudre le sha de main et afficher le patch à committer.",
        ),
    ] = False,
) -> None:
    """Télécharge les poids d'un variant à sa révision épinglée."""
    root, registry, config = _context()
    model, variant, résolu, _ = _resolve(registry, ref)

    if resolve:
        if not variant.source.repo:
            console.print(f"[red]{résolu} : pas de dépôt Hugging Face au manifeste[/red]")
            raise typer.Exit(code=1)
        try:
            info = resolve_revision(variant.source.repo)
        except PullError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(
            f"{info.repo} : {info.sha} ({info.last_modified or 'date inconnue'}), "
            f"licence {info.license or 'non déclarée'}"
        )
        console.print(
            "Patch à committer (l'outil ne touche pas au registre) :\n"
            f"[bold]{revision_patch(résolu, info)}[/bold]"
        )
        return

    try:
        plan = plan_pull(config, variant, ref=résolu)
    except PullError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"{résolu} — {plan.repo}@{plan.revision[:12]}", pad_edge=False,
                  show_header=False)
    table.add_column(style="bold")
    table.add_column(justify="right")
    table.add_row("Poids du variant", fmt_bytes(plan.selected_bytes))
    if plan.excluded_bytes:
        table.add_row("Écarté par allow_patterns", fmt_bytes(plan.excluded_bytes))
    table.add_row("Déjà sur le disque", fmt_bytes(plan.presence.bytes_on_disk))
    table.add_row("Reste à télécharger", fmt_bytes(plan.download_bytes))
    console.print(table)
    console.print(plan.guard.message)

    if dry_run:
        console.print("[yellow]à blanc — rien n'a été téléchargé[/yellow]")
        return

    try:
        with console.status(f"téléchargement de {résolu}…"):
            résultat = run_pull(
                config,
                variant,
                ref=résolu,
                plan=plan,
                force=force,
                ignore_disk_guard=ignore_disk_guard,
            )
    except PullError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(résultat.note)
    if résultat.downloaded:
        console.print(
            f"{fmt_bytes(résultat.bytes_downloaded)} écrits — {résultat.path}\n"
            f"Mesurer le profil : [bold]ecurie bench {résolu}[/bold]"
        )


# --- greffe ------------------------------------------------------------------


def register(app: typer.Typer) -> None:
    """Ajoute les commandes d'exécution à la CLI racine."""
    app.add_typer(env_app, name="env")
    app.command("ps")(ps_command)
    app.command("unload")(unload_command)
    app.command("pull")(pull_command)
    app.command("run")(run_command)
    app.command("bench")(bench_command)


def budget_summary(config: Config, root: Path | None = None) -> str:
    budget = detect_budget(config, repo_root=root)
    return f"{fmt_memory(budget.bytes)} ({budget.source})"
