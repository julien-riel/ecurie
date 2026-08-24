"""Contrôle d'admission mémoire (CONCEPTION.md §5.4, ARCHITECTURE.md §7).

Fonction pure : le parc, le budget et le candidat sont donnés, la décision en
découle. Tout se simule donc au chiffre près, et tout est écrit au chiffre près —
c'est le seul module dont une erreur se paie en swap de trente secondes ou en
OOM, et un `assert marge > 0` n'y prouverait rien.

Convention des jeux d'essai : budgets ronds en Gio, et les `last_used` ne valent
que par leur ordre — 1.0 est le moins récemment utilisé, 3.0 le plus récent. Les
listes de résidents sont volontairement données dans un ordre qui n'est pas
l'ordre LRU.
"""

from ecurie_core.config import Config, resolve_heavy_threshold
from ecurie_runtime.admission import (
    DEFAULT_HEAVY_THRESHOLD,
    DEFAULT_MAX_HEAVY_RESIDENT,
    Policy,
    Resident,
    plan_admission,
    residual_bytes,
)

GIB = 1 << 30
# Ce que Metal annonce sur la machine de référence (24 Go de mémoire unifiée),
# et le budget sous lequel les 8 Gio du seuil ont été calés.
BUDGET_REFERENCE = 19_069_665_280

BUDGET_16 = Policy(budget_bytes=16 * GIB)
BUDGET_32 = Policy(budget_bytes=32 * GIB)


# --- la politique par défaut ------------------------------------------------------


def test_la_politique_par_defaut_est_celle_de_l_architecture():
    politique = Policy(budget_bytes=16 * GIB)

    assert politique.max_heavy_resident == 1
    assert politique.heavy_threshold_bytes == 8 * GIB
    assert (DEFAULT_MAX_HEAVY_RESIDENT, DEFAULT_HEAVY_THRESHOLD) == (1, 8 * GIB)


def test_le_seuil_par_defaut_est_le_meme_des_deux_cotes():
    """`core` ne peut pas dépendre de `runtime` : le seuil est donc écrit deux fois.

    Deux valeurs qui divergent ne donneraient pas une erreur mais deux politiques
    — celle d'une machine configurée et celle d'un `Policy()` construit sans
    config — dont une seule serait celle qu'on croit appliquer. Le défaut de la
    config est une part du budget ; c'est donc au budget de référence que les
    deux doivent se rejoindre, à l'arrondi près.
    """
    assert Config().heavy_threshold_bytes == "auto"
    assert Config().max_heavy_resident == DEFAULT_MAX_HEAVY_RESIDENT
    résolu = resolve_heavy_threshold(Config(), BUDGET_REFERENCE)
    assert abs(résolu - DEFAULT_HEAVY_THRESHOLD) < GIB // 10


def test_le_seuil_auto_suit_le_budget_de_la_machine():
    """Le sens de la règle se transporte d'un Mac à l'autre, pas sa valeur.

    Sur la machine de référence, la voix (7,65 Gio) reste légère et l'image
    (15,95) est lourde. Sur un Mac de 16 Go — budget ~11,8 Gio —, un seuil resté
    à 8 Gio garderait la voix légère alors qu'elle occupe les deux tiers du
    budget : deux « légers » de ce calibre ne tiennent plus ensemble, et la
    politique croirait le contraire.
    """
    voix = 8_215_308_206  # 7,65 Gio, profil mesuré du parc

    référence = resolve_heavy_threshold(Config(), BUDGET_REFERENCE)
    assert voix < référence

    petit = resolve_heavy_threshold(Config(), 11_824_000_000)
    assert voix > petit


def test_un_seuil_explicite_dans_la_config_l_emporte_sur_la_part():
    """Le réglage reste un réglage : qui écrit un nombre d'octets l'obtient."""
    assert resolve_heavy_threshold(Config(heavy_threshold_bytes=2 * GIB), 32 * GIB) == 2 * GIB


# --- parc vide, et le refus qu'aucune éviction ne sauverait ------------------------


