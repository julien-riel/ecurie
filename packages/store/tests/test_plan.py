"""Générateur de plan de GC (CONCEPTION.md §4.3).

Le plan est le document qu'on relit avant de laisser un outil toucher trente
giga-octets : son format, son total et les empreintes qu'il embarque sont des
promesses faites au lecteur autant qu'à `ecurie store apply`.

Deux contrats sont vérifiés ici plus que le reste :
- le total du plan est celui du rapport `status` — plan et rapport dérivent de
  la même classification, ils ne peuvent pas annoncer deux gains différents ;
- aucune action ne dit « lie ces deux fichiers » sans le sha256 qui le prouve.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ecurie_store.db import LocationRecord
from ecurie_store.figures import compute_figures
from ecurie_store.plan import (
    PLAN_VERSION,
    REASON_LABELS,
    PlanError,
    generate_plan,
    load_plan,
    summarize,
    write_plan,
)
from ecurie_store.scanners import scan_hf, scan_ollama, scan_tree
from park import REV_STALE, SHA_HORPH, SHA_LIVE, SHA_OORPH, SHA_STALE

# Horodatage figé : un plan est un document daté, sa date doit être celle qu'on lui donne.
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

DEVICE = 42  # un seul volume dans les jeux construits à la main : les liens durs y sont permis

SHA_A = "aa" * 32
SHA_B = "bb" * 32
SHA_C = "cc" * 32
SHA_D = "dd" * 32


def _parc(fake_hf, fake_ollama, fake_lmstudio) -> list[LocationRecord]:
    return scan_hf(fake_hf) + scan_ollama(fake_ollama) + scan_tree(fake_lmstudio, "lmstudio")


def _rec(
    path: str,
    *,
    size: int,
    inode: int,
    manager: str = "lmstudio",
    sha256: str | None = None,
    variant_ref: str | None = None,
    device: int = DEVICE,
    **meta,
) -> LocationRecord:
    """Une Location observée, fabriquée à la main : le plan ne lit jamais le disque."""
    return LocationRecord(
        path=path,
        manager=manager,
        size=size,
        mtime=1_700_000_000.0,
        device=device,
        inode=inode,
        link_kind="plain",
        sha256=sha256,
        variant_ref=variant_ref,
        meta=meta,
    )


def _actions(plan: dict, kind: str) -> list[dict]:
    return [a for a in plan["actions"] if a["kind"] == kind]


def _touched(action: dict) -> list[str]:
    """Les chemins que l'action déplacerait ou remplacerait."""
    if action["kind"] == "hardlink":
        return [action["keep"], *action["replace"]]
    return [action["path"]]


# --- format du document ----------------------------------------------------------


def test_format_du_plan_suit_la_conception(fake_hf, fake_ollama, fake_lmstudio):
    plan = generate_plan(
        _parc(fake_hf, fake_ollama, fake_lmstudio), scan_id="scan-42", now=NOW, plan_id="p1"
    )

    assert plan["generated_at"] == "2026-08-19T12:00:00+00:00"
    assert plan["scan_id"] == "scan-42"  # un plan sait de quel scan il descend
    assert plan["plan_id"] == "p1"
    assert plan["plan_version"] == PLAN_VERSION
    assert plan["total_bytes_reclaimed"] == sum(a["bytes_reclaimed"] for a in plan["actions"])

    for action in plan["actions"]:
        assert action["reason"] in REASON_LABELS  # sinon la CLI affiche un slug brut
        assert isinstance(action["bytes_reclaimed"], int)

    hardlink = _actions(plan, "hardlink")[0]
    assert set(hardlink) >= {"kind", "keep", "replace", "sha256", "bytes_reclaimed"}
    assert isinstance(hardlink["keep"], str)
    assert isinstance(hardlink["replace"], list)
    assert hardlink["keep"] not in hardlink["replace"]

    trash = _actions(plan, "trash")[0]
    assert set(trash) >= {"kind", "path", "reason", "bytes_reclaimed"}


