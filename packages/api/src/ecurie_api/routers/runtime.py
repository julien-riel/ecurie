"""`/runtime` — qui occupe la mémoire, et ce que coûterait le prochain job.

Ces routes ne chargent ni ne déchargent rien. La différence avec `ecurie ps` est
délibérée et vaut d'être dite : la commande, elle, appelle `prune()`, qui **tue**
les workers devenus injoignables. C'est légitime pour une commande qu'on tape ;
ça ne l'est pas pour un `GET` que l'UI rafraîchit toutes les deux secondes, où un
processus mourrait sans que personne ait rien demandé. Les entrées périmées sont
donc rapportées dans `stale`, avec la seule distinction qui compte : `holds_memory`
dit si le processus vit encore, auquel cas il retient toujours ses gigaoctets sans
plus figurer au budget.

`?for=` et `POST /runtime/admission` répondent à la même question par deux
chemins. Le premier chiffre un variant seul, ce qui suffit tant que son pic ne
dépend pas de l'entrée ; le second reçoit l'entrée envisagée et fait donc parler
`peak_scaling` — trente secondes de musique coûtent le double de quinze, et un
bandeau qui l'ignorerait annoncerait 13,8 Gio pour un job qui en demande 23,9.
C'est ce que réclame le §4 du plan : l'admission doit être interrogeable **avant**
de soumettre, et un refus doit se lire.
"""

from ecurie_core.capabilities import CapabilityContract
from ecurie_core.models import Model, Variant
from ecurie_runtime.readiness import inspect_variant
from ecurie_runtime.runner import InputError, resolve_typed_input
from ecurie_runtime.supervisor import RefError, Supervisor, parse_ref
from ecurie_runtime.worker import pid_alive
from fastapi import APIRouter, HTTPException, Query

from ecurie_api.deps import StateDep
from ecurie_api.projection import admission_fields
from ecurie_api.schemas import (
    AdmissionOut,
    AdmissionRequest,
    AdmissionResponse,
    PolicyOut,
    ResidentOut,
    ResidentsResponse,
    StaleWorkerOut,
)
from ecurie_api.state import AppState

router = APIRouter(prefix="/runtime", tags=["exécution"])


def _resolve_ref(state: AppState, ref: str) -> tuple[Model, Variant, str, CapabilityContract]:
    registry = state.registry()
    try:
        model, variant, résolu = parse_ref(registry, ref)
    except RefError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    contract = registry.capabilities.get(model.capability)
    if contract is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{résolu} : aucun contrat registry/capabilities/{model.capability}.json — "
                "le registre est incohérent, voir /registry/capabilities"
            ),
        )
    return model, variant, résolu, contract


def _admission_out(
    supervisor: Supervisor,
    ref: str,
    variant: Variant,
    values: dict | None = None,
    *,
    overcommit: bool = False,
) -> AdmissionOut:
    pic = supervisor.peak_bytes(variant, values)
    décision = supervisor.simulate(ref, pic, overcommit=overcommit)
    return AdmissionOut(
        ref=ref,
        peak_bytes=pic,
        peak_note=supervisor.peak_note(variant, values),
        **admission_fields(décision),
    )


@router.get("/residents", response_model=ResidentsResponse, summary="Modèles résidents et budget")
def residents(
    state: StateDep,
    for_ref: str | None = Query(
        default=None,
        alias="for",
        description="Simuler l'admission de ce variant, sans rien charger ni décharger.",
    ),
) -> ResidentsResponse:
    supervisor = state.supervisor()
    entrées = supervisor.residents()
    budget = supervisor.budget
    politique = supervisor.policy
    occupé = sum(e.peak_bytes for e in entrées)

    admission = None
    if for_ref:
        _, variant, résolu, _ = _resolve_ref(state, for_ref)
        admission = _admission_out(supervisor, résolu, variant)

    return ResidentsResponse(
        budget_bytes=budget.bytes,
        budget_source=budget.source,
        budget_measured=budget.measured,
        used_bytes=occupé,
        free_bytes=budget.bytes - occupé,
        policy=PolicyOut(
            budget_bytes=budget.bytes,
            max_heavy_resident=politique.max_heavy_resident,
            heavy_threshold_bytes=politique.heavy_threshold_bytes,
        ),
        residents=[
            ResidentOut(
                ref=e.ref,
                pid=e.pid,
                peak_bytes=e.peak_bytes,
                heavy=e.peak_bytes > politique.heavy_threshold_bytes,
                runtime=e.runtime,
                env=e.env,
                loaded_at=e.loaded_at,
                last_used=e.last_used,
                pinned=e.pinned,
                busy=e.busy,
                busy_by=e.busy_by,
                busy_since=e.busy_since,
                warmup_ms=e.warmup_ms,
                options=e.options,
                socket=e.socket,
                log=e.log,
            )
            for e in entrées
        ],
        stale=[
            StaleWorkerOut(
                ref=e.ref, pid=e.pid, holds_memory=pid_alive(e.pid), socket=e.socket
            )
            for e in supervisor.registry_file.stale()
        ],
        admission=admission,
    )


@router.post("/admission", response_model=AdmissionResponse, summary="Ce que coûterait ce job")
def admission(state: StateDep, demande: AdmissionRequest) -> AdmissionResponse:
    """Chiffre un job envisagé : pic attendu, évictions, marge — sans rien charger.

    C'est une lecture malgré le verbe : rien n'est écrit, rien n'est chargé, et
    deux appels identiques donnent la même réponse. `POST` parce que la question
    porte une entrée complète, que l'on n'écrit pas dans une chaîne de requête.
    """
    model, variant, résolu, contract = _resolve_ref(state, demande.ref)
    try:
        # Non stricte : le bandeau interroge pendant la saisie, et un champ encore
        # vide doit rendre un chiffre plutôt qu'une erreur. Un paramètre hors
        # contrat, lui, lève — et c'est un 422, pas une saisie à corriger.
        entrée = resolve_typed_input(
            contract, variant, demande.input, seed=demande.seed, strict=False
        )
    except InputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    état = inspect_variant(state.root, state.config, model, variant, résolu)
    return AdmissionResponse(
        ref=résolu,
        admission=_admission_out(
            state.supervisor(), résolu, variant, entrée.values, overcommit=demande.overcommit
        ),
        input=entrée.values,
        input_errors=entrée.errors,
        ready=état.ready,
        blockers=état.blockers,
    )
