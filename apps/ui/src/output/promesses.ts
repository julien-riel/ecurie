/**
 * Ce qu'une capacité **promet** de produire — lu sur le contrat, pas sur une réponse.
 *
 * C'est l'exact opposé de `flattenOutput`, et la symétrie est voulue. Ce
 * dernier parcourt la **réponse** d'un job, parce qu'`audio-separation` déclare
 * cinq pistes et n'en produit que deux ou quatre selon `stems` : montrer le
 * contrat afficherait des lecteurs pour des fichiers absents. Ici il n'y a pas
 * encore de réponse — l'écran annonce ce qu'un job produirait **avant** qu'on le
 * lance —, et la question n'est plus « qu'a-t-on obtenu » mais « qu'obtiendrait
 * -on ». Le contrat est alors la seule source possible, et la bonne. Dès qu'un
 * job a tourné, `OutputPanel` prend cette place et le titre change avec lui.
 *
 * Les sorties qui ne sont pas des fichiers y figurent aussi. `page_count`,
 * `detected_source_language`, `finish_reason` : aucune n'a de type de média,
 * aucune n'aura de visualiseur, et toutes disent quelque chose du job. Les
 * omettre laisserait croire qu'un OCR ne rend qu'un fichier de texte.
 */

import type { Capability } from "../api/types";

export interface Promesse {
  /** Chemin pointé, même clé que dans `output_media_types`. */
  chemin: string;
  /** Dernière composante du chemin. */
  nom: string;
  description: string | null;
  /** `null` quand la sortie n'est pas un fichier. */
  mediaType: string | null;
  /** Requis à son niveau : le contrat garantit qu'elle sera là. */
  requis: boolean;
}

interface NoeudSchema {
  properties?: Record<string, NoeudSchema>;
  required?: string[];
  description?: string;
}

export function promessesDeSortie(
  capability: Pick<Capability, "output" | "output_media_types">,
): Promesse[] {
  const mediaTypes = capability.output_media_types;
  const promesses: Promesse[] = [];

  function descendre(noeud: NoeudSchema, prefixe: string): void {
    const requis = new Set(noeud.required ?? []);
    for (const [clé, sous] of Object.entries(noeud.properties ?? {})) {
      const chemin = prefixe ? `${prefixe}.${clé}` : clé;
      const mediaType = mediaTypes[chemin] ?? null;

      // Un type de média arrête la descente, comme dans l'aplatissement d'une
      // réponse : la valeur est un chemin de fichier, pas un sous-objet.
      if (mediaType === null && sous.properties) {
        descendre(sous, chemin);
        continue;
      }
      promesses.push({
        chemin,
        nom: clé,
        description: sous.description ?? null,
        mediaType,
        requis: requis.has(clé),
      });
    }
  }

  descendre(capability.output as NoeudSchema, "");
  return promesses;
}
