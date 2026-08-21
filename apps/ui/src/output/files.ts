/**
 * Du chemin qu'un worker écrit vers une URL que le navigateur sait charger.
 *
 * Toute sortie portant un `contentMediaType` est un **chemin relatif au dossier
 * du job**, jamais un contenu. Pour l'afficher, il faut une URL — et la route
 * qui la sert, `GET /jobs/{id}/files/{name}`, n'existe pas : elle attend le
 * déménagement du superviseur dans le processus de l'API (tâche 4.6), en même
 * temps que le `POST /jobs` qui produirait le job dont on afficherait la sortie.
 *
 * Le point d'injection est posé, `NO_FILE` reste sa valeur, et **le 4.4 n'écrit
 * pas le résolveur réel** malgré ce que le plan annonçait. La raison n'est pas
 * l'effort — c'est une ligne — mais qu'on ne peut pas l'écrire juste : la forme
 * exacte de la route est une décision du 4.6, qui a un vrai choix à faire — un
 * nom de fichier ou un chemin à plusieurs segments, `audio-separation` produisant
 * des sorties imbriquées. Une fonction écrite avant cette décision serait
 * réécrite après, et entre-temps aucun test n'aurait pu dire laquelle des deux
 * formes est la bonne. C'est la leçon du v0.3 appliquée au front : un chemin de
 * code que rien n'exécute est un chemin de code faux.
 *
 * Ce que le 4.4 met à la place : `SortiesPromises`, qui annonce ce que le
 * contrat produirait, sans prétendre l'avoir produit.
 */

export type FileResolver = (chemin: string) => string | null;

/** Aucune route ne sert les fichiers de sortie avant la tâche 4.6. */
export const NO_FILE: FileResolver = () => null;
