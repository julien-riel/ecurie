"""Rattachement des fichiers aux variants, quand plusieurs se partagent des poids.

Le §4 de l'architecture y invite explicitement : un même jeu de poids peut
remplir deux capacités — les mêmes poids vision-langage transcrivent un document
et décrivent une image —, et le registre le déclare par deux manifestes pointant
le même dépôt. Les octets, eux, n'existent qu'une fois.

Le rattachement se faisait par un dictionnaire indexé par dépôt, donc le dernier
manifeste chargé écrasait le premier **en silence**. Trois conséquences, dans
l'ordre de gravité : la comptabilité par variant attribuait plusieurs gigaoctets
au mauvais modèle ; `ecurie store tier` n'en déportait qu'une partie ; et le
poste « jamais utilisé » du plan de récupération proposait à la corbeille des
poids servis tous les jours par l'autre manifeste. C'est le dernier qui fait de
ce fichier un test de non-régression et pas un test de confort.
"""

from datetime import UTC, datetime, timedelta

import yaml
from ecurie_core.registry import load_registry
from ecurie_store.db import LocationRecord
from ecurie_store.figures import compute_figures, unused_variants
from ecurie_store.resolver import resolve_variant_refs
from ecurie_store.tier import variant_records
from ecurie_store.weights import variant_disk_bytes

REPO = "editeur/poids-partages"
REVISION = "a" * 40
AUTRE_REVISION = "b" * 40


def _manifeste(model_id: str, capability: str, revision: str = REVISION) -> dict:
    return {
        "id": model_id,
        "capability": capability,
        "license": "apache-2.0",
        "status": "candidate",
        "variants": [
            {
                "id": "4bit",
                "tier": "absent",
                "runtime": "mlx-vlm",
                "source": {"kind": "huggingface", "repo": REPO, "revision": revision},
            }
        ],
    }


