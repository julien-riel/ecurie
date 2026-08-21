"""Les golden sets du dépôt : ce qui doit tenir à chaque cas ajouté.

Un jeu d'essai est de la donnée committée, et la donnée committée se valide comme
le reste du registre. Trois choses sont vérifiées ici, et chacune correspond à une
façon précise dont un golden set se dégrade sans qu'on s'en aperçoive :

1. **la forme** — contre `registry/schema/golden.schema.json`, l'autorité ;
2. **l'accord avec le contrat de capacité** — l'entrée d'un cas doit valider
   contre le schéma d'entrée de sa capacité. Sans ce croisement, un contrat qui
   se resserre laisse derrière lui un jeu d'essai qui n'est plus exécutable, et
   on ne l'apprend qu'au moment de l'évaluer ;
3. **l'existence des fichiers** — sauf pour les cas déclarés `pending`, qui sont
   la façon honnête de figer une vérité terrain avant d'avoir son enregistrement.

Ce fichier ne fige aucun inventaire : la règle est append-only, donc les jeux ne
peuvent que grandir, et un test qui compterait les cas casserait à chaque ajout
sans rien apprendre.
"""

import json
from pathlib import Path

import pytest
from ecurie_core.capabilities import load_capabilities
from jsonschema import Draft202012Validator

GOLDEN = Path("registry/evals/golden")
SCHEMA = Path("registry/schema/golden.schema.json")


def _jeux(repo_root: Path) -> list[Path]:
    return sorted(
        chemin.parent
        for chemin in (repo_root / GOLDEN).glob("*/manifest.json")
    )


def _manifeste(dossier: Path) -> dict:
    return json.loads((dossier / "manifest.json").read_text())


@pytest.fixture
def jeux(repo_root):
    dossiers = _jeux(repo_root)
    assert dossiers, "aucun golden set — registry/evals/golden/ est vide"
    return dossiers


def test_chaque_jeu_respecte_le_meta_schema(repo_root, jeux):
    validateur = Draft202012Validator(json.loads((repo_root / SCHEMA).read_text()))
    for dossier in jeux:
        erreurs = sorted(
            validateur.iter_errors(_manifeste(dossier)), key=lambda e: list(e.absolute_path)
        )
        détail = "\n".join(
            f"  {'/'.join(str(p) for p in e.absolute_path) or 'racine'} : {e.message}"
            for e in erreurs
        )
        assert not erreurs, f"{dossier.name}/manifest.json :\n{détail}"


def test_le_nom_du_dossier_est_la_capacite(repo_root, jeux):
    for dossier in jeux:
        assert _manifeste(dossier)["capability"] == dossier.name


def test_chaque_capacite_jugee_a_un_contrat(repo_root, jeux):
    """Un jeu d'essai pour une capacité qui n'existe pas au registre ne s'exécute
    pas : il n'y a ni formulaire, ni validation d'entrée, ni sortie typée."""
    contrats, _ = load_capabilities(repo_root)
    for dossier in jeux:
        assert dossier.name in contrats, f"{dossier.name} : aucun contrat de capacité"


def test_chaque_entree_valide_contre_le_contrat_de_sa_capacite(repo_root, jeux):
    """Le croisement qui rapporte le plus.

    Un paramètre que le contrat ne déclare plus — ou dont les bornes se sont
    resserrées — rend le cas inexécutable. Le découvrir ici coûte une seconde ;
    le découvrir au milieu d'une campagne d'évaluation coûte la campagne.
    """
    contrats, _ = load_capabilities(repo_root)
    for dossier in jeux:
        validateur = contrats[dossier.name].validator()
        for cas in _manifeste(dossier)["cases"]:
            erreurs = sorted(
                validateur.iter_errors(cas["input"]), key=lambda e: list(e.absolute_path)
            )
            détail = "; ".join(e.message for e in erreurs)
            assert not erreurs, f"{dossier.name}/{cas['id']} : {détail}"


def test_les_identifiants_de_cas_sont_uniques(repo_root, jeux):
    """L'identifiant est la clé sous laquelle les résultats s'accumulent d'un
    variant à l'autre : deux cas homonymes écraseraient leurs notes."""
    for dossier in jeux:
        ids = [cas["id"] for cas in _manifeste(dossier)["cases"]]
        assert len(ids) == len(set(ids)), f"{dossier.name} : identifiant dupliqué"


