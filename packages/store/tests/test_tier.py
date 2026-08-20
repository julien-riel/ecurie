"""Tiering vers un volume externe (§6.4) et télémétrie d'usage — tâches 2.5 et 2.6.

Le banc de test n'a qu'un seul volume : `require_other_volume=False` partout où la
migration doit aboutir, et un test dédié pour la garde « même volume ».
"""

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ecurie_core.config import Config, ScanConfig
from ecurie_store.cli import store_app
from ecurie_store.db import LocationRecord, StateDB
from ecurie_store.figures import compute_figures, telemetry_is_conclusive
from ecurie_store.scan import fill_from_hash_cache, run_scan
from ecurie_store.scanners import scan_tree
from ecurie_store.tier import TierError, migrate_path, tier_variant, yaml_patch
from ecurie_store.trash import list_trash
from typer.testing import CliRunner

REF = "qwen3-tts-1.7b@8bit-mlx"  # variants du registre réel du dépôt
AUTRE_REF = "trellis2@bf16"
DOSSIER_REF = "qwen3-tts-1.7b@8bit-mlx"  # nom du dossier créé sur le volume de destination

POIDS = b"P" * 4096
SHA_POIDS = "26b7e40be0bcf3e6667020b3acf6e07faa17585b21b2936305dd6c9ad3860b15"
TOKEN = b"T" * 300
SHA_TOKEN = "001f6eb6c3d14062ea4214f8b7220c63e444d32620ca7c8f0a3e900d09658c86"
AUTRE = b"Z" * 4096  # même taille que POIDS : seul le contenu les sépare
VOIX = b"V" * 2048
SHA_VOIX = "9958dd923b59a3981af666bda8fb06e82005b6058a8261dfb2ba21576f07e32a"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    """Quarantaine, base et config confinées au tmp : aucun test ne touche ~/.ecurie."""
    racine = tmp_path / "home"
    monkeypatch.setenv("ECURIE_HOME", str(racine))
    monkeypatch.setenv("COLUMNS", "400")  # sinon Rich replie les libellés attendus
    return racine


@pytest.fixture
def depot(tmp_path) -> Path:
    """Un variant téléchargé : un poids et un tokenizer dans un sous-dossier."""
    racine = tmp_path / "parc" / "depot"
    (racine / "tokenizer").mkdir(parents=True)
    (racine / "model.safetensors").write_bytes(POIDS)
    (racine / "tokenizer" / "tokenizer.json").write_bytes(TOKEN)
    return racine


@pytest.fixture
def db(tmp_path) -> Iterator[StateDB]:
    base = StateDB(tmp_path / "state.db")
    yield base
    base.close()


def observés(root: Path, ref: str | None = REF) -> list[LocationRecord]:
    return [_rattaché(rec, ref) for rec in scan_tree(root, "lmstudio")]


def _rattaché(rec: LocationRecord, ref: str | None) -> LocationRecord:
    rec.variant_ref = ref
    return rec


def _copie_interdite(source: Path, dest: Path) -> None:
    """Sentinelle : toute copie effective fait échouer le test qui l'installe."""
    raise AssertionError(f"copie inattendue de {source} vers {dest}")


def _deux_variants(depot: Path, tmp_path: Path) -> list[LocationRecord]:
    """Deux variants rattachés — l'un aura un run enregistré, l'autre jamais."""
    autre = tmp_path / "parc" / "voix"
    autre.mkdir(parents=True)
    (autre / "voix.gguf").write_bytes(VOIX)
    return observés(depot) + observés(autre, ref=AUTRE_REF)


# --- migrate_path : la séquence copie → preuve → quarantaine → lien -----------------


def test_migrate_path_copie_prouvee_puis_lien_et_quarantaine(depot, tmp_path, db, home):
    source = depot / "model.safetensors"
    dest_dir = tmp_path / "volume" / "Parc"
    inode = os.stat(source).st_ino

    résultat = migrate_path(source, dest_dir, require_other_volume=False, home=home, db=db)

    dest = dest_dir / "model.safetensors"
    assert résultat.sha256 == SHA_POIDS
    assert résultat.size == 4096
    assert résultat.reused is False
    assert résultat.dest == str(dest)
    assert dest.read_bytes() == POIDS

    assert source.is_symlink()
    assert os.readlink(source) == str(dest)
    assert source.read_bytes() == POIDS  # le lien mène au contenu, pas dans le vide

    entrées = list_trash(home=home)
    assert len(entrées) == 1
    entrée = entrées[0]
    assert entrée.reason == "tiered"
    assert entrée.origin == str(source)
    assert entrée.manifest["dest"] == str(dest)
    assert entrée.manifest["sha256"] == SHA_POIDS
    assert entrée.manifest["link_kind"] == "plain"
    assert entrée.payload.read_bytes() == POIDS  # l'original est intact, restaurable
    assert os.stat(entrée.payload).st_ino == inode  # déménagé, pas recopié


