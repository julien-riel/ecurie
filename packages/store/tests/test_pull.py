"""`ecurie pull` : ce qui se télécharge, ce qui se refuse, et à quel prix.

Aucun test de ce fichier n'ouvre de connexion : l'API HF, le téléchargement et
`shutil.disk_usage` sont injectés. Les fausses API lèvent quand on les appelle
au mauvais moment — c'est ainsi qu'on prouve qu'un refus de révision non
épinglée coûte zéro requête.

Les deux inventaires figés plus bas sont ceux des dépôts réels aux révisions
épinglées dans `registry/models/`. Une révision épinglée ne bouge plus : ces
listes ne peuvent pas se périmer, et elles valent contrat pour les
`allow_patterns` des manifestes.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from ecurie_core.config import Config, ScanConfig
from ecurie_core.models import Variant
from ecurie_core.registry import load_registry
from ecurie_store.pull import (
    PullError,
    check_revision,
    disk_guard,
    is_pinned,
    list_repo_files,
    plan_pull,
    plan_pulls,
    resolve_revision,
    revision_patch,
    run_pull,
    run_pulls,
    select_files,
    variant_presence,
)
from ecurie_store.weights import repo_dir_name

GO = 1_000_000_000

REPO_TTS = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"
REV_TTS = "41d3337e8b7f2843a75841595fc14e4b9a7a4b96"
REPO_3D = "tencent/Hunyuan3D-2.1"
REV_3D = "0b94677654c57bb9a6b6845cd7b704ccf551d327"

# Inventaire réel de REPO_TTS@REV_TTS (api/models/…/tree/main?recursive=true).
ARBRE_TTS = {
    ".gitattributes": 1519,
    "README.md": 1118,
    "config.json": 6058,
    "generation_config.json": 245,
    "merges.txt": 1_671_839,
    "model.safetensors": 2_393_308_931,
    "model.safetensors.index.json": 71786,
    "preprocessor_config.json": 127,
    "speech_tokenizer/config.json": 2336,
    "speech_tokenizer/configuration.json": 76,
    "speech_tokenizer/model.safetensors": 682_293_092,
    "speech_tokenizer/preprocessor_config.json": 234,
    "tokenizer_config.json": 7344,
    "vocab.json": 2_776_833,
}

# Inventaire réel de REPO_3D@REV_3D, deux pipelines dans un seul dépôt.
ARBRE_3D = {
    ".gitattributes": 1519,
    "LICENSE": 17915,
    "Notice.txt": 16644,
    "README.md": 3998,
    "demo.py": 1490,
    "hunyuan3d-dit-v2-1/config.yaml": 2078,
    "hunyuan3d-dit-v2-1/model.fp16.ckpt": 7_366_389_768,
    "hunyuan3d-vae-v2-1/config.yaml": 409,
    "hunyuan3d-vae-v2-1/model.fp16.ckpt": 655_648_152,
    "hunyuan3d-paintpbr-v2-1/README.md": 1801,
    "hunyuan3d-paintpbr-v2-1/model_index.json": 617,
    "hunyuan3d-paintpbr-v2-1/feature_extractor/preprocessor_config.json": 342,
    "hunyuan3d-paintpbr-v2-1/image_encoder/config.json": 554,
    "hunyuan3d-paintpbr-v2-1/image_encoder/model.safetensors": 1_264_217_240,
    "hunyuan3d-paintpbr-v2-1/scheduler/scheduler_config.json": 387,
    "hunyuan3d-paintpbr-v2-1/text_encoder/config.json": 613,
    "hunyuan3d-paintpbr-v2-1/text_encoder/pytorch_model.bin": 1_361_671_895,
    "hunyuan3d-paintpbr-v2-1/tokenizer/merges.txt": 524_619,
    "hunyuan3d-paintpbr-v2-1/tokenizer/special_tokens_map.json": 460,
    "hunyuan3d-paintpbr-v2-1/tokenizer/tokenizer_config.json": 807,
    "hunyuan3d-paintpbr-v2-1/tokenizer/vocab.json": 1_059_962,
    "hunyuan3d-paintpbr-v2-1/unet/attn_processor.py": 35104,
    "hunyuan3d-paintpbr-v2-1/unet/config.json": 911,
    "hunyuan3d-paintpbr-v2-1/unet/diffusion_pytorch_model.bin": 3_925_293_863,
    "hunyuan3d-paintpbr-v2-1/unet/model.py": 25514,
    "hunyuan3d-paintpbr-v2-1/unet/modules.py": 45181,
    "hunyuan3d-paintpbr-v2-1/vae/config.json": 553,
    "hunyuan3d-paintpbr-v2-1/vae/diffusion_pytorch_model.bin": 334_707_217,
    "hy3dpaint/textureGenPipeline.py": 8126,
    "hy3dpaint/utils/multiview_utils.py": 5536,
}

# Un variant jouet : trois fichiers, des tailles qui se lisent d'un coup d'œil.
ARBRE_JOUET = {"config.json": 100, "model.safetensors": 900, "notes.md": 50}
REV = "a" * 40


# --- doublures : rien ici ne touche au réseau ---------------------------------------


@dataclass
class FauxAPI:
    """`list_repo_tree` et `model_info`, avec la même forme que celles de hf_hub."""

    arbre: dict[str, int] = field(default_factory=dict)
    sha: str = REV
    licence: str | None = "apache-2.0"
    modifie: str | None = "2026-01-26T20:11:42+00:00"
    appels: list[tuple] = field(default_factory=list)

    def list_repo_tree(self, repo, *, revision, recursive):
        self.appels.append(("tree", repo, revision, recursive))
        entrées = [SimpleNamespace(path=p, size=t) for p, t in sorted(self.arbre.items())]
        dossiers = {str(Path(p).parent) for p in self.arbre if "/" in p}
        # L'API mêle dossiers et fichiers ; un dossier porte `tree_id` et size 0.
        return [*entrées, *(SimpleNamespace(path=d, size=0, tree_id="t" * 40) for d in dossiers)]

    def model_info(self, repo, *, revision):
        self.appels.append(("info", repo, revision))
        return SimpleNamespace(
            sha=self.sha,
            last_modified=self.modifie,
            card_data={"license": self.licence},
            gated=False,
            tags=[],
        )


class APIInterdite:
    """Toute requête est une erreur : sert à prouver qu'un refus est immédiat."""

    def list_repo_tree(self, *a, **kw):
        raise AssertionError("le réseau ne doit pas être sollicité ici")

    def model_info(self, *a, **kw):
        raise AssertionError("le réseau ne doit pas être sollicité ici")