def test_les_fichiers_d_entree_existent_sauf_ceux_declares_en_attente(repo_root, jeux):
    for dossier in jeux:
        for cas in _manifeste(dossier)["cases"]:
            for clé in ("document", "image", "audio"):
                valeur = cas["input"].get(clé)
                if not isinstance(valeur, str):
                    continue
                chemin = dossier / valeur
                if "pending" in cas:
                    assert not chemin.exists(), (
                        f"{dossier.name}/{cas['id']} : le fichier est là, "
                        "retirer la clé pending"
                    )
                else:
                    assert chemin.is_file(), f"{dossier.name}/{cas['id']} : {valeur} introuvable"


def test_les_fichiers_de_reference_existent_et_ne_sont_pas_vides(repo_root, jeux):
    """Texte attendu, masque attendu, image attendue, réponse structurée attendue.

    Une référence qui pointe un fichier absent ne se découvre qu'au moment de
    noter — c'est-à-dire après avoir exécuté le modèle, donc après avoir payé.
    """
    for dossier in jeux:
        for cas in _manifeste(dossier)["cases"]:
            référence = cas.get("reference") or {}
            for clé in ("text_file", "mask_file", "image_file", "json_file"):
                fichier = référence.get(clé)
                if fichier is None:
                    continue
                chemin = dossier / fichier
                assert chemin.is_file(), (
                    f"{dossier.name}/{cas['id']} : {clé} → {fichier} introuvable"
                )
                assert chemin.stat().st_size > 0, (
                    f"{dossier.name}/{cas['id']} : {clé} → {fichier} est vide"
                )


def test_les_reponses_structurees_attendues_sont_du_json_valide(repo_root, jeux):
    """Une référence d'appel d'outils est relue par `ecurie eval` : elle doit
    parser, et porter une liste — un appel unique écrit sans crochets ferait
    échouer la comparaison sur une différence de forme, pas de fond."""
    for dossier in jeux:
        for cas in _manifeste(dossier)["cases"]:
            fichier = (cas.get("reference") or {}).get("json_file")
            if fichier is None:
                continue
            contenu = json.loads((dossier / fichier).read_text())
            assert isinstance(contenu, list), (
                f"{dossier.name}/{cas['id']} : {fichier} doit porter une liste d'appels"
            )
            for appel in contenu:
                assert set(appel) == {"name", "arguments"}, (
                    f"{dossier.name}/{cas['id']} : un appel s'écrit {{name, arguments}}"
                )


def test_un_jeu_complet_n_a_aucun_cas_en_attente(repo_root, jeux):
    """`status` est ce que `ecurie eval` rapportera : il ne doit pas mentir sur
    la couverture réelle du jeu."""
    for dossier in jeux:
        manifeste = _manifeste(dossier)
        en_attente = [cas["id"] for cas in manifeste["cases"] if "pending" in cas]
        if manifeste["status"] == "complet":
            assert not en_attente, f"{dossier.name} : déclaré complet mais {en_attente} en attente"
        else:
            assert en_attente, f"{dossier.name} : déclaré incomplet sans aucun cas en attente"


def test_les_champs_attendus_se_retrouvent_dans_le_texte_de_reference(repo_root, jeux):
    """Un champ est un extrait de la vérité terrain, pas une seconde vérité.

    Deux affirmations sur la même page finiraient par diverger — c'est
    exactement ce qui arrive entre `measurements/` et le bloc `profile:` d'un
    manifeste, et on ne le refait pas ici.

    Le contrôle ne vaut que **quand il y a une vérité terrain textuelle**. Une
    description d'image n'en a pas : deux descriptions également justes n'ont
    aucune raison de se ressembler. Les champs y valent alors pour eux-mêmes —
    ce sont les mentions qu'une réponse correcte ne peut pas omettre, et il n'y
    a rien à recouper.
    """
    for dossier in jeux:
        for cas in _manifeste(dossier)["cases"]:
            référence = cas.get("reference") or {}
            champs = référence.get("fields")
            fichier = référence.get("text_file")
            if not champs or not (fichier or référence.get("text")):
                continue
            texte = (dossier / fichier).read_text() if fichier else référence["text"]
            normalisé = " ".join(texte.split())
            for nom, valeur in champs.items():
                assert " ".join(valeur.split()) in normalisé, (
                    f"{dossier.name}/{cas['id']} : le champ {nom} = {valeur!r} "
                    "ne se trouve pas dans le texte de référence"
                )
