"""Contrats de capacité : forme, validité de schéma, croisement avec les manifestes.

Le contrat est le pivot de l'architecture (§4) : c'est lui qui engendre le
formulaire, choisit le visualiseur et rend l'A/B possible. Un contrat faux ne
casse rien au chargement — il casse l'UI, ou pire, il laisse un réglage se perdre
en silence entre le manifeste et le worker. D'où ces tests, qui vérifient moins
que le code fait ce qu'il dit que ce qu'il attrape avant que ça ne coûte cher.
"""

import json
from pathlib import Path

import pytest
from ecurie_core.capabilities import (
    CapabilityContract,
    check_variant_defaults,
    load_capabilities,
)
from ecurie_core.issues import Issue
from ecurie_core.registry import load_registry
from manifest_helpers import make_manifest, minimal_capability

CAPACITÉS_DU_DÉPÔT = [
    "audio-denoise",
    "audio-separation",
    "audio-to-text",
    "depth-estimation",
    "document-to-text",
    "face-detect",
    "face-embed",
    "face-gaze",
    "face-headpose",
    "face-landmark",
    "face-parse",
    "image-detect",
    "image-inpaint",
    "image-matting",
    "image-segment",
    "image-to-image",
    "image-to-mesh",
    "image-to-text",
    "image-to-video",
    "image-upscale",
    "speaker-diarization",
    "speech-to-text",
    "text-generation",
    "text-to-image",
    "text-to-music",
    "text-to-speech",
    "text-to-video",
    "tool-use",
    "translation",
    "video-to-motion",
    "video-to-text",
    "voice-clone",
]

# La composite du §11 de la conception : deux étapes, un point d'arrêt après la première.
TEXT_TO_MESH = {
    "id": "text-to-mesh",
    "title": "Texte vers maillage 3D",
    "composite": True,
    "input": {
        "type": "object",
        "required": ["prompt"],
        "additionalProperties": False,
        "properties": {
            "prompt": {"type": "string", "x-ui": "textarea"},
            "seed": {"type": "integer", "minimum": 0},
        },
    },
    "output": {
        "type": "object",
        "required": ["mesh"],
        "properties": {
            "mesh": {
                "type": "string",
                "contentMediaType": "model/gltf-binary",
                "description": "Chemin du maillage produit, relatif au dossier du job.",
            }
        },
    },
    "steps": [
        {"capability": "text-to-image", "expose": ["prompt", "seed"]},
        {
            "capability": "image-to-mesh",
            "input_from": "steps[0].output.image",
            "expose": ["octree_resolution"],
        },
    ],
    "checkpoint_after": [0],
}


def _contrat(repo_root: Path, cap_id: str) -> CapabilityContract:
    """Le vrai contrat du dépôt, chargé seul."""
    chemin = repo_root / "registry" / "capabilities" / f"{cap_id}.json"
    return CapabilityContract(id=cap_id, document=json.loads(chemin.read_text()))


def _composite(cap_id: str, *capacités: str) -> dict:
    """Un contrat composite minimal mais conforme, enchaînant les capacités données."""
    document = minimal_capability(cap_id)
    document["composite"] = True
    document["steps"] = [{"capability": c} for c in capacités]
    return document


def _sorties_typées(schéma: dict, préfixe: str = "") -> dict[str, dict]:
    """Toutes les sorties portant un contentMediaType, imbriquées comprises."""
    trouvées: dict[str, dict] = {}
    for nom, champ in (schéma.get("properties") or {}).items():
        chemin = f"{préfixe}{nom}"
        if "contentMediaType" in champ:
            trouvées[chemin] = champ
        trouvées.update(_sorties_typées(champ, f"{chemin}."))
    return trouvées


# --- le registre réel : les douze contrats du dépôt ---------------------------------


def test_les_contrats_du_depot_se_chargent_sans_aucune_issue(repo_root):
    contracts, issues = load_capabilities(repo_root)

    assert issues == []
    assert sorted(contracts) == CAPACITÉS_DU_DÉPÔT


def test_chaque_contrat_porte_une_capacite_declarable(repo_root):
    """Un contrat dont l'id sort de l'enum du schéma de manifeste ne sert à personne :
    aucun modèle ne peut le désigner. Un contrat dont l'id diffère du nom de fichier
    n'est jamais trouvé non plus, le chargeur des manifestes cherchant par nom."""
    contracts, _ = load_capabilities(repo_root)
    schéma = json.loads((repo_root / "registry" / "schema" / "model.schema.json").read_text())
    enum = schéma["properties"]["capability"]["enum"]

    for cap_id, contract in contracts.items():
        assert contract.id == cap_id
        assert (repo_root / "registry" / "capabilities" / f"{cap_id}.json").is_file()
        assert cap_id in enum

    # text-to-mesh est déclarée sans contrat : c'est la composite, câblée plus tard.
    assert set(enum) - set(contracts) == {"text-to-mesh"}


