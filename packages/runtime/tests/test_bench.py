"""Le banc d'essai : mesurer, écrire la mesure, proposer le patch — sans committer."""

import json
import struct
import zlib

from ecurie_runtime.bench import (
    CaseResult,
    build_profile,
    default_workload,
    load_workload,
    run_bench,
    write_measurement,
    yaml_patch,
)
from ecurie_runtime.supervisor import parse_ref

GIB = 1 << 30
REPO = __import__("pathlib").Path(__file__).parents[3]


def _pieces(superviseur, ref="tts-test"):
    model, variant, _ = parse_ref(superviseur.registry, ref)
    return model, variant, superviseur.registry.capabilities[model.capability]


def test_la_charge_type_reelle_du_depot_se_charge():
    """Les trois entrées figées de `registry/evals/bench/` sont lisibles telles quelles."""
    from ecurie_core.registry import load_registry

    registre = load_registry(REPO)
    charge = load_workload(REPO, registre.capabilities["text-to-speech"])
    assert charge.version >= 1
    assert [c.id for c in charge.cases] == ["court", "moyen", "difficile"]
    assert charge.base_dir == REPO / "registry" / "evals" / "bench"

    mesh = load_workload(REPO, registre.capabilities["image-to-mesh"])
    for cas in mesh.cases:
        image = mesh.base_dir / cas.input["image"]
        assert image.is_file()
        # RGBA avec un fond détouré : le pipeline de reconstruction recadre sur
        # le canal alpha. Une image opaque le prive de toute silhouette, et le
        # worker le signale en caveat à chaque job de mesure.
        largeur, hauteur, profondeur, couleur = struct.unpack(">IIBB", image.read_bytes()[16:26])
        assert (largeur, hauteur, profondeur, couleur) == (256, 256, 8, 6)
        assert _alphas(image) == {0, 255}


def _alphas(image) -> set[int]:
    """Valeurs du canal alpha présentes dans un PNG RGBA non entrelacé."""
    données = image.read_bytes()
    largeur, hauteur = struct.unpack(">II", données[16:24])
    idat, curseur = b"", 8
    while curseur < len(données):
        taille = struct.unpack(">I", données[curseur : curseur + 4])[0]
        if données[curseur + 4 : curseur + 8] == b"IDAT":
            idat += données[curseur + 8 : curseur + 8 + taille]
        curseur += 12 + taille
    brut = zlib.decompress(idat)
    pas = largeur * 4 + 1
    return {
        brut[ligne * pas + 1 + colonne * 4 + 3]
        for ligne in range(hauteur)
        for colonne in range(0, largeur, 7)
    }


def test_sans_charge_type_le_banc_mesure_quand_meme_mais_le_dit(parc, supervisor_factory):
    parc.capability().model()
    superviseur = supervisor_factory(parc)
    _, _, contract = _pieces(superviseur)

    # Le dépôt synthétique n'a pas de registry/evals/bench/ : on retombe sur le contrat.
    charge = load_workload(parc.root, contract)
    assert charge.version == 0
    assert charge.cases[0].input["text"]
    assert default_workload(contract).source.startswith("déduit")


def test_le_banc_mesure_le_profil_et_ecrit_la_mesure(parc, supervisor_factory):
    parc.capability().model(peak_bytes=None)  # pas de profil : c'est justement l'objet
    superviseur = supervisor_factory(parc, env_vars={"ECURIE_FAKE_PEAK_BYTES": str(2 * GIB)})
    model, variant, contract = _pieces(superviseur)

    rapport = run_bench(superviseur, model, variant, contract)
    assert rapport.ok
    assert rapport.profile["peak_unified_memory_bytes"] == 2 * GIB
    assert rapport.profile["disk_bytes"] == 4096, "les poids synthétiques font 4 Kio"
    assert "latency_ms_p50" in rapport.profile
    # Le rtf est agrégé sur toute la charge (temps total / audio total), et non
    # repris de ce que l'adaptateur rapporte par cas : les deux ne coïncident que
    # si tous les cas ont la même durée, et `throughput` doit en rester l'inverse.
    rtf, débit = rapport.profile["rtf"], float(rapport.profile["throughput"].split("×")[0])
    assert abs(1 / rtf - débit) / débit < 0.001  # au seul arrondi d'affichage près
    assert "temps réel" in rapport.profile["throughput"]
    assert rapport.measured_on
    assert any("aucune charge type versionnée" in a for a in rapport.warnings)

    chemin = write_measurement(parc.root, rapport)
    assert chemin == parc.root / "registry" / "measurements" / "tts-test@essai.json"
    document = json.loads(chemin.read_text())
    assert document["harness_version"] == rapport.harness_version
    assert document["profile"] == rapport.profile
    assert [c["id"] for c in document["cases"]] == ["défaut"]


