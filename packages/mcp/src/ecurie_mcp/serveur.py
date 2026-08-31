"""Le serveur MCP — `tools/list`, `tools/call`, et les fichiers qu'il rend.

Le SDK 2.x prend ses handlers en **arguments du constructeur** et non plus en
décorateurs, et il ne valide rien : `on_call_tool` reçoit les arguments tels que
le client les a envoyés, y compris ceux qu'un `inputSchema` interdit. La
validation est donc ici (`schemas.valider`), avant toute dépense — et c'est le
même ordre que partout ailleurs dans ce projet : on refuse ce qui est gratuit à
refuser avant de toucher au disque.

**Le catalogue est figé au démarrage, pas l'exécution.** `tools/list` est lu une
fois par session, souvent avant que l'agent ait posé sa première question : le
composer demande d'inspecter chaque variant sur le disque, et le refaire à chaque
appel coûterait soixante-douze lectures pour rien. L'exécution, elle, **re-résout
le variant** : un `ecurie pull` lancé pendant la session doit servir, et la
réponse porte toujours le variant qui a réellement tourné. Le schéma déclaré ne
ment pas pour autant — il ne porte que le contrat de capacité, identique quel que
soit le variant, plus les défauts de celui qui était prêt au démarrage.

**Les erreurs se répartissent en deux familles, et la spec les sépare
nettement.** Un outil inconnu est une erreur de protocole (`MCPError`) : le
modèle ne peut rien en faire, il a appelé quelque chose qui n'existe pas. Tout le
reste — entrée invalide, variant indisponible, admission refusée, job échoué —
revient dans le résultat avec `isError`, parce que « clients SHOULD provide tool
execution errors to language models to enable self-correction ». C'est
exactement ce que le §6.3 attend d'un refus d'admission : une donnée que l'agent
peut exploiter, pas une panne.
"""

import json
from pathlib import Path
from typing import Any

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.shared.exceptions import MCPError

from ecurie_mcp import catalogue, execution, meta, schemas
from ecurie_mcp.catalogue import Outil
from ecurie_mcp.choix import Retenu, choisir_dans
from ecurie_mcp.contexte import Contexte
from ecurie_mcp.textes import META_TEXTES, TEXTES

NOM = "ecurie"

# Ce qu'un `resources/read` accepte d'inliner. Claude Code plafonne une sortie
# d'outil à 25 000 jetons ; un mégaoctet encodé en base64 en fait déjà plus de
# trois cent mille. Le chiffre est donc bas à dessein — au-delà, le chemin
# suffit, puisque le fichier est sur la machine de qui lit.
PLAFOND_RESSOURCE = 1 << 20


def _annotations(lecture_seule: bool = False) -> types.ToolAnnotations:
    """Ce que l'outil promet de ne pas faire.

    `destructive_hint` est faux partout, et ce n'est pas une politesse : rien du
    côté agent n'est destructif. Le GC, la quarantaine, les suppressions et le
    tiering restent des gestes de CLI humaine — un agent qui sait générer une
    image n'a aucune raison de savoir vider un cache.

    `open_world_hint` est faux aussi : les workers tournent avec
    `HF_HUB_OFFLINE=1`, et un job ne sort jamais sur le réseau.
    """
    return types.ToolAnnotations(
        read_only_hint=lecture_seule,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )


class Serveur:
    """Le catalogue monté, et ce qu'il faut pour servir un appel."""

    def __init__(self, contexte: Contexte) -> None:
        self.contexte = contexte
        self.tous = catalogue.outils(TEXTES)
        self.exposes: list[Outil] = catalogue.outils_exposes(
            self.tous, contexte.exposition.familles
        )
        # Le variant retenu par capacité, au démarrage. Il sert à composer les
        # schémas — les défauts d'un variant diffèrent de ceux du contrat — et à
        # savoir quels outils sont réellement servables.
        self.retenus: dict[str, Retenu | None] = {}
        # Ce qui n'est pas servi, et pourquoi. La CLI le lit pour le dire sur
        # stderr : un outil promis par le catalogue et absent de `tools/list` est
        # une question que l'utilisateur se posera, et à laquelle rien d'autre ne
        # répond avant qu'il pense à `ecurie_catalog`.
        self.muets: dict[str, str] = {}
        for outil in self.exposes:
            retenu = choisir_dans(contexte, outil.capability)
            self.retenus[outil.capability] = retenu
            if retenu is None:
                self.muets[outil.capability] = "aucun variant exécutable"

    # --- tools/list ----------------------------------------------------------

    def declarer(self) -> list[types.Tool]:
        """Les outils annoncés au client.

        Une capacité dont aucun variant n'est exécutable **n'est pas déclarée** :
        annoncer `text_to_image` sur une machine où les poids ne sont pas
        téléchargés donnerait à l'agent un outil qui échoue à tous les coups, et
        il n'a aucun moyen d'apprendre pourquoi depuis `tools/list`.
        `ecurie_catalog`, lui, le dit — avec la commande qui répare.
        """
        déclarés: list[types.Tool] = []
        registry = self.contexte.registry()
        for outil in self.exposes:
            retenu = self.retenus.get(outil.capability)
            if retenu is None:
                continue
            contract = registry.capabilities.get(outil.capability)
            if contract is None:
                # Un modèle déclare cette capacité mais son contrat est absent
                # ou invalide : `load_registry` a rangé l'erreur dans `issues` et
                # servi le reste. L'outil ne peut pas exister sans son schéma,
                # mais disparaître sans un mot ferait chercher longtemps — le
                # variant, lui, est prêt, et rien à l'écran ne dirait pourquoi.
                self.muets[outil.capability] = (
                    f"contrat registry/capabilities/{outil.capability}.json absent ou invalide"
                )
                continue
            déclarés.append(
                types.Tool(
                    name=outil.nom,
                    title=outil.title,
                    description=outil.description,
                    input_schema=schemas.input_schema(
                        contract, retenu.variant, descriptions=outil.champs
                    ),
                    output_schema=schemas.output_schema(contract),
                    annotations=_annotations(),
                )
            )
        déclarés.extend(meta.declarer(self.contexte, META_TEXTES, _annotations))
        return déclarés

    # --- tools/call ----------------------------------------------------------

    async def appeler(
        self, ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        nom = params.name
        arguments: dict[str, Any] = dict(params.arguments or {})

        if nom in (catalogue.CATALOGUE_OUTIL, catalogue.STATUS_OUTIL, catalogue.RUN_OUTIL):
            return await meta.appeler(self, ctx, nom, arguments)

        outil = next((o for o in self.exposes if o.nom == nom), None)
        if outil is None:
            # Le modèle a appelé ce qui n'existe pas : rien à corriger dans son
            # entrée, c'est le protocole qui doit le dire.
            raise MCPError(code=types.INVALID_PARAMS, message=f"unknown tool: {nom}")

        return await self.executer_capacite(ctx, outil.capability, arguments, textes=outil.champs)

    async def executer_capacite(
        self,
        ctx: ServerRequestContext,
        capability: str,
        arguments: dict[str, Any],
        *,
        textes: dict[str, str] | None = None,
        variant_impose: str | None = None,
    ) -> types.CallToolResult:
        """Le chemin commun aux douze outils et à `ecurie_run`.

        `variant_impose` sert l'option `variant` d'un refus d'admission : le
        payload nomme un voisin plus léger qui tient, et sans ce paramètre
        l'agent n'avait aucun moyen de le prendre.
        """
        registry = self.contexte.registry()
        contract = registry.capabilities.get(capability)
        if contract is None:
            return execution.resultat_refus(
                {
                    "error": "unknown_capability",
                    "reason": f"no contract for {capability}",
                }
            )

        schéma = schemas.input_schema(contract, descriptions=textes)
        reproches = schemas.valider(schéma, arguments)
        if reproches:
            return execution.resultat_refus(
                {
                    "error": "invalid_input",
                    "capability": capability,
                    "problems": reproches,
                }
            )

        if variant_impose:
            retenu = _variant_nomme(self.contexte, capability, variant_impose)
            if retenu is None:
                return execution.resultat_refus(
                    {
                        "error": "unknown_variant",
                        "capability": capability,
                        "variant": variant_impose,
                        "reason": f"{variant_impose} does not serve {capability}, or is not "
                        "runnable here. Call ecurie_catalog with this capability to see "
                        "which variants exist and what each is missing.",
                    }
                )
        else:
            # Re-résolu à l'appel : un `ecurie pull` lancé pendant la session doit
            # servir sans redémarrer le client.
            retenu = choisir_dans(self.contexte, capability)
        if retenu is None:
            return execution.resultat_refus(_rien_de_pret(self.contexte, capability))

        try:
            outcome = await execution.executer(
                self.contexte,
                retenu,
                contract,
                arguments,
                session=ctx.session,
                progress_token=_jeton(ctx),
                request_id=ctx.request_id,
            )
        except execution.Refus as refus:
            return execution.resultat_refus(refus.charge)
        return execution.resultat(outcome, retenu, contract)

    # --- resources/read ------------------------------------------------------

    def lire_ressource(self, uri: str) -> types.ReadResourceResult:
        """Sert un fichier produit, et rien d'autre.

        La garde est celle de la route HTTP dont ce serveur prend la suite :
        tout ce qui sort du dossier des sorties est introuvable — pas interdit.
        La question « ce fichier existe-t-il ailleurs sur cette machine » n'a pas
        à recevoir de réponse, fût-elle négative, et un 403 en est une.

        Les liens rendus par les outils portent des URI `file://`, qu'un agent
        local peut ouvrir lui-même. Ce handler existe quand même : la spec veut
        qu'un `ResourceLink` soit résolvable, et un lien qu'on ne peut pas lire
        est un cul-de-sac.

        **Et il refuse ce qui est trop gros.** Le module voisin tient une règle —
        rien ne transite en base64 dans le contexte de l'agent — que ce handler
        pouvait défaire à lui seul : une image de cinquante mégaoctets encodée en
        fait soixante-sept de texte, versés d'un coup. Au-delà du plafond, on
        rend le chemin plutôt que le contenu : le fichier est sur la machine de
        l'agent, il n'a jamais eu besoin de nous pour l'ouvrir.
        """
        chemin = _chemin_de(uri)
        racine = self.contexte.config.outputs_dir.resolve()
        if chemin is None or not chemin.is_relative_to(racine) or not chemin.is_file():
            raise MCPError(code=types.INVALID_PARAMS, message=f"no such resource: {uri}")

        média = _media_type(chemin)
        taille = chemin.stat().st_size
        if taille > PLAFOND_RESSOURCE:
            raise MCPError(
                code=types.INVALID_PARAMS,
                message=(
                    f"resource too large to inline ({taille} bytes, cap {PLAFOND_RESSOURCE}). "
                    f"It is a local file: open {chemin} directly."
                ),
                data={"path": str(chemin), "size": taille, "media_type": média},
            )

        octets = chemin.read_bytes()
        if média.startswith("text/") or média in ("application/json",):
            return types.ReadResourceResult(
                contents=[
                    types.TextResourceContents(
                        uri=uri, text=octets.decode("utf-8", "replace"), mime_type=média
                    )
                ]
            )
        import base64

        return types.ReadResourceResult(
            contents=[
                types.BlobResourceContents(
                    uri=uri, blob=base64.b64encode(octets).decode("ascii"), mime_type=média
                )
            ]
        )


def _jeton(ctx: ServerRequestContext):
    """Le jeton de progression, s'il y en a un.

    `RequestParamsMeta` est un **TypedDict**, pas un modèle pydantic : `ctx.meta`
    est un dictionnaire, et l'interroger par attribut rend `None` sans jamais
    lever. C'est ce qui a coûté un premier job réel entièrement muet — le nom du
    type, au singulier près de ceux du SDK, laissait croire à un objet. Le mode
    de panne est celui que ce projet redoute le plus : une réussite silencieuse.

    Rien n'est envoyé quand le client ne fournit pas de jeton : une notification
    de progression sans jeton n'a nulle part où aller.
    """
    méta = ctx.meta
    if not méta:
        return None
    if isinstance(méta, dict):
        # `if … is not None` et non un `or` : un client dont le compteur part de
        # zéro envoie `progressToken: 0`, parfaitement valide (`ProgressToken =
        # str | int`), et un `or` le confondrait avec l'absence de jeton. Toute
        # la progression de cette session s'éteindrait sans un mot — la panne
        # exacte que le paragraphe ci-dessus raconte, sous une autre forme.
        jeton = méta.get("progress_token")
        return jeton if jeton is not None else méta.get("progressToken")
    return getattr(méta, "progress_token", None)


def _chemin_de(uri: str) -> Path | None:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    try:
        return Path(unquote(parsed.path)).resolve()
    except (OSError, ValueError):
        return None


def _media_type(chemin: Path) -> str:
    import mimetypes

    deviné, _ = mimetypes.guess_type(chemin.name)
    return deviné or "application/octet-stream"


def _variant_nomme(contexte: Contexte, capability: str, ref: str) -> Retenu | None:
    """Le variant que l'agent a nommé — s'il sert cette capacité et qu'il tourne.

    Deux refus, pas un : nommer un variant d'une autre capacité est une erreur
    d'aiguillage, nommer un variant non exécutable en est une autre. Les deux
    rendent None ici, et l'appelant dit lesquelles en une phrase — c'est
    `ecurie_catalog` qui porte le détail, avec la commande qui répare.
    """
    from ecurie_runtime.readiness import inspect_variant

    from ecurie_mcp.choix import variants_de

    for model, variant in variants_de(contexte.registry(), capability):
        if f"{model.id}@{variant.id}" != ref:
            continue
        état = inspect_variant(contexte.root, contexte.config, model, variant, ref)
        if not état.ready:
            return None
        return Retenu(
            model=model,
            variant=variant,
            ref=ref,
            titulaire=bool(model.incumbent),
        )
    return None


def _rien_de_pret(contexte: Contexte, capability: str) -> dict[str, Any]:
    """Aucun variant exécutable : dire lesquels et ce qui leur manque.

    Un outil indisponible qui ne dit pas pourquoi envoie l'agent deviner, et
    c'est exactement ce que ce projet refuse de faire à la ligne de commande.
    Les causes viennent d'`inspect_variant`, qui les rend toutes plutôt que la
    première — un variant fraîchement ajouté les cumule souvent.
    """
    from ecurie_runtime.readiness import inspect_variant

    from ecurie_mcp.choix import variants_de

    causes: list[dict[str, Any]] = []
    registry = contexte.registry()
    for model, variant in variants_de(registry, capability):
        ref = f"{model.id}@{variant.id}"
        état = inspect_variant(contexte.root, contexte.config, model, variant, ref)
        if not état.ready:
            causes.append({"variant": ref, "blockers": list(état.blockers)})
    return {
        "error": "no_runnable_variant",
        "capability": capability,
        "reason": "no variant of this capability can run on this machine right now",
        "candidates": causes,
    }


def construire(contexte: Contexte) -> tuple[Server, Serveur]:
    """Monte le serveur MCP sur ce contexte."""
    servi = Serveur(contexte)

    async def on_list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=servi.declarer())

    async def on_call_tool(
        ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return await servi.appeler(ctx, params)

    async def on_read_resource(
        ctx: ServerRequestContext, params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        return servi.lire_ressource(str(params.uri))

    async def on_list_resources(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListResourcesResult:
        """La liste est vide, et le handler est indispensable quand même.

        Le SDK ne déduit la capacité `resources` que de la présence d'un
        gestionnaire de `resources/list` : sans lui, `resources/read` existe mais
        le serveur ne l'annonce pas, et un client conforme n'essaie jamais de
        suivre un `ResourceLink`. Tous les liens deviendraient des culs-de-sac.

        Vide, parce que les ressources d'Écurie sont les sorties des jobs : elles
        naissent d'un appel d'outil, se nomment dans son résultat, et les
        énumérer reviendrait à publier l'historique de tout ce que la machine a
        produit à qui demande une liste. La spec l'autorise explicitement — un
        `ResourceLink` n'a pas à figurer dans `resources/list`, il doit
        seulement être lisible.
        """
        return types.ListResourcesResult(resources=[])

    serveur = Server(
        NOM,
        version=_version(),
        title="Écurie",
        # Ce texte est lu une fois par session ; une description d'outil est
        # payée douze fois. Tout ce qui vaut pour les douze vit donc ici — c'est
        # ce qui a permis de retirer la prose de l'enveloppe des `outputSchema`,
        # 59 % du catalogue à elle seule.
        instructions=(
            "Local multimodal tools on this machine: hearing, sight, voice, making. "
            "Every tool returns the same envelope: 'ok' whether the job produced its "
            "outputs, 'output' the contract's own result, 'files' the absolute path of "
            "each file produced, 'ref' the variant that served (Écurie picks it, and it "
            "may differ between two calls), 'job_id' its output directory, 'admission' "
            "what the memory check decided, plus 'duration_ms' and 'warnings'.\n"
            "Files are written to disk and returned as paths and resource links, never "
            "as bytes: read them from disk, do not ask for their contents.\n"
            "Every job passes a measured memory admission first. A refusal is data, not "
            "a failure: it carries the peak requested, the budget, what is resident, and "
            "executable options — wait for a job to end, switch to a lighter variant, "
            "reduce an input, or relay a command to your human. Read them and pick one; "
            "a pinned model is a human preference you never override.\n"
            "ecurie_catalog lists everything this machine can do, including the "
            "capabilities with no dedicated tool, which ecurie_run executes."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_read_resource=on_read_resource,
        on_list_resources=on_list_resources,
    )
    return serveur, servi


def options(serveur: Server):
    """Les options d'initialisation.

    `tools_changed` reste faux : le catalogue ne bouge pas en cours de session,
    et annoncer une capacité de notification qu'on n'émettra jamais est un
    mensonge au client — il attendrait un signal qui ne viendra pas.
    """
    return serveur.create_initialization_options(NotificationOptions())


def _version() -> str:
    from ecurie_mcp import __version__

    return __version__


def json_compact(charge: Any) -> str:
    return json.dumps(charge, ensure_ascii=False, default=str)