def test_migrate_path_met_le_hash_de_la_copie_en_cache(depot, tmp_path, db, home):
    source = depot / "model.safetensors"
    dest_dir = tmp_path / "volume"

    migrate_path(source, dest_dir, require_other_volume=False, home=home, db=db)

    dest = dest_dir / "model.safetensors"
    st = os.stat(dest)
    assert db.hash_cache_get(str(dest), st.st_size, st.st_mtime, st.st_ino) == SHA_POIDS

    # Ce que le scan suivant en fait : la copie déportée retrouve son sha256 sans être
    # relue, et c'est par ce hash que le résolveur la rattachera à son variant.
    copie = scan_tree(dest_dir, "tier")
    assert fill_from_hash_cache(db, copie) == 1
    assert copie[0].sha256 == SHA_POIDS
    assert copie[0].meta["hash_source"] == "verified"


def test_migrate_path_attrape_une_copie_corrompue(depot, tmp_path, db, home, monkeypatch):
    source = depot / "model.safetensors"
    dest_dir = tmp_path / "volume"
    monkeypatch.setattr(
        "ecurie_store.tier._copy_fsync", lambda src, dst: Path(dst).write_bytes(b"X" * 4096)
    )

    with pytest.raises(TierError, match="corrompue"):
        migrate_path(source, dest_dir, require_other_volume=False, home=home, db=db)

    assert not source.is_symlink()
    assert source.read_bytes() == POIDS  # l'original n'a pas bougé d'un octet
    assert not (dest_dir / "model.safetensors").exists()

    entrées = list_trash(home=home)
    assert [e.reason for e in entrées] == ["tier-copie-corrompue"]
    assert entrées[0].payload.read_bytes() == b"X" * 4096
    assert db.hash_cache_size() == 0  # une copie non prouvée n'entre pas au cache


def test_migrate_path_refuse_un_contenu_different_du_scan(depot, tmp_path, db, home, monkeypatch):
    source = depot / "model.safetensors"
    dest_dir = tmp_path / "volume"
    monkeypatch.setattr("ecurie_store.tier._copy_fsync", _copie_interdite)

    with pytest.raises(TierError, match="rescanner"):
        migrate_path(
            source,
            dest_dir,
            expected_sha256=SHA_VOIX,
            require_other_volume=False,
            home=home,
            db=db,
        )

    assert not (dest_dir / "model.safetensors").exists()
    assert not source.is_symlink()
    assert list_trash(home=home) == []


def test_migrate_path_reutilise_une_copie_deja_presente(depot, tmp_path, db, home, monkeypatch):
    source = depot / "model.safetensors"
    dest_dir = tmp_path / "volume"
    dest_dir.mkdir(parents=True)
    dest = dest_dir / "model.safetensors"
    dest.write_bytes(POIDS)
    inode = os.stat(dest).st_ino
    monkeypatch.setattr("ecurie_store.tier._copy_fsync", _copie_interdite)

    résultat = migrate_path(source, dest_dir, require_other_volume=False, home=home, db=db)

    assert résultat.reused is True
    assert résultat.sha256 == SHA_POIDS
    assert os.stat(dest).st_ino == inode  # la copie en place n'a pas été réécrite
    assert os.readlink(source) == str(dest)
    assert [e.reason for e in list_trash(home=home)] == ["tiered"]


def test_migrate_path_refuse_une_destination_occupee(depot, tmp_path, db, home, monkeypatch):
    source = depot / "model.safetensors"
    dest_dir = tmp_path / "volume"
    dest_dir.mkdir(parents=True)
    dest = dest_dir / "model.safetensors"
    dest.write_bytes(AUTRE)
    monkeypatch.setattr("ecurie_store.tier._copy_fsync", _copie_interdite)

    with pytest.raises(TierError, match="contenu différent"):
        migrate_path(source, dest_dir, require_other_volume=False, home=home, db=db)

    assert dest.read_bytes() == AUTRE
    assert not source.is_symlink()
    assert source.read_bytes() == POIDS


