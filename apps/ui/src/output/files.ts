/**
 * Du chemin qu'un worker écrit vers une URL que le navigateur sait charger.
 *
 * Toute sortie portant un `contentMediaType` est un **chemin relatif au dossier
 * du job**, jamais un contenu. Pour l'afficher, il faut une URL — et la route
 * qui la sert, `GET /jobs/{id}/files/{name}`, n'existe pas encore : elle attend
 * le déménagement du superviseur dans le processus de l'API (tâche 4.6).
 *
 * Plutôt que d'écrire des visualiseurs qu'il faudrait rouvrir ensuite, on pose
 * dès maintenant le point d'injection. `NO_FILE` est la valeur du 4.3 ; le 4.4
 * remplacera **une fonction**, et les huit visualiseurs sauront déjà rendre
 * `href: null` parce qu'un test le leur impose un par un.
 */

export type FileResolver = (chemin: string) => string | null;

/** La valeur de la tâche 4.3 : aucune route ne sert les fichiers de sortie. */
export const NO_FILE: FileResolver = () => null;
