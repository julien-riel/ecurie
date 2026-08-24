"""Recettes des assets de charge type, une par famille de capacités.

`tools/golden_assets.py` sait fabriquer des images et du son à partir du
manifeste d'un golden set : une page, un solide, une scène, un mélange, un
portrait. Les capacités de mesure arrivées le 24 août 2026 ne prennent rien de
tout cela — une série de nombres, un nuage de points, une scène satellite à six
bandes, une séquence protéique — et leurs recettes n'avaient aucune raison
d'entrer dans un fichier qui en compte déjà cinq et pèse mille lignes.

Un module par famille, donc, et un point d'entrée commun (`tools/bench_assets.py`)
qui les découvre. Chaque module déclare :

    ENV = "chronos"          # l'environnement dont la recette a besoin, ou None
    CIBLES = ("serie-…csv",) # ce qu'il écrit, relatif à registry/evals/bench/assets/

    def produire(dossier: Path, *, force: bool) -> list[Path]: ...

**La règle qui compte est celle du banc d'essai, pas celle de ce fichier** : une
charge type est figée, et une recette qui réécrirait un asset existant détruirait
la comparabilité de toutes les mesures antérieures. `produire` ne remplace donc
jamais un fichier sans `force`, et la vérification est dans chaque recette plutôt
qu'ici — c'est la recette qui sait ce qu'elle écrit.

Ces recettes existent pour une raison qui a coûté cher : les six premières images
de `registry/evals/bench/assets/` ont été produites par une recette
« déterministe » jamais committée. Elles sont aujourd'hui des données orphelines,
qu'on ne sait plus refaire ni expliquer.
"""
