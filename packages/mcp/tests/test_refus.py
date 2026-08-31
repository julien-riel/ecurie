"""Un refus est une donnée, et l'agent doit pouvoir le réparer seul.

C'est la tâche 1.3, et c'est la partie du §6.3 qu'on ne peut pas vérifier en
lisant le code : ce qui compte n'est pas que le refus soit structuré, c'est que
ses options soient **exécutables** — qu'un agent qui les suit obtienne autre
chose qu'un second refus.
"""

import json

import pytest
from ecurie_mcp import catalogue
from mcp.client import Client

pytestmark = pytest.mark.anyio

GIB = 1 << 30


@pytest.fixture
def anyio_backend():
    return "asyncio"


def charge(résultat) -> dict:
    if résultat.structured_content is not None:
        return résultat.structured_content
    return json.loads(résultat.content[0].text)


async def test_un_variant_plus_gros_que_le_budget_est_refuse_avec_ses_chiffres(
    depot, serveur_factory
):
    """Décharger ne changerait rien : le refus doit le dire, chiffres à l'appui."""
    parc = (
        depot.capability("text-to-speech")
        .env("mlx-audio")
        .model(peak_bytes=20 * GIB)  # budget d'essai : 16 Gio
    )
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        résultat = await client.call_tool("text_to_speech", {"text": "bonjour"})

    assert résultat.is_error
    payload = charge(résultat)
    assert payload["error"] == "admission_refused"
    assert payload["requested"]["peak_bytes"] == 20 * GIB
    assert payload["budget_bytes"] == 16 * GIB
    assert payload["requested"]["capability"] == "text-to-speech"
    # Le champ `basis` des trois états d'admission n'existe pas encore (tâche
    # 2.3) : mieux vaut son absence qu'un « measured-local » écrit en dur, qui
    # serait faux pour un profil mesuré sur une autre machine.
    assert "basis" not in payload["requested"]


async def test_le_refus_propose_un_variant_plus_leger_de_la_meme_capacite(
    depot, serveur_factory
):
    """L'option qui change le résultat le moins : la capacité reste la même."""
    depot.capability("text-to-speech").env("mlx-audio")
    depot.model("tts-lourd", peak_bytes=20 * GIB, incumbent=True)
    depot.model("tts-leger", peak_bytes=2 * GIB, incumbent=False)

    serveur, _, _ = serveur_factory(depot)
    async with Client(serveur) as client:
        outils = (await client.list_tools()).tools
        assert "text_to_speech" in {o.name for o in outils}
        # Le titulaire est le lourd : c'est lui qui sera retenu, et refusé.
        résultat = await client.call_tool("text_to_speech", {"text": "bonjour"})

    payload = charge(résultat)
    assert payload["error"] == "admission_refused"
    variantes = [o for o in payload["options"] if o["kind"] == "variant"]
    assert variantes, "un variant plus léger existe et doit être proposé"
    assert variantes[0]["ref"] == "tts-leger@essai"
    assert variantes[0]["fits_now"] is True
    assert variantes[0]["peak_bytes"] == 2 * GIB


async def test_un_variant_propose_est_reellement_executable(depot, serveur_factory):
    """Proposer un variant dont les poids manquent remplacerait un refus par un autre."""
    depot.capability("text-to-speech").env("mlx-audio")
    depot.model("tts-lourd", peak_bytes=20 * GIB, incumbent=True)
    # Léger, mais sans profil mesuré : l'admission le refuserait aussi.
    depot.model("tts-sans-profil", peak_bytes=None, incumbent=False)

    serveur, _, _ = serveur_factory(depot)
    async with Client(serveur) as client:
        résultat = await client.call_tool("text_to_speech", {"text": "bonjour"})

    payload = charge(résultat)
    proposés = {o.get("ref") for o in payload["options"] if o["kind"] == "variant"}
    assert "tts-sans-profil@essai" not in proposés


async def test_sans_profil_le_refus_renvoie_au_banc_et_ne_negocie_pas(depot, serveur_factory):
    """Le seul refus qu'on ne force pas : on ne peut pas assumer un dépassement inconnu."""
    parc = depot.capability("text-to-speech").env("mlx-audio").model(peak_bytes=None)
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        # L'outil n'est pas déclaré faute de variant prêt ; `ecurie_run` reste
        # la porte, et c'est elle qui doit expliquer.
        résultat = await client.call_tool(
            catalogue.RUN_OUTIL,
            {"capability": "text-to-speech", "input": {"text": "bonjour"}},
        )

    assert résultat.is_error
    payload = charge(résultat)
    assert payload["error"] == "no_runnable_variant"
    blocages = " ".join(
        b for candidat in payload["candidates"] for b in candidat["blockers"]
    )
    assert "ecurie bench" in blocages


async def test_une_capacite_a_human_subject_est_refusee_meme_par_ecurie_run(
    depot, serveur_factory
):
    """L'échappatoire ne rouvre pas ce que le champ ferme.

    Le §6.3 se contredit sur ce point — il exclut « application du champ » puis
    annonce les autres capacités « exécutables par ecurie_run ». Le champ gagne :
    il dit ce qu'une capacité fait d'une personne réelle, et cela ne dépend pas
    de la porte empruntée.
    """
    parc = (
        depot.capability("face-detect")
        .env("uniface")
        .model("visage-test", capability="face-detect", runtime="uniface")
    )
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        résultat = await client.call_tool(
            catalogue.RUN_OUTIL,
            {"capability": "face-detect", "input": {"image": "/tmp/x.png"}},
        )

    assert résultat.is_error
    payload = charge(résultat)
    assert payload["error"] == "capability_excluded"
    assert payload["human_subject"]
    assert payload["options"][0]["command"] == "ecurie mcp --tools faces"