def test_parc_vide_le_candidat_tient_et_la_marge_est_exacte():
    décision = plan_admission("tts@v1", 4 * GIB, [], BUDGET_16)

    assert décision.admitted is True
    assert décision.evict == ()
    assert décision.headroom_bytes == 12 * GIB
    assert décision.reason == "tient dans le budget résiduel"
    assert décision.already_resident is False
    assert décision.measure_mode is False
    assert décision.blockers == ()


def test_un_candidat_qui_remplit_exactement_le_budget_est_admis():
    """Le refus porte sur « dépasse », pas sur « atteint » : à 16 Gio pile dans un
    budget de 16 Gio le modèle tient, et le refuser priverait la machine du plus
    gros variant qu'elle sait exécuter."""
    décision = plan_admission("gros@v1", 16 * GIB, [], BUDGET_16)

    assert décision.admitted is True
    assert décision.headroom_bytes == 0


def test_un_candidat_plus_gros_que_le_budget_est_refuse_sans_eviction():
    décision = plan_admission("enorme@v1", 20 * GIB, [], BUDGET_16)

    assert décision.admitted is False
    assert "décharger ne changerait rien" in décision.reason
    # Le refus est lu tel quel par « ecurie ps --for » et par le bandeau de
    # l'Atelier : il porte des Gio, pas onze chiffres bruts.
    assert "20 Gio" in décision.reason
    assert "16 Gio" in décision.reason
    assert décision.evict == ()
    assert décision.blockers == ()
    assert décision.headroom_bytes == 0

    # Même verdict avec un parc à vider : le refus ne dépend pas de ce qui est chargé.
    peuplé = plan_admission("enorme@v1", 20 * GIB, [Resident("petit@v1", 2 * GIB, 1.0)], BUDGET_16)
    assert peuplé.admitted is False
    assert peuplé.evict == ()


# --- variant sans profil mesuré ----------------------------------------------------


def test_un_variant_sans_profil_mesure_est_refuse_hors_mode_mesure():
    décision = plan_admission("neuf@v1", None, [], BUDGET_16)

    assert décision.admitted is False
    # La commande exacte à taper : sans elle, le refus est un cul-de-sac.
    assert "ecurie bench neuf@v1" in décision.reason
    assert décision.evict == ()
    assert décision.measure_mode is False
    assert décision.headroom_bytes == 0


# --- mode mesure -------------------------------------------------------------------


def test_le_mode_mesure_vide_le_parc_y_compris_les_epingles():
    résidents = [
        Resident("chaud@v1", 2 * GIB, 3.0),
        Resident("epingle@v1", 5 * GIB, 1.0, pinned=True),
    ]

    décision = plan_admission("a-mesurer@v1", 4 * GIB, résidents, BUDGET_16, measure=True)

    assert décision.admitted is True
    assert décision.measure_mode is True
    # Un profil mesuré avec d'autres modèles en mémoire mesure la machine, pas le
    # modèle : l'épingle ne protège rien ici, et elle part la première (LRU).
    assert décision.evict == ("epingle@v1", "chaud@v1")
    assert décision.headroom_bytes == 12 * GIB


def test_le_mode_mesure_admet_un_pic_inconnu():
    """C'est le seul chemin qui accepte un variant sans profil : c'est lui qui écrit
    le premier profil, il ne peut pas l'exiger d'avance."""
    décision = plan_admission(
        "neuf@v1", None, [Resident("chaud@v1", 3 * GIB, 1.0)], BUDGET_16, measure=True
    )

    assert décision.admitted is True
    assert décision.measure_mode is True
    assert décision.evict == ("chaud@v1",)
    assert décision.headroom_bytes == 16 * GIB  # pic inconnu compté pour zéro