@dataclass
class FauxTelechargement:
    """Écrit ce qu'un vrai `snapshot_download` écrirait, en miniature."""

    ecrit: dict[str, int] = field(default_factory=dict)
    appels: list[dict] = field(default_factory=list)

    def __call__(self, **kwargs):
        self.appels.append(kwargs)
        return str(
            _pose_instantane(
                Path(kwargs["cache_dir"]), kwargs["repo_id"], kwargs["revision"], self.ecrit
            )
        )


def _disque(total: int, free: int):
    return lambda path: SimpleNamespace(total=total, free=free, used=total - free)


def _pose_instantane(hub: Path, repo: str, revision: str, fichiers: dict[str, int]) -> Path:
    """Un instantané HF fidèle : des blobs, et des liens symboliques vers eux."""
    racine = hub / repo_dir_name(repo)
    blobs = racine / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    snap = racine / "snapshots" / revision
    snap.mkdir(parents=True, exist_ok=True)
    for nom, taille in fichiers.items():
        blob = blobs / nom.replace("/", "--")
        blob.write_bytes(b"x" * taille)
        cible = snap / nom
        cible.parent.mkdir(parents=True, exist_ok=True)
        if cible.is_symlink() or cible.exists():
            cible.unlink()
        os.symlink(os.path.relpath(blob, cible.parent), cible)
    return snap


