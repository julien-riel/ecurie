"""La conversion d'un contrat en `inputSchema`, et ce qu'elle doit retirer.

Ces tests portent sur la seule chose que le §6.3 promet d'automatique — « le bloc
`input` devient l'`inputSchema`, `x-ui` est ignoré » — et sur ce que le SDK ne
fait pas à notre place : valider les arguments.
"""

import json
from pathlib import Path

import pytest
from ecurie_core.capabilities import CapabilityContract
from ecurie_core.models import Variant
from ecurie_mcp import schemas

REPO_ROOT = Path(__file__).parents[3]


def contrat(cap_id: str) -> CapabilityContract:
    chemin = REPO_ROOT / "registry" / "capabilities" / f"{cap_id}.json"
    return CapabilityContract(id=cap_id, document=json.loads(chemin.read_text()))


def test_les_extensions_x_ne_sortent_pas_du_schema():
    """Un client qui valide en AJV strict refuse un mot-clé inconnu.

    L'UI, elle, désactive ce mode pour pouvoir lire `x-ui` ; on ne peut pas
    parier sur la même indulgence chez un client MCP qu'on ne connaît pas.
    """
    schéma = schemas.input_schema(contrat("text-to-speech"))
    sérialisé = json.dumps(schéma)
    assert "x-ui" not in sérialisé
    assert "x-options-from" not in sérialisé


def test_le_retrait_est_recursif():
    """Un `x-ui` porté par un champ imbriqué ou par les `items` d'un tableau."""
    document = {
        "id": "essai",
        "input": {
            "type": "object",
            "properties": {
                "fichiers": {
                    "type": "array",
                    "x-ui": "file",
                    "items": {"type": "string", "x-ui": "file"},
                },
                "réglages": {
                    "type": "object",
                    "properties": {"grain": {"type": "number", "x-ui": "slider"}},
                },
            },
        },
    }
    schéma = schemas.input_schema(CapabilityContract(id="essai", document=document))
    assert "x-ui" not in json.dumps(schéma)
    # Ce qui n'est pas une extension reste intact : le retrait ne doit pas
    # emporter la structure avec lui.
    assert schéma["properties"]["fichiers"]["items"]["type"] == "string"
    assert schéma["properties"]["réglages"]["properties"]["grain"]["type"] == "number"


def test_le_contrat_charge_nest_pas_modifie():
    """Le même contrat sert l'API, qui a besoin de ses `x-ui`."""
    contract = contrat("text-to-speech")
    schemas.input_schema(contract)
    assert contract.input_properties["text"]["x-ui"] == "textarea"


def test_les_defauts_du_variant_priment_sur_ceux_du_contrat():
    """`merge_defaults` fait diverger les deux ; le schéma annonce ce qui s'appliquera."""
    contract = contrat("text-to-speech")
    assert contract.input_properties["speed"]["default"] == 1.0

    variant = Variant.model_validate(
        {
            "id": "essai",
            "runtime": "mlx-audio",
            "source": {"kind": "local", "path": "/tmp"},
            "defaults": {"speed": 0.8},
        }
    )
    schéma = schemas.input_schema(contract, variant)
    assert schéma["properties"]["speed"]["default"] == 0.8


def test_un_champ_a_options_dynamiques_dit_dou_viennent_ses_valeurs():
    """Sans cela, un modèle invente une voix et le job meurt après le chargement."""
    schéma = schemas.input_schema(contrat("text-to-speech"))
    description = schéma["properties"]["voice"]["description"]
    assert "announced by the model" in description
    # Et surtout : aucune valeur inventée.
    assert "alloy" not in description


def test_la_racine_est_toujours_un_objet():
    """La spec l'exige, même pour un contrat sans bloc `input`."""
    schéma = schemas.input_schema(CapabilityContract(id="vide", document={"id": "vide"}))
    assert schéma["type"] == "object"


@pytest.mark.parametrize(
    "arguments, attendu",
    [
        ({}, "text"),  # requis manquant
        ({"text": "bonjour", "speed": 9.0}, "speed"),  # hors bornes
        ({"text": "bonjour", "inconnu": 1}, "inconnu"),  # additionalProperties: false
    ],
)
def test_la_validation_rattrape_ce_que_le_sdk_laisse_passer(arguments, attendu):
    """Le SDK 2.1.1 ne valide pas les arguments : ces trois-là atteindraient le worker."""
    schéma = schemas.input_schema(contrat("text-to-speech"))
    reproches = schemas.valider(schéma, arguments)
    assert reproches, f"{arguments} aurait dû être refusé"
    assert any(attendu in r for r in reproches)


def test_tous_les_reproches_sont_rendus_ensemble():
    """Un agent qui corrige un champ à la fois paie un aller-retour par erreur."""
    schéma = schemas.input_schema(contrat("text-to-speech"))
    reproches = schemas.valider(schéma, {"speed": 9.0, "inconnu": 1})
    assert len(reproches) >= 2


def test_une_entree_valide_ne_reproche_rien():
    schéma = schemas.input_schema(contrat("text-to-speech"))
    assert schemas.valider(schéma, {"text": "bonjour"}) == []


def test_le_schema_de_sortie_decrit_lenveloppe_et_non_la_sortie_nue():
    """La spec impose de s'y conformer : il doit décrire ce qu'on rend vraiment."""
    schéma = schemas.output_schema(contrat("text-to-speech"))
    assert set(schéma["required"]) == {"ok", "capability", "ref", "job_id", "output"}
    # La sortie du contrat vit dessous, à sa forme exacte.
    assert schéma["properties"]["output"]["properties"]["audio"]["contentMediaType"] == "audio/wav"
