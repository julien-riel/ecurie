"""`/jobs` — soumettre un job, le suivre, récupérer ce qu'il a produit.

C'est la première surface d'écriture de l'API : tout le reste lit. Elle a attendu
que le superviseur vive dans ce processus (tâche 4.6), parce qu'un serveur qui
lance des jobs doit savoir lequel tourne, sur quel worker, et faire attendre le
suivant plutôt que de le laisser dans le backlog d'un socket.

Quatre décisions, et chacune évite un aller-retour à qui écrit le client.

**Ce qui empêchera le job d'aboutir est dit à la soumission, pas découvert
ensuite.** Un variant dont les poids ne sont pas téléchargés, un environnement
non synchronisé, une entrée que le contrat refuse : le job n'est pas créé, la
réponse porte la cause et la commande qui répare. Créer un job voué à l'échec
pour le faire échouer trois secondes plus tard n'apprend rien de plus et laisse
une trace inutile.

**Ce qui dépend de la mémoire ne l'est pas.** Une admission refusée ne peut être
tranchée qu'au moment de charger, après le tour de rôle : d'ici là, un autre job
a pu finir et libérer la place. Le job est donc créé, et il échoue avec la raison
lisible que le contrôle d'admission compose — « ce morceau de 30 s demanderait
24,2 Gio, au-delà des 17,8 disponibles ».

**Le flux rejoue depuis le début.** Un client qui ouvre `/events` après la
soumission voit d'abord où l'on en est, et non un flux muet jusqu'au premier
événement suivant. Il se termine par un `end` explicite : sans lui, `EventSource`
rouvrirait la connexion indéfiniment sur un job terminé.

**Les fichiers se lisent sur le disque, pas dans la table des jobs.** Le dossier
d'un job survit au redémarrage du serveur, sa table non. C'est ce qui permet de
rouvrir la sortie d'un job d'hier sans attendre la Bibliothèque (4.2).
"""

import asyncio
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any

from ecurie_core.capabilities import CapabilityContract
from ecurie_core.models import Model, Variant
from ecurie_runtime.runner import InputError, resolve_typed_input
from ecurie_runtime.supervisor import RefError, parse_ref
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse

from ecurie_api.deps import StateDep
from ecurie_api.jobs import Job, TooManyJobs, file_url
from ecurie_api.readiness import inspect_variant
from ecurie_api.schemas import JobDetail, JobOut, JobRequest
from ecurie_api.state import AppState

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Un identifiant de job est horodaté et suffixé (`runner.new_job_id`). Le
# vérifier avant de toucher au disque n'est pas de la coquetterie : il compose un
# chemin, et « ../.. » en est un aussi.
JOB_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")

# Le flux relit le journal du job à cette cadence. C'est un sondage dans le
# processus — un réveil de tâche, pas une requête HTTP — et le journal ne bouge
# qu'aux événements réels : une progression qui répète le même pourcentage n'en
# est pas un.
PAS_DU_FLUX_S = 0.1
BATTEMENT_S = 15.0  # commentaire SSE, pour qu'une connexion muette ne passe pas pour morte


def _resolve(state: AppState, ref: str) -> tuple[Model, Variant, str, CapabilityContract]:
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


def _job_dir(state: AppState, job_id: str) -> Path:
    if not JOB_ID.match(job_id):
        raise HTTPException(status_code=404, detail=f"identifiant de job invalide : {job_id!r}")
    return state.config.outputs_dir / job_id