def test_gain_par_poste_exact_sur_le_faux_parc(fake_hf, fake_ollama, fake_lmstudio):
    plan = generate_plan(_parc(fake_hf, fake_ollama, fake_lmstudio), now=NOW)

    assert plan["by_reason"] == {
        "duplication": 1000,  # blob LIVE : présent chez HF et chez Ollama
        "hf-stale-revision": 500,
        "orphan-blob": 300,  # 200 (HF) + 100 (Ollama)
        "detached-snapshot": 0,
    }
    assert plan["total_bytes_reclaimed"] == 1800
    assert plan["ignored"] == []
    # Le poste le plus gras d'abord : c'est l'ordre d'affichage de `store plan`.
    assert list(summarize(plan)) == [
        "duplication",
        "hf-stale-revision",
        "orphan-blob",
        "detached-snapshot",
    ]

    hardlink = _actions(plan, "hardlink")
    assert len(hardlink) == 1
    assert hardlink[0]["sha256"] == SHA_LIVE
    assert hardlink[0]["bytes_reclaimed"] == 1000
    # HF garde le vrai fichier : ses snapshots pointent dessus par lien symbolique.
    assert hardlink[0]["keep"].endswith(f"blobs/{SHA_LIVE}")
    assert [Path(p).name for p in hardlink[0]["replace"]] == [f"sha256-{SHA_LIVE}"]

    corbeille = {Path(a["path"]).name: a["bytes_reclaimed"] for a in _actions(plan, "trash")}
    assert corbeille[SHA_STALE] == 500
    assert corbeille[SHA_HORPH] == 200
    assert corbeille[f"sha256-{SHA_OORPH}"] == 100
    # Les deux .gguf de LM Studio sont déjà le même inode : rien à récupérer là.
    assert not [c for a in plan["actions"] for c in _touched(a) if c.endswith(".gguf")]


# --- le contrat de non-divergence avec les trois chiffres --------------------------


def test_le_total_du_plan_egale_le_recuperable_du_rapport(fake_hf, fake_ollama, fake_lmstudio):
    records = _parc(fake_hf, fake_ollama, fake_lmstudio)
    plan = generate_plan(records, now=NOW)
    figures = compute_figures(records)

    assert plan["total_bytes_reclaimed"] == figures.recoverable.total_known_bytes == 1800


def _quatre_postes() -> list[LocationRecord]:
    """Les quatre postes du récupérable, un contenu distinct par poste."""
    return [
        # duplication : deux copies vivantes du même contenu, deux gestionnaires
        _rec("/hf/blobs/x", size=1000, inode=1, manager="hf", sha256=SHA_A,
             variant_ref="m@servi", hash_source="announced"),
        _rec("/ollama/blobs/x", size=1000, inode=2, manager="ollama", sha256=SHA_A,
             variant_ref="m@servi", hash_source="announced"),
        # révision HF obsolète
        _rec("/hf/blobs/vieux", size=500, inode=3, manager="hf", sha256=SHA_B, stale=True),
        # blob orphelin
        _rec("/ollama/blobs/orphelin", size=200, inode=4, manager="ollama", sha256=SHA_C,
             orphan=True),
        # variant jamais lancé
        _rec("/lms/jamais.gguf", size=700, inode=5, sha256=SHA_D, variant_ref="m@jamais",
             hash_source="verified"),
    ]


LAST_RUNS = {"m@servi": "2026-08-10T00:00:00+00:00"}


@pytest.mark.parametrize(("telemetry", "attendu"), [(False, 1700), (True, 2400)])
def test_le_total_egale_le_recuperable_sur_les_quatre_postes(telemetry, attendu):
    records = _quatre_postes()
    plan = generate_plan(records, last_runs=LAST_RUNS, telemetry=telemetry, now=NOW)
    figures = compute_figures(records, last_runs=LAST_RUNS, telemetry=telemetry, now=NOW)

    assert plan["total_bytes_reclaimed"] == figures.recoverable.total_known_bytes == attendu
    assert plan["by_reason"]["duplication"] == figures.recoverable.duplication_bytes == 1000
    assert plan["by_reason"]["hf-stale-revision"] == figures.recoverable.hf_stale_bytes == 500
    assert plan["by_reason"]["orphan-blob"] == figures.recoverable.orphan_bytes == 200


# --- empreintes : ce que `apply` revérifiera sur le disque -------------------------


def test_chaque_action_embarque_l_empreinte_des_chemins_touches(
    fake_hf, fake_ollama, fake_lmstudio
):
    plan = generate_plan(_parc(fake_hf, fake_ollama, fake_lmstudio), now=NOW)

    fichiers = [a for a in plan["actions"] if a["reason"] != "detached-snapshot"]
    assert fichiers, "le faux parc doit produire des actions sur des fichiers"
    for action in fichiers:
        chemins = _touched(action)
        assert sorted(action["stats"]) == sorted(chemins)
        for chemin in chemins:
            st = os.lstat(chemin)
            # Les quatre champs que `apply` compare avant d'agir : un seul manquant
            # et l'action s'appliquerait à un fichier qui a changé depuis le scan.
            assert action["stats"][chemin] == {
                "size": st.st_size,
                "mtime": st.st_mtime,
                "inode": st.st_ino,
                "device": st.st_dev,
            }


# --- garde-fou : pas de lien dur sans preuve de contenu ----------------------------


