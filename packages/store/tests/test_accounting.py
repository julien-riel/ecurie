"""Comptabilité du récupérable : les quatre postes ne doivent jamais se recouvrir.

Le total du récupérable est un engagement pris devant un utilisateur qui s'apprête
à laisser un outil toucher trente giga-octets de poids de modèles. Deux façons de
mentir : promettre deux fois le même octet (postes qui se recouvrent) et promettre
un octet qu'il faudra garder. Les tests d'ici construisent des `LocationRecord` à
la main — le disque n'a rien à dire sur une règle d'attribution — et vérifient les
deux, cas par cas puis globalement (`verifie_comptabilite`).
"""

from datetime import UTC, datetime, timedelta

import pytest
from ecurie_store.db import LocationRecord
from ecurie_store.figures import Figures, classify, compute_figures
from ecurie_store.scanners import scan_hf, scan_ollama, scan_tree

SHA_A = "aa" * 32
SHA_B = "bb" * 32
TAILLE = 1000
MAINTENANT = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

_INODES: dict[str, int] = {}


def _inode_pour(path: str) -> int:
    """Un inode distinct par chemin : deux chemins n'en partagent un que si le
    test le demande explicitement, c'est-à-dire quand il parle de liens durs."""
    return _INODES.setdefault(path, 1000 + len(_INODES))


def loc(
    path: str,
    *,
    manager: str = "hf",
    size: int = TAILLE,
    sha256: str | None = SHA_A,
    device: int = 1,
    inode: int | None = None,
    link_kind: str = "plain",
    variant_ref: str | None = None,
    **meta: object,
) -> LocationRecord:
    return LocationRecord(
        path=path,
        manager=manager,
        size=size,
        mtime=1.0,
        device=device,
        inode=_inode_pour(path) if inode is None else inode,
        link_kind=link_kind,
        sha256=sha256,
        variant_ref=variant_ref,
        meta=dict(meta),
    )


def lien(
    path: str,
    *,
    target: str = "/ailleurs",
    available: bool = True,
    size: int = 0,
    variant_ref: str | None = None,
    **meta: object,
) -> LocationRecord:
    """Un lien symbolique tel que le scanner l'observe : cible et disponibilité en `meta`."""
    return loc(
        path,
        size=size,
        sha256=None,
        link_kind="symlink",
        variant_ref=variant_ref,
        target=target,
        available=available,
        **meta,
    )


def il_y_a(jours: int) -> str:
    return (MAINTENANT - timedelta(days=jours)).isoformat()


def attribution_unique(records: list[LocationRecord], unused: set[str] | None = None):
    """L'attribution d'un jeu de records qui ne décrit qu'un seul contenu."""
    resultats = classify(records, unused)
    assert len(resultats) == 1, [g.key for g, _ in resultats]
    return resultats[0][1]


# --- vérificateur d'invariant ---------------------------------------------------


def _tailles_par_inode(records: list[LocationRecord]) -> dict[tuple[int, int], int]:
    """Ce que le disque contient vraiment : un inode, une taille, une seule fois."""
    tailles: dict[tuple[int, int], int] = {}
    for r in records:
        if r.link_kind == "symlink":
            continue
        tailles.setdefault((r.device, r.inode), r.size)
    return tailles


def _promesses(
    records: list[LocationRecord], unused: set[str]
) -> tuple[list[tuple[str, str, int]], set[tuple[int, int]]]:
    """Ce que le module promet, chemin par chemin : (poste, chemin, octets annoncés).

    Renvoie aussi les inodes des exemplaires gardés par une déduplication : ceux-là
    doivent survivre, aucun poste n'a le droit de les compter.
    """
    fichiers = [r for r in records if r.link_kind != "symlink"]
    par_chemin = {r.path: r for r in fichiers}
    promesses: list[tuple[str, str, int]] = []
    gardes: set[tuple[int, int]] = set()
    for _group, attribution in classify(fichiers, unused):
        for chemin, octets in attribution.trash_paths:
            promesses.append(("corbeille", chemin, octets))
        for action in attribution.dup_actions:
            # Un lien dur ne libère que la taille réelle du fichier qu'il remplace.
            reels = sum(par_chemin[p].size for p in action.replace)
            assert action.bytes_reclaimed == reels, (
                f"{action.keep} : {action.bytes_reclaimed} o annoncés pour {reels} o réels"
            )
            garde = par_chemin[action.keep]
            gardes.add((garde.device, garde.inode))
            for chemin in action.replace:
                promesses.append(("duplication", chemin, par_chemin[chemin].size))
        for chemin, octets in attribution.unused_paths:
            promesses.append(("jamais-utilisé", chemin, octets))
    return promesses, gardes


