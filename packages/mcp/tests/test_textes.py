"""Le catalogue éditorial, tenu contre les contrats qu'il décrit.

Une description est de la prose, et la prose dérive : un contrat gagne un champ,
personne ne pense au texte anglais, et l'agent reçoit un schéma dont un
paramètre n'est expliqué nulle part. Ces tests sont la laisse — ils échouent le
jour où le registre bouge sans que la rédaction suive.

Ils vérifient aussi les règles que le catalogue s'est données, parce qu'une règle
de style que rien ne mesure n'est qu'une intention.
"""

import json
from pathlib import Path

import pytest
from ecurie_mcp import catalogue
from ecurie_mcp.textes import META_TEXTES, TEXTES

REPO_ROOT = Path(__file__).parents[3]

# Les mots qui ne disent rien à un sélecteur d'outil, et que le projet refuse
# faute de les avoir mesurés.
MOTS_VIDES = (
    "high-quality",
    "high quality",
    "state-of-the-art",
    "state of the art",
    "powerful",
    "accurate",
    "this tool",
    "allows you to",
)


def contrat(cap_id: str) -> dict:
    return json.loads((REPO_ROOT / "registry" / "capabilities" / f"{cap_id}.json").read_text())


def test_les_douze_promises_ont_toutes_leur_texte():
    """Le catalogue promis par le README et par le §6.3, au complet."""
    assert set(TEXTES) == set(catalogue.DOUZE)


@pytest.mark.parametrize("capability", catalogue.DOUZE)
def test_les_champs_decrits_sont_exactement_ceux_du_contrat(capability):
    """Un champ inventé est démenti par le schéma ; un champ omis laisse deviner."""
    réels = set((contrat(capability).get("input") or {}).get("properties") or {})
    décrits = set(TEXTES[capability]["champs"])
    assert décrits == réels, (
        f"{capability} : manquants={sorted(réels - décrits)} "
        f"inventés={sorted(décrits - réels)}"
    )


@pytest.mark.parametrize("capability", catalogue.DOUZE)
def test_la_capacite_decrite_existe_au_registre(capability):
    assert (REPO_ROOT / "registry" / "capabilities" / f"{capability}.json").is_file()


@pytest.mark.parametrize("capability", catalogue.DOUZE)
def test_la_description_tient_dans_son_budget(capability):
    """Chaque mot est payé par tous les appels de toutes les sessions."""
    mots = len(TEXTES[capability]["description"].split())
    assert 20 <= mots <= 60, f"{capability} : {mots} mots"


@pytest.mark.parametrize("capability", catalogue.DOUZE)
def test_aucune_promesse_de_qualite_ni_de_formule_creuse(capability):
    texte = TEXTES[capability]["description"].lower()
    for mot in MOTS_VIDES:
        assert mot not in texte, f"{capability} emploie « {mot} »"


@pytest.mark.parametrize("capability", catalogue.DOUZE)
def test_aucun_nom_de_modele_dans_la_description(capability):
    """Écurie choisit le variant : une description qui nomme ses poids ment au premier pull."""
    import yaml

    modèles = {
        yaml.safe_load(f.read_text())["id"]
        for f in (REPO_ROOT / "registry" / "models").glob("*.yaml")
    }
    texte = TEXTES[capability]["description"].lower()
    cités = [m for m in modèles if m.lower() in texte]
    assert not cités, f"{capability} nomme {cités}"


def test_un_champ_a_options_dynamiques_ne_se_voit_attribuer_aucune_valeur():
    """Une valeur inventée coûte un chargement complet avant d'échouer.

    Les valeurs d'un `x-options-from` ne sont connues qu'après chargement du
    modèle. La description doit dire d'où elles viennent — c'est
    `schemas.input_schema` qui ajoute la phrase — et surtout n'en citer aucune.
    """
    for capability in catalogue.DOUZE:
        propriétés = (contrat(capability).get("input") or {}).get("properties") or {}
        for nom, champ in propriétés.items():
            if not champ.get("x-options-from"):
                continue
            texte = TEXTES[capability]["champs"][nom]
            assert "announce" in texte.lower() or "omit" in texte.lower(), (
                f"{capability}.{nom} doit dire d'où viennent ses valeurs"
            )


