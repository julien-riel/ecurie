"""`/uploads` — déposer un fichier et recevoir le chemin local qu'un champ attend.

La deuxième surface d'écriture de l'API, après `/jobs`, et la dernière prévue.
Elle existe parce que la note laissée au test des routes figées est arrivée à
échéance : *« aucune route de téléversement, alors que dix champs du registre
attendent un fichier. Sans conséquence tant que le navigateur et le serveur
partagent la machine — le champ porte un chemin local — et à reprendre le jour
où ce ne sera plus vrai. »*

Ce n'est pas la portabilité réseau qui l'a rendue nécessaire, c'est **l'absence
de chemin**. Une image choisie dans une page web, une photo prise par la caméra,
un son capté par le micro : aucun de ces trois n'a jamais existé sur le disque,
et aucun n'aurait de chemin à saisir même si l'on tapait vite. Le serveur écrit
le contenu, et rend le chemin qu'il vient de créer — le champ du formulaire
reste ce qu'il était, une chaîne que le worker ouvrira.

Ce que la route ne fait pas, et ne fera pas : elle ne relit rien, ne liste rien,
ne supprime rien sur demande. Le sas se purge tout seul (`ecurie_api.uploads`) et
ce qui compte pour la reproductibilité est la copie que `runner.stage_inputs`
place dans le dossier du job, avec son sha256. Une route de lecture ferait de
`~/.ecurie/uploads` une bibliothèque parallèle à celle du v0.5, avec deux
vérités sur ce qui a servi d'entrée.
"""

import mimetypes
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from ecurie_api import uploads as sas
from ecurie_api.deps import StateDep
from ecurie_api.schemas import UploadOut
from ecurie_api.uploads import UploadError, UploadTooLarge, accepted_media_types, deposer

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post(
    "",
    response_model=UploadOut,
    status_code=status.HTTP_201_CREATED,
    summary="Déposer un fichier et obtenir son chemin local",
)
def deposer_fichier(state: StateDep, file: Annotated[UploadFile, File()]) -> UploadOut:
    """Écrit le fichier dans le sas et rend le chemin à poser dans le champ.

    Le type de média vient du client quand il l'annonce, sinon de l'extension du
    nom : `MediaRecorder` déclare toujours le sien, un glisser-déposer depuis le
    Finder pas toujours. Il est ensuite confronté à ce que les contrats du
    registre acceptent — c'est le registre qui décide, pas une liste écrite dans
    ce fichier.
    """
    deviné, _ = mimetypes.guess_type(file.filename or "")
    media_type = file.content_type or deviné or "application/octet-stream"
    motifs = accepted_media_types(state.registry().capabilities.values())

    try:
        dépôt = deposer(
            file.file,
            dossier=state.config.uploads_dir,
            nom_origine=file.filename or "",
            media_type=media_type,
            motifs=motifs,
            # Nommée ici plutôt que laissée au défaut de `deposer` : la borne se
            # lit à l'endroit où son dépassement devient un code HTTP, et non
            # trois fichiers plus loin.
            max_bytes=sas.MAX_BYTES,
        )
    except UploadTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)
        ) from exc
    except UploadError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except OSError as exc:
        # Disque plein, dossier non inscriptible : la cause est lisible et la
        # réparation est côté machine, pas côté requête.
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail=f"dépôt impossible dans {state.config.uploads_dir} : {exc}",
        ) from exc

    return UploadOut(
        path=str(dépôt.path),
        name=dépôt.name,
        media_type=dépôt.media_type,
        size_bytes=dépôt.size_bytes,
    )
