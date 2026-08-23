"""`/store` — la comptabilité disque, telle que `ecurie store` la donne.

Trois lectures, une même autorité : `compute_figures`, `generate_plan` et
`footprints` sur l'état observé du dernier scan. L'API ne recalcule rien de son
côté, sans quoi l'écran Parc et la CLI finiraient par annoncer deux gains
différents pour le même disque.

Aucune des trois ne scanne. Un scan écrit dans la base et prend des dizaines de
secondes : ce n'est pas une lecture, et ce n'est pas ce que fait un `GET`. Tant
qu'aucun scan n'a eu lieu, la réponse le dit — `figures: null`, `plan: null` — et
non des chiffres à zéro qui se liraient « le parc est vide ». C'est la même règle
que le poste « jamais utilisé » du plan de GC : inconnu n'est pas zéro.

**Aucune des trois n'écrit non plus, et c'est ce qui les sépare de la CLI.**
`ecurie store plan` dépose un fichier, `ecurie store tier` copie des
giga-octets ; ici, on montre ce qu'ils feraient et on rend la commande qui le
fait. Ce n'est pas de la prudence de façade : appliquer un plan re-vérifie
chaque sha256 **au moment de l'exécution** (CONCEPTION.md §4.3), et un déport
met des originaux en quarantaine. Deux opérations qui demandent une
confirmation, pas un survol de souris dans un onglet resté ouvert.
"""

import shutil
from dataclasses import asdict
from pathlib import Path

from ecurie_store.figures import cold_links, compute_figures, telemetry_is_conclusive
from ecurie_store.plan import REASON_LABELS, generate_plan
from ecurie_store.tier import footprints
from fastapi import APIRouter, Query

from ecurie_api.deps import StateDep
from ecurie_api.schemas import (
    ColdLinkOut,
    StorePlanResponse,
    StoreSummaryResponse,
    TelemetryOut,
    TieringResponse,
    TierVolumeOut,
    VariantFootprintOut,
)

router = APIRouter(prefix="/store", tags=["parc"])

# Les phrases rendues à l'utilisateur ne portent **pas** d'accents graves. La
# convention du dépôt tient sur les blockers depuis le v0.4 — « mesurer avec
# ecurie bench <ref> » et non « avec `ecurie bench` » — et elle a une raison :
# ces chaînes finissent dans un navigateur, qui affiche l'accent grave tel quel.
# L'écran Parc l'a montré en une capture, là où trois suites de tests cherchant
# des sous-chaînes n'y voyaient rien.
SANS_SCAN = (
    "aucun état observé — lancer ecurie store scan pour remplir "
    "~/.ecurie/state.db (lecture seule, rien n'est modifié sur le disque scanné)"
)


def _perime(last_apply: str | None, last_scan: str | None) -> str | None:
    """Un plan appliqué après le dernier scan décrit un disque qui n'existe plus."""
    if not (last_apply and last_scan and last_apply > last_scan):
        return None
    return (
        f"un plan a été appliqué depuis le dernier scan ({last_apply[:19]}) : "
        "ces chiffres décrivent le disque d'avant — relancer ecurie store scan"
    )


@router.get("/summary", response_model=StoreSummaryResponse, summary="Occupation du parc")
def summary(
    state: StateDep,
    unused_after_days: int = Query(
        default=90, ge=1, le=3650, description="Seuil du poste « variants jamais utilisés »."
    ),
) -> StoreSummaryResponse:
    db = state.open_db()
    try:
        records = db.locations()
        last_scan = db.get_kv("last_scan_at")
        last_apply = db.get_kv("last_apply_at")
        premier_run = db.first_run_at()
        last_runs = db.last_run_by_variant()
    finally:
        db.close()

    # Un scan qui n'a rien trouvé n'est pas un scan qui n'a pas eu lieu : le
    # premier rend des chiffres à zéro, qui sont vrais ; le second ne peut rien
    # dire. Seul `last_scan_at` distingue les deux, et confondre les deux ferait
    # afficher « lancer un scan » à qui vient d'en lancer un.
    if not records and last_scan is None:
        return StoreSummaryResponse(scanned=False, hint=SANS_SCAN)

    télémétrie = telemetry_is_conclusive(premier_run, unused_after_days)
    figures = compute_figures(
        records,
        last_runs=last_runs,
        telemetry=télémétrie,
        unused_after_days=unused_after_days,
    )

    payload = asdict(figures)
    # Les propriétés calculées ne sortent pas d'`asdict` : sans elles, l'UI
    # referait l'addition des quatre postes, et une divergence d'arrondi
    # afficherait un total qui ne serait pas celui de la CLI.
    payload["recoverable"]["total_known_bytes"] = figures.recoverable.total_known_bytes
    payload["cold_unavailable"] = [asdict(c) for c in figures.cold_unavailable]

    avis = _perime(last_apply, last_scan)
    return StoreSummaryResponse(
        scanned=True,
        last_scan_at=last_scan,
        stale=avis is not None,
        figures=payload,
        telemetry=TelemetryOut(
            conclusive=télémétrie,
            first_run_at=premier_run,
            unused_after_days=unused_after_days,
        ),
        hint=avis,
    )