def _parc(tmp_path, *manifestes: dict):
    """Un registre minimal sur disque, avec les manifestes donnés."""
    import shutil
    from pathlib import Path

    racine = tmp_path / "depot"
    (racine / "registry" / "schema").mkdir(parents=True)
    (racine / "registry" / "models").mkdir(parents=True)
    (racine / "registry" / "capabilities").mkdir(parents=True)
    source = Path(__file__).parents[3] / "registry"
    for nom in ("model.schema.json", "capability.schema.json"):
        shutil.copy(source / "schema" / nom, racine / "registry" / "schema" / nom)
    for document in manifestes:
        shutil.copy(
            source / "capabilities" / f"{document['capability']}.json",
            racine / "registry" / "capabilities" / f"{document['capability']}.json",
        )
        (racine / "registry" / "models" / f"{document['id']}.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        )
    return load_registry(racine)


def _fichier(path: str = "/hub/models--editeur--poids-partages/blobs/aa", size: int = 5000):
    return LocationRecord(
        path=path,
        manager="hf",
        size=size,
        mtime=0.0,
        device=1,
        inode=10,
        link_kind="plain",
        sha256="a" * 64,
        meta={"repos": [REPO], "nlink": 1},
    )


# --- rattachement -----------------------------------------------------------------


def test_deux_manifestes_sur_le_meme_depot_sont_tous_deux_rattaches(tmp_path):
    registre = _parc(
        tmp_path,
        _manifeste("lecteur", "document-to-text"),
        _manifeste("descripteur", "image-to-text"),
    )
    records = [_fichier()]

    resolve_variant_refs(registre, records)

    assert records[0].variant_refs == ["descripteur@4bit", "lecteur@4bit"]
    # L'étiquette principale reste unique, et stable : tout le code qui n'a besoin
    # que d'un nom continue de marcher, et deux scans du même parc s'accordent.
    assert records[0].variant_ref == "descripteur@4bit"


def test_un_seul_manifeste_ne_laisse_aucune_liste_dans_le_meta(tmp_path):
    """Le cas courant ne doit rien payer : pas de clé en plus dans l'état observé."""
    registre = _parc(tmp_path, _manifeste("lecteur", "document-to-text"))
    records = [_fichier()]

    resolve_variant_refs(registre, records)

    assert records[0].variant_refs == ["lecteur@4bit"]
    assert "variant_refs" not in records[0].meta


def test_le_partage_se_voit_partout_ou_l_on_compte_des_octets(tmp_path):
    registre = _parc(
        tmp_path,
        _manifeste("lecteur", "document-to-text"),
        _manifeste("descripteur", "image-to-text"),
    )
    records = [_fichier(size=5000)]
    resolve_variant_refs(registre, records)

    # Les deux variants pèsent les mêmes 5 000 octets : ce sont les mêmes fichiers,
    # comptés une fois par le parc et une fois par chacun de ceux qui s'en servent.
    assert variant_disk_bytes(records, "lecteur@4bit") == 5000
    assert variant_disk_bytes(records, "descripteur@4bit") == 5000
    assert [r.path for r in variant_records(records, "lecteur@4bit")] == [records[0].path]
    assert [r.path for r in variant_records(records, "descripteur@4bit")] == [records[0].path]


# --- le poste « jamais utilisé » ---------------------------------------------------


def _telemetrie(*refs: str) -> dict[str, str]:
    """Un usage tout récent pour chacun des variants nommés."""
    maintenant = datetime.now(UTC).isoformat()
    return {ref: maintenant for ref in refs}


def test_il_suffit_qu_un_des_deux_variants_serve_pour_que_les_poids_restent(tmp_path):
    """Le test qui vaut le correctif : sans lui, ces octets partaient à la corbeille."""
    registre = _parc(
        tmp_path,
        _manifeste("lecteur", "document-to-text"),
        _manifeste("descripteur", "image-to-text"),
    )
    records = [_fichier()]
    resolve_variant_refs(registre, records)

    # `descripteur` est l'étiquette principale et n'a jamais servi ; `lecteur`,
    # lui, tourne tous les jours. Les fichiers sont les mêmes.
    inutilisés = unused_variants(records, _telemetrie("lecteur@4bit"), unused_after_days=90)
    assert inutilisés == {"descripteur@4bit"}

    figures = compute_figures(
        records, last_runs=_telemetrie("lecteur@4bit"), telemetry=True, unused_after_days=90
    )
    assert figures.recoverable.unused_bytes == 0


def test_des_poids_qu_aucun_des_deux_variants_n_emploie_restent_recuperables(tmp_path):
    registre = _parc(
        tmp_path,
        _manifeste("lecteur", "document-to-text"),
        _manifeste("descripteur", "image-to-text"),
    )
    records = [_fichier()]
    resolve_variant_refs(registre, records)

    vieux = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    figures = compute_figures(
        records,
        last_runs={"lecteur@4bit": vieux, "descripteur@4bit": vieux},
        telemetry=True,
        unused_after_days=90,
    )

    assert figures.recoverable.unused_bytes == 5000


# --- l'ambiguïté que le registre doit signaler -------------------------------------


def test_un_depot_declare_a_deux_revisions_est_signale(tmp_path):
    """Deux instantanés dans le cache, un rattachement par dépôt : l'attribution
    par variant devient une supposition, et il faut le dire."""
    registre = _parc(
        tmp_path,
        _manifeste("lecteur", "document-to-text", revision=REVISION),
        _manifeste("descripteur", "image-to-text", revision=AUTRE_REVISION),
    )

    avertissements = [i for i in registre.warnings if REPO in i.message]
    assert len(avertissements) == 1
    assert "révisions" in avertissements[0].message


def test_un_depot_partage_a_la_meme_revision_ne_signale_rien(tmp_path):
    """C'est l'usage prévu, pas une anomalie : les mêmes poids, deux capacités."""
    registre = _parc(
        tmp_path,
        _manifeste("lecteur", "document-to-text"),
        _manifeste("descripteur", "image-to-text"),
    )

    assert not [i for i in registre.warnings if REPO in i.message]
