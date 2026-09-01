"""Lancer un job depuis un handler MCP, et en rendre compte.

Le module tient sur une frontière : d'un côté une boucle `anyio` qui doit rester
libre pour parler JSON-RPC, de l'autre un `run_job` synchrone qui peut durer des
minutes. Trois choses la traversent, et chacune se rate en silence.

**Le job part dans un fil.** `run_job` attend son tour sur le variant — jusqu'à
une heure, `Timeouts.queue_s` —, charge un modèle, exécute. L'appeler dans le
handler gèlerait la boucle du transport stdio tout entière : plus un message ne
sortirait ni n'entrerait, `ping` compris, et le client conclurait à un serveur
mort. `anyio.to_thread.run_sync` le déporte.

**La progression revient dans l'autre sens.** `send_progress_notification` est
une coroutine, et les rappels de `run_job` s'exécutent dans le fil du job : ils
ne peuvent pas l'appeler. `anyio.from_thread.run` fait le trajet inverse — il
n'est légal que depuis un fil ouvert par `to_thread.run_sync`, ce qui est
exactement le nôtre. Une notification qui n'atteint jamais la boucle ne lève
rien : elle manque, simplement, et le client voit un outil muet pendant deux
minutes.

**Et elle ne part pas toujours, pour une raison qui n'est pas la nôtre.** Mesuré
sur le SDK 2.1.1, les deux ères du protocole ne se comportent pas pareil : en ère
handshake (≤ 2025-11-25), l'appel porte `_meta: {"progressToken": …}` et la barre
avance ; en ère moderne (2026-07-28), le client **n'émet aucun jeton** — le
`_meta` reçu se réduit aux clés `io.modelcontextprotocol/*`. Sans jeton, une
notification n'a nulle part où aller, et rien n'est envoyé. Le job aboutit dans
les deux cas ; ce qui manque est la barre, pas le résultat. Deux tests tiennent
ce fait, dont un qui **échouera** le jour où le SDK transportera le jeton.

**Rien ne part en base64.** Une image générée revient comme chemin plus lien de
ressource ; le contexte de l'agent est un budget, pas un tuyau. Seule exception,
et elle est bornée : une sortie texte assez courte pour être lue tout de suite
est inlinée — demander à l'agent d'ouvrir un fichier pour lire trois phrases de
transcription lui coûterait un aller-retour de plus que le texte lui-même.
"""

import json
import sys
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from ecurie_core.capabilities import CapabilityContract
from ecurie_runtime.runner import InputError, JobOutcome, resolve_typed_input, run_job

from ecurie_mcp import refus
from ecurie_mcp.choix import Retenu
from ecurie_mcp.contexte import Contexte

# Au-delà, une sortie texte n'est plus « courte » : elle part par son chemin.
# Le chiffre vient d'en face — Claude Code avertit au-delà de 10 000 jetons de
# sortie d'outil et plafonne à 25 000 — et il est délibérément bien en deçà :
# le texte inliné n'est pas seul dans le résultat, et une transcription qui
# remplit à elle seule le tiers du plafond a cessé d'être une commodité.
SEUIL_TEXTE_INLINE = 4000

# Le trajet fil → boucle n'est pas gratuit, et un adaptateur bavard le paierait
# à chaque pas. Mais filtrer sur le seul pourcentage supprime précisément ce qui
# compte sur un job long : plusieurs adaptateurs battent la mesure **au même
# pourcentage** en changeant la note (« diffusion, pas 12 sur 50 »), et c'est le
# seul signe de vie pendant des minutes. Le pourcentage OU la note doit avoir
# changé ; ni l'un ni l'autre, rien ne part.
PROGRESSION_MINIMALE = 1


class Refus(Exception):
    """Le job n'a pas eu lieu, et le payload dit ce que l'agent peut y faire."""

    def __init__(self, charge: dict[str, Any]) -> None:
        super().__init__(charge.get("reason") or charge.get("error") or "refused")
        self.charge = charge


