"""`/store/summary` — les trois chiffres, tels que `ecurie store status` les donne.

Même calcul, même autorité : `compute_figures` sur l'état observé du dernier
scan. L'API ne recalcule rien de son côté, sans quoi l'écran Parc et la CLI
finiraient par annoncer deux gains différents pour le même disque.

Elle ne scanne pas non plus. Un scan écrit dans la base et prend des dizaines de
secondes : ce n'est pas une lecture, et ce n'est pas ce que fait un `GET`. Tant
qu'aucun scan n'a eu lieu, la réponse le dit — `figures: null`, et non des
chiffres à zéro qui se liraient « le parc est vide ». C'est la même règle que le
poste « jamais utilisé » du plan de GC : inconnu n'est pas zéro.
"""

from dataclasses import asdict

from ecurie_store.figures import compute_figures, telemetry_is_conclusive
from fastapi import APIRouter, Query

from ecurie_api.deps import StateDep
from ecurie_api.schemas import StoreSummaryResponse, TelemetryOut

router = APIRouter(prefix="/store", tags=["parc"])


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
        return StoreSummaryResponse(
            scanned=False,
            hint=(
                "aucun état observé — lancer `ecurie store scan` pour remplir "
                "~/.ecurie/state.db (lecture seule, rien n'est modifié sur le disque scanné)"
            ),
        )

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

    périmé = bool(last_apply and last_scan and last_apply > last_scan)
    return StoreSummaryResponse(
        scanned=True,
        last_scan_at=last_scan,
        stale=périmé,
        figures=payload,
        telemetry=TelemetryOut(
            conclusive=télémétrie,
            first_run_at=premier_run,
            unused_after_days=unused_after_days,
        ),
        hint=(
            f"un plan a été appliqué depuis le dernier scan ({(last_apply or '')[:19]}) : "
            "ces chiffres décrivent le disque d'avant — relancer `ecurie store scan`"
        )
        if périmé
        else None,
    )
