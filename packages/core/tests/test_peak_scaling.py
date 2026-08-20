"""Un pic qui dépend de l'entrée (ARCHITECTURE.md §7, découvert au v0.3).

MiniMax Music 3, mesuré sur cette machine : 13,2 Gio pour 15 s d'audio, 18,1 pour
20, 23,9 pour 30. Avec un pic unique, le contrôle d'admission doit choisir entre
refuser tous les jobs courts et laisser passer les longs. La pente lève le
dilemme — à condition d'être mesurée, comme le reste du profil.
"""

import pytest
from ecurie_core.models import PeakScaling, Profile

GIB = 1 << 30


def profil(**extra) -> Profile:
    return Profile.model_validate(
        {
            "disk_bytes": 9 * GIB,
            "peak_unified_memory_bytes": 24 * GIB,
            "measured_on": "essai",
            "measured_at": "2026-08-20",
            **extra,
        }
    )


ECHELLE = {
    "parameter": "duration_seconds",
    "base_bytes": 3 * GIB,
    "bytes_per_unit": 0.7 * GIB,
    "measured_range": [15, 30],
}


def test_sans_pente_le_pic_mesure_fait_foi():
    attendu, note = profil().expected_peak({"duration_seconds": 5})
    assert attendu == 24 * GIB
    assert note is None


def test_avec_pente_le_pic_suit_l_entree():
    p = profil(peak_scaling=ECHELLE)
    assert p.expected_peak({"duration_seconds": 15})[0] == int(3 * GIB + 15 * 0.7 * GIB)
    assert p.expected_peak({"duration_seconds": 30})[0] == int(3 * GIB + 30 * 0.7 * GIB)


def test_sans_le_parametre_dans_l_entree_on_reste_prudent():
    """Le pire cas mesuré plutôt qu'une extrapolation depuis rien."""
    p = profil(peak_scaling=ECHELLE)
    assert p.expected_peak({"prompt": "jazz"})[0] == 24 * GIB
    assert p.expected_peak(None)[0] == 24 * GIB
    assert p.expected_peak({})[0] == 24 * GIB


def test_un_booleen_n_est_pas_une_mesure():
    """`True` vaut 1 en Python : sans ce garde-fou, un drapeau deviendrait une durée."""
    p = profil(peak_scaling=ECHELLE)
    assert p.expected_peak({"duration_seconds": True})[0] == 24 * GIB


def test_hors_intervalle_mesure_on_extrapole_et_on_le_dit():
    p = profil(peak_scaling=ECHELLE)
    attendu, note = p.expected_peak({"duration_seconds": 90})
    assert attendu == int(3 * GIB + 90 * 0.7 * GIB)
    assert note and "extrapolé" in note and "[15, 30]" in note


def test_sous_l_intervalle_on_ne_descend_pas_sous_le_pire_cas():
    """La droite n'est éprouvée que dans sa plage : en dessous, elle pourrait
    sous-estimer un coût fixe qu'aucun point de mesure n'a montré."""
    p = profil(peak_scaling=ECHELLE)
    attendu, note = p.expected_peak({"duration_seconds": 2})
    assert attendu == 24 * GIB
    assert note is not None


def test_la_pente_est_bornee_par_le_schema():
    with pytest.raises(ValueError):
        PeakScaling.model_validate({**ECHELLE, "measured_range": [15]})
