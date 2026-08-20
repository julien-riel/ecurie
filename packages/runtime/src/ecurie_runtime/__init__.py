"""Écurie — exécution : protocole worker, environnements isolés, admission mémoire.

Ce module ne ré-exporte rien et n'importe rien au chargement, contrairement aux
autres paquets du dépôt. Ce n'est pas un oubli : `ecurie_runtime.workers.*` est
importé **dans les venv isolés des runtimes** (CONCEPTION.md §5.3), qui ne
connaissent ni pydantic, ni ecurie-core, ni typer. Charger ce paquet ne doit donc
jamais rien coûter d'autre que la bibliothèque standard — sans quoi le worker
échouerait à l'import, dans l'environnement précis où l'isolation a le plus de
valeur.

Le reste du paquet (superviseur, CLI, banc d'essai) tourne dans l'env racine et
s'importe explicitement, module par module.
"""

__version__ = "0.3.0"