@router.get("/plan", response_model=StorePlanResponse, summary="Plan de récupération, à blanc")
def plan(
    state: StateDep,
    unused_after_days: int = Query(default=90, ge=1, le=3650),
    verified_only: bool = Query(
        default=False,
        description="Ne dédupliquer que sur des sha256 relus, jamais sur un hash annoncé.",
    ),
) -> StorePlanResponse:
    """Ce que `ecurie store plan` proposerait, sans écrire le fichier qu'il écrit.

    Le plan rendu est **complet** — actions, empreintes, écartés — et non un
    résumé : c'est le document qu'on relit avant de laisser un outil toucher
    trente giga-octets, et un écran qui n'en montrerait que le total demanderait
    de faire confiance sans donner de quoi juger.

    Il porte un `plan_id` neuf à chaque appel, comme n'importe quelle génération.
    Rien ne le rend rejouable pour autant : `apply` re-vérifie chaque fichier sur
    le disque au moment d'agir, et refuse tout ce qui a bougé depuis le scan.
    """
    db = state.open_db()
    try:
        records = db.locations()
        last_scan = db.get_kv("last_scan_at")
        last_apply = db.get_kv("last_apply_at")
        scan_id = db.get_kv("scan_id")
        premier_run = db.first_run_at()
        last_runs = db.last_run_by_variant()
    finally:
        db.close()

    if not records and last_scan is None:
        return StorePlanResponse(scanned=False, hint=SANS_SCAN)

    document = generate_plan(
        records,
        scan_id=scan_id,
        last_runs=last_runs,
        telemetry=telemetry_is_conclusive(premier_run, unused_after_days),
        unused_after_days=unused_after_days,
        verified_only=verified_only,
    )

    options = " --verified-only" if verified_only else ""
    if unused_after_days != 90:
        options += f" --unused-after-days {unused_after_days}"
    return StorePlanResponse(
        scanned=True,
        last_scan_at=last_scan,
        stale=_perime(last_apply, last_scan) is not None,
        plan=document,
        labels=dict(REASON_LABELS),
        command=f"ecurie store plan{options}",
        hint=_perime(last_apply, last_scan),
    )


@router.get("/tiering", response_model=TieringResponse, summary="Volumes, variants déportés, poids")
def tiering(state: StateDep) -> TieringResponse:
    """Où l'on peut déporter, ce qui l'est déjà, et ce qui pèserait le plus lourd.

    Les volumes viennent de la **configuration** et non du disque observé : un
    volume déclaré mais démonté doit apparaître, justement parce qu'il est
    démonté — c'est ce qui explique qu'un variant froid soit indisponible. Sa
    place libre est alors `null`, et non zéro.
    """
    db = state.open_db()
    try:
        records = db.locations()
        last_scan = db.get_kv("last_scan_at")
        last_apply = db.get_kv("last_apply_at")
    finally:
        db.close()

    volumes = [_volume(chemin) for chemin in state.config.tier_volumes]
    if not records and last_scan is None:
        return TieringResponse(scanned=False, volumes=volumes, hint=SANS_SCAN)

    avis = _perime(last_apply, last_scan)
    if avis is None and not volumes:
        avis = (
            "aucun tier_volumes dans ~/.ecurie/config.toml : le déclarer fait scanner "
            "le volume externe et signale les variants froids quand il est démonté"
        )
    return TieringResponse(
        scanned=True,
        last_scan_at=last_scan,
        volumes=volumes,
        cold=[ColdLinkOut(**asdict(lien)) for lien in cold_links(records)],
        variants=[
            VariantFootprintOut(**asdict(f), tierable=f.tierable) for f in footprints(records)
        ],
        hint=avis,
    )


def _volume(chemin: Path) -> TierVolumeOut:
    dossier = Path(chemin).expanduser()
    try:
        usage = shutil.disk_usage(dossier)
    except OSError:
        # Un volume externe débranché : c'est un état normal du tiering, pas une
        # panne de la route. `null` dit « inconnu » là où 0 dirait « plein ».
        return TierVolumeOut(path=str(dossier), mounted=False)
    return TierVolumeOut(
        path=str(dossier),
        mounted=dossier.is_dir(),
        free_bytes=usage.free,
        total_bytes=usage.total,
    )
