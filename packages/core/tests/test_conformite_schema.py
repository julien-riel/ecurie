"""Le JSON Schema et son miroir pydantic acceptent-ils les mêmes documents ?

« Un test de conformité garantit que pydantic et le JSON Schema acceptent /
refusent les mêmes documents — le schéma JSON reste l'autorité » (CONCEPTION.md
§3). C'est ce test.

Ce n'est pas de la coquetterie : le schéma est ce que lisent la CI et les agents
de veille, pydantic est ce que lit le code. Quand les deux divergent, un
manifeste passe la revue et casse à l'exécution — ou pire, il est accepté par
les deux avec des sens différents. `load_registry` traite d'ailleurs la
divergence comme une erreur, sous un message (« divergence pydantic/schéma »)
qui ne dit pas laquelle des deux définitions est fautive : ce test-ci le dit.
"""

import json
from pathlib import Path

import pytest
from ecurie_core.models import Model
from jsonschema import Draft202012Validator
from manifest_helpers import make_manifest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).parents[3]


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schéma = json.loads((REPO_ROOT / "registry" / "schema" / "model.schema.json").read_text())
    return Draft202012Validator(schéma)


def accepte_par_le_schema(validator: Draft202012Validator, doc: dict) -> bool:
    return not list(validator.iter_errors(doc))


def accepte_par_pydantic(doc: dict) -> bool:
    try:
        Model.model_validate(doc)
    except ValidationError:
        return False
    return True


def muté(**modifications) -> dict:
    """Un manifeste valide, sur lequel on applique une mutation dans le variant."""
    doc = make_manifest()
    doc["variants"][0].update(modifications)
    return doc


# Chaque cas : un document, et s'il devrait être accepté. Les deux validateurs
# doivent tomber d'accord sur chacun — c'est la seule assertion du fichier.
CAS = [
    ("manifeste minimal", make_manifest(), True),
    (
        "profil complet",
        muté(
            profile={
                "disk_bytes": 1,
                "peak_unified_memory_bytes": 2,
                "warmup_ms": 3,
                "latency_ms_p50": 4,
                "rtf": 0.5,
                "throughput": "2× temps réel",
                "measured_on": "M5 24 Gio / macOS 26",
                "measured_at": "2026-08-20",
                "harness_version": "0.3.0",
            }
        ),
        True,
    ),
    ("options propres au runtime", muté(options={"language": "french"}), True),
    ("defaults", muté(defaults={"speed": 1.0}), True),
    # Les fautes de frappe dans un sous-objet : le piège que `additionalProperties`
    # ferme. Sans lui, `revison` est simplement ignoré et la révision épinglée
    # disparaît sans un mot.
    (
        "faute de frappe dans source",
        muté(source={"kind": "huggingface", "repo": "org/nom", "revison": "abc1234"}),
        False,
    ),
    (
        "champ inventé dans source",
        muté(source={"kind": "huggingface", "repo": "org/nom", "revision": "abc1234",
                     "filename": "x.safetensors"}),
        False,
    ),
    (
        "champ inventé dans profile",
        muté(
            profile={
                "disk_bytes": 1,
                "peak_unified_memory_bytes": 2,
                "measured_on": "M",
                "measured_at": "2026-08-20",
                "gpu_layers": 30,
            }
        ),
        False,
    ),
    ("champ inventé dans le variant", muté(gpu_layers=30), False),
    ("révision flottante", muté(source={"kind": "huggingface", "repo": "o/n",
                                        "revision": "main"}), False),
    ("révision trop courte", muté(source={"kind": "huggingface", "repo": "o/n",
                                          "revision": "abc12"}), False),
    ("quantization inconnue", muté(quantization="Q3_K_XS"), False),
    ("runtime inconnu", muté(runtime="vllm"), False),
    ("tier inconnu", muté(tier="tiède"), False),
    ("source de type inconnu", muté(source={"kind": "torrent"}), False),
    # Un variant peut avoir besoin de plus d'un dépôt — un tokenizer publié à
    # part, un encodeur visuel. La source secondaire suit exactement les mêmes
    # règles que celle des poids : la révision y est épinglée de la même façon,
    # et un champ inventé y est refusé de la même façon.
    (
        "second dépôt avec son rôle",
        muté(
            extra_sources=[
                {
                    "kind": "huggingface",
                    "repo": "Qwen/Qwen2-1.5B",
                    "revision": "8a16abf2848eda07cc5253dec660bf1ce007ad7a",
                    "role": "tokenizer",
                }
            ]
        ),
        True,
    ),
    (
        "second dépôt à révision flottante",
        muté(extra_sources=[{"kind": "huggingface", "repo": "o/n", "revision": "main"}]),
        False,
    ),
    (
        "rôle en majuscules",
        muté(extra_sources=[{"kind": "huggingface", "repo": "o/n", "role": "Tokenizer"}]),
        False,
    ),
    (
        "champ inventé dans un second dépôt",
        muté(extra_sources=[{"kind": "huggingface", "repo": "o/n", "priorité": 2}]),
        False,
    ),
]


@pytest.mark.parametrize(("nom", "document", "attendu"), CAS, ids=[c[0] for c in CAS])
def test_le_schema_et_pydantic_disent_la_meme_chose(validator, nom, document, attendu):
    par_schéma = accepte_par_le_schema(validator, document)
    par_pydantic = accepte_par_pydantic(document)
    assert par_schéma == par_pydantic, (
        f"{nom} : le schéma dit {par_schéma}, pydantic dit {par_pydantic} — "
        "l'un des deux est à corriger, et c'est le schéma qui fait autorité"
    )
    assert par_schéma is attendu


def test_champ_invente_a_la_racine(validator):
    doc = {**make_manifest(), "maintainer": "moi"}
    assert not accepte_par_le_schema(validator, doc)
    assert not accepte_par_pydantic(doc)


def test_champ_invente_dans_links(validator):
    doc = {**make_manifest(), "links": {"demo": "https://exemple.test"}}
    assert not accepte_par_le_schema(validator, doc)
    assert not accepte_par_pydantic(doc)


def test_les_manifestes_reels_passent_les_deux(validator):
    import yaml

    for chemin in sorted((REPO_ROOT / "registry" / "models").glob("*.yaml")):
        doc = yaml.safe_load(chemin.read_text())
        assert accepte_par_le_schema(validator, doc), chemin.name
        assert accepte_par_pydantic(doc), chemin.name