def _jumeaux_sans_hash() -> list[LocationRecord]:
    """Deux fichiers de taille identique, sans hash, sur deux inodes distincts :
    des candidats à la dédup (CONCEPTION.md §1.2), rien de plus."""
    return [
        _rec("/lms/a.gguf", size=1000, inode=91),
        _rec("/lms/b.gguf", size=1000, inode=92),
    ]


def test_aucune_action_hardlink_sans_sha256(fake_hf, fake_ollama, fake_lmstudio):
    # Les jumeaux font exactement la taille du blob LIVE : si le plan groupait par
    # taille, il proposerait de les lier entre eux — ou pire, au blob HF.
    records = _parc(fake_hf, fake_ollama, fake_lmstudio) + _jumeaux_sans_hash()
    plan = generate_plan(records, now=NOW)

    assert all(a["sha256"] for a in _actions(plan, "hardlink"))
    touches = {c for a in plan["actions"] for c in _touched(a)}
    assert touches.isdisjoint({"/lms/a.gguf", "/lms/b.gguf"})
    assert plan["total_bytes_reclaimed"] == 1800  # inchangé : une taille ne prouve rien


def test_les_jumeaux_sans_hash_sont_ecartes_et_non_ignores_en_silence():
    """Le plan ne peut pas les lier faute de preuve, mais il doit le dire : c'est
    ce qui invite à lancer `ecurie store verify` avant de replanifier."""
    plan = generate_plan(_jumeaux_sans_hash(), now=NOW)

    assert _actions(plan, "hardlink") == []
    ecartes = [i for i in plan["ignored"] if i["reason"] == "sans-sha256"]
    assert [c for i in ecartes for c in i["paths"]] == ["/lms/a.gguf", "/lms/b.gguf"]


def test_verified_only_ecarte_les_hash_seulement_annonces():
    records = [
        _rec("/hf/annonce", size=1000, inode=1, manager="hf", sha256=SHA_A,
             hash_source="announced"),
        _rec("/ollama/annonce", size=1000, inode=2, manager="ollama", sha256=SHA_A,
             hash_source="announced"),
        _rec("/hf/relu", size=400, inode=3, manager="hf", sha256=SHA_B, hash_source="verified"),
        _rec("/lms/relu", size=400, inode=4, sha256=SHA_B, hash_source="verified"),
        # Un seul des deux chemins relu : le contenu de l'autre reste une affirmation.
        _rec("/hf/mixte", size=600, inode=5, manager="hf", sha256=SHA_C, hash_source="verified"),
        _rec("/lms/mixte", size=600, inode=6, sha256=SHA_C, hash_source="announced"),
    ]

    complet = generate_plan(records, now=NOW)
    assert complet["total_bytes_reclaimed"] == 2000
    assert {a["sha256"] for a in _actions(complet, "hardlink")} == {SHA_A, SHA_B, SHA_C}
    assert complet["ignored"] == []

    prudent = generate_plan(records, verified_only=True, now=NOW)
    assert [a["sha256"] for a in _actions(prudent, "hardlink")] == [SHA_B]
    assert prudent["total_bytes_reclaimed"] == 400
    assert {tuple(i["paths"]) for i in prudent["ignored"]} == {
        ("/hf/annonce", "/ollama/annonce"),
        ("/hf/mixte", "/lms/mixte"),
    }
    assert {i["reason"] for i in prudent["ignored"]} == {"hash-annonce-non-verifie"}
    assert _actions(prudent, "hardlink")[0]["hash_source"] == "verified"


# --- poste « jamais utilisé » : muet sans télémétrie --------------------------------


def _parc_avec_usage() -> list[LocationRecord]:
    return [
        _rec("/lms/jamais.gguf", size=700, inode=21, sha256=SHA_A, variant_ref="m@jamais",
             hash_source="verified"),
        _rec("/lms/ancien.gguf", size=100, inode=22, sha256=SHA_B, variant_ref="m@ancien",
             hash_source="verified"),
        _rec("/lms/servi.gguf", size=300, inode=23, sha256=SHA_C, variant_ref="m@servi",
             hash_source="verified"),
    ]


USAGE = {"m@servi": "2026-08-10T00:00:00+00:00", "m@ancien": "2026-01-01T00:00:00+00:00"}


def test_poste_jamais_utilise_muet_sans_telemetrie():
    """Table `runs` vide : aucun usage observé ne veut pas dire aucun usage. Jeter
    tout le parc parce qu'on ne l'a pas encore instrumenté serait le pire des bogues."""
    plan = generate_plan(_parc_avec_usage(), last_runs={}, telemetry=False, now=NOW)

    assert plan["actions"] == []
    assert plan["total_bytes_reclaimed"] == 0
    assert plan["telemetry"] is False


