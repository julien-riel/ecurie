"""Écurie — API HTTP : registre, parc disque, résidents, admission.

Le v0.4 livre d'abord les lectures. Elles n'engagent rien — aucun modèle n'est
chargé, aucun octet n'est déplacé, aucun manifeste n'est écrit — et elles
suffisent à faire vivre l'Atelier : un contrat de capacité rend un formulaire, un
manifeste rend une liste de variants, les trois chiffres rendent l'écran Parc, et
les résidents rendent le bandeau de ressources. Les jobs et leur flux SSE
viennent après le déménagement du superviseur dans ce processus (tâche 4.6).

Comme `ecurie_runtime`, ce module ne ré-exporte rien : `create_app` s'importe
depuis `ecurie_api.app`. Importer l'application ici ferait dépendre la lecture du
numéro de version du chargement de FastAPI et de tout le reste du dépôt.
"""

__version__ = "0.4.0"
