"""L'application FastAPI et ce qui l'entoure : origines autorisées, erreurs, index.

Deux choix méritent d'être expliqués, parce qu'ils touchent à la sécurité d'une
machine qui héberge un parc de modèles.

**Le serveur écoute sur la boucle locale, et il faut le vouloir pour en sortir.**
`ecurie serve` refuse une adresse non locale sans `--expose`. Ce n'est pas de la
prudence de principe : l'API dit où sont les poids sur le disque, ce que la
machine a en mémoire, et lance des jobs. Publiée sur un réseau, elle
donne tout cela à qui passe.

**Les origines CORS sont énumérées, jamais `*`.** L'UI de développement tourne
sur Vite (5173) et parle à l'API sur un autre port : sans CORS, rien ne marche.
Mais `allow_origins=["*"]` laisserait n'importe quelle page ouverte dans le
navigateur interroger le parc — la boucle locale ne protège pas de cela. On
autorise donc les origines locales connues, sans cookies (`allow_credentials`
reste faux), et on ajoute les autres à la main.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ecurie_api import __version__
from ecurie_api.routers import jobs as jobs_router
from ecurie_api.routers import registry as registry_router
from ecurie_api.routers import runtime as runtime_router
from ecurie_api.routers import store as store_router
from ecurie_api.routers import uploads as uploads_router
from ecurie_api.state import AppState

# Vite en développement, et le même port en 127.0.0.1 — les deux graphies sont
# des origines distinctes pour un navigateur.
DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

DESCRIPTION = """
Le parc Écurie : ce qu'il contient, ce qu'il a en mémoire, ce qu'il exécute
(v0.4, tâche 4.1).

* `/registry/capabilities` — les contrats qui engendrent les formulaires
* `/registry/models` — les manifestes, avec ce que le disque en dit
* `/store/summary` — les trois chiffres d'occupation
* `/runtime/residents` — mémoire occupée, budget, et ce que coûterait un job
* `/runtime/admission` — le même calcul pour une entrée précise
* `/jobs` — lancer, suivre en direct (SSE), récupérer les fichiers produits
* `/uploads` — déposer un fichier et recevoir le chemin local qu'un champ attend

Les cinq premières ne chargent rien, n'écrivent rien, ne déplacent aucun octet.
`/jobs` est la première surface d'écriture, et elle a attendu que le superviseur
vive dans ce processus (tâche 4.6) : un serveur qui lance des jobs doit savoir
lequel tourne, sur quel worker, et faire attendre le suivant.

`/uploads` est la seconde, et la dernière prévue. Elle n'existe pas pour rendre
l'API utilisable depuis une autre machine — elle ne l'est toujours pas, le
chemin rendu est local — mais parce qu'une image choisie dans une page, une
photo de la caméra et un son du micro n'ont **jamais** eu de chemin à saisir.
"""


def create_app(state: AppState, *, cors_origins: Sequence[str] | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        # Les workers survivent au serveur — c'est ce qu'être résident veut dire.
        # Ce qui ne doit pas lui survivre, c'est l'occupation qu'il publiait : un
        # `ecurie ps` lancé juste après annoncerait des jobs qui n'existent plus.
        state.close()

    app = FastAPI(
        title="Écurie",
        version=__version__,
        description=DESCRIPTION,
        summary="Parc de modèles locaux — registre, disque, mémoire.",
        lifespan=lifespan,
    )
    app.state.ecurie = state

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEFAULT_CORS_ORIGINS if cors_origins is None else cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(registry_router.router)
    app.include_router(store_router.router)
    app.include_router(runtime_router.router)
    app.include_router(jobs_router.router)
    app.include_router(uploads_router.router)

    @app.get("/", tags=["service"], summary="Où l'on est, et sur quel dépôt")
    def index() -> dict:
        registre = state.registry()
        return {
            "service": "ecurie",
            "version": __version__,
            "root": str(state.root),
            "home": str(state.config.state_db.parent),
            "models": len(registre.models),
            "capabilities": len(registre.capabilities),
            "registry_errors": len(registre.errors),
            "registry_warnings": len(registre.warnings),
            "docs": "/docs",
        }

    @app.get("/healthz", tags=["service"], summary="Le serveur répond-il")
    def healthz() -> dict:
        return {"ok": True}

    @app.exception_handler(Exception)
    def erreur_inattendue(request: Request, exc: Exception) -> JSONResponse:
        """Un imprévu se dit, il ne se réduit pas à « Internal Server Error ».

        C'est la même règle que dans la CLI, qui affiche `TypeError : …` plutôt
        qu'un échec muet. L'API n'écoute que la boucle locale : le nom de
        l'exception ne fuite vers personne, et sans lui il faudrait aller lire le
        journal du serveur pour apprendre ce que le navigateur vient d'échouer à
        faire.
        """
        return JSONResponse(
            status_code=500,
            content={"detail": f"{type(exc).__name__} : {exc}", "path": request.url.path},
        )

    return app