def test_le_mode_mesure_decharge_meme_le_candidat_deja_resident():
    résidents = [
        Resident("a-mesurer@v1", 4 * GIB, 2.0),
        Resident("chaud@v1", 2 * GIB, 1.0),
    ]

    décision = plan_admission("a-mesurer@v1", 4 * GIB, résidents, BUDGET_16, measure=True)

    assert décision.evict == ("chaud@v1", "a-mesurer@v1")
    # Mesurer un worker déjà chaud mesurerait son état, pas son chargement : la
    # décision ne le déclare pas résident, il sera relancé à neuf.
    assert décision.already_resident is False


def test_le_mode_mesure_ne_passe_pas_sur_un_job_en_cours():
    """L'épingle est une préférence, un job en cours est un travail.

    Le banc d'essai vide le parc, et c'est sa raison d'être ; mais évincer un
    worker en pleine inférence ne rend pas la mémoire tout de suite, cela détruit
    une sortie que plus personne n'attendra. Le cas ne pouvait pas se poser tant
    qu'une commande tenait seule le parc.
    """
    résidents = [
        Resident("libre@v1", 2 * GIB, 3.0),
        Resident("occupe@v1", 5 * GIB, 1.0, busy=True),
    ]

    décision = plan_admission("a-mesurer@v1", 4 * GIB, résidents, BUDGET_16, measure=True)

    assert décision.admitted is False
    assert décision.blockers == ("occupe@v1",)
    assert "occupe@v1 a un job en cours" in décision.reason
    assert décision.evict == (), "un refus n'annonce pas d'éviction"


def test_le_mode_mesure_sur_un_parc_vide_n_evince_rien():
    décision = plan_admission("neuf@v1", 4 * GIB, [], BUDGET_16, measure=True)

    assert décision.admitted is True
    assert décision.evict == ()
    assert décision.headroom_bytes == 12 * GIB


# --- candidat déjà résident ---------------------------------------------------------


def test_un_candidat_deja_resident_est_admis_sans_rien_decharger():
    résidents = [
        Resident("asr@v1", 5 * GIB, 2.0),
        Resident("tts@v1", 4 * GIB, 1.0),
    ]

    décision = plan_admission("tts@v1", 4 * GIB, résidents, BUDGET_16)

    assert décision.admitted is True
    assert décision.already_resident is True
    assert décision.evict == ()
    # 16 − 5 (l'autre résident) − 4 (le candidat, compté une seule fois). Le compter
    # deux fois annoncerait 3 Gio et ferait décharger un voisin pour rien.
    assert décision.headroom_bytes == 7 * GIB


def test_un_lourd_deja_resident_ne_s_evince_pas_lui_meme():
    """Sans le court-circuit « déjà résident », la règle du parc verrait un lourd de
    trop et déchargerait précisément le modèle qu'on vient de demander."""
    résidents = [Resident("gros@v1", 9 * GIB, 1.0), Resident("petit@v1", 1 * GIB, 2.0)]

    décision = plan_admission("gros@v1", 9 * GIB, résidents, BUDGET_16)

    assert décision.admitted is True
    assert décision.already_resident is True
    assert décision.evict == ()
    assert décision.headroom_bytes == 6 * GIB


# --- éviction LRU -------------------------------------------------------------------


def test_l_eviction_lru_decharge_le_plus_ancien_et_pas_un_de_plus():
    résidents = [
        Resident("recent@v1", 4 * GIB, 3.0),
        Resident("ancien@v1", 4 * GIB, 1.0),
        Resident("median@v1", 4 * GIB, 2.0),
    ]

    décision = plan_admission("nouveau@v1", 6 * GIB, résidents, BUDGET_16)

    # 12 + 6 dépasse 16 ; un seul déchargement repasse dessous, le deuxième serait
    # un modèle rechargé pour rien. L'ordre de la liste n'est pas l'ordre LRU.
    assert décision.evict == ("ancien@v1",)
    assert décision.headroom_bytes == 2 * GIB
    assert "décharge ancien@v1" in décision.reason