def _config(tmp_path: Path, **kw) -> Config:
    return Config(scan=ScanConfig(hf_hub=tmp_path / "hf-hub"), **kw)


def _variant(*, repo=REPO_TTS, revision=REV, patterns=None, kind="huggingface", **kw) -> Variant:
    source = {"kind": kind, "repo": repo, "revision": revision, "allow_patterns": patterns}
    return Variant.model_validate(
        {"id": "v", "runtime": "mlx-audio", "source": {k: v for k, v in source.items() if v}, **kw}
    )


REF = "jouet@v"


# --- garde de disque ------------------------------------------------------------------


def test_la_garde_laisse_passer_quand_la_marge_reste_au_dessus_du_seuil(tmp_path):
    garde = disk_guard(
        tmp_path, 40 * GO, min_ratio=0.15, disk_usage=_disque(1000 * GO, 200 * GO)
    )

    assert garde.free_after == 160 * GO
    assert garde.ratio_before == pytest.approx(0.20)
    assert garde.ratio_after == pytest.approx(0.16)
    assert garde.ok


def test_la_garde_refuse_et_chiffre_les_quatre_nombres(tmp_path):
    garde = disk_guard(
        tmp_path, 60 * GO, min_ratio=0.15, disk_usage=_disque(1000 * GO, 200 * GO)
    )

    assert garde.free_after == 140 * GO
    assert garde.ratio_after == pytest.approx(0.14)
    assert not garde.ok
    # Un refus qui ne chiffre pas laisse l'utilisateur deviner ce qu'il doit libérer.
    for morceau in ("60.00 Go", "200.00 Go", "140.00 Go", "15.0%"):
        assert morceau in garde.message


def test_le_volume_mesure_est_le_premier_parent_existant(tmp_path):
    """Au tout premier pull, le dossier du cache HF n'existe pas encore."""
    vus = []

    def usage(path):
        vus.append(path)
        return SimpleNamespace(total=100, free=50, used=50)

    disk_guard(tmp_path / "hf-hub" / "models--x--y", 1, min_ratio=0.15, disk_usage=usage)
    assert vus == [tmp_path]


def test_run_pull_refuse_sur_la_garde_puis_passe_outre_sur_demande(tmp_path):
    config = _config(tmp_path)
    variant = _variant()
    api = FauxAPI({"model.safetensors": 60 * GO})
    telecharge = FauxTelechargement({"model.safetensors": 10})
    serré = _disque(1000 * GO, 200 * GO)

    with pytest.raises(PullError) as refus:
        run_pull(config, variant, ref=REF, api=api, download=telecharge, disk_usage=serré)
    assert "60.00 Go" in str(refus.value) and "15.0%" in str(refus.value)
    assert telecharge.appels == []  # rien n'a été écrit, pas même un début

    résultat = run_pull(
        config,
        variant,
        ref=REF,
        api=api,
        download=telecharge,
        disk_usage=serré,
        ignore_disk_guard=True,
    )
    assert résultat.downloaded
    assert len(telecharge.appels) == 1


def test_la_garde_ne_compte_que_ce_qui_manque(tmp_path):
    """Un dépôt à moitié téléchargé ne redemande pas la place de la moitié présente."""
    config = _config(tmp_path)
    _pose_instantane(tmp_path / "hf-hub", REPO_TTS, REV, {"config.json": 100})
    api = FauxAPI({"config.json": 100, "model.safetensors": 40 * GO})

    plan = plan_pull(config, _variant(), ref=REF, api=api, disk_usage=_disque(1000 * GO, 200 * GO))

    assert plan.selected_bytes == 40 * GO + 100
    assert plan.download_bytes == 40 * GO
    assert plan.guard.needed_bytes == 40 * GO
    assert plan.guard.ok


# --- révisions épinglées ---------------------------------------------------------------