def test_migrate_path_refuse_le_meme_volume(depot, tmp_path, db, home):
    source = depot / "model.safetensors"

    with pytest.raises(TierError, match="même volume"):
        migrate_path(source, tmp_path / "volume", home=home, db=db)

    assert not source.is_symlink()
    assert not (tmp_path / "volume" / "model.safetensors").exists()


def test_migrate_path_refuse_un_variant_deja_deporte(depot, tmp_path, db, home):
    source = depot / "model.safetensors"
    migrate_path(source, tmp_path / "v1", require_other_volume=False, home=home, db=db)

    with pytest.raises(TierError, match="déjà un lien symbolique"):
        migrate_path(source, tmp_path / "v2", require_other_volume=False, home=home, db=db)

    assert os.readlink(source) == str(tmp_path / "v1" / "model.safetensors")
    assert len(list_trash(home=home)) == 1  # le premier déport, et lui seul


# --- tier_variant : un variant entier ------------------------------------------------


def test_tier_variant_refuse_un_variant_sans_fichier_observe(depot, tmp_path, home):
    records = observés(depot, ref=AUTRE_REF)

    with pytest.raises(TierError, match="aucun fichier observé"):
        tier_variant(records, REF, tmp_path / "volume", require_other_volume=False, home=home)

    assert not (tmp_path / "volume").exists()


def test_tier_variant_reproduit_l_arborescence(depot, tmp_path, db, home):
    dest_root = tmp_path / "volume" / "Parc"

    résultats = tier_variant(
        observés(depot), REF, dest_root, require_other_volume=False, home=home, db=db
    )

    racine = dest_root / DOSSIER_REF
    assert [r.dest for r in résultats] == [
        str(racine / "model.safetensors"),
        str(racine / "tokenizer" / "tokenizer.json"),
    ]
    assert [r.sha256 for r in résultats] == [SHA_POIDS, SHA_TOKEN]
    assert (racine / "model.safetensors").read_bytes() == POIDS
    assert (racine / "tokenizer" / "tokenizer.json").read_bytes() == TOKEN

    assert (depot / "model.safetensors").is_symlink()
    assert (depot / "tokenizer" / "tokenizer.json").is_symlink()
    assert sorted(e.origin for e in list_trash(home=home)) == [
        str(depot / "model.safetensors"),
        str(depot / "tokenizer" / "tokenizer.json"),
    ]


def test_tier_variant_dry_run_ne_deplace_rien(depot, tmp_path, db, home):
    dest_root = tmp_path / "volume"

    résultats = tier_variant(
        observés(depot),
        REF,
        dest_root,
        require_other_volume=False,
        home=home,
        db=db,
        dry_run=True,
    )

    racine = dest_root / DOSSIER_REF
    assert [r.dest for r in résultats] == [
        str(racine / "model.safetensors"),
        str(racine / "tokenizer" / "tokenizer.json"),
    ]
    assert not dest_root.exists()
    assert not (depot / "model.safetensors").is_symlink()
    assert (depot / "model.safetensors").read_bytes() == POIDS
    assert list_trash(home=home) == []
    assert db.hash_cache_size() == 0


def test_tier_variant_refuse_des_fichiers_sur_deux_volumes(depot, tmp_path, home):
    records = observés(depot)
    tokenizer = next(r for r in records if r.path.endswith("tokenizer.json"))
    tokenizer.device += 1  # comme si le tokenizer vivait sur un autre disque

    with pytest.raises(TierError, match="volumes"):
        tier_variant(records, REF, tmp_path / "volume", require_other_volume=False, home=home)

    assert not (depot / "model.safetensors").is_symlink()
    assert list_trash(home=home) == []


