"""Nommer la machine qui a mesuré (CONCEPTION.md §1.1).

Le slug devient un nom de fichier committé dans `registry/measurements/`. Il doit
donc être stable — deux relevés pris à six mois d'écart sur le même Mac se
remplacent, ils ne s'accumulent pas — et il doit survivre à la virgule de
`Mac17,4`, que peu d'outils aiment voir dans un chemin.
"""

from ecurie_core.machine import (
    MACHINE_INCONNUE,
    describe_machine,
    hardware_of,
    machine_id,
    machine_slug,
)


def test_le_slug_reduit_l_identite_materielle_a_un_nom_de_fichier():
    assert machine_slug("Mac17,4 24 Gio") == "mac17-4-24-gio"
    assert machine_slug("Mac16,12 48 Gio") == "mac16-12-48-gio"


def test_le_slug_ignore_le_systeme_et_les_bibliotheques():
    """Une mise à jour de macOS ne doit pas créer un second relevé pour le même Mac.

    Sinon `measurements/<ref>/` accumulerait un fichier par version d'OS et par
    version de mlx, et plus personne ne saurait lequel le manifeste recopie.
    """
    complet = "Mac17,4 24 Gio / macOS 26.5.2 / mlx 0.32.1 / mlx-vlm 0.6.15"
    après_maj = "Mac17,4 24 Gio / macOS 26.6.0 / mlx 0.33.0 / mlx-vlm 0.7.0"

    assert machine_slug(hardware_of(complet)) == machine_slug(hardware_of(après_maj))


def test_une_identite_vide_ne_donne_pas_un_nom_de_fichier_vide():
    """`measurements/<ref>/.json` serait un fichier caché, et personne ne le verrait."""
    assert machine_slug("///") == MACHINE_INCONNUE
    assert hardware_of("") == MACHINE_INCONNUE


def test_hardware_of_retient_le_premier_segment():
    assert hardware_of("Mac17,4 24 Gio / macOS 26.5.2 / mlx 0.32.1") == "Mac17,4 24 Gio"
    assert hardware_of("Mac17,4 24 Gio") == "Mac17,4 24 Gio"


def test_l_identite_annoncee_commence_par_le_materiel():
    """`describe_machine` est ce qui s'inscrit dans `measured_on` ; `hardware_of`
    doit savoir en ressortir la partie qui nomme le fichier."""
    complet = describe_machine({"mlx": "0.32.1"})

    assert complet.startswith(machine_id())
    assert hardware_of(complet) == machine_id()
    assert complet.endswith("mlx 0.32.1")
