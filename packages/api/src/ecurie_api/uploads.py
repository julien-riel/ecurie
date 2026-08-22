"""Le sas d'entrée : ce que le navigateur dépose, et le chemin local qu'il en obtient.

Jusqu'ici, un champ `x-ui: "file"` se remplissait à la main. Le widget du front
le disait sans détour : *« le navigateur ne donne pas le chemin réel d'un fichier
choisi — seulement son nom et son contenu »*. C'était vrai, et ce l'est toujours ;
ce qui a changé, c'est qu'on en tire la conséquence au lieu de s'y arrêter. Le
navigateur a le **contenu** — il l'a aussi pour une image glissée depuis une page
web, pour une photo prise par la caméra, pour un son capté par le micro, et
aucun de ces trois-là n'a jamais eu de chemin sur le disque. Le serveur l'écrit,
et rend le chemin qu'il vient de créer.

Quatre décisions gouvernent ce module, et chacune se paie si on la manque.

**Le sas n'est pas une bibliothèque.** Ce qui est déposé ici sert à lancer un
job ; `runner.stage_inputs` en recopie aussitôt le contenu dans le dossier du
job, avec son sha256, et c'est ce dossier-là qui fait foi pour la
reproductibilité. Garder l'original indéfiniment ferait grossir `~/.ecurie` d'une
copie par capture sans que rien ne les relise jamais. Les dépôts plus vieux que
`RETENTION` sont donc balayés à chaque nouveau dépôt — pas par une tâche de fond,
qui serait un fil de plus à surveiller pour un `unlink()`.

**Le type est vérifié contre le registre, pas contre une liste écrite ici.** Les
contrats déclarent ce que leurs champs fichier acceptent — `image/*`, `audio/*`,
`video/*`, `application/pdf` —, et c'est exactement la question posée. Une liste
en dur divergerait au premier contrat ajouté, ce qui est le défaut que ce projet
signale ailleurs entre `measurements/` et un bloc `profile:`.

**Le nom d'origine ne compose jamais le chemin.** Il est réduit à ce qui peut
tenir dans un nom de fichier, précédé d'un identifiant unique, et jamais employé
seul : deux captures s'appellent toutes les deux `enregistrement.wav`, et
« ../.. » est un nom de fichier acceptable pour qui l'envoie.

**La taille est comptée pendant l'écriture, pas avant.** `Content-Length` est
déclaratif ; le seul chiffre auquel on peut se fier est celui des octets déjà
écrits. Au-delà de la borne, le fichier partiel est supprimé — laisser 4 Gio de
moitié de vidéo dans le sas serait une façon coûteuse de refuser.
"""

import mimetypes
import re
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from ecurie_core.capabilities import CapabilityContract

# Le plus gros dépôt plausible est une vidéo à transcrire ou à décrire ; les
# deux capacités vidéo du parc lisent des fichiers de plusieurs centaines de
# mégaoctets. Un gigaoctet laisse la marge, et reste très en dessous de ce qu'un
# disque plein coûterait. Ce n'est pas un réglage de config : l'API n'écoute que
# la boucle locale, et le seul client est l'UI de cette machine.
MAX_BYTES = 1 << 30

# Au-delà, un dépôt n'a plus de raison d'être : ou bien son job a tourné et son
# contenu est dans le dossier du job, ou bien il n'a jamais servi.
RETENTION_S = 7 * 24 * 3600

# Écrit par blocs : un `await file.read()` sur une vidéo d'un gigaoctet la
# chargerait entière en mémoire, dans le processus qui tient aussi le budget des
# modèles.
BLOC = 1 << 20

# Ce qu'on garde du nom d'origine. Tout le reste — séparateurs, points de tête,
# caractères de contrôle, unicode décoratif — devient un tiret bas.
_INTERDITS = re.compile(r"[^A-Za-z0-9._-]+")

# Ce que la table système ignore, et qu'on ne peut pas se permettre d'ignorer.
# `mimetypes.guess_extension("audio/wav")` rend `None` — le type canonique de
# l'IANA est `audio/vnd.wave`, et aucune table de macOS ne fait le lien. Or c'est
# précisément le type que produit la capture du micro, et un fichier sans
# suffixe fait échouer les bibliothèques audio qui se fient à lui. Le même trou
# que `model/gltf-binary` côté sorties, bouché de la même façon.
_EXTENSIONS = {
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/vnd.wave": ".wav",
}


class UploadError(ValueError):
    """Dépôt refusé — le message porte la raison, en clair."""


class UploadTooLarge(UploadError):
    """Dépôt interrompu parce qu'il dépasse la borne. Distinct : c'est un 413."""


@dataclass(frozen=True)
class Upload:
    """Ce qu'un dépôt devient une fois écrit."""

    path: Path
    name: str
    media_type: str
    size_bytes: int


def accepted_media_types(contracts: Iterable[CapabilityContract]) -> list[str]:
    """Les motifs de type de média qu'au moins un champ fichier du registre accepte.

    Rendu trié pour que la phrase d'un refus soit stable d'une requête à
    l'autre, et pour que le schéma OpenAPI ne bouge pas au gré de l'ordre de
    lecture des contrats.
    """
    motifs: set[str] = set()
    for contrat in contracts:
        for déclaré in contrat.input_media_types().values():
            motifs.update(part.strip() for part in déclaré.split(",") if part.strip())
    return sorted(motifs)


