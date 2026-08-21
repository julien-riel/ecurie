/**
 * L'état d'une capacité, en trois cas et non deux.
 *
 * Le parc réel impose la nuance : sur dix-sept contrats, six n'ont aucun modèle
 * et un en a sans qu'aucun variant soit exécutable. `image-to-mesh` est le cas exemplaire —
 * il affiche un **titulaire** et n'a rien de lançable, parce que ses 7,37 Go de
 * poids ne sont pas téléchargés. Réduire cela à « prête / pas prête » afficherait
 * la même phrase pour une capacité qu'on n'a jamais pourvue et pour une capacité
 * dont il ne manque qu'un `ecurie pull`, alors que la seconde est à une commande
 * de marcher.
 */

import type { Capability } from "../api/types";

export type EtatCapacite = "prête" | "sans-modèle" | "sans-variant-prêt";

export function etatCapacite(
  capability: Pick<Capability, "models" | "ready_variants">,
): EtatCapacite {
  if (capability.ready_variants.length > 0) return "prête";
  if (capability.models.length === 0) return "sans-modèle";
  return "sans-variant-prêt";
}

/** La phrase qui va avec l'état — les blockers du variant disent le reste. */
export function phraseEtat(etat: EtatCapacite): string {
  switch (etat) {
    case "prête":
      return "exécutable";
    case "sans-modèle":
      return "aucun modèle au registre pour cette capacité";
    case "sans-variant-prêt":
      return "des modèles déclarés, aucun variant exécutable en l'état";
    default:
      return "état inconnu";
  }
}