def test_le_banc_vide_le_parc_avant_de_mesurer(parc, supervisor_factory):
    parc.capability().model("resident", peak_bytes=1 * GIB)
    parc.model("a-mesurer", peak_bytes=None)
    superviseur = supervisor_factory(parc)
    model, variant, contract = _pieces(superviseur, "resident")
    superviseur.acquire(model, variant).release()
    assert superviseur.residents()

    model, variant, contract = _pieces(superviseur, "a-mesurer")
    rapport = run_bench(superviseur, model, variant, contract)
    assert rapport.ok
    # Ni le résident d'avant, ni le worker de mesure ne restent en mémoire.
    assert superviseur.residents() == []


def test_un_cas_en_echec_n_annule_pas_les_autres(parc, supervisor_factory):
    parc.capability().model()
    superviseur = supervisor_factory(parc, env_vars={"ECURIE_FAKE_FAIL": "infer"})
    model, variant, contract = _pieces(superviseur)

    rapport = run_bench(superviseur, model, variant, contract)
    assert not rapport.ok
    assert rapport.cases and all(not c.ok for c in rapport.cases)
    assert "panne demandée" in rapport.cases[0].error


def test_le_rtf_agrege_ne_se_laisse_pas_biaiser_par_le_cas_le_plus_court():
    """Une phrase courte porte tout le coût fixe : la moyenne des ratios ment.

    Sur le vrai parc, la moyenne donnait 0,87 là où la charge entière tournait à
    0,54 — et le débit annoncé n'était plus l'inverse du rtf annoncé.
    """
    cas = [
        CaseResult("court", True, 5000, {"audio_seconds": 2.0}),  # rtf par cas : 2,50
        CaseResult("long", True, 10000, {"audio_seconds": 30.0}),  # rtf par cas : 0,33
    ]
    profil = build_profile(disk_bytes=0, peak_bytes=0, warmup_ms=0, cases=cas)

    # Agrégé : 15 s de calcul pour 32 s d'audio.
    assert profil["rtf"] == round(15 / 32, 6)
    assert profil["throughput"] == f"{32 / 15:.2f}× temps réel"


def test_le_pic_retenu_est_le_plus_grand_releve():
    profil = build_profile(
        disk_bytes=10,
        peak_bytes=3 * GIB,
        warmup_ms=120,
        cases=[
            CaseResult("a", True, 1000, {"peak_memory_bytes": 2 * GIB}),
            CaseResult("b", True, 3000, {"peak_memory_bytes": 3 * GIB}),
        ],
    )
    assert profil["peak_unified_memory_bytes"] == 3 * GIB
    assert profil["latency_ms_p50"] == 2000


def test_le_patch_yaml_est_collable_tel_quel(parc, supervisor_factory):
    parc.capability().model(peak_bytes=None)
    superviseur = supervisor_factory(parc)
    model, variant, contract = _pieces(superviseur)
    rapport = run_bench(superviseur, model, variant, contract)

    patch = yaml_patch(rapport)
    entête, _, corps = patch.partition("\n")
    # L'en-tête dit où coller : sans lui, un bloc posé un cran trop loin atterrit
    # dans `source:`, et YAML l'accepte sans rien dire.
    assert entête.startswith("# registry/models/tts-test.yaml")
    assert "id: essai" in entête
    assert corps.startswith("    profile:")
    assert "      peak_unified_memory_bytes:" in corps
    assert f'      measured_at: "{rapport.measured_at}"' in corps

    # Le corps doit être du YAML valide une fois greffé sous un variant.
    import yaml

    document = yaml.safe_load("variants:\n  - id: essai\n" + corps)
    profil = document["variants"][0]["profile"]
    assert profil["peak_unified_memory_bytes"] == rapport.profile["peak_unified_memory_bytes"]
    assert profil["harness_version"] == rapport.harness_version
    # Et il doit passer le schéma, qui attend une chaîne au format date : sans
    # guillemets, YAML rendrait un objet date et le manifeste serait refusé.
    assert isinstance(profil["measured_at"], str)


def test_le_patch_colle_dans_un_manifeste_donne_un_registre_valide(parc, supervisor_factory):
    """Le vrai critère : coller le patch et relancer la validation, sans rien retoucher.

    Vérifier que le patch est du YAML lisible ne suffit pas — c'est le schéma du
    manifeste qui tranche, et c'est lui qui refusait la date non quotée.
    """
    from ecurie_core.registry import load_registry

    parc.capability().model(peak_bytes=None)
    superviseur = supervisor_factory(parc)
    model, variant, contract = _pieces(superviseur)
    rapport = run_bench(superviseur, model, variant, contract)
    write_measurement(parc.root, rapport)

    manifeste = parc.root / "registry" / "models" / "tts-test.yaml"
    corps = [x for x in yaml_patch(rapport).splitlines() if not x.startswith("#")]
    patch = "\n".join(corps)
    manifeste.write_text(manifeste.read_text().rstrip("\n") + "\n" + patch + "\n")

    registre = load_registry(parc.root)
    assert registre.errors == [], registre.errors
    profil = registre.models["tts-test"].variants[0].profile
    assert profil is not None
    assert profil.peak_unified_memory_bytes == rapport.profile["peak_unified_memory_bytes"]
    # La mesure est là, le manifeste la reflète : aucun avertissement de dérive.
    assert not any("divergé" in i.message for i in registre.warnings)