async def test_lopt_in_faces_rouvre_la_famille(depot, serveur_factory):
    """`--tools faces` ouvre le catalogue et l'échappatoire d'un même geste."""
    parc = (
        depot.capability("face-detect")
        .env("uniface")
        .model("visage-test", capability="face-detect", runtime="uniface")
    )
    serveur, _, _ = serveur_factory(parc, familles=frozenset({"faces"}))
    async with Client(serveur) as client:
        résultat = await client.call_tool(
            catalogue.RUN_OUTIL,
            {"capability": "face-detect", "input": {"image": "/tmp/x.png"}},
        )

    payload = charge(résultat)
    # Le refus d'exclusion n'a plus lieu : ce qui échoue ensuite, s'il échoue,
    # relève de l'entrée ou du worker, pas de la politique du serveur.
    assert payload.get("error") != "capability_excluded"


async def test_une_capacite_inconnue_liste_celles_qui_existent(parc, serveur_factory):
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        résultat = await client.call_tool(
            catalogue.RUN_OUTIL, {"capability": "text-to-licorne", "input": {}}
        )

    payload = charge(résultat)
    assert payload["error"] == "unknown_capability"
    assert "text-to-speech" in payload["known"]


async def test_loption_variant_du_refus_est_reellement_executable(depot, serveur_factory):
    """La boucle complète : refusé, on lit l'option, on la prend, ça passe.

    C'est le test qui manquait, et son absence avait laissé passer une option
    parfaitement chiffrée mais impossible à suivre : le payload nommait un
    variant plus léger, les instructions du serveur disaient à l'agent d'en
    choisir une, et aucun outil n'acceptait de référence de variant. Une option
    qu'on ne peut pas exécuter n'est pas une option.
    """
    depot.capability("text-to-speech").env("mlx-audio")
    depot.model("tts-lourd", peak_bytes=20 * GIB, incumbent=True)
    depot.model("tts-leger", peak_bytes=2 * GIB, incumbent=False)

    serveur, _, _ = serveur_factory(depot)
    async with Client(serveur) as client:
        refusé = await client.call_tool("text_to_speech", {"text": "bonjour"})
        payload = charge(refusé)
        option = next(o for o in payload["options"] if o["kind"] == "variant")

        # L'agent suit l'option, telle qu'elle lui est donnée.
        repris = await client.call_tool(
            catalogue.RUN_OUTIL,
            {
                "capability": "text-to-speech",
                "input": {"text": "bonjour"},
                "variant": option["ref"],
            },
        )

    assert not repris.is_error, charge(repris)
    abouti = charge(repris)
    assert abouti["ok"] is True
    assert abouti["ref"] == option["ref"]


async def test_un_variant_impose_qui_ne_sert_pas_la_capacite_est_refuse(parc, serveur_factory):
    """Nommer n'importe quoi ne doit pas lancer n'importe quoi."""
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        résultat = await client.call_tool(
            catalogue.RUN_OUTIL,
            {
                "capability": "text-to-speech",
                "input": {"text": "bonjour"},
                "variant": "nexiste-pas@jamais",
            },
        )

    assert résultat.is_error
    assert charge(résultat)["error"] == "unknown_variant"


async def test_le_refus_parle_anglais_et_ne_propose_pas_de_drapeau_a_lagent(
    depot, serveur_factory
):
    """« Le refus MCP parle anglais » (§6.3), et il ne relaie pas la phrase de la CLI.

    Celle-là est écrite pour un humain devant un terminal : elle est française,
    et elle propose `--hors-budget` — un arbitrage qui appartient à qui lance le
    job, jamais à l'agent qui lit ce payload. Elle voyage dans `cli_reason`, où
    elle sert à rapprocher le refus de ce que le manifeste racontera.
    """
    parc = depot.capability("text-to-speech").env("mlx-audio").model(peak_bytes=20 * GIB)
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        résultat = await client.call_tool("text_to_speech", {"text": "bonjour"})

    payload = charge(résultat)
    assert "budget" in payload["reason"]
    assert "--hors-budget" not in payload["reason"]
    assert "décharger" not in payload["reason"]
    # La phrase de la CLI reste consultable, à sa place.
    assert "--hors-budget" in payload["cli_reason"]


async def test_un_refus_porte_toujours_au_moins_une_option(depot, serveur_factory):
    """Un refus sans issue laisserait l'agent boucler sur le même appel.

    Le cas est le plus simple qui soit : une capacité servie par un seul variant
    trop gros pour le budget, sur un parc vide. Rien à évincer, aucun voisin,
    aucune pente — et pourtant il reste une voie, hors de la boucle de l'agent.
    """
    parc = depot.capability("text-to-speech").env("mlx-audio").model(peak_bytes=20 * GIB)
    serveur, _, _ = serveur_factory(parc)
    async with Client(serveur) as client:
        résultat = await client.call_tool("text_to_speech", {"text": "bonjour"})

    options = charge(résultat)["options"]
    assert options, "aucune option : l'agent n'a rien à faire de ce refus"
    assert options[-1]["kind"] == "human_command"