@pytest.mark.parametrize(
    "revision",
    [
        None,  # source huggingface sans révision : le schéma l'autorise, pas nous
        "0000000",
        "0" * 40,
        "41d3337",  # sha court : le schéma l'accepte (7 à 40), le cache HF non
    ],
)
def test_revision_non_epinglee_refusee_sans_une_seule_requete(tmp_path, revision):
    assert not is_pinned(revision)
    with pytest.raises(PullError):
        plan_pull(_config(tmp_path), _variant(revision=revision), ref=REF, api=APIInterdite())


def test_le_refus_nomme_la_regle_qui_casse():
    """`main` et compagnie ne franchissent déjà pas pydantic ; la garde reste ici
    parce que `check_revision` sert aussi à des documents qui n'en viennent pas —
    un manifeste de veille en cours de rédaction, un patch collé à la main."""
    assert "placeholder" in str(_refus("0000000"))
    assert "flottante" in str(_refus("main"))
    assert "sha court" in str(_refus("41d3337"))
    assert "aucune révision" in str(_refus(None))
    for message in (_refus("main"), _refus("0000000")):
        assert "--resolve" in str(message)  # le message porte la réparation


def _refus(revision):
    with pytest.raises(PullError) as exc:
        check_revision(REF, revision)
    return exc.value


def test_une_revision_epinglee_est_rendue_telle_quelle():
    assert check_revision(REF, REV_TTS) == REV_TTS
    assert is_pinned(REV_TTS)


# --- allow_patterns : ce qui est retenu, ce qui est épargné ------------------------------


def _infos(arbre: dict[str, int]):
    return [SimpleNamespace(path=p, size=t) for p, t in sorted(arbre.items())]


def test_les_patterns_du_manifeste_tts_prennent_merges_et_le_speech_tokenizer(repo_root):
    """Sans merges.txt le tokenizer BPE ne se charge pas ; sans speech_tokenizer/,
    la synthèse échoue sur « Speech tokenizer not loaded ». Les patterns du
    manifeste doivent prendre les deux — c'était le défaut du v0.1."""
    variant = load_registry(repo_root).models["qwen3-tts-1.7b"].variant("8bit-mlx")
    retenus, écartés = select_files(_infos(ARBRE_TTS), variant.source.allow_patterns)
    noms = {f.path for f in retenus}

    assert variant.source.repo == REPO_TTS
    assert variant.source.revision == REV_TTS
    assert "merges.txt" in noms
    assert {n for n in ARBRE_TTS if n.startswith("speech_tokenizer/")} <= noms
    assert {f.path for f in écartés} == {".gitattributes", "README.md"}
    assert sum(f.size for f in retenus) == 3_080_138_901


def test_les_patterns_du_manifeste_hunyuan_ecartent_la_texturation(repo_root):
    """Ce variant ne veut que la forme ; la texturation PBR pèse près de 7 Go."""
    variant = load_registry(repo_root).models["hunyuan3d-2.1-shape-mlx"].variant("mlx-bf16")
    retenus, écartés = select_files(_infos(ARBRE_3D), variant.source.allow_patterns)

    assert variant.source.revision == REV_3D
    assert {f.path for f in retenus} == {
        "hunyuan3d-dit-v2-1/config.yaml",
        "hunyuan3d-dit-v2-1/model.fp16.ckpt",
        "LICENSE",
        "Notice.txt",
    }
    assert not any(f.path.startswith("hunyuan3d-paintpbr") for f in retenus)
    assert sum(f.size for f in retenus) == 7_366_426_405
    assert sum(f.size for f in écartés) == 7_543_256_870
    assert sum(ARBRE_3D.values()) == 14_909_683_275


def test_l_etoile_de_fnmatch_traverse_les_barres_obliques():
    """Piège de lecture : `*.json` ne se limite pas à la racine du dépôt. Le filtre
    est celui de huggingface_hub, donc la garde chiffre ce qui sera écrit."""
    retenus, _ = select_files(_infos(ARBRE_TTS), ["*.json"])
    assert "speech_tokenizer/config.json" in {f.path for f in retenus}