def test_yaml_patch_affiche_le_tier_cold_sans_toucher_au_registre(
    depot, tmp_path, db, home, repo_root
):
    patch = yaml_patch(REF)
    assert "registry/models/qwen3-tts-1.7b.yaml" in patch
    assert "- id: 8bit-mlx" in patch
    assert "tier: cold" in patch

    manifestes = sorted((repo_root / "registry" / "models").glob("*.yaml"))
    assert manifestes  # sans quoi la comparaison ci-dessous ne prouverait rien
    avant = {p: p.read_bytes() for p in manifestes}

    tier_variant(
        observés(depot), REF, tmp_path / "volume", require_other_volume=False, home=home, db=db
    )

    assert {p: p.read_bytes() for p in manifestes} == avant


# --- le lien froid vu par le scan (ARCHITECTURE.md §6.4) -----------------------------


def test_scan_retrouve_le_lien_froid_puis_le_volume_absent(depot, tmp_path, db, home):
    dest_root = tmp_path / "volume"
    tier_variant(observés(depot), REF, dest_root, require_other_volume=False, home=home, db=db)
    config = Config(scan=ScanConfig(lmstudio=depot), tier_volumes=[dest_root])

    report = run_scan(config, db)
    assert report.counts == {"lmstudio": 2, "tier": 2}
    assert report.cached_hashes == 2  # les copies déportées retrouvent leur sha256

    records = db.locations()
    liens = {r.path: r for r in records if r.link_kind == "symlink"}
    assert sorted(liens) == [
        str(depot / "model.safetensors"),
        str(depot / "tokenizer" / "tokenizer.json"),
    ]
    lien = liens[str(depot / "model.safetensors")]
    assert lien.size == 0  # un lien n'occupe pas les octets de sa cible
    assert lien.meta["available"] is True
    assert lien.meta["target"] == str(dest_root / DOSSIER_REF / "model.safetensors")

    figures = compute_figures(records)
    assert figures.apparent_bytes == 4396  # comptés une fois, là où ils sont réellement
    assert len(figures.cold) == 2
    assert figures.cold_unavailable == []

    # Volume démonté : les liens restent, leur cible non — c'est « froid indisponible ».
    dest_root.rename(tmp_path / "volume-demonte")
    report = run_scan(config, db)
    assert "chemin absent" in report.skipped["tier"]
    figures = compute_figures(db.locations())
    assert [c.path for c in figures.cold_unavailable] == sorted(liens)


# --- télémétrie (tâche 2.6) -----------------------------------------------------------


def test_runs_enregistres_et_dernier_par_variant(db):
    assert db.runs_count() == 0
    assert db.last_run_by_variant() == {}

    db.record_run("r1", REF, started_at="2026-01-05T10:00:00+00:00", duration_ms=1200, ok=True)
    db.record_run("r2", REF, started_at="2026-06-01T09:30:00+00:00", ok=False)
    db.record_run("r3", AUTRE_REF, started_at="2026-02-02T08:00:00+00:00")

    assert db.runs_count() == 3
    assert db.last_run_by_variant() == {
        REF: "2026-06-01T09:30:00+00:00",
        AUTRE_REF: "2026-02-02T08:00:00+00:00",
    }

    # Un même identifiant de run corrige sa ligne, il n'en ajoute pas une seconde.
    db.record_run("r2", REF, started_at="2026-07-01T09:30:00+00:00")
    assert db.runs_count() == 3
    assert db.last_run_by_variant()[REF] == "2026-07-01T09:30:00+00:00"


def test_poste_jamais_utilise_inconnu_tant_que_runs_est_vide(depot, tmp_path):
    figures = compute_figures(_deux_variants(depot, tmp_path))

    assert figures.recoverable.unused_known is False
    assert figures.recoverable.unused_bytes == 0
    assert figures.unused_variants == []
    assert figures.recoverable.total_known_bytes == 0


def test_un_run_fait_basculer_les_variants_sans_run(depot, tmp_path):
    figures = compute_figures(
        _deux_variants(depot, tmp_path),
        last_runs={REF: "2026-08-18T12:00:00+00:00"},
        telemetry=True,
        now=datetime(2026, 8, 19, tzinfo=UTC),
    )

    assert figures.recoverable.unused_known is True
    assert figures.unused_variants == [AUTRE_REF]
    assert figures.recoverable.unused_bytes == 2048  # le seul variant sans exécution
    assert figures.recoverable.total_known_bytes == 2048


