"""Deux domaines, deux unités — et la raison de ne pas les confondre.

`ecurie_store.figures.fmt_bytes` compte le disque en unités **décimales** (Go,
Mo), parce que c'est ce que le fabricant écrit sur le boîtier et ce que le
Finder affiche : annoncer « 1,82 Gio récupérables » là où le disque en rendra
« 1,95 Go » ferait douter du chiffre.

La mémoire n'a pas cette convention. Le budget se lit en **Gio**, le seuil de
lourdeur vaut `8 * (1 << 30)`, les profils mesurés sont des puissances de deux,
et l'UI les affiche déjà ainsi. Un `fmt_bytes` décimal rendrait un seuil de 8 Gio
sous la forme « 8.59 Go » — un chiffre que personne n'a écrit nulle part et qui
ne se retrouve dans aucun fichier de configuration.

D'où ce second formateur, réservé à la mémoire unifiée. Il vit dans `core` parce
que `runtime` et `api` en ont besoin tous les deux, et que `core` est ce dont
tout le monde dépend déjà.
"""

_UNITÉS = ("o", "Kio", "Mio", "Gio", "Tio")


def fmt_memory(n: int | float | None) -> str:
    """Des octets de mémoire unifiée vers une phrase, en unités binaires.

    `None` n'est jamais « 0 o » : un pic non mesuré et un pic nul ne disent pas
    la même chose — le premier fait refuser l'admission par principe, le second
    n'existe pas. C'est la même règle que `formatBytes` côté UI, et elle doit
    l'être : les deux rendent les mêmes grandeurs.
    """
    if n is None:
        return "pic inconnu"

    valeur = float(abs(n))
    rang = 0
    while valeur >= 1024 and rang < len(_UNITÉS) - 1:
        valeur /= 1024
        rang += 1

    if rang == 0:
        chiffre = f"{int(valeur)}"
    elif valeur >= 100:
        chiffre = f"{valeur:.0f}"
    else:
        chiffre = f"{valeur:.2f}".rstrip("0").rstrip(".")
    return f"{'-' if n < 0 else ''}{chiffre} {_UNITÉS[rang]}"