def correspond(media_type: str, motif: str) -> bool:
    """Un type de média correspond-il à un motif de la graphie `accept` ?

    Trois formes, celles que les contrats emploient : `*/*`, `image/*`, et le
    type exact. La comparaison est insensible à la casse et ignore les
    paramètres — un navigateur envoie parfois `audio/webm;codecs=opus`.
    """
    type_nu = media_type.split(";")[0].strip().lower()
    motif = motif.strip().lower()
    if motif in ("*/*", "*"):
        return True
    if motif.endswith("/*"):
        return type_nu.startswith(motif[:-1])
    return type_nu == motif


def verifier_type(media_type: str, motifs: Iterable[str]) -> None:
    acceptés = list(motifs)
    if any(correspond(media_type, motif) for motif in acceptés):
        return
    raise UploadError(
        f"type de média refusé : {media_type or '(non déclaré)'} — "
        f"les champs fichier du registre acceptent {', '.join(acceptés) or 'aucun type'}"
    )


def nom_sur_disque(nom_origine: str, media_type: str, *, jeton: str) -> str:
    """Un nom unique, lisible, et qui ne peut désigner que ce dossier.

    L'extension compte pour la suite : le worker ouvre le fichier avec une
    bibliothèque qui se fie souvent au suffixe, et une capture de `MediaRecorder`
    arrive sans nom du tout. On la déduit alors du type de média — c'est la seule
    information dont on dispose, et elle est exacte.
    """
    propre = _INTERDITS.sub("_", Path(nom_origine or "").name).strip("._-")
    if not propre:
        propre = "depot"
    if not Path(propre).suffix:
        type_nu = media_type.split(";")[0].strip().lower()
        suffixe = _EXTENSIONS.get(type_nu) or mimetypes.guess_extension(type_nu) or ""
        propre = f"{propre}{suffixe}"
    # Le nom d'origine passe en dernier : tronqué, il reste lisible, et le jeton
    # qui garantit l'unicité n'est jamais celui qu'on ampute.
    return f"{jeton}-{propre[:80]}"


def jeton(now: datetime | None = None) -> str:
    """Horodaté pour se trier, suffixé pour ne jamais entrer en collision.

    La même forme que `runner.new_job_id`, et pour la même raison : un dossier
    qu'on ouvre au Finder doit se lire dans l'ordre où on y a déposé.
    """
    horodatage = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"{horodatage}-{uuid.uuid4().hex[:6]}"


def purger(
    dossier: Path, *, retention_s: int = RETENTION_S, maintenant: float | None = None
) -> int:
    """Supprime les dépôts trop vieux. Rend le nombre de fichiers retirés.

    Ne lève jamais : un sas qu'on n'arrive pas à balayer ne doit pas empêcher
    d'en déposer un de plus. Le pire cas est quelques mégaoctets qui restent.
    """
    if not dossier.is_dir():
        return 0
    limite = (maintenant if maintenant is not None else time.time()) - retention_s
    retirés = 0
    for chemin in dossier.iterdir():
        try:
            if chemin.is_file() and chemin.stat().st_mtime < limite:
                chemin.unlink()
                retirés += 1
        except OSError:
            continue
    return retirés


def ecrire(
    source: BinaryIO,
    destination: Path,
    *,
    max_bytes: int = MAX_BYTES,
) -> int:
    """Copie par blocs en comptant, et n'écrit rien de plus que la borne.

    Le fichier partiel est supprimé au dépassement : un refus qui laisserait la
    moitié d'une vidéo sur le disque coûterait plus cher que ce qu'il refuse.
    """
    écrits = 0
    try:
        with open(destination, "wb") as sortie:
            while bloc := source.read(BLOC):
                écrits += len(bloc)
                if écrits > max_bytes:
                    raise UploadTooLarge(
                        f"dépôt interrompu au-delà de {max_bytes} octets — "
                        "découper le fichier, ou le désigner par son chemin sur cette machine"
                    )
                sortie.write(bloc)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return écrits


def deposer(
    source: BinaryIO,
    *,
    dossier: Path,
    nom_origine: str,
    media_type: str,
    motifs: Iterable[str],
    max_bytes: int = MAX_BYTES,
    retention_s: int = RETENTION_S,
) -> Upload:
    """Écrit un dépôt dans le sas et rend le chemin local qu'un champ fichier attend.

    L'ordre est celui du coût : le type d'abord — un refus qui n'a rien écrit ne
    laisse rien à nettoyer —, la purge ensuite, l'écriture en dernier.
    """
    verifier_type(media_type, motifs)
    dossier.mkdir(parents=True, exist_ok=True)
    purger(dossier, retention_s=retention_s)

    nom = nom_sur_disque(nom_origine, media_type, jeton=jeton())
    destination = dossier / nom
    # Ceinture : `nom_sur_disque` ne peut pas produire de séparateur, mais c'est
    # le genre d'invariant qu'une refonte casse en silence.
    if destination.parent.resolve() != dossier.resolve():
        raise UploadError(f"nom de dépôt refusé : {nom_origine!r}")

    taille = ecrire(source, destination, max_bytes=max_bytes)
    return Upload(
        path=destination,
        name=nom,
        media_type=media_type.split(";")[0].strip() or "application/octet-stream",
        size_bytes=taille,
    )