def verifie_comptabilite(records: list[LocationRecord], **kwargs) -> Figures:
    """L'invariant général, sous sa forme la plus forte qu'on puisse défendre.

    **Chaque octet physique est soit gardé, soit récupéré — jamais les deux, et
    jamais promis deux fois.** Formellement, sur les octets *physiques* (une fois
    par inode, ce que le disque contient réellement) :

        Σ(quatre postes) + Σ(octets des inodes qu'aucun poste ne touche) = physique

    C'est une égalité, pas une borne : un octet qui échapperait aux deux côtés
    serait un octet dont personne ne sait s'il faut le garder. La forme demandée
    au §6.2 de l'architecture — Σ(postes) ≤ apparent − réel_unique_conservé — s'en
    déduit et est plus faible à deux titres : l'apparent majore le physique (il
    compte les liens durs plusieurs fois) et le réel unique conservé minore les
    octets conservés (il dédoublonne à travers les volumes, ce qu'un lien dur ne
    sait pas faire). On vérifie donc l'égalité forte, puis la forme demandée.
    """
    figures = compute_figures(records, now=MAINTENANT, **kwargs)
    promesses, gardes = _promesses(records, set(figures.unused_variants))
    tailles = _tailles_par_inode(records)
    par_chemin = {r.path: r for r in records}

    promis: dict[tuple[int, int], str] = {}
    total_promis = 0
    for poste, chemin, octets in promesses:
        rec = par_chemin[chemin]
        clef = (rec.device, rec.inode)
        if octets == 0:
            # Exemplaire supplémentaire d'un inode déjà promis : zéro octet de plus.
            assert clef in promis, f"{chemin} : 0 o promis sans que l'inode le soit ailleurs"
            continue
        assert octets == rec.size, f"{chemin} : {octets} o promis pour {rec.size} o réels"
        assert clef not in promis, (
            f"{chemin} : octets déjà promis par le poste {promis[clef]} — postes qui se recouvrent"
        )
        assert clef not in gardes, f"{chemin} : promis alors qu'une déduplication le garde"
        promis[clef] = poste
        total_promis += octets

    recuperable = figures.recoverable
    quatre_postes = (
        recuperable.duplication_bytes
        + recuperable.hf_stale_bytes
        + recuperable.orphan_bytes
        + recuperable.unused_bytes
    )
    assert quatre_postes == total_promis, "les totaux ne sont pas ceux des actions attribuées"

    conserve = sum(taille for clef, taille in tailles.items() if clef not in promis)
    assert quatre_postes + conserve == sum(tailles.values())
    assert quatre_postes <= figures.apparent_bytes - conserve
    return figures


# --- corbeille : révisions obsolètes et blobs orphelins -------------------------


def test_contenu_entierement_jetable_part_en_corbeille_sans_duplication():
    records = [loc("/hf/a", stale=True), loc("/hf/b", stale=True)]

    figures = verifie_comptabilite(records)
    assert figures.recoverable.hf_stale_bytes == 2 * TAILLE
    assert figures.recoverable.duplication_bytes == 0
    assert figures.duplicates == []  # rien à dédupliquer dans ce qu'on jette entièrement


def test_copie_obsolete_comptee_une_fois_et_jamais_recomptee_en_duplication():
    records = [loc("/hf/vivant"), loc("/hf/obsolete", stale=True)]

    figures = verifie_comptabilite(records)
    assert figures.recoverable.hf_stale_bytes == TAILLE
    assert figures.recoverable.duplication_bytes == 0
    # Deux copies apparentes, une seule réelle à garder, une seule promise.
    assert figures.apparent_bytes == 2 * TAILLE
    assert figures.recoverable.total_known_bytes == TAILLE