def test_cli_tier_dry_run_annonce_sans_rien_deplacer(depot, tmp_path, home):
    base = StateDB(home / "state.db")
    base.replace_manager("lmstudio", observés(depot))
    base.close()
    dest = tmp_path / "volume"

    résultat = CliRunner().invoke(store_app, ["tier", REF, str(dest), "--dry-run"])

    assert résultat.exit_code == 0
    assert "2 fichier(s), 4 ko" in résultat.stdout
    assert "à blanc" in résultat.stdout
    assert not dest.exists()
    assert not (depot / "model.safetensors").is_symlink()
    assert list_trash(home=home) == []


def test_cli_status_dit_inconnu_puis_chiffre_le_poste(depot, tmp_path, home):
    base = StateDB(home / "state.db")
    base.replace_manager("lmstudio", _deux_variants(depot, tmp_path))
    base.close()
    runner = CliRunner()

    résultat = runner.invoke(store_app, ["status"])
    assert résultat.exit_code == 0
    assert "inconnu" in résultat.stdout  # pas 0 : aucun usage observé ≠ aucun usage

    # Une télémétrie ouverte à l'instant ne prouve rien sur les trois mois d'avant :
    # le poste reste inconnu tant qu'elle n'observe pas depuis assez longtemps.
    base = StateDB(home / "state.db")
    base.record_run("r0", REF)
    base.close()
    résultat = runner.invoke(store_app, ["status"])
    assert résultat.exit_code == 0
    assert "trop jeune" in résultat.stdout

    base = StateDB(home / "state.db")
    base.record_run("r1", REF, started_at=(datetime.now(UTC) - timedelta(days=200)).isoformat())
    base.close()

    résultat = runner.invoke(store_app, ["status"])
    assert résultat.exit_code == 0
    assert "inconnu" not in résultat.stdout

    résultat = runner.invoke(store_app, ["status", "--json"])
    assert résultat.exit_code == 0
    payload = json.loads(résultat.stdout)
    assert payload["recoverable"]["unused_known"] is True
    assert payload["recoverable"]["unused_bytes"] == 2048
    assert payload["unused_variants"] == [AUTRE_REF]


# --- un inode partagé est un seul fichier, pas deux ---------------------------------


def test_tier_variant_ne_copie_qu_une_fois_un_inode_partage(tmp_path, db, home):
    """Deux chemins d'un même inode sont le même fichier : le déporter deux fois
    écrirait deux fois les mêmes octets sur le volume externe. Et il faut convertir
    TOUS ses chemins, sinon l'inode reste tenu et la place ne revient pas."""
    parc = tmp_path / "parc" / "depot"
    parc.mkdir(parents=True)
    original = parc / "model.safetensors"
    original.write_bytes(POIDS)
    jumeau = parc / "alias.safetensors"
    os.link(original, jumeau)

    records = observés(parc)
    assert {r.meta["nlink"] for r in records} == {2}

    résultats = tier_variant(
        records, REF, tmp_path / "volume", require_other_volume=False, home=home, db=db
    )

    destinations = {r.dest for r in résultats}
    assert len(destinations) == 1  # une seule copie pour les deux chemins
    (destination,) = destinations
    assert Path(destination).read_bytes() == POIDS

    assert original.is_symlink() and jumeau.is_symlink()
    assert os.readlink(original) == destination
    assert os.readlink(jumeau) == destination
    # La place n'est rendue qu'une fois, et seulement parce que les deux chemins
    # du même inode sont partis : en laisser un aurait tout retenu.
    assert sum(r.freed_bytes for r in résultats) == 4096
    assert sorted(e.origin for e in list_trash(home=home)) == [str(jumeau), str(original)]


def test_une_telemetrie_trop_jeune_ne_conclut_rien():
    """« Jamais exécuté » ne se déduit pas d'un journal ouvert hier : tant que la
    télémétrie n'observe pas depuis le délai considéré, le poste reste inconnu."""
    maintenant = datetime(2026, 8, 19, tzinfo=UTC)
    hier = (maintenant - timedelta(days=1)).isoformat()
    il_y_a_un_an = (maintenant - timedelta(days=365)).isoformat()

    assert telemetry_is_conclusive(None, 90, maintenant) is False
    assert telemetry_is_conclusive(hier, 90, maintenant) is False
    assert telemetry_is_conclusive(il_y_a_un_an, 90, maintenant) is True
    assert telemetry_is_conclusive("pas une date", 90, maintenant) is False
