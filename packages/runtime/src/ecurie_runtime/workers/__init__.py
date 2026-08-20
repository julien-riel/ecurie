"""Workers — le code qui tourne *dans* les venv isolés des runtimes.

Tout ce qui vit ici est exécuté par l'interpréteur de `runtimes/<env>/.venv`, pas
par celui d'Écurie. Deux règles en découlent, et elles ne se négocient pas :

1. hors de la bibliothèque standard, un worker n'importe que la bibliothèque du
   runtime qu'il adapte, et jamais au chargement du module — toujours dans
   `load()`, pour qu'un env mal synchronisé échoue avec un message utile plutôt
   qu'à l'import ;
2. rien d'Écurie n'est importé sauf `ecurie_runtime.protocol`, `.channel` et
   `workers.base`, qui sont en bibliothèque standard pure. Le superviseur rend
   ces modules visibles par `PYTHONPATH`, sans installer quoi que ce soit dans le
   venv du runtime.
"""