def test_l_eviction_lru_enchaine_du_plus_ancien_au_plus_recent():
    résidents = [
        Resident("recent@v1", 4 * GIB, 3.0),
        Resident("ancien@v1", 4 * GIB, 1.0),
        Resident("median@v1", 4 * GIB, 2.0),
    ]

    # Aucun résident n'est lourd : seul le budget parle ici.
    décision = plan_admission("nouveau@v1", 10 * GIB, résidents, BUDGET_16)

    assert décision.evict == ("ancien@v1", "median@v1")
    assert décision.headroom_bytes == 2 * GIB


# --- la règle du parc : un seul lourd ------------------------------------------------


def test_un_candidat_lourd_evince_le_lourd_resident_meme_si_le_budget_suffisait():
    résidents = [Resident("lourd-a@v1", 9 * GIB, 1.0)]

    décision = plan_admission("lourd-b@v1", 9 * GIB, résidents, BUDGET_32)

    # 9 + 9 = 18 Gio tiendraient dans 32 : c'est la règle du parc, pas l'arithmétique,
    # qui décharge — deux lourds « qui tiennent » ne laissent rien au reste de la machine.
    assert décision.admitted is True
    assert décision.evict == ("lourd-a@v1",)
    assert décision.headroom_bytes == 23 * GIB
    assert "lourd-a@v1" in décision.reason


def test_deux_legers_restent_chauds_ensemble():
    résidents = [Resident("asr@v1", 2 * GIB, 1.0)]

    décision = plan_admission("tts@v1", 3 * GIB, résidents, BUDGET_16)

    assert décision.evict == ()
    assert décision.reason == "tient dans le budget résiduel"
    assert décision.headroom_bytes == 11 * GIB


def test_le_seuil_recalibre_laisse_cohabiter_la_voix_et_la_lecture_de_document():
    """La raison d'être du seuil à 8 Gio, sur les chiffres du parc réel.

    Ce ne sont pas des valeurs d'exemple : ce sont les quatre pics de
    `registry/measurements/`, et le budget relevé par Metal sur la machine de
    référence. À 6 Go — le seuil qu'avançait l'architecture avant toute mesure —
    les quatre modèles sont lourds, donc aucun ne cohabite jamais avec un autre
    et la politique ne distingue plus rien. Le test le vérifie dans les deux
    sens : si un jour quelqu'un remet le seuil sous 7,65 Gio, l'usage quotidien
    redevient une succession de rechargements et c'est ici qu'on l'apprend.
    """
    budget = Policy(budget_bytes=19_069_665_280)  # Metal, Mac17,4 24 Gio
    voix = 8_209_951_240  # qwen3-tts-1.7b@8bit-mlx
    document = 6_712_856_963  # qwen3-vl-8b-ocr@4bit
    image = 17_123_246_080  # sdxl-base@fp16

    ensemble = plan_admission("ocr@4bit", document, [Resident("tts@mlx", voix, 1.0)], budget)
    assert ensemble.evict == ()
    assert ensemble.headroom_bytes == 4_146_857_077

    # L'image, elle, reste lourde et prend toute la place : la mesure ne laisse
    # pas le choix, elle occupe 90 % du budget à elle seule.
    seule = plan_admission(
        "sdxl@fp16",
        image,
        [Resident("tts@mlx", voix, 2.0), Resident("ocr@4bit", document, 1.0)],
        budget,
    )
    assert seule.evict == ("ocr@4bit", "tts@mlx")

    # Le même parc sous l'ancien seuil : la voix devient lourde, et charger l'OCR
    # la décharge pour rien — 13,9 Gio tenaient pourtant dans 17,76.
    ancien = Policy(budget_bytes=19_069_665_280, heavy_threshold_bytes=6 * GIB)
    assert plan_admission("ocr@4bit", document, [Resident("tts@mlx", voix, 1.0)], ancien).evict == (
        "tts@mlx",
    )


def test_un_candidat_leger_n_evince_pas_un_lourd_s_il_y_a_la_place():
    résidents = [Resident("lourd@v1", 9 * GIB, 1.0)]

    décision = plan_admission("leger@v1", 2 * GIB, résidents, BUDGET_16)

    assert décision.evict == ()
    assert décision.headroom_bytes == 5 * GIB