async def executer(
    contexte: Contexte,
    retenu: Retenu,
    contract: CapabilityContract,
    arguments: dict[str, Any],
    *,
    session=None,
    progress_token=None,
    request_id=None,
) -> JobOutcome:
    """Lance le job dans un fil et rend son résultat, progression relayée."""
    dernier: dict[str, Any] = {"pct": -PROGRESSION_MINIMALE, "note": None}

    def annoncer(pct: int, note: str = "") -> None:
        """Appelé dans le fil du job — d'où le passage explicite par la boucle."""
        # `progress_token is None` et non une vérité booléenne : un client dont
        # le compteur part de zéro envoie `progressToken: 0`, que le SDK accepte
        # (`ProgressToken = str | int`, seul `bool` est rejeté). Un test de
        # vérité éteindrait alors toute la progression de la session, en silence.
        if session is None or progress_token is None:
            return
        if pct - dernier["pct"] < PROGRESSION_MINIMALE and note == dernier["note"]:
            return
        dernier["pct"] = pct
        dernier["note"] = note
        try:
            anyio.from_thread.run(
                lambda: session.send_progress_notification(
                    progress_token=progress_token,
                    progress=float(pct),
                    total=100.0,
                    message=note or None,
                    related_request_id=request_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            # Le client a fermé, ou n'écoute plus : un job en cours n'a aucune
            # raison de mourir parce que personne ne regarde sa barre. Mais
            # l'avaler sans un mot est ce qui a laissé passer un relais
            # entièrement muet — le jeton se lisait par attribut sur ce qui est
            # un dictionnaire, et rien ne levait. On abandonne la progression
            # pour ce job, et on le dit une fois, sur stderr.
            if not dernier.get("mort"):
                dernier["mort"] = True
                print(
                    f"ecurie-mcp: progression abandonnée pour ce job ({exc!r})",
                    file=sys.stderr,
                )

    def lancer() -> JobOutcome:
        db = contexte.open_db()
        try:
            return run_job(
                contexte.supervisor(),
                retenu.model,
                retenu.variant,
                contract,
                arguments,
                typed=True,
                db=db,
                source="mcp",
                on_progress=annoncer,
            )
        finally:
            db.close()

    try:
        outcome = await anyio.to_thread.run_sync(lancer)
    except InputError as exc:
        # Levée avant tout `try` de `run_job` : aucun manifeste n'est écrit, et
        # le dossier du job reste vide. C'est une faute d'entrée, pas une panne.
        raise Refus(
            {
                "error": "invalid_input",
                "reason": str(exc),
                "capability": retenu.capability,
                "variant": retenu.ref,
            }
        ) from exc

    if outcome.admission is not None and not outcome.admission.admitted:
        # Le pic se rechiffre sur l'entrée **résolue**, jamais sur les arguments
        # bruts, et l'écart n'est pas théorique : `run_job` fusionne les défauts
        # du contrat et ceux du variant avant de planifier l'admission
        # (`merge_defaults`), si bien qu'un paramètre de pente que l'agent n'a pas
        # écrit vaut son défaut pour la décision et **rien** pour un recalcul
        # naïf. Le refus annoncerait alors un pic qui n'est pas celui qui a
        # décidé du refus — deux vérités dans le même payload, et un `fits_now`
        # qui bascule sur un variant qui tenait.
        valeurs = _valeurs_resolues(retenu, contract, arguments)
        raise Refus(
            refus.payload(
                contexte.supervisor(),
                contexte.registry(),
                refus.Demande(
                    capability=retenu.capability,
                    ref=retenu.ref,
                    peak_bytes=contexte.supervisor().peak_bytes(retenu.variant, valeurs),
                    values=valeurs,
                    contract=contract,
                ),
                outcome.admission,
                root=contexte.root,
                config=contexte.config,
            )
        )
    return outcome


def _valeurs_resolues(
    retenu: Retenu, contract: CapabilityContract, arguments: dict[str, Any]
) -> dict[str, Any]:
    """L'entrée telle que l'admission l'a vue — défauts du contrat et du variant compris.

    Le même appel que `run_job` fait avant de planifier. S'il échoue ici, c'est
    qu'il a échoué là-bas aussi et que le job n'a jamais atteint l'admission :
    on retombe alors sur les arguments bruts, qui sont tout ce qu'on a.
    """
    try:
        return dict(resolve_typed_input(contract, retenu.variant, arguments).values)
    except InputError:
        return dict(arguments)


def enveloppe(outcome: JobOutcome, retenu: Retenu) -> dict[str, Any]:
    """Ce que l'outil rend, conforme à l'`outputSchema` déclaré.

    `output` porte la sortie du worker telle qu'il l'a rendue — y compris ce qui
    n'est pas un fichier : une langue détectée, un nombre de pages, une liste
    d'appels. Le reste est l'enveloppe, identique d'un outil à l'autre.

    Les chemins sont **absolus**. Un chemin relatif au dossier du job obligerait
    l'agent à savoir où ce dossier se trouve, ce qui est une question de plus, et
    la seule réponse utile à « où est le fichier » est celle qu'on peut ouvrir.
    """
    fichiers = {clé: str(chemin) for clé, chemin in outcome.files.items()}
    charge: dict[str, Any] = {
        "ok": outcome.ok,
        "capability": retenu.capability,
        "ref": outcome.ref,
        "job_id": outcome.job_id,
        "output": (outcome.result.output if outcome.result else {}),
        "files": fichiers,
        "duration_ms": outcome.duration_ms,
    }
    if outcome.warnings:
        charge["warnings"] = list(outcome.warnings)
    if outcome.error:
        charge["error"] = outcome.error
    admission = outcome.admission
    if admission is not None:
        charge["admission"] = {
            "reason": admission.reason,
            "reused": outcome.reused,
            "evicted": list(outcome.evicted),
            "headroom_bytes": admission.headroom_bytes,
        }
    return charge


def contenu(outcome: JobOutcome, contract: CapabilityContract) -> list[types.ContentBlock]:
    """Les blocs de contenu du résultat : le JSON, puis un lien par fichier.

    Le JSON d'abord parce qu'il porte la réponse ; les liens ensuite parce qu'ils
    sont un moyen, pas un résultat. `ResourceLink` ne transporte qu'une URI et sa
    taille — le client décide de lire ou non, et c'est là toute la différence
    avec un `EmbeddedResource`, qui verserait les octets dans le contexte sans
    lui demander son avis.
    """
    types_media = contract.output_media_types()
    return [_lien(chemin, types_media.get(clé)) for clé, chemin in sorted(outcome.files.items())]


def _lien(chemin: Path, media_type: str | None) -> types.ResourceLink:
    try:
        taille = chemin.stat().st_size
    except OSError:
        taille = None
    return types.ResourceLink(
        uri=chemin.as_uri(),
        name=chemin.name,
        mime_type=media_type,
        size=taille,
    )


def texte_inline(outcome: JobOutcome, contract: CapabilityContract) -> str | None:
    """Le contenu d'une sortie texte, quand elle est assez courte pour être lue.

    Le §6.3 promet qu'« une transcription courte revient en texte, une longue en
    fichier » sans dire où passe la frontière. Elle est ici, et elle est
    explicite : sous `SEUIL_TEXTE_INLINE` caractères, le texte accompagne le
    résultat ; au-delà, seul son chemin. Les contrats concernés déclarent
    `text/plain` en sortie — c'est la seule famille où lire le fichier à la place
    de l'agent lui épargne un aller-retour sans lui coûter son contexte.
    """
    for clé, media in sorted(contract.output_media_types().items()):
        if not str(media).startswith("text/"):
            continue
        chemin = outcome.files.get(clé)
        if chemin is None:
            continue
        try:
            if chemin.stat().st_size > SEUIL_TEXTE_INLINE:
                return None
            return chemin.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    return None


def resultat(
    outcome: JobOutcome, retenu: Retenu, contract: CapabilityContract
) -> types.CallToolResult:
    """Le `CallToolResult` complet d'un job qui a eu lieu."""
    charge = enveloppe(outcome, retenu)
    inline = texte_inline(outcome, contract)
    if inline is not None:
        charge["text"] = inline

    blocs: list[types.ContentBlock] = [
        types.TextContent(type="text", text=json.dumps(charge, ensure_ascii=False, default=str))
    ]
    blocs.extend(contenu(outcome, contract))
    return types.CallToolResult(
        content=blocs,
        structured_content=charge,
        # Un job qui n'a pas produit sa sortie est une erreur d'exécution : la
        # spec veut qu'elle revienne dans le résultat, pas en erreur de
        # protocole — « Clients SHOULD provide tool execution errors to language
        # models to enable self-correction ».
        is_error=not outcome.ok,
    )


def resultat_refus(charge: dict[str, Any]) -> types.CallToolResult:
    """Un refus : `isError`, le payload en clair et en structuré.

    Les deux formes, et ce n'est pas une redondance : `structuredContent` est ce
    qu'un client outillé exploitera, `content` ce qu'un modèle lira si son client
    n'en fait rien. Vérifié sur le SDK 2.1.1 : quand `is_error` est vrai, le
    client ne valide pas `structuredContent` contre l'`outputSchema` — le refus
    peut donc porter sa propre forme sans faire échouer l'appel.
    """
    return types.CallToolResult(
        content=[
            types.TextContent(type="text", text=json.dumps(charge, ensure_ascii=False, default=str))
        ],
        structured_content=charge,
        is_error=True,
    )