def test_blob_orphelin_et_revision_obsolete_ne_se_melangent_pas():
    records = [
        loc("/hf/vieux", sha256=SHA_A, stale=True),
        loc("/ol/perdu", sha256=SHA_B, size=300, manager="ollama", orphan=True),
    ]

    figures = verifie_comptabilite(records)
    assert figures.recoverable.hf_stale_bytes == TAILLE
    assert figures.recoverable.orphan_bytes == 300


def test_contenu_jetable_des_deux_cotes_n_est_compte_qu_une_fois():
    """Le même contenu détaché côté HF et non référencé côté Ollama : deux copies,
    deux fois 1000 octets à la corbeille, pas un de plus — quel que soit celui des
    deux postes où le module choisit de les ranger."""
    records = [loc("/hf/vieux", stale=True), loc("/ol/perdu", manager="ollama", orphan=True)]

    figures = verifie_comptabilite(records)
    corbeille = figures.recoverable.hf_stale_bytes + figures.recoverable.orphan_bytes
    assert corbeille == 2 * TAILLE
    assert figures.recoverable.duplication_bytes == 0


def test_copie_jetable_liee_en_dur_a_une_copie_gardee_ne_promet_rien():
    # Le blob obsolète est le même fichier que le vivant : le jeter ne rend rien.
    records = [loc("/hf/vivant", inode=77), loc("/hf/obsolete", inode=77, stale=True)]

    figures = verifie_comptabilite(records)
    assert figures.recoverable.total_known_bytes == 0
    assert attribution_unique(records).trash_paths == []


# --- liens durs et volumes ------------------------------------------------------


def test_deux_liens_durs_ne_sont_jamais_une_duplication_recuperable():
    records = [
        loc("/lms/a.gguf", inode=42, link_kind="hardlink"),
        loc("/lms/b.gguf", inode=42, link_kind="hardlink"),
    ]

    figures = verifie_comptabilite(records)
    assert figures.apparent_bytes == 2 * TAILLE  # `du` compterait deux fois
    assert figures.real_unique_bytes == TAILLE
    assert figures.recoverable.duplication_bytes == 0
    assert figures.duplicates == []


def test_liens_durs_jetables_ne_liberent_leurs_octets_qu_une_fois():
    records = [loc("/hf/x", inode=51, orphan=True), loc("/hf/y", inode=51, orphan=True)]

    figures = verifie_comptabilite(records)
    assert figures.recoverable.orphan_bytes == TAILLE
    # Les deux chemins partent quand même : c'est le second qui ne rapporte rien.
    assert attribution_unique(records).trash_paths == [("/hf/x", TAILLE), ("/hf/y", 0)]


def test_deux_copies_sur_deux_volumes_ne_sont_pas_deduplicables():
    records = [loc("/interne/a", device=1), loc("/Volumes/Parc/a", device=2)]

    figures = verifie_comptabilite(records)
    assert figures.recoverable.duplication_bytes == 0  # un lien dur ne traverse pas les volumes
    assert figures.real_unique_bytes == TAILLE  # mais le contenu reste unique


def test_trois_copies_reparties_sur_deux_volumes_recuperent_une_seule_copie():
    records = [
        loc("/interne/a", device=1),
        loc("/interne/b", device=1),
        loc("/Volumes/Parc/a", device=2),
    ]

    figures = verifie_comptabilite(records)
    assert figures.recoverable.duplication_bytes == TAILLE
    actions = attribution_unique(records).dup_actions
    assert [(a.keep, a.replace) for a in actions] == [("/interne/a", ["/interne/b"])]


# --- choix de l'exemplaire gardé ------------------------------------------------


def test_le_garde_suit_la_priorite_des_gestionnaires():
    records = [
        loc("/lms/a", manager="lmstudio"),
        loc("/ol/b", manager="ollama"),
        loc("/hf/c", manager="hf"),
    ]

    action = attribution_unique(records).dup_actions[0]
    assert action.keep == "/hf/c"  # l'outillage HF a le plus besoin du vrai fichier
    assert action.replace == ["/lms/a", "/ol/b"]
    verifie_comptabilite(records)