def test_un_candidat_pile_au_seuil_n_est_pas_lourd():
    """Le seuil est strict des deux côtés : à 8 Gio pile le candidat n'est pas lourd
    et ne déclenche pas la règle du parc — sinon tout modèle de 8 Gio la déclencherait.

    Ce n'est pas une subtilité gratuite : la voix du parc réel pèse 7,65 Gio, et
    c'est précisément pour la garder chaude que le seuil a été porté à 8.
    """
    résidents = [Resident("lourd@v1", 9 * GIB, 1.0)]

    décision = plan_admission("pile@v1", 8 * GIB, résidents, BUDGET_32)

    assert décision.evict == ()
    assert décision.headroom_bytes == 15 * GIB


def test_un_resident_pile_au_seuil_n_est_pas_lourd():
    résidents = [Resident("pile@v1", 8 * GIB, 1.0)]

    décision = plan_admission("lourd@v1", 9 * GIB, résidents, BUDGET_32)

    assert décision.evict == ()
    assert décision.headroom_bytes == 15 * GIB


def test_un_parc_qui_a_derive_revient_a_un_seul_lourd():
    """Deux lourds déjà résidents — un profil révisé à la hausse suffit à en arriver
    là : le candidat lourd les décharge tous les deux, pas seulement le premier."""
    résidents = [
        Resident("lourd-b@v1", 9 * GIB, 2.0),
        Resident("lourd-a@v1", 9 * GIB, 1.0),
    ]

    décision = plan_admission("lourd-c@v1", 10 * GIB, résidents, BUDGET_32)

    assert décision.evict == ("lourd-a@v1", "lourd-b@v1")
    assert décision.headroom_bytes == 22 * GIB


# --- la politique vient de `Policy`, pas du code -------------------------------------


def test_le_seuil_de_lourdeur_vient_de_la_politique():
    politique = Policy(budget_bytes=32 * GIB, heavy_threshold_bytes=2 * GIB)
    résidents = [Resident("trois@v1", 3 * GIB, 1.0)]

    serré = plan_admission("autre-trois@v1", 3 * GIB, résidents, politique)
    assert serré.evict == ("trois@v1",)  # deux lourds au sens de cette politique
    assert serré.headroom_bytes == 29 * GIB

    # Même parc, seuil par défaut : 3 Gio n'est plus lourd, rien ne bouge.
    assert plan_admission("autre-trois@v1", 3 * GIB, résidents, BUDGET_32).evict == ()


def test_max_heavy_resident_a_deux_laisse_deux_lourds_ensemble():
    politique = Policy(budget_bytes=32 * GIB, max_heavy_resident=2)
    résidents = [
        Resident("lourd-b@v1", 9 * GIB, 2.0),
        Resident("lourd-a@v1", 9 * GIB, 1.0),
    ]

    seul = plan_admission("lourd-c@v1", 9 * GIB, résidents[:1], politique)
    assert seul.evict == ()  # un lourd résident, deux autorisés

    plein = plan_admission("lourd-c@v1", 9 * GIB, résidents, politique)
    assert plein.evict == ("lourd-a@v1",)  # le troisième déloge le plus ancien, un seul
    assert plein.headroom_bytes == 14 * GIB


def test_max_heavy_resident_a_zero_interdit_tout_lourd():
    politique = Policy(budget_bytes=32 * GIB, max_heavy_resident=0)

    décision = plan_admission("lourd@v1", 9 * GIB, [], politique)

    assert décision.admitted is False


# --- épinglage ------------------------------------------------------------------------


def test_un_epingle_n_est_jamais_evince_hors_mode_mesure():
    résidents = [
        # Le plus ancien, donc la victime LRU naturelle — et pourtant intouchable.
        Resident("epingle@v1", 6 * GIB, 1.0, pinned=True),
        Resident("libre@v1", 5 * GIB, 2.0),
    ]

    décision = plan_admission("nouveau@v1", 6 * GIB, résidents, BUDGET_16)

    assert décision.admitted is True
    assert décision.evict == ("libre@v1",)
    assert décision.headroom_bytes == 4 * GIB


