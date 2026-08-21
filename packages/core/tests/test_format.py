"""Le formateur de la mémoire unifiée — et ce qu'il refuse de dire.

Il double `fmt_bytes` du store au lieu de le remplacer, et le doublon est
assumé : le disque se compte en unités décimales parce que c'est ce que le
Finder affiche, la mémoire en unités binaires parce que c'est ce que le budget,
le seuil de lourdeur et les profils mesurés écrivent. Ces tests fixent la
frontière.
"""

from ecurie_core.format import fmt_memory

GIB = 1 << 30


def test_les_puissances_de_deux_du_registre_se_lisent_rondes():
    """Le seuil de lourdeur est écrit `8 * (1 << 30)` dans deux fichiers.

    `fmt_bytes` en ferait « 8.59 Go » : un chiffre qui n'apparaît ni dans
    `admission.py`, ni dans `Config`, ni dans `~/.ecurie/config.toml`, et que
    personne ne pourrait relier au réglage qu'il faut changer.
    """
    assert fmt_memory(8 * GIB) == "8 Gio"
    assert fmt_memory(16 * GIB) == "16 Gio"
    assert fmt_memory(2 * GIB) == "2 Gio"


def test_un_budget_mesure_garde_deux_decimales():
    # Le budget Metal de la machine de développement, tel que `detect_budget` le rend.
    assert fmt_memory(19_069_665_280) == "17.76 Gio"
    # Le pic du TTS, tel que `registry/models/qwen3-tts-1.7b.yaml` le porte.
    assert fmt_memory(8_209_951_240) == "7.65 Gio"


def test_les_petites_tailles_ne_montent_pas_d_unite():
    assert fmt_memory(0) == "0 o"
    assert fmt_memory(512) == "512 o"
    assert fmt_memory(1024) == "1 Kio"
    assert fmt_memory(4 * 1024 * 1024) == "4 Mio"


def test_un_pic_inconnu_ne_devient_jamais_zero():
    """`None` et `0` ne disent pas la même chose.

    Un pic non mesuré fait refuser l'admission par principe ; un pic nul
    n'existe pas. La règle est celle de `formatBytes` côté UI, et elle doit
    l'être : les deux rendent les mêmes grandeurs, souvent dans la même phrase.
    """
    assert fmt_memory(None) == "pic inconnu"
    assert fmt_memory(0) == "0 o"


def test_un_manque_negatif_garde_son_signe():
    """`headroom_bytes` passe sous zéro quand des workers hors budget subsistent."""
    assert fmt_memory(-2 * GIB) == "-2 Gio"