def test_le_gestionnaire_numerote_garde_sa_priorite():
    # `declared:1` est un second dossier déclaré, pas un gestionnaire inconnu.
    records = [loc("/tier/a", manager="tier"), loc("/decl/b", manager="declared:1")]

    assert attribution_unique(records).dup_actions[0].keep == "/decl/b"


def test_a_priorite_egale_le_variant_rattache_puis_le_chemin_departagent():
    rattaches = [loc("/hf/z", variant_ref="m@v"), loc("/hf/a")]
    assert attribution_unique(rattaches).dup_actions[0].keep == "/hf/z"

    egaux = [loc("/hf/zz"), loc("/hf/aa")]
    assert attribution_unique(egaux).dup_actions[0].keep == "/hf/aa"


# --- poste « jamais utilisés » --------------------------------------------------


def test_poste_jamais_utilises_inconnu_tant_qu_il_n_y_a_pas_de_telemetrie():
    records = [loc("/hf/a", variant_ref="m@v"), loc("/hf/b", variant_ref="m@v")]

    figures = verifie_comptabilite(records, last_runs={}, telemetry=False)
    assert figures.recoverable.unused_known is False
    assert figures.recoverable.unused_bytes == 0
    assert figures.unused_variants == []
    # Inconnu n'est pas zéro : le poste n'entre pas dans le total annoncé, qui se
    # limite ici à la duplication.
    assert figures.recoverable.duplication_bytes == TAILLE
    assert figures.recoverable.total_known_bytes == TAILLE


def test_variant_sans_run_bascule_au_poste_jamais_utilises():
    records = [loc("/hf/a", variant_ref="m@v"), loc("/ol/b", manager="ollama", variant_ref="m@v")]

    figures = verifie_comptabilite(records, last_runs={}, telemetry=True)
    assert figures.recoverable.unused_known is True
    assert figures.recoverable.unused_bytes == 2 * TAILLE  # les deux exemplaires partent
    assert figures.recoverable.duplication_bytes == 0  # et ne sont plus comptés en double
    assert figures.unused_variants == ["m@v"]


def test_variant_utilise_recemment_ne_bascule_pas():
    records = [loc("/hf/a", variant_ref="m@v")]

    figures = verifie_comptabilite(
        records, last_runs={"m@v": il_y_a(3)}, telemetry=True, unused_after_days=90
    )
    assert figures.recoverable.unused_bytes == 0
    assert figures.unused_variants == []


def test_variant_dont_le_dernier_run_depasse_le_delai_bascule():
    records = [loc("/hf/a", variant_ref="m@v")]

    figures = verifie_comptabilite(
        records, last_runs={"m@v": il_y_a(120)}, telemetry=True, unused_after_days=90
    )
    assert figures.recoverable.unused_bytes == TAILLE
    assert figures.unused_variants == ["m@v"]


def test_fichier_non_rattache_au_registre_n_est_jamais_declare_inutilise():
    # Sans variant_ref, aucun run ne peut prouver l'usage : le silence n'accuse pas.
    records = [loc("/comfy/inconnu.safetensors", manager="comfy", variant_ref=None)]

    figures = verifie_comptabilite(records, last_runs={}, telemetry=True)
    assert figures.recoverable.unused_bytes == 0
    assert figures.unresolved_bytes == TAILLE


def test_contenu_partage_avec_un_variant_utilise_reste_en_duplication():
    records = [
        loc("/hf/dormant", variant_ref="dort@v"),
        loc("/ol/actif", manager="ollama", variant_ref="actif@v"),
    ]

    figures = verifie_comptabilite(
        records, last_runs={"actif@v": il_y_a(1)}, telemetry=True, unused_after_days=90
    )
    assert figures.unused_variants == ["dort@v"]
    assert figures.recoverable.unused_bytes == 0  # le contenu sert encore au variant actif
    assert figures.recoverable.duplication_bytes == TAILLE


# --- liens symboliques : froid, et hors des chiffres ----------------------------


