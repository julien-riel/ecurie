/**
 * D'une sortie du contrat vers une URL que le navigateur sait charger.
 *
 * Le point d'injection est resté `NO_FILE` pendant tout le 4.4, et la raison
 * écrite alors était qu'on ne pouvait pas l'écrire *juste* : la forme de la
 * route dépendait d'un choix que la route des jobs n'avait pas encore fait — un
 * nom de fichier, ou un chemin à plusieurs segments ? Les deux existent,
 * `audio-separation` produisant `tracks/vocals.wav` sous la clé pointée
 * `tracks.vocals`.
 *
 * Le choix a été fait, et il retire au front le travail plutôt que de le lui
 * donner : **le serveur compose l'URL**, la réponse du job porte `files` déjà
 * fait, indexé par les clés du contrat. Ce résolveur est donc une *lecture* —
 * une recherche dans une table —, et non la construction de chemin qu'on
 * craignait d'écrire deux fois.
 *
 * Ce qu'on lui passe est le **chemin pointé** de la sortie (`tracks.vocals`),
 * qui est à la fois la clé d'`output_media_types`, celle de `files`, et
 * l'identité que `flattenOutput` donne à chaque sortie. Lui passer la valeur —
 * `tracks/vocals.wav` — obligerait à inverser `outputs` pour retrouver la clé,
 * c'est-à-dire à refaire à l'envers ce que le serveur vient de donner à
 * l'endroit.
 */

import { fichierDuJob } from "../api/endpoints";

/** Chemin pointé d'une sortie → URL du fichier, ou `null` s'il n'y en a pas. */
export type FileResolver = (chemin: string) => string | null;

/** Aucune URL : l'état d'un panneau qui montre une sortie sans job derrière. */
export const NO_FILE: FileResolver = () => null;

/**
 * Le résolveur d'un job, depuis le `files` de sa réponse.
 *
 * Une clé absente rend `null` plutôt que de composer une URL vraisemblable : une
 * sortie facultative que le worker n'a pas produite n'a pas de fichier, et
 * fabriquer son adresse ne ferait que déplacer le 404 dans une balise `<img>`,
 * où plus personne ne le lit.
 */
export function resolveurDeJob(files: Record<string, string>): FileResolver {
  return (chemin) => {
    const url = files[chemin];
    return url ? fichierDuJob(url) : null;
  };
}