def test_les_trois_meta_outils_ont_leur_texte():
    assert set(META_TEXTES) == {
        catalogue.CATALOGUE_OUTIL,
        catalogue.RUN_OUTIL,
        catalogue.STATUS_OUTIL,
    }


def test_aucune_des_douze_ne_porte_de_human_subject():
    """Le catalogue par défaut applique le champ, il ne le contourne pas."""
    for capability in catalogue.DOUZE:
        assert not contrat(capability).get("human_subject"), capability


def test_les_familles_ne_nomment_que_des_capacites_reelles():
    """`--tools faces` doit ouvrir quelque chose qui existe."""
    for famille, capacités in catalogue.FAMILLES.items():
        for capability in capacités:
            chemin = REPO_ROOT / "registry" / "capabilities" / f"{capability}.json"
            assert chemin.is_file(), f"{famille} nomme {capability}, absent du registre"
            assert json.loads(chemin.read_text()).get("human_subject"), (
                f"{famille} nomme {capability}, qui ne porte pas de human_subject — "
                "la famille n'a alors aucune raison d'être un opt-in"
            )


def test_aucune_description_ne_contredit_le_defaut_du_schema():
    """Le schéma porte le défaut du variant, la prose citait celui du contrat.

    Les deux moitiés du même objet se contredisaient : `steps` déclaré
    `default: 30` par le variant retenu, décrit « default 25 » — le chiffre du
    contrat. Un agent qui lit la description et omet le champ obtient l'autre
    valeur, sans rien pour le lui dire.

    La règle qui en sort : **le schéma fait foi, la prose n'y touche pas.** Une
    description explique ce que les bornes font ; elle ne répète pas un nombre
    qu'un manifeste peut changer sans elle.
    """
    import re

    from ecurie_core.config import Config, ScanConfig
    from ecurie_core.registry import load_registry
    from ecurie_mcp import schemas
    from ecurie_mcp.choix import choisir

    registry = load_registry(REPO_ROOT)
    config = Config(memory_budget=1 << 34, scan=ScanConfig())
    fautes: list[str] = []

    for capability in catalogue.DOUZE:
        retenu = choisir(REPO_ROOT, config, registry, capability)
        if retenu is None:
            continue  # non exécutable ici : le schéma exposé n'existe pas
        contract = registry.capabilities[capability]
        schéma = schemas.input_schema(contract, retenu.variant)
        for nom, texte in TEXTES[capability]["champs"].items():
            # Le point appartient à la valeur — « default 0.5 » se lirait sinon
            # « default 0 » et le test inventerait ses fautes — mais les autres
            # ponctuations la terminent, y compris le deux-points de
            # « default 0.6: 0 keeps the source ».
            trouvé = re.search(r"\bdefault\s+([^\s;,:]+)", texte)
            if trouvé is None:
                continue
            réel = (schéma["properties"].get(nom) or {}).get("default")
            if réel is None or isinstance(réel, list | dict):
                continue  # une liste ne se cite pas en un mot
            cité = trouvé.group(1).rstrip(".")
            if _même_nombre(cité, réel):
                continue
            fautes.append(f"{capability}.{nom}: schéma={réel!r}, texte dit «{cité}»")

    assert not fautes, "défaut cité qui contredit le schéma : " + " ; ".join(fautes)


def _même_nombre(cité: str, réel) -> bool:
    """`true`/`True`, `0`/`0.0`, `empty`/`''` disent la même chose."""
    if isinstance(réel, bool):
        return cité.lower() == str(réel).lower()
    if isinstance(réel, int | float):
        try:
            return float(cité) == float(réel)
        except ValueError:
            return False
    if réel == "":
        return cité.lower() in ("empty", "none", "''")
    return cité.strip("'\"") == str(réel)