def test_liens_symboliques_hors_de_l_apparent_et_du_reel_unique():
    # Taille non nulle exprès : c'est le `link_kind` qui exclut, pas la taille.
    records = [
        loc("/lms/present.gguf", sha256=None, manager="lmstudio"),
        lien("/lms/deporte", size=999, manager="lmstudio", target="/Volumes/Parc/deporte"),
    ]

    figures = verifie_comptabilite(records)
    assert figures.apparent_bytes == TAILLE
    assert figures.real_unique_bytes == TAILLE
    assert figures.by_manager == {"lmstudio": (TAILLE, 1)}  # le lien ne pèse pour personne


def test_lien_dont_la_cible_manque_remonte_en_froid_indisponible():
    records = [
        lien(
            "/lms/absent.gguf",
            target="/Volumes/Parc/absent.gguf",
            available=False,
            variant_ref="m@v",
            manager="lmstudio",
        ),
        lien(
            "/hf/snapshots/rev/model.safetensors",
            target="/hf/blobs/aa",
            snapshot="/hf/snapshots/rev",
        ),
    ]

    figures = compute_figures(records, now=MAINTENANT)
    # Les liens internes au cache HF ne sont pas du tiering : ils n'ont rien à dire ici.
    assert [c.path for c in figures.cold] == ["/lms/absent.gguf"]
    froid = figures.cold[0]
    assert froid.available is False
    assert froid.variant_ref == "m@v"
    assert froid.target == "/Volumes/Parc/absent.gguf"
    assert figures.cold_unavailable == [froid]


# --- l'invariant, sur des jeux entiers ------------------------------------------


def _parc_synthetique() -> list[LocationRecord]:
    """Tous les cas à la fois : c'est leur cohabitation qui fait mentir un total."""
    return [
        loc("/hf/live", sha256=SHA_A),
        loc("/ol/live", sha256=SHA_A, manager="ollama"),
        loc("/hf/stale", sha256=SHA_B, size=500, stale=True),
        loc("/hf/orphan", sha256="cc" * 32, size=200, orphan=True),
        loc("/lms/a", sha256=None, size=2000, inode=90, link_kind="hardlink", manager="lmstudio"),
        loc("/lms/b", sha256=None, size=2000, inode=90, link_kind="hardlink", manager="lmstudio"),
        loc("/Volumes/Parc/live", sha256=SHA_A, device=2, manager="tier"),
        loc("/hf/dormant", sha256="dd" * 32, size=700, variant_ref="dort@v"),
        lien("/hf/froid", available=False),
    ]


@pytest.mark.parametrize(
    ("records", "options"),
    [
        pytest.param([], {}, id="vide"),
        pytest.param([loc("/j1", stale=True), loc("/j2", stale=True)], {}, id="tout-jetable"),
        pytest.param([loc("/g"), loc("/j", stale=True)], {}, id="jetable-et-garde"),
        pytest.param(
            _parc_synthetique(),
            {"last_runs": {}, "telemetry": True, "unused_after_days": 30},
            id="parc-avec-telemetrie",
        ),
        pytest.param(_parc_synthetique(), {}, id="parc-sans-telemetrie"),
    ],
)
def test_invariant_chaque_octet_est_garde_ou_recupere_jamais_les_deux(records, options):
    verifie_comptabilite(records, **options)


def test_l_invariant_est_serre_et_non_vacant():
    # Une borne qu'on n'atteint jamais ne prouverait rien : ici tout le récupérable
    # est promis et tout le reste est conservé, à l'octet près.
    records = [loc("/hf/garde"), loc("/ol/copie", manager="ollama"), loc("/hf/j", stale=True)]

    figures = verifie_comptabilite(records)
    physique = sum(_tailles_par_inode(records).values())
    assert physique == 3 * TAILLE
    assert figures.recoverable.total_known_bytes == 2 * TAILLE
    assert figures.recoverable.total_known_bytes == physique - figures.real_unique_bytes


# --- le faux parc complet -------------------------------------------------------