def test_sans_allow_patterns_tout_le_depot_est_retenu():
    retenus, écartés = select_files(_infos(ARBRE_JOUET), None)
    assert sum(f.size for f in retenus) == 1050
    assert écartés == []


def test_les_dossiers_annonces_par_l_api_ne_pesent_rien(tmp_path):
    api = FauxAPI(ARBRE_TTS)
    fichiers = list_repo_files(REPO_TTS, REV_TTS, api=api)

    assert [f.path for f in fichiers] == sorted(ARBRE_TTS)
    assert sum(f.size for f in fichiers) == sum(ARBRE_TTS.values()) == 3_080_141_538
    assert api.appels == [("tree", REPO_TTS, REV_TTS, True)]


def test_des_patterns_qui_ne_retiennent_rien_sont_un_refus(tmp_path):
    api = FauxAPI(ARBRE_JOUET)
    variant = _variant(patterns=["*.gguf"])

    with pytest.raises(PullError, match="aucun des 3 fichiers"):
        plan_pull(_config(tmp_path), variant, ref=REF, api=api)


# --- déjà présent ------------------------------------------------------------------------


def test_un_variant_deja_complet_ne_se_retelecharge_pas(tmp_path):
    config = _config(tmp_path)
    _pose_instantane(tmp_path / "hf-hub", REPO_TTS, REV, ARBRE_JOUET)
    telecharge = FauxTelechargement()

    résultat = run_pull(
        config, _variant(), ref=REF, api=FauxAPI(ARBRE_JOUET), download=telecharge
    )

    assert résultat.downloaded is False
    assert "déjà complet" in résultat.note
    assert résultat.plan.presence.complete
    assert résultat.plan.presence.bytes_on_disk == 1050
    assert résultat.plan.download_bytes == 0
    assert telecharge.appels == []


def test_force_retelecharge_un_variant_complet(tmp_path):
    config = _config(tmp_path)
    _pose_instantane(tmp_path / "hf-hub", REPO_TTS, REV, ARBRE_JOUET)
    telecharge = FauxTelechargement(ARBRE_JOUET)

    résultat = run_pull(
        config, _variant(), ref=REF, api=FauxAPI(ARBRE_JOUET), download=telecharge, force=True
    )

    assert résultat.downloaded
    assert len(telecharge.appels) == 1


def test_un_lien_symbolique_casse_compte_comme_manquant(tmp_path):
    """Le GC met un blob en quarantaine : l'instantané garde ses entrées, mais
    elles ne mènent plus nulle part. Un pull doit le voir et retélécharger."""
    config = _config(tmp_path)
    snap = _pose_instantane(tmp_path / "hf-hub", REPO_TTS, REV, ARBRE_JOUET)
    (snap.parent.parent / "blobs" / "model.safetensors").unlink()

    presence = variant_presence(config, _variant(), ref=REF, expected=list(ARBRE_JOUET))
    assert presence.present
    assert not presence.complete
    assert presence.missing == ["model.safetensors"]
    assert presence.bytes_on_disk == 150

    plan = plan_pull(config, _variant(), ref=REF, api=FauxAPI(ARBRE_JOUET))
    assert plan.download_bytes == 900


def test_une_autre_revision_en_cache_ne_compte_pas(tmp_path):
    """Le cache a le dépôt, mais pas la révision épinglée : tout est à télécharger."""
    config = _config(tmp_path)
    _pose_instantane(tmp_path / "hf-hub", REPO_TTS, "b" * 40, ARBRE_JOUET)

    presence = variant_presence(config, _variant(), ref=REF, expected=list(ARBRE_JOUET))
    assert not presence.present
    assert "révision" in (presence.reason or "")

    plan = plan_pull(config, _variant(), ref=REF, api=FauxAPI(ARBRE_JOUET))
    assert plan.download_bytes == 1050


# --- exécution ----------------------------------------------------------------------------