def test_chaque_sortie_typee_annonce_un_chemin_de_fichier(repo_root):
    """`contentMediaType` a deux effets : il choisit le visualiseur et il dit que la
    valeur est un chemin, pas le contenu. Une sortie typée décrite comme portant la
    donnée elle-même ferait passer un fichier entier par le canal JSON du worker."""
    contracts, _ = load_capabilities(repo_root)
    typées = {
        f"{contract.id}.{nom}": champ
        for contract in contracts.values()
        for nom, champ in _sorties_typées(contract.output_schema).items()
    }

    # 56 depuis que la famille visage en apporte douze : chacune de ses six
    # capacités rend son relevé JSON, et cinq d'entre elles un aperçu annoté —
    # une liste de coordonnées ou une carte d'indices ne se relit pas autrement.
    assert len(typées) == 56
    mal_décrites = {
        ref
        for ref, champ in typées.items()
        if champ["type"] != "string"
        or "relatif au dossier du job" not in (champ.get("description") or "")
    }
    assert mal_décrites == set()

    # Ce parcours-ci est écrit à part : s'il trouve une sortie que le contrat ne
    # déclare pas comme fichier, c'est output_media_types() qui a un angle mort.
    assert set(typées) == {
        f"{contract.id}.{nom}" for contract in contracts.values() for nom in contract.file_outputs()
    }


def test_les_pistes_imbriquees_sont_des_sorties_fichier(repo_root):
    """Les cinq pistes de audio-separation vivent sous `tracks`, pas au premier niveau.
    S'arrêter au premier niveau les rendrait invisibles : aucun visualiseur choisi
    par l'UI, et rien à vérifier pour `_check_outputs` du runner — un job annonçant
    une piste qu'il n'a pas écrite passerait pour réussi."""
    contrat = _contrat(repo_root, "audio-separation")

    assert contrat.output_media_types() == {
        "tracks.vocals": "audio/wav",
        "tracks.accompaniment": "audio/wav",
        "tracks.drums": "audio/wav",
        "tracks.bass": "audio/wav",
        "tracks.other": "audio/wav",
    }
    # `tracks` lui-même est l'objet qui les porte, pas un fichier à ouvrir.
    assert "tracks" not in contrat.file_outputs()


# --- ce qui casse un contrat, sans casser les autres --------------------------------


def test_contrat_au_json_illisible(registry_builder):
    registry_builder.capability("text-to-speech")
    (registry_builder.root / "registry" / "capabilities" / "abimé.json").write_text(
        '{"id": "abimé", "title":', encoding="utf-8"
    )

    contracts, issues = load_capabilities(registry_builder.root)

    assert list(contracts) == ["text-to-speech"]
    assert [(i.severity, i.file) for i in issues] == [
        ("error", "registry/capabilities/abimé.json")
    ]
    assert issues[0].message.startswith("JSON illisible :")


def test_contrat_qui_n_est_pas_un_objet(registry_builder):
    registry_builder.capability("text-to-speech")
    (registry_builder.root / "registry" / "capabilities" / "liste.json").write_text(
        json.dumps([minimal_capability("liste")]), encoding="utf-8"
    )

    contracts, issues = load_capabilities(registry_builder.root)

    assert list(contracts) == ["text-to-speech"]
    assert issues == [
        Issue("error", "registry/capabilities/liste.json", "contrat qui n'est pas un objet JSON")
    ]


def test_id_du_contrat_different_du_nom_de_fichier(registry_builder):
    registry_builder.capability("autre-nom", document=minimal_capability("text-to-speech"))

    contracts, issues = load_capabilities(registry_builder.root)

    assert issues == [
        Issue(
            "error",
            "registry/capabilities/autre-nom.json",
            "id du contrat ('text-to-speech') ≠ nom de fichier",
        )
    ]
    # Chargé sous son id : le manifeste qui déclare text-to-speech le trouve quand même.
    assert list(contracts) == ["text-to-speech"]


def test_deux_contrats_pour_le_meme_id(registry_builder):
    registry_builder.capability("aa", document=minimal_capability("bb"))
    registry_builder.capability("bb")
    registry_builder.capability("text-to-speech")

    contracts, issues = load_capabilities(registry_builder.root)

    assert issues == [
        Issue(
            "error",
            "registry/capabilities/aa.json",
            "id du contrat ('bb') ≠ nom de fichier",
        ),
        Issue("error", "registry/capabilities/bb.json", "contrat en double pour 'bb'"),
    ]
    assert sorted(contracts) == ["bb", "text-to-speech"]


