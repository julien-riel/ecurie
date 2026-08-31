"""Le serveur, vu par un vrai client MCP.

`Client(serveur)` connecte l'objet `Server` en processus et parle le protocole
pour de bon : ce qui est éprouvé ici est ce qu'un client verra, jusqu'à la
sérialisation des `Tool` et des `CallToolResult`. Les tests portent sur le
critère de sortie de J1 — depuis un client, un outil non-texte exécuté produit un
fichier et l'admission est tracée dans la réponse — et sur les refus, qui sont la
partie du §6.3 qu'on ne peut pas vérifier en lisant le code.
"""

import json

import pytest
from ecurie_mcp import catalogue
from mcp.client import Client

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    """Les tests du SDK tournent sur asyncio ; trio n'est pas une dépendance."""
    return "asyncio"


def charge(résultat) -> dict:
    """Le payload structuré d'un résultat d'outil."""
    if résultat.structured_content is not None:
        return résultat.structured_content
    return json.loads(résultat.content[0].text)


async def test_tools_list_declare_les_outils_et_les_meta(parc, serveur_factory):
    serveur, servi, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        outils = (await client.list_tools()).tools

    noms = {o.name for o in outils}
    # Les trois méta-outils sont toujours là, quel que soit le parc.
    assert {
        catalogue.CATALOGUE_OUTIL,
        catalogue.RUN_OUTIL,
        catalogue.STATUS_OUTIL,
    } <= noms
    # Et la capacité servie par le dépôt d'essai, sous son nom d'outil.
    assert "text_to_speech" in noms


async def test_le_schema_declare_est_celui_du_contrat_sans_extensions(parc, serveur_factory):
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        outils = (await client.list_tools()).tools

    tts = next(o for o in outils if o.name == "text_to_speech")
    assert tts.input_schema["type"] == "object"
    assert "text" in tts.input_schema["properties"]
    assert "x-ui" not in json.dumps(tts.input_schema)


async def test_un_outil_dont_aucun_variant_nest_pret_nest_pas_declare(depot, serveur_factory):
    """Annoncer un outil qui échoue à tous les coups n'apprend rien à l'agent.

    Ici, le profil manque : l'admission refuserait par principe. `ecurie_catalog`
    reste seul à en parler, avec la commande qui répare.
    """
    parc = depot.capability("text-to-speech").env("mlx-audio").model(peak_bytes=None)
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        noms = {o.name for o in (await client.list_tools()).tools}

    assert "text_to_speech" not in noms
    assert catalogue.CATALOGUE_OUTIL in noms


async def test_un_outil_inconnu_est_une_erreur_de_protocole(parc, serveur_factory):
    """Le modèle a appelé ce qui n'existe pas : rien à corriger dans son entrée."""
    from mcp.shared.exceptions import MCPError

    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        with pytest.raises(MCPError):
            await client.call_tool("nexiste_pas", {})


async def test_une_entree_invalide_revient_en_erreur_dexecution(parc, serveur_factory):
    """`isError`, pas une exception : la spec veut que le modèle puisse se corriger."""
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        résultat = await client.call_tool("text_to_speech", {"speed": 9.0})

    assert résultat.is_error
    payload = charge(résultat)
    assert payload["error"] == "invalid_input"
    assert any("speed" in p for p in payload["problems"])


async def test_un_job_produit_un_fichier_et_trace_son_admission(parc, serveur_factory):
    """Le critère de sortie de J1, en un test."""
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        résultat = await client.call_tool("text_to_speech", {"text": "bonjour"})

    assert not résultat.is_error
    payload = charge(résultat)
    assert payload["ok"] is True
    assert payload["ref"] == "tts-test@essai"

    # Un fichier, sur le disque, à un chemin absolu qu'on peut ouvrir.
    chemin = payload["files"]["audio"]
    assert chemin.startswith("/")
    from pathlib import Path

    assert Path(chemin).is_file()

    # L'admission est dans la réponse, pas seulement dans le manifeste.
    assert "admission" in payload
    assert payload["admission"]["reason"]


async def test_les_fichiers_reviennent_en_liens_jamais_en_octets(parc, serveur_factory):
    """Le contexte de l'agent est un budget, pas un tuyau."""
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        résultat = await client.call_tool("text_to_speech", {"text": "bonjour"})

    liens = [b for b in résultat.content if b.type == "resource_link"]
    assert liens, "un fichier produit doit revenir comme lien de ressource"
    assert liens[0].uri.startswith("file://")
    assert liens[0].size is not None
    # Aucun bloc n'inline le binaire.
    assert not [b for b in résultat.content if b.type == "resource"]


async def test_un_lien_rendu_est_lisible_par_resources_read(parc, serveur_factory):
    """Un `ResourceLink` qu'on ne peut pas lire est un cul-de-sac."""
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        résultat = await client.call_tool("text_to_speech", {"text": "bonjour"})
        lien = next(b for b in résultat.content if b.type == "resource_link")
        lu = await client.read_resource(lien.uri)

    assert lu.contents