def test_dry_run_verifie_chiffre_et_n_ecrit_rien(tmp_path):
    config = _config(tmp_path)
    telecharge = FauxTelechargement(ARBRE_JOUET)

    résultat = run_pull(
        config,
        _variant(),
        ref=REF,
        api=FauxAPI(ARBRE_JOUET),
        download=telecharge,
        dry_run=True,
    )

    assert résultat.dry_run and not résultat.downloaded
    assert résultat.plan.download_bytes == 1050
    assert telecharge.appels == []
    assert not résultat.plan.snapshot_dir.exists()


def test_le_telechargement_recoit_la_revision_epinglee_et_les_patterns(tmp_path):
    config = _config(tmp_path)
    variant = _variant(revision=REV_TTS, patterns=["*.json", "*.safetensors"])
    telecharge = FauxTelechargement({"config.json": 100, "model.safetensors": 900})

    résultat = run_pull(
        config, variant, ref=REF, api=FauxAPI(ARBRE_JOUET), download=telecharge
    )

    (appel,) = telecharge.appels
    assert appel["repo_id"] == REPO_TTS
    assert appel["revision"] == REV_TTS  # jamais "main", jamais un sha court
    assert appel["allow_patterns"] == ["*.json", "*.safetensors"]
    assert appel["cache_dir"] == str(tmp_path / "hf-hub")
    assert appel["local_files_only"] is False
    assert résultat.path == tmp_path / "hf-hub" / repo_dir_name(REPO_TTS) / "snapshots" / REV_TTS
    # Mesuré sur le disque après coup, pas recopié de l'estimation.
    assert résultat.bytes_downloaded == 1000


def test_un_telechargement_incomplet_ne_passe_pas_pour_un_succes(tmp_path):
    config = _config(tmp_path)
    telecharge = FauxTelechargement({"config.json": 100})  # il en manque deux

    with pytest.raises(PullError, match="incomplet"):
        run_pull(config, _variant(), ref=REF, api=FauxAPI(ARBRE_JOUET), download=telecharge)


def test_une_source_locale_ne_se_telecharge_pas(tmp_path):
    variant = Variant.model_validate(
        {"id": "v", "runtime": "custom", "source": {"kind": "local", "path": str(tmp_path)}}
    )
    with pytest.raises(PullError, match="local"):
        plan_pull(_config(tmp_path), variant, ref=REF, api=APIInterdite())


# --- épinglage -----------------------------------------------------------------------------


def test_resolve_revision_rapporte_le_sha_la_licence_et_la_date():
    api = FauxAPI(sha=REV_TTS)
    info = resolve_revision(REPO_TTS, api=api)

    assert info.sha == REV_TTS
    assert info.license == "apache-2.0"
    assert info.last_modified == "2026-01-26T20:11:42+00:00"
    assert api.appels == [("info", REPO_TTS, "main")]


def test_le_patch_a_committer_porte_le_depot_et_le_sha_complet():
    patch = revision_patch("qwen3-tts-1.7b@8bit-mlx", resolve_revision(REPO_TTS, api=FauxAPI()))

    assert "registry/models/qwen3-tts-1.7b.yaml" in patch
    assert "- id: 8bit-mlx" in patch
    assert f'revision: "{REV}"' in patch
    assert REPO_TTS in patch


# --- un variant, plusieurs dépôts ----------------------------------------------------------


REPO_TOK = "Qwen/Qwen2-1.5B"
REV_TOK = "b" * 40
ARBRE_TOK = {"tokenizer.json": 700, "tokenizer_config.json": 60, "vocab.json": 240}


def _variant_a_deux_depots(**kw) -> Variant:
    """Le cas de CAD-Recode : 3 Go de poids sans le moindre tokenizer.

    Le dépôt des poids ne publie que `config.json` et `model.safetensors` ; le
    tokenizer est celui d'un autre modèle, sous une autre licence. Sans
    `extra_sources`, le manifeste ne pouvait pas le dire, et le variant se
    chargeait jusqu'à mourir sur un fichier absent.
    """
    return _variant(
        repo=REPO_TTS,
        revision=REV,
        extra_sources=[
            {
                "kind": "huggingface",
                "repo": REPO_TOK,
                "revision": REV_TOK,
                "role": "tokenizer",
            }
        ],
        **kw,
    )