def test_parc_complet_chiffres_documentes(fake_hf, fake_ollama, fake_lmstudio):
    records = scan_hf(fake_hf) + scan_ollama(fake_ollama) + scan_tree(fake_lmstudio, "lmstudio")

    figures = verifie_comptabilite(records)
    assert figures.apparent_bytes == 7110
    assert figures.real_unique_bytes == 4110
    assert figures.recoverable.duplication_bytes == 1000
    assert figures.recoverable.hf_stale_bytes == 500
    assert figures.recoverable.orphan_bytes == 300
    assert figures.recoverable.total_known_bytes == 1800
    assert figures.cold == []  # les liens du cache HF ne sont pas des variants déportés

    # L'égalité forte, sur le vrai résultat d'un scan : 5110 octets physiques,
    # 1800 promis, 3310 conservés — le lien dur LM Studio explique les 2000 octets
    # d'écart entre l'apparent et le physique.
    physique = sum(_tailles_par_inode(records).values())
    assert physique == 5110
    assert figures.recoverable.total_known_bytes + 3310 == physique


# --- les pièges de l'identité de contenu ------------------------------------------
#
# Ces trois cas ont d'abord été rouges : le regroupement se faisait sur le seul
# sha256, ce qui scindait un inode partagé en deux contenus et rapprochait deux
# tailles différentes. `group_by_content` applique désormais les niveaux du
# hachage dans l'ordre (inode, puis hash, à taille exacte).


def test_liens_durs_comptent_une_fois_meme_si_un_seul_porte_un_sha256():
    """Cas produit par la déduplication d'Écurie elle-même : après `apply`, le
    fichier LM Studio est un lien dur vers le blob HF, mais son sha256 n'est pas
    réinjecté (le cache est invalidé par le changement d'inode). L'identité
    d'inode est le niveau 1 du hachage (CONCEPTION.md §1.2) : elle suffit à dire
    « même artefact », le réel unique ne doit pas compter ces octets deux fois."""
    records = [
        loc("/hf/blobs/aa", inode=61, sha256=SHA_A),  # inode partagé : un seul fichier
        loc("/lms/a.gguf", inode=61, sha256=None, manager="lmstudio"),
    ]

    figures = compute_figures(records, now=MAINTENANT)
    assert figures.apparent_bytes == 2 * TAILLE
    assert figures.real_unique_bytes == TAILLE


def test_deux_postes_ne_promettent_jamais_les_memes_octets_physiques():
    """Même cause, conséquence plus grave : les deux chemins du même inode tombent
    dans deux groupes de contenu, et le poste « jamais utilisés » promet chacun
    d'eux — soit deux fois les mêmes 1000 octets physiques."""
    records = [
        loc("/hf/blobs/aa", inode=62, sha256=SHA_A, variant_ref="m@v"),
        loc("/lms/a.gguf", inode=62, sha256=None, manager="lmstudio", variant_ref="m@v"),
    ]

    figures = compute_figures(records, last_runs={}, telemetry=True, now=MAINTENANT)
    assert figures.recoverable.unused_bytes == TAILLE


def test_deux_tailles_differentes_ne_sont_jamais_comparees_comme_doublons():
    """« Le pré-filtre des doublons potentiels est la taille exacte en octets :
    deux fichiers de tailles différentes ne sont jamais comparés » (CONCEPTION.md
    §1.2, appliqué par `hashing.dedup_candidates`). Un hash annoncé n'est pas
    vérifié : un blob tronqué porte le nom du contenu complet. Le regrouper sur le
    seul sha256 fait promettre la taille du gros pour la suppression du petit."""
    records = [
        loc("/hf/blobs/complet", size=1000, sha256=SHA_A, hash_source="announced"),
        loc("/ol/blobs/tronque", size=10, sha256=SHA_A, manager="ollama", hash_source="announced"),
    ]

    figures = compute_figures(records, now=MAINTENANT)
    assert figures.recoverable.duplication_bytes == 0
    assert figures.apparent_bytes == 1010
    assert figures.real_unique_bytes == 1010


# --- ce qu'un inode ne dit pas tout seul -------------------------------------------