async def test_un_fichier_hors_du_dossier_des_sorties_est_introuvable(parc, serveur_factory):
    """404 et non 403 : « ce fichier existe-t-il ailleurs » n'a pas à recevoir de réponse."""
    from mcp.shared.exceptions import MCPError

    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        with pytest.raises(MCPError):
            await client.read_resource("file:///etc/passwd")


async def test_le_job_laisse_une_ligne_de_runs_marquee_mcp(parc, serveur_factory, config):
    """La gate du mois 1 compte des jobs MCP : sans la colonne, elle est aveugle."""
    serveur, _, contexte = serveur_factory(parc)
    async with Client(serveur) as client:
        await client.call_tool("text_to_speech", {"text": "bonjour"})

    db = contexte.open_db()
    try:
        lignes = db.conn.execute("SELECT variant_ref, source FROM runs").fetchall()
    finally:
        db.close()

    assert lignes == [("tts-test@essai", "mcp")]


async def test_ecurie_status_dit_ce_quil_ignore(parc, serveur_factory):
    """Une liste vide de résidents étrangers se lirait « rien d'autre n'occupe la mémoire »."""
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        résultat = await client.call_tool(catalogue.STATUS_OUTIL, {})

    payload = charge(résultat)
    assert payload["budget"]["bytes"] == 16 * (1 << 30)
    assert "not read yet" in payload["foreign_residents"]
    # Aucun scan n'a eu lieu : le disque est inconnu, pas nul.
    assert payload["disk"]["known"] is False
    assert payload["disk"]["command"] == "ecurie store scan"


async def test_ecurie_catalog_reste_court_et_dit_ce_qui_est_pret(parc, serveur_factory):
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        résultat = await client.call_tool(catalogue.CATALOGUE_OUTIL, {})

    payload = charge(résultat)
    ligne = next(c for c in payload["capabilities"] if c["capability"] == "text-to-speech")
    assert ligne["ready"] is True
    assert ligne["tool"] == "text_to_speech"
    # La forme courte ne porte aucun schéma : c'est ce qui la garde petite.
    assert "input_schema" not in ligne


async def test_ecurie_catalog_rend_le_contrat_dune_capacite_sur_demande(parc, serveur_factory):
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        résultat = await client.call_tool(
            catalogue.CATALOGUE_OUTIL, {"capability": "text-to-speech"}
        )

    payload = charge(résultat)
    assert "text" in payload["input_schema"]["properties"]
    assert payload["variants"][0]["ref"] == "tts-test@essai"
    assert payload["variants"][0]["ready"] is True


async def test_la_progression_du_job_atteint_le_client(parc, serveur_factory):
    """La progression traverse la frontière fil du job → boucle du transport.

    C'est le trajet le plus facile à rater en silence : `run_job` appelle ses
    rappels dans le fil de travail, et `send_progress_notification` est une
    coroutine de la boucle. `anyio.from_thread.run` fait le passage, et une
    notification qui n'arrive jamais ne lève rien.

    Le test tourne en mode **legacy** délibérément : voir le test suivant, qui
    documente pourquoi l'ère moderne ne transporte pas de jeton.
    """
    reçues: list[tuple[float, str | None]] = []

    async def suivre(progress, total, message):
        reçues.append((progress, message))

    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur, mode="legacy") as client:
        await client.call_tool(
            "text_to_speech", {"text": "bonjour"}, progress_callback=suivre
        )

    assert reçues, "aucune notification de progression n'est arrivée au client"
    assert all(0 <= p <= 100 for p, _ in reçues)


async def test_lere_moderne_ne_transporte_pas_de_jeton_de_progression(parc, serveur_factory):
    """Un fait du SDK 2.1.1, mesuré — pas un défaut de ce paquet.

    En protocole 2026-07-28, le client du SDK **n'émet aucun `progressToken`** :
    les params bruts reçus par le handler portent un `_meta` réduit aux clés
    `io.modelcontextprotocol/*` (protocolVersion, clientInfo, clientCapabilities)
    et rien d'autre. En ère handshake, le même appel porte
    `_meta: {"progressToken": 2}` et la progression arrive — c'est le test
    précédent.

    Le job aboutit dans les deux cas ; ce qui manque est la barre, pas le
    résultat. Ce test existe pour que le jour où le SDK transporte le jeton en
    ère moderne, **il échoue** et nous l'apprenne, au lieu que la progression
    reste muette pour une raison qu'on croirait connaître.
    """
    reçues: list[float] = []

    async def suivre(progress, total, message):
        reçues.append(progress)

    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur, mode="auto") as client:
        résultat = await client.call_tool(
            "text_to_speech", {"text": "bonjour"}, progress_callback=suivre
        )

    assert not résultat.is_error, "le job aboutit : c'est la progression qui manque"
    assert reçues == [], (
        "le SDK transporte désormais un progressToken en ère moderne — "
        "retirer cette limitation de execution.py et du PLAN"
    )