def test_poste_jamais_utilise_avec_telemetrie():
    plan = generate_plan(_parc_avec_usage(), last_runs=USAGE, telemetry=True, now=NOW)

    actions = {a["path"]: a for a in _actions(plan, "trash")}
    assert sorted(actions) == ["/lms/ancien.gguf", "/lms/jamais.gguf"]
    assert all(a["reason"] == "unused-variant" for a in actions.values())
    assert plan["total_bytes_reclaimed"] == 800  # 700 (aucun run) + 100 (run trop vieux)
    assert plan["telemetry"] is True
    assert plan["unused_after_days"] == 90
    # La corbeille doit savoir quel variant elle emporte : c'est ce que le manifeste
    # de quarantaine consigne, et la seule façon de restaurer à bon escient.
    assert actions["/lms/jamais.gguf"]["variant_ref"] == "m@jamais"

    # Fenêtre élargie : le variant lancé en janvier n'est plus « jamais utilisé ».
    large = generate_plan(
        _parc_avec_usage(), last_runs=USAGE, telemetry=True, unused_after_days=365, now=NOW
    )
    assert [a["path"] for a in _actions(large, "trash")] == ["/lms/jamais.gguf"]
    assert large["total_bytes_reclaimed"] == 700


# --- instantanés HF détachés --------------------------------------------------------


def test_instantane_hf_detache_produit_une_action_de_dossier(fake_hf, fake_ollama, fake_lmstudio):
    plan = generate_plan(_parc(fake_hf, fake_ollama, fake_lmstudio), now=NOW)

    detaches = [a for a in plan["actions"] if a["reason"] == "detached-snapshot"]
    assert len(detaches) == 1
    action = detaches[0]
    dossier = next(fake_hf.glob(f"models--*/snapshots/{REV_STALE}"))
    assert action["kind"] == "trash"
    assert Path(action["path"]) == dossier
    assert dossier.is_dir()
    assert action["revision"] == REV_STALE
    # Zéro octet : ce ne sont que des liens. Les jeter n'en rend aucun, mais les
    # laisser après avoir jeté leurs blobs laisse un cache jonché de liens cassés.
    assert action["bytes_reclaimed"] == 0
    assert action["sha256"] is None

    # Le lien lui-même n'a pas d'action propre : il part avec son dossier.
    lien = str(dossier / "old.safetensors")
    touches = {c for a in plan["actions"] if a is not action for c in _touched(a)}
    assert lien not in touches


# --- aller-retour sur disque ---------------------------------------------------------


def test_ecriture_et_relecture_fidele(tmp_path, fake_hf, fake_ollama, fake_lmstudio):
    plan = generate_plan(
        _parc(fake_hf, fake_ollama, fake_lmstudio), scan_id="scan-42", now=NOW, plan_id="p1"
    )
    chemin = write_plan(plan, tmp_path / "plans" / "plan.json")  # le dossier n'existe pas encore

    assert chemin.exists()
    assert load_plan(chemin) == plan
    # Relisible par un humain comme par un autre outil, accents compris.
    assert json.loads(chemin.read_text(encoding="utf-8")) == plan


def test_load_plan_refuse_ce_qui_n_est_pas_un_plan(tmp_path):
    pas_json = tmp_path / "notes.json"
    pas_json.write_text("ceci n'est pas du JSON")
    liste = tmp_path / "liste.json"
    liste.write_text(json.dumps([{"kind": "trash", "path": "/x"}]))
    sans_actions = tmp_path / "config.json"
    sans_actions.write_text(json.dumps({"plan_version": PLAN_VERSION, "total_bytes_reclaimed": 9}))

    for chemin in (pas_json, liste, sans_actions, tmp_path / "absent.json"):
        with pytest.raises(PlanError):
            load_plan(chemin)


def test_load_plan_refuse_une_version_inconnue(tmp_path):
    """Un plan d'une autre version peut avoir d'autres invariants : on le régénère,
    on ne le devine pas — c'est un fichier qui pilote des opérations destructives."""
    futur = generate_plan(_quatre_postes(), now=NOW)
    futur["plan_version"] = PLAN_VERSION + 1
    write_plan(futur, tmp_path / "futur.json")
    with pytest.raises(PlanError, match="régénérer"):
        load_plan(tmp_path / "futur.json")

    ancien = generate_plan(_quatre_postes(), now=NOW)
    del ancien["plan_version"]
    write_plan(ancien, tmp_path / "ancien.json")
    with pytest.raises(PlanError, match="régénérer"):
        load_plan(tmp_path / "ancien.json")