def test_les_sources_rendent_les_depots_dans_l_ordre_les_poids_d_abord():
    variant = _variant_a_deux_depots()

    assert [s.repo for s in variant.sources] == [REPO_TTS, REPO_TOK]
    assert variant.sources[1].role == "tokenizer"


def test_un_variant_ordinaire_n_a_qu_une_source():
    """Le champ ne change rien pour les cinquante-quatre modèles déjà au parc."""
    assert [s.repo for s in _variant().sources] == [REPO_TTS]


def test_planifier_un_variant_a_deux_depots_donne_deux_plans_chiffres(tmp_path):
    config = _config(tmp_path)
    plans = plan_pulls(
        config,
        _variant_a_deux_depots(),
        ref=REF,
        api=_api_par_depot({REPO_TTS: ARBRE_JOUET, REPO_TOK: ARBRE_TOK}),
        disk_usage=_disque(1000 * GO, 500 * GO),
    )

    assert [p.repo for p in plans] == [REPO_TTS, REPO_TOK]
    # Le rôle n'est porté que par la source secondaire : c'est ce qui distingue
    # les deux tableaux à l'écran, et ce qui dit pourquoi un second
    # téléchargement part.
    assert [p.role for p in plans] == [None, "tokenizer"]
    assert plans[0].selected_bytes == sum(ARBRE_JOUET.values())
    assert plans[1].selected_bytes == sum(ARBRE_TOK.values())


def test_le_second_depot_manquant_ne_passe_pas_pour_un_variant_complet(tmp_path):
    """Le pire état d'un variant : présent pour le contrôle d'admission, et incapable
    de charger. Les poids sont là, le tokenizer ne l'est pas."""
    config = _config(tmp_path)
    hub = tmp_path / "hf-hub"
    _pose_instantane(hub, REPO_TTS, REV, ARBRE_JOUET)
    variant = _variant_a_deux_depots()

    poids = variant_presence(config, variant, ref=REF, expected=list(ARBRE_JOUET))
    tokenizer = variant_presence(
        config, variant, ref=REF, expected=list(ARBRE_TOK), source=variant.sources[1]
    )

    assert poids.complete
    assert not tokenizer.present
    assert "ecurie pull" in (tokenizer.reason or "")


def test_telecharger_ramene_les_deux_depots_en_un_seul_appel(tmp_path):
    config = _config(tmp_path)
    hub = tmp_path / "hf-hub"
    variant = _variant_a_deux_depots()
    arbres = {REPO_TTS: ARBRE_JOUET, REPO_TOK: ARBRE_TOK}

    def faux_download(*, repo_id, revision, **kwargs):
        return str(_pose_instantane(hub, repo_id, revision, arbres[repo_id]))

    résultats = run_pulls(
        config,
        variant,
        ref=REF,
        api=_api_par_depot(arbres),
        download=faux_download,
        disk_usage=_disque(1000 * GO, 500 * GO),
    )

    assert [r.plan.repo for r in résultats] == [REPO_TTS, REPO_TOK]
    assert all(r.downloaded for r in résultats)
    assert sum(r.bytes_downloaded for r in résultats) == sum(ARBRE_JOUET.values()) + sum(
        ARBRE_TOK.values()
    )


def _api_par_depot(arbres: dict[str, dict[str, int]]):
    """Une API qui répond selon le dépôt interrogé — deux inventaires, pas un."""

    class ParDepot:
        def list_repo_tree(self, repo, *, revision, recursive):
            return FauxAPI(arbre=arbres[repo]).list_repo_tree(
                repo, revision=revision, recursive=recursive
            )

        def model_info(self, repo, *, revision):
            return FauxAPI(arbre=arbres[repo]).model_info(repo, revision=revision)

    return ParDepot()