def test_un_epingle_qui_bloque_fait_refuser_en_nommant_ce_qui_bloque_et_ce_qui_manque():
    résidents = [
        Resident("libre@v1", 2 * GIB, 1.0),
        Resident("epingle@v1", 12 * GIB, 2.0, pinned=True),
    ]

    décision = plan_admission("nouveau@v1", 6 * GIB, résidents, BUDGET_16)

    assert décision.admitted is False
    assert décision.blockers == ("epingle@v1",)
    # 12 (l'épinglé qui reste) + 6 − 16 : ce qui manque une fois déchargé tout ce
    # qui pouvait l'être, pas avant — sinon le message réclame de libérer 4 Gio
    # alors que 2 suffisent.
    assert "il manque 2 Gio" in décision.reason
    assert "epingle@v1" in décision.reason
    assert "ecurie unload --force" in décision.reason
    assert décision.evict == ()  # un refus ne décharge rien


def test_le_refus_nomme_tous_les_epingles_qui_pesent():
    résidents = [
        Resident("epingle-a@v1", 6 * GIB, 1.0, pinned=True),
        Resident("epingle-b@v1", 6 * GIB, 2.0, pinned=True),
    ]

    décision = plan_admission("nouveau@v1", 6 * GIB, résidents, BUDGET_16)

    assert décision.admitted is False
    assert décision.blockers == ("epingle-a@v1", "epingle-b@v1")
    # Le motif accompagne chaque nom : « épinglé » se désépingle, « en cours de
    # job » s'attend. Ce ne sont pas les mêmes gestes.
    assert "epingle-a@v1 (épinglé), epingle-b@v1 (épinglé)" in décision.reason
    assert "il manque 2 Gio" in décision.reason


def test_un_resident_en_plein_job_n_est_jamais_evince():
    """Décharger le LRU ne doit pas vouloir dire tuer un travail en cours.

    L'évincer ne libérerait pas de la mémoire tout de suite : le worker meurt au
    milieu de son inférence, les fichiers de sortie sont perdus, et la commande
    qui a provoqué l'éviction ne sait même pas qu'elle vient de casser un job.
    """
    résidents = [
        Resident("occupe@v1", 6 * GIB, 1.0, busy=True),  # le plus ancien, donc le LRU
        Resident("libre@v1", 6 * GIB, 2.0),
    ]

    décision = plan_admission("nouveau@v1", 6 * GIB, résidents, BUDGET_16)

    assert décision.admitted is True
    assert décision.evict == ("libre@v1",)


def test_quand_tout_est_occupe_le_refus_le_dit():
    résidents = [
        Resident("occupe-a@v1", 6 * GIB, 1.0, busy=True),
        Resident("occupe-b@v1", 6 * GIB, 2.0, busy=True),
    ]

    décision = plan_admission("nouveau@v1", 6 * GIB, résidents, BUDGET_16)

    assert décision.admitted is False
    assert décision.evict == ()
    assert "en cours de job" in décision.reason
    assert décision.blockers == ("occupe-a@v1", "occupe-b@v1")


def test_un_lourd_epingle_bloque_un_candidat_lourd_meme_avec_du_budget():
    résidents = [Resident("lourd-epingle@v1", 9 * GIB, 1.0, pinned=True)]

    décision = plan_admission("lourd@v1", 9 * GIB, résidents, BUDGET_32)

    assert décision.admitted is False
    assert décision.blockers == ("lourd-epingle@v1",)
    assert décision.evict == ()