def test_meta_schema_absent_avertit_et_laisse_charger(registry_builder):
    """Sans méta-schéma on ne valide plus la forme des contrats, mais refuser de
    charger le registre pour autant priverait la CLI de tout le reste."""
    (registry_builder.root / "registry" / "schema" / "capability.schema.json").unlink()
    hors_forme = minimal_capability("text-to-speech")
    hors_forme["widget"] = "clé que le méta-schéma refuserait"
    registry_builder.capability("text-to-speech", document=hors_forme)

    contracts, issues = load_capabilities(registry_builder.root)

    assert issues == [
        Issue(
            "warning",
            "registry/schema/capability.schema.json",
            "méta-schéma des capacités absent — les contrats ne sont pas validés dans leur forme",
        )
    ]
    assert list(contracts) == ["text-to-speech"]


@pytest.mark.parametrize(("emplacement", "champ"), [("input", "text"), ("output", "result")])
def test_expression_rationnelle_invalide_signalee_au_chargement(
    registry_builder, emplacement, champ
):
    """Le méta-schéma laisse passer un `pattern` que `re` refuse : son mot-clé `format`
    n'est pas assertif. Sans ce contrôle, la faute ne se verrait qu'au premier
    formulaire rendu — c'est-à-dire chez l'utilisateur, et pas chez qui édite le
    contrat."""
    document = minimal_capability("text-to-speech")
    document[emplacement]["properties"][champ]["pattern"] = "[a-z"
    registry_builder.capability("text-to-speech", document=document)

    contracts, issues = load_capabilities(registry_builder.root)

    assert issues == [
        Issue(
            "error",
            "registry/capabilities/text-to-speech.json",
            f"{emplacement} n'est pas un JSON Schema valide : '[a-z' is not a 'regex'",
        )
    ]
    assert list(contracts) == ["text-to-speech"]


# --- l'objet CapabilityContract ------------------------------------------------------


def test_contrat_expose_defauts_champs_requis_et_sorties_fichier(repo_root):
    contrat = _contrat(repo_root, "text-to-speech")

    assert contrat.title == "Synthèse vocale"
    assert sorted(contrat.input_properties) == ["seed", "speed", "text", "voice"]
    assert contrat.required == ["text"]
    assert contrat.defaults() == {"speed": 1.0}  # ni voice ni seed n'ont de défaut
    assert contrat.output_media_types() == {"audio": "audio/wav"}
    assert contrat.file_outputs() == {"audio"}
    assert contrat.composite is False
    assert contrat.steps == []


def test_sortie_sans_media_type_n_est_pas_un_fichier(repo_root):
    contrat = _contrat(repo_root, "text-generation")

    assert contrat.defaults() == {
        "max_tokens": 1024,
        "temperature": 0.7,
        "top_p": 0.95,
        "repetition_penalty": 1.0,
    }
    # tokens_generated et finish_reason voyagent dans le JSON, pas sur le disque.
    assert contrat.file_outputs() == {"text"}


def test_contrat_sans_document_prend_son_id_pour_titre():
    """Le titre est ce que l'UI affiche : à défaut de libellé, l'id vaut mieux qu'un vide."""
    vide = CapabilityContract(id="text-to-speech")

    assert vide.title == "text-to-speech"
    assert vide.input_schema == {}
    assert vide.required == []
    assert vide.defaults() == {}
    assert vide.output_media_types() == {}
    assert vide.file_outputs() == set()
    assert vide.composite is False
    assert vide.steps == []


# --- validation croisée : les defaults d'un variant ----------------------------------


def test_defaut_absent_du_contrat_liste_les_parametres_existants(repo_root):
    """La liste n'est pas de la politesse : neuf fois sur dix le défaut fautif est une
    faute de frappe ou le nom qu'un autre moteur donne au même réglage, et la
    correction est dans la liste."""
    contrat = _contrat(repo_root, "text-to-speech")

    issues = check_variant_defaults(contrat, {"vitesse": 1.5}, "essai@v", "registry/models/e.yaml")

    assert issues == [
        Issue(
            "error",
            "registry/models/e.yaml",
            "essai@v : defaults.vitesse n'existe pas dans le contrat text-to-speech "
            "(paramètres : seed, speed, text, voice)",
        )
    ]


@pytest.mark.parametrize(
    ("capacité", "défauts", "message"),
    [
        (
            "text-to-speech",
            {"speed": 3.0},
            "essai@v : defaults.speed = 3.0 — 3.0 is greater than the maximum of 2.0",
        ),
        (
            "text-to-speech",
            {"speed": "vite"},
            "essai@v : defaults.speed = 'vite' — 'vite' is not of type 'number'",
        ),
        (
            "image-to-mesh",
            {"octree_resolution": 300},
            "essai@v : defaults.octree_resolution = 300 — 300 is not one of [128, 256, 384, 512]",
        ),
    ],
)
def test_defaut_qui_viole_le_sous_schema_cite_la_valeur(repo_root, capacité, défauts, message):
    contrat = _contrat(repo_root, capacité)

    issues = check_variant_defaults(contrat, défauts, "essai@v", "registry/models/e.yaml")

    assert issues == [Issue("error", "registry/models/e.yaml", message)]