def test_deux_volumes_peuvent_porter_le_meme_numero_d_inode():
    """Un numéro d'inode est propre à son système de fichiers : le blob interne et
    sa copie sur le disque externe peuvent parfaitement porter tous les deux le 42.
    Les confondre ferait promettre une seule fois deux fichiers bien réels."""
    records = [
        loc("/hf/blobs/aa", device=1, inode=42, stale=True, nlink=1),
        loc("/Volumes/Parc/blobs/aa", device=2, inode=42, manager="tier", stale=True, nlink=1),
    ]

    figures = verifie_comptabilite(records)
    assert figures.apparent_bytes == 2 * TAILLE
    assert figures.real_unique_bytes == TAILLE  # un seul contenu, deux exemplaires
    assert figures.recoverable.hf_stale_bytes == 2 * TAILLE


def test_trois_copies_sur_deux_volumes_ne_rendent_qu_une_copie_par_volume():
    """Un lien dur ne traverse pas les volumes : sur un disque qui porte deux
    copies et un autre qui n'en porte qu'une, seule la copie en trop du premier
    est récupérable."""
    records = [
        loc("/hf/blobs/aa", device=1, inode=10, nlink=1),
        loc("/lms/copie.gguf", device=1, inode=11, manager="lmstudio", nlink=1),
        loc("/Volumes/Parc/aa", device=2, inode=10, manager="tier", nlink=1),
    ]

    figures = verifie_comptabilite(records)
    assert figures.recoverable.duplication_bytes == TAILLE


def test_le_nlink_le_plus_eleve_de_la_grappe_fait_foi():
    """Une grappe mixte — un enregistrement d'une base d'avant qu'on relève le
    nombre de liens, un enregistrement récent qui le porte — ne doit pas devenir
    crédule à cause de l'ordre alphabétique des chemins. Ici une troisième
    référence échappe au parc : rien n'est récupérable, et le dire est le seul
    résultat honnête."""
    records = [
        loc("/a/vieux.gguf", manager="lmstudio", inode=99, orphan=True),  # sans nlink
        loc("/b/recent.gguf", manager="ollama", inode=99, orphan=True, nlink=3),
    ]

    figures = verifie_comptabilite(records)
    assert figures.recoverable.orphan_bytes == 0
    assert figures.recoverable.total_known_bytes == 0

    # Et l'inverse : les deux chemins connus couvrent l'inode, les octets reviennent.
    couverts = [
        loc("/a/vieux.gguf", manager="lmstudio", inode=99, orphan=True),
        loc("/b/recent.gguf", manager="ollama", inode=99, orphan=True, nlink=2),
    ]
    assert verifie_comptabilite(couverts).recoverable.orphan_bytes == TAILLE


def test_un_inode_tenu_hors_du_parc_est_ecarte_et_non_tu():
    """Ne rien promettre ne suffit pas : le rapport doit dire pourquoi il n'a rien
    proposé, sinon un parc plein de doublons passe pour un parc propre. Ici les
    deux copies sont tenues par une référence hors du parc : rien à gagner."""
    records = [
        loc("/hf/blobs/aa", inode=20, nlink=2),
        loc("/lms/copie.gguf", inode=21, manager="lmstudio", nlink=2),
    ]

    attribution = attribution_unique(records)
    assert attribution.dup_bytes == 0
    assert attribution.skipped == [("/lms/copie.gguf", "lien dur tenu hors du parc scanné")]


def test_on_garde_de_preference_l_inode_qu_on_ne_pourrait_pas_liberer():
    """Entre deux copies, garder celle qu'une référence extérieure retient de toute
    façon rend l'autre récupérable ; garder l'autre ne rendrait rien. La priorité
    des gestionnaires ne passe qu'après."""
    records = [
        loc("/hf/blobs/aa", inode=20, nlink=2),  # une référence hors du parc le tient
        loc("/lms/copie.gguf", inode=21, manager="lmstudio", nlink=1),
    ]

    attribution = attribution_unique(records)
    assert attribution.dup_bytes == TAILLE
    assert attribution.dup_actions[0].keep == "/hf/blobs/aa"
    assert attribution.dup_actions[0].replace == ["/lms/copie.gguf"]
    assert attribution.skipped == []