def test_un_refus_du_a_la_regle_du_parc_ne_parle_pas_d_octets_manquants():
    résidents = [
        Resident("lourd-epingle@v1", 9 * GIB, 1.0, pinned=True),
        Resident("leger-epingle@v1", 1 * GIB, 2.0, pinned=True),
    ]

    décision = plan_admission("lourd@v1", 9 * GIB, résidents, BUDGET_32)

    # 9 + 9 + 1 tiennent largement dans 32 : rien ne manque en octets, et le léger
    # épinglé n'a aucune part au blocage. Le désépingler ne débloquerait rien.
    assert décision.admitted is False
    assert "il manque 0 o" not in décision.reason
    assert décision.blockers == ("lourd-epingle@v1",)


# --- cumul des deux contraintes --------------------------------------------------------


def test_les_deux_regles_se_cumulent_sans_evincer_deux_fois_le_meme():
    résidents = [
        Resident("leger-ancien@v1", 3 * GIB, 1.0),
        Resident("lourd@v1", 9 * GIB, 2.0),
        Resident("leger-recent@v1", 3 * GIB, 3.0),
    ]

    décision = plan_admission("lourd-neuf@v1", 12 * GIB, résidents, BUDGET_16)

    # La règle du parc décharge d'abord le lourd, puis le budget réclame le plus
    # ancien des légers. Le lourd ne doit pas reparaître dans la liste : le
    # superviseur tuerait deux fois le même worker et la marge serait fausse.
    assert décision.evict == ("lourd@v1", "leger-ancien@v1")
    assert len(set(décision.evict)) == len(décision.evict)
    # 16 − 3 (le seul survivant) − 12 : la marge annoncée est celle du parc restant.
    assert décision.headroom_bytes == 1 * GIB
    assert décision.reason == "décharge lourd@v1, leger-ancien@v1 (moins récemment utilisés)"


def test_la_decision_ne_touche_pas_au_parc_qu_on_lui_passe():
    """La table des résidents appartient au superviseur : l'admission la lit, c'est
    lui qui l'amende une fois les workers réellement tués."""
    résidents = [Resident("lourd@v1", 9 * GIB, 1.0), Resident("leger@v1", 3 * GIB, 2.0)]
    copie = list(résidents)

    plan_admission("lourd-neuf@v1", 12 * GIB, résidents, BUDGET_16)

    assert résidents == copie


# --- budget résiduel et seuil de lourdeur -----------------------------------------------


def test_residual_bytes_est_le_budget_moins_les_residents():
    résidents = [Resident("a@v1", 4 * GIB, 1.0), Resident("b@v1", 5 * GIB, 2.0)]

    assert residual_bytes(résidents, BUDGET_16) == 7 * GIB
    assert residual_bytes([], BUDGET_16) == 16 * GIB


def test_residual_bytes_devient_negatif_sur_un_parc_surcharge():
    """Un parc au-delà du budget — profil révisé à la hausse, worker qui a dépassé
    le sien — doit se voir : un plancher à zéro ferait croire qu'il reste la place."""
    résidents = [Resident("a@v1", 10 * GIB, 1.0), Resident("b@v1", 10 * GIB, 2.0)]

    assert residual_bytes(résidents, BUDGET_16) == -4 * GIB


def test_resident_heavy_est_strictement_au_dessus_du_seuil():
    assert Resident("pile@v1", 8 * GIB, 1.0).heavy() is False
    assert Resident("au-dessus@v1", 8 * GIB + 1, 1.0).heavy() is True
    assert Resident("en-dessous@v1", 8 * GIB - 1, 1.0).heavy() is False


def test_resident_heavy_accepte_un_autre_seuil():
    résident = Resident("moyen@v1", 3 * GIB, 1.0)

    assert résident.heavy() is False
    assert résident.heavy(2 * GIB) is True
    assert résident.heavy(3 * GIB) is False  # strict avec un seuil donné aussi


# --- mode hors budget -------------------------------------------------------------
#
# Le seul refus qui se force, et les trois garanties qui l'encadrent : le parc
# part entier, un travail en cours ne se sacrifie pas, et la décision se lit.