def _lu_sur_disque(state: AppState, job_id: str) -> JobDetail | None:
    """Le job d'une session précédente, reconstitué depuis son manifeste.

    La table des jobs ne survit pas au redémarrage du serveur ; le dossier du
    job, si. Rendre 404 pour un job dont le manifeste est là serait faux — et
    c'est ce manifeste, pas la table, qui fait foi sur ce qui a été exécuté.
    """
    manifeste_file = _job_dir(state, job_id) / "manifest.json"
    if not manifeste_file.is_file():
        return None
    try:
        manifeste = json.loads(manifeste_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    sorties = _sorties_du_manifeste(manifeste)
    ok = bool(manifeste.get("ok"))
    return JobDetail(
        id=job_id,
        ref=f"{manifeste.get('model')}@{manifeste.get('variant')}",
        model=str(manifeste.get("model") or ""),
        variant=str(manifeste.get("variant") or ""),
        capability=str(manifeste.get("capability") or ""),
        state="done" if ok else "failed",
        submitted_at=str(manifeste.get("started_at") or ""),
        started_at=manifeste.get("started_at"),
        finished_at=manifeste.get("started_at"),
        progress=100 if ok else 0,
        error=manifeste.get("error"),
        warnings=list(manifeste.get("warnings") or []),
        metrics=dict(manifeste.get("metrics") or {}),
        output=dict(manifeste.get("output") or {}),
        outputs=sorties,
        files={clé: file_url(job_id, chemin) for clé, chemin in sorties.items()},
        input=dict(manifeste.get("input") or {}),
        seed=manifeste.get("seed"),
        manifest=manifeste,
    )


def _sorties_du_manifeste(manifeste: dict[str, Any]) -> dict[str, str]:
    """Les sorties qui sont des fichiers, d'après ce que le contrat en disait.

    Le manifeste porte les types de média du contrat au moment de l'exécution :
    c'est lui qui dit lesquelles de ces clés désignent un fichier, plutôt qu'une
    devinette sur l'extension ou un contrat qui a pu changer depuis.
    """
    attendues = (manifeste.get("contract") or {}).get("outputs") or {}
    sortie = manifeste.get("output") or {}
    trouvées: dict[str, str] = {}
    for clé in sorted(attendues):
        valeur = _suivre(sortie, clé)
        if isinstance(valeur, str) and valeur:
            trouvées[clé] = valeur
    return trouvées


def _suivre(sortie: dict[str, Any], chemin: str) -> Any:
    courant: Any = sortie
    for segment in chemin.split("."):
        if not isinstance(courant, dict) or segment not in courant:
            return None
        courant = courant[segment]
    return courant


def _media_type(state: AppState, job_id: str, chemin: str) -> str | None:
    """Le type de média que le contrat promettait pour ce fichier.

    Le contrat sait ce que `mimetypes` ignore — un `.glb` est un
    `model/gltf-binary`, et aucune table système ne le dit. On ne devine que
    lorsqu'il n'a rien à en dire.
    """
    manifeste_file = _job_dir(state, job_id) / "manifest.json"
    if manifeste_file.is_file():
        try:
            manifeste = json.loads(manifeste_file.read_text())
        except (OSError, json.JSONDecodeError):
            manifeste = {}
        types = (manifeste.get("contract") or {}).get("outputs") or {}
        for clé, valeur in _sorties_du_manifeste(manifeste).items():
            if valeur == chemin:
                return types.get(clé)
    deviné, _ = mimetypes.guess_type(chemin)
    return deviné


# --- soumission ----------------------------------------------------------------


@router.post(
    "",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Lancer un job",
)
def submit(state: StateDep, demande: JobRequest) -> JobOut:
    """Accepte le job et rend son identifiant : le travail part dans un fil.

    202 et non 201 : rien n'est encore produit. Ce que la réponse garantit, c'est
    qu'un job existe, qu'il porte cet identifiant, et qu'il est suivable.
    """
    model, variant, résolu, contract = _resolve(state, demande.ref)

    état = inspect_variant(state.root, state.config, model, variant, résolu)
    if not état.ready:
        raise HTTPException(
            status_code=409,
            detail={
                "ref": résolu,
                "message": f"{résolu} n'est pas exécutable en l'état",
                "blockers": état.blockers,
            },
        )

    try:
        # Le résultat ne sert pas ici : `run_job` refera la résolution, et c'est
        # elle qui écrira le manifeste. Ce qu'on lui demande, c'est de refuser
        # maintenant ce que le contrat refuserait dans trois secondes — un job
        # créé pour échouer aussitôt n'apprend rien et laisse une trace.
        resolve_typed_input(contract, variant, demande.input, seed=demande.seed)
    except InputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        job = state.jobs.submit(
            model, variant, contract, résolu, demande.input, seed=demande.seed
        )
    except TooManyJobs as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    return JobOut(**job.snapshot())


@router.get("/{job_id}", response_model=JobDetail, summary="État d'un job et son manifeste")
def detail(state: StateDep, job_id: str) -> JobDetail:
    job = state.jobs.get(job_id)
    if job is not None:
        return JobDetail(**job.snapshot(), manifest=job.manifest)
    depuis_le_disque = _lu_sur_disque(state, job_id)
    if depuis_le_disque is None:
        raise HTTPException(status_code=404, detail=f"job inconnu : {job_id}")
    return depuis_le_disque


@router.get("/{job_id}/events", summary="Suivre un job en direct (SSE)")
async def events(state: StateDep, job_id: str, request: Request) -> StreamingResponse:
    """Flux d'événements : un `job` à chaque changement, puis un `end`.

    Chaque événement porte l'état complet plutôt qu'un fragment : le client
    remplace ce qu'il affiche, et un événement perdu ne le laisse pas dans un
    état composé de moitiés.
    """
    job = state.jobs.get(job_id)
    if job is None:
        # Pas de repli sur le disque ici : un job terminé avant le démarrage du
        # serveur n'a plus rien à diffuser. Sa sortie se lit par `GET /jobs/{id}`.
        raise HTTPException(status_code=404, detail=f"job inconnu : {job_id}")

    return StreamingResponse(
        _flux(job, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # aucun tampon intermédiaire ne doit retenir le flux
        },
    )


async def _flux(job: Job, request: Request):
    index = 0
    dernier_signe = time.monotonic()
    while True:
        if await request.is_disconnected():
            return
        # L'état terminal se lit **avant** le journal, et l'ordre est tout ce qui
        # sépare ce flux d'une impasse. Dans l'autre sens : la lecture rend une
        # liste vide, le fil du job exécute `finish()` en entier — dernier
        # événement compris —, et le test de terminaison qui suit conclut « plus
        # rien à venir, plus rien à dire ». Le client reste alors sur l'avant
        # -dernier état, en cours à jamais, avec sa barre figée et sa sortie
        # jamais montrée. Dans cet ordre-ci l'inversion est sûre : `finish()`
        # pose l'état **puis** émet, tous deux sous le verrou, donc voir
        # « terminé » garantit que la lecture suivante attendra ce verrou et
        # verra l'événement.
        terminal = job.terminal
        nouveaux = job.events_since(index)
        for événement in nouveaux:
            index += 1
            yield _sse(événement["kind"], événement["job"])
            dernier_signe = time.monotonic()
        if not nouveaux and terminal:
            yield _sse("end", {"id": job.id, "state": job.state})
            return
        if time.monotonic() - dernier_signe > BATTEMENT_S:
            yield ": battement\n\n"
            dernier_signe = time.monotonic()
        await asyncio.sleep(PAS_DU_FLUX_S)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/{job_id}/files/{chemin:path}", summary="Un fichier produit par un job")
def fichier(state: StateDep, job_id: str, chemin: str) -> FileResponse:
    """Sert un fichier du dossier du job — sorties, entrées copiées, manifeste.

    `{chemin:path}` et non un simple nom : une séparation audio produit
    `tracks/vocals.wav`, et un client qui devrait recomposer ce chemin depuis une
    clé pointée referait le travail que `files` lui donne déjà fait.

    Tout ce qui sort du dossier est un 404 et non un 403 : la question « ce
    fichier existe-t-il ailleurs sur cette machine » n'a pas à recevoir de
    réponse, fût-elle négative.
    """
    racine = _job_dir(state, job_id).resolve()
    cible = (racine / chemin).resolve()
    if not cible.is_relative_to(racine) or not cible.is_file():
        raise HTTPException(status_code=404, detail=f"fichier introuvable : {chemin}")
    # Pas de `filename=` : il poserait un `Content-Disposition: attachment`, et
    # une image de sortie se regarde dans l'écran qui l'a demandée avant de se
    # télécharger.
    return FileResponse(
        cible, media_type=_media_type(state, job_id, chemin) or "application/octet-stream"
    )