def test_defauts_conformes_ne_produisent_rien(repo_root):
    contrat = _contrat(repo_root, "image-to-mesh")

    issues = check_variant_defaults(
        contrat,
        {"octree_resolution": 512, "num_inference_steps": 50, "seed": 0},
        "essai@v",
        "registry/models/e.yaml",
    )

    assert issues == []


def test_defaut_inconnu_est_une_erreur_au_chargement_du_registre(registry_builder):
    """Erreur et non avertissement : la clé n'existe pas dans le schéma d'entrée, donc
    le résolveur ne la transmettra jamais au worker. Le variant tournerait avec la
    valeur d'origine du paramètre, le manifeste continuerait d'annoncer un réglage
    qui n'est appliqué nulle part, et rien dans la sortie ne le trahirait."""
    doc = make_manifest()
    doc["variants"][0]["defaults"] = {"vitesse": 1.5}
    registry_builder.capability("text-to-speech").model(doc)

    reg = load_registry(registry_builder.root)

    assert [i.message for i in reg.errors] == [
        "test-tts@8bit-mlx : defaults.vitesse n'existe pas dans le contrat text-to-speech "
        "(paramètres : seed, speed, text, voice)"
    ]
    assert reg.warnings == []


def test_options_du_variant_ne_sont_pas_croisees_avec_le_contrat(registry_builder):
    """`options:` porte les réglages propres au runtime — la langue forcée d'un moteur
    TTS, par exemple. Ils ne sont pas interchangeables entre variants, donc pas au
    contrat, donc jamais croisés avec lui : `vitesse` y est muet là où le même nom en
    `defaults:` est une erreur."""
    doc = make_manifest()
    doc["variants"][0]["defaults"] = {"speed": 1.5}
    doc["variants"][0]["options"] = {"lang": "fr", "vitesse": 1.5}
    registry_builder.capability("text-to-speech").model(doc)

    reg = load_registry(registry_builder.root)

    assert reg.issues == []


# --- capacités composites -------------------------------------------------------------


def test_composite_bien_forme_ne_signale_rien(registry_builder):
    registry_builder.capability("text-to-image").capability("image-to-mesh")
    registry_builder.capability("text-to-mesh", document=TEXT_TO_MESH)

    contracts, issues = load_capabilities(registry_builder.root)

    assert issues == []
    composite = contracts["text-to-mesh"]
    assert composite.composite is True
    assert [é["capability"] for é in composite.steps] == ["text-to-image", "image-to-mesh"]
    assert contracts["text-to-image"].composite is False


def test_etape_designant_une_capacite_inconnue(registry_builder):
    registry_builder.capability("text-to-image")  # image-to-mesh manque à l'appel
    registry_builder.capability("text-to-mesh", document=TEXT_TO_MESH)

    _, issues = load_capabilities(registry_builder.root)

    assert issues == [
        Issue(
            "error",
            "registry/capabilities/text-to-mesh.json",
            "étape 1 : capacité inconnue 'image-to-mesh'",
        )
    ]


def test_etape_designant_une_capacite_elle_meme_composite(registry_builder):
    registry_builder.capability("text-to-image").capability("text-to-speech")
    registry_builder.capability(
        "image-to-mesh", document=_composite("image-to-mesh", "text-to-image", "text-to-speech")
    )
    registry_builder.capability("text-to-mesh", document=TEXT_TO_MESH)

    _, issues = load_capabilities(registry_builder.root)

    assert issues == [
        Issue(
            "error",
            "registry/capabilities/text-to-mesh.json",
            "étape 1 : 'image-to-mesh' est elle-même composite — "
            "une seule composition est câblée (ARCHITECTURE.md §11)",
        )
    ]


def test_aucun_modele_ne_remplit_une_capacite_composite(registry_builder):
    """Une composite s'exécute en enchaînant des jobs atomiques : lui rattacher un
    modèle laisserait croire qu'un worker sait la servir d'un bloc."""
    registry_builder.capability("text-to-image").capability("image-to-mesh")
    registry_builder.capability("text-to-mesh", document=TEXT_TO_MESH)
    registry_builder.model(make_manifest("mesh-direct", capability="text-to-mesh"))

    reg = load_registry(registry_builder.root)

    assert [i.message for i in reg.issues] == [
        "text-to-mesh est une capacité composite : elle s'exécute en enchaînant "
        "d'autres capacités, aucun modèle ne la remplit directement"
    ]
    assert list(reg.models) == ["mesh-direct"]