def test_le_registre_signale_un_profil_qui_a_derive_de_sa_mesure(parc):
    """Le manifeste est une copie ; le fichier de mesure est l'autorité."""
    from ecurie_core.registry import load_registry

    parc.capability().model(peak_bytes=3 * GIB)
    parc.mesure(
        "tts-test@essai",
        {"disk_bytes": 4096, "peak_unified_memory_bytes": 9 * GIB},
    )
    registre = load_registry(parc.root)
    assert any("a divergé de measurements/" in i.message for i in registre.warnings)


def test_un_profil_sans_mesure_est_signale(parc):
    from ecurie_core.registry import load_registry

    parc.capability().model(peak_bytes=3 * GIB)
    registre = load_registry(parc.root)
    assert any("sans mesure correspondante" in i.message for i in registre.warnings)


# --- pente du pic mémoire -------------------------------------------------------------


def _charge(nom, valeurs):
    from ecurie_runtime.bench import BenchCase, Workload

    return Workload(
        capability="text-to-music",
        version=1,
        cases=[BenchCase(id=f"c{i}", input={nom: v}) for i, v in enumerate(valeurs)],
        source="essai",
        scaling_parameter=nom,
    )


def test_la_pente_du_pic_est_ajustee_sur_la_charge():
    """Mesuré sur MiniMax Music 3 : le pic croît avec la durée demandée.

    Sans pente, le profil doit inscrire le pire cas et refuse alors des jobs
    courts qui passeraient largement — c'est ce qui a rendu ce modèle
    inexécutable sur cette machine.
    """
    from ecurie_runtime.bench import fit_peak_scaling

    charge = _charge("duration_seconds", [15, 20, 30])
    résultats = [
        CaseResult("c0", True, 1, {"peak_memory_bytes": 13 * GIB}),
        CaseResult("c1", True, 1, {"peak_memory_bytes": 18 * GIB}),
        CaseResult("c2", True, 1, {"peak_memory_bytes": 24 * GIB}),
    ]
    échelle = fit_peak_scaling(charge, résultats)
    assert échelle["parameter"] == "duration_seconds"
    assert échelle["measured_range"] == [15.0, 30.0]
    # Les trois points ne sont pas parfaitement alignés — le coût par seconde
    # décroît un peu sur les longues durées — mais assez pour que la droite
    # décide bien mieux qu'un chiffre unique.
    assert 0.95 < échelle["r_squared"] < 1.0
    assert 0.7 * GIB < échelle["bytes_per_unit"] < 0.8 * GIB
    assert échelle["base_bytes"] > 0


def test_pas_de_pente_sans_parametre_declare():
    """Deviner la colonne qui corrèle donnerait une pente à tous les coups."""
    from ecurie_runtime.bench import fit_peak_scaling

    charge = _charge("duration_seconds", [15, 30])
    charge.scaling_parameter = None
    résultats = [
        CaseResult("c0", True, 1, {"peak_memory_bytes": 13 * GIB}),
        CaseResult("c1", True, 1, {"peak_memory_bytes": 24 * GIB}),
    ]
    assert fit_peak_scaling(charge, résultats) is None


def test_pas_de_pente_sur_un_seul_point_utile():
    from ecurie_runtime.bench import fit_peak_scaling

    charge = _charge("duration_seconds", [15, 15])
    résultats = [
        CaseResult("c0", True, 1, {"peak_memory_bytes": 13 * GIB}),
        CaseResult("c1", True, 1, {"peak_memory_bytes": 14 * GIB}),
    ]
    assert fit_peak_scaling(charge, résultats) is None


def test_un_cas_en_echec_ne_compte_pas_dans_la_pente():
    from ecurie_runtime.bench import fit_peak_scaling

    charge = _charge("duration_seconds", [15, 20, 30])
    résultats = [
        CaseResult("c0", True, 1, {"peak_memory_bytes": 13 * GIB}),
        CaseResult("c1", False, 0, {}, error="panne"),
        CaseResult("c2", True, 1, {"peak_memory_bytes": 24 * GIB}),
    ]
    échelle = fit_peak_scaling(charge, résultats)
    assert échelle["measured_range"] == [15.0, 30.0]
