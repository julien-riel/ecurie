"""`python -m ecurie_mcp` — le vrai montage stdio, celui que les tests éprouvent.

Il existe parce qu'un serveur MCP se teste à deux profondeurs, et que la seconde
est la seule qui prouve quelque chose sur la livraison : en processus, où le
client du SDK parle directement à l'objet `Server` — rapide, et suffisant pour la
logique —, et en sous-processus, par stdin et stdout, qui est le seul montage qui
échoue si quoi que ce soit dans l'arbre d'imports écrit une ligne sur la sortie
standard.
"""

from ecurie_mcp.cli import main

main()