def test_sans_le_mode_un_candidat_trop_gros_est_refuse_et_le_refus_dit_combien_il_manque():
    décision = plan_admission("gros@4bit", 20 * GIB, [], BUDGET_16)

    assert not décision.admitted
    assert not décision.overcommit
    assert décision.overflow_bytes == 4 * GIB
    assert "--hors-budget" in décision.reason


def test_le_mode_hors_budget_admet_ce_que_le_budget_refusait():
    décision = plan_admission("gros@4bit", 20 * GIB, [], BUDGET_16, overcommit=True)

    assert décision.admitted
    assert décision.overcommit
    assert décision.overflow_bytes == 4 * GIB
    # Il ne reste rien, et le chiffre le dit plutôt que de s'arrêter à zéro.
    assert décision.headroom_bytes == -4 * GIB


def test_le_mode_hors_budget_vide_le_parc_entier_y_compris_ce_qui_tiendrait():
    """Un modèle qui déborde seul ne laisse pas de marge à partager.

    Épargner le petit résident économiserait un gigaoctet et le paierait en
    pages échangées pendant toute la durée du job.
    """
    parc = [
        Resident(ref="petit@4bit", peak_bytes=1 * GIB, last_used=2.0),
        Resident(ref="moyen@4bit", peak_bytes=5 * GIB, last_used=1.0),
    ]

    décision = plan_admission("gros@4bit", 20 * GIB, parc, BUDGET_16, overcommit=True)

    assert décision.admitted
    assert set(décision.evict) == {"petit@4bit", "moyen@4bit"}


def test_le_mode_hors_budget_ne_detruit_pas_un_job_en_cours():
    parc = [Resident(ref="occupé@4bit", peak_bytes=2 * GIB, last_used=1.0, busy=True)]

    décision = plan_admission("gros@4bit", 20 * GIB, parc, BUDGET_16, overcommit=True)

    assert not décision.admitted
    assert décision.blockers == ("occupé@4bit",)
    assert "en cours de job" in décision.reason


def test_le_mode_hors_budget_ne_passe_pas_outre_un_epingle():
    parc = [Resident(ref="épinglé@4bit", peak_bytes=2 * GIB, last_used=1.0, pinned=True)]

    décision = plan_admission("gros@4bit", 20 * GIB, parc, BUDGET_16, overcommit=True)

    assert not décision.admitted
    assert "épinglé" in décision.reason


def test_le_mode_hors_budget_ne_force_pas_un_profil_manquant():
    """On ne peut pas assumer un dépassement dont on ignore la taille."""
    décision = plan_admission("jamais-mesuré@4bit", None, [], BUDGET_16, overcommit=True)

    assert not décision.admitted
    assert not décision.overcommit
    assert "ecurie bench" in décision.reason


def test_le_mode_hors_budget_ne_change_rien_a_un_modele_qui_tient():
    """Le drapeau est sans effet quand il n'y a rien à forcer."""
    dedans = plan_admission("petit@4bit", 4 * GIB, [], BUDGET_16)
    forcé = plan_admission("petit@4bit", 4 * GIB, [], BUDGET_16, overcommit=True)

    assert (dedans.admitted, forcé.admitted) == (True, True)
    assert not forcé.overcommit
    assert forcé.headroom_bytes == dedans.headroom_bytes == 12 * GIB


def test_le_refus_hors_budget_annonce_ce_que_le_mode_coute():
    """Ces phrases sont rendues telles quelles par `ecurie ps --for` et l'Atelier.

    La dernière n'est pas une précaution de style : le mode a été mesuré, et il
    n'évite pas tout. Qwen3.6-27B tient en génération de texte et échoue à
    décrire une image, où Metal refuse d'un coup un buffer trop grand au lieu de
    le paginer. Promettre « ça marchera, en plus lent » serait faux.
    """
    décision = plan_admission("gros@4bit", 20 * GIB, [], BUDGET_16, overcommit=True)

    for attendu in ("pagine", "plus lentement", "Insufficient Memory"):
        assert attendu in décision.reason
