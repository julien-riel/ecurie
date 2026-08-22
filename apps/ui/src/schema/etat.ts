/**
 * L'état d'une capacité, en trois cas et non deux.
 *
 * La nuance vient de ce qu'il ne suffit pas d'avoir un modèle pour pouvoir
 * lancer : `image-to-mesh` est le cas exemplaire — elle affiche un **titulaire**
 * et n'a rien de lançable, parce que ses 7,37 Go de poids ne sont pas
 * téléchargés. Réduire cela à « prête / pas prête » afficherait la même phrase
 * pour une capacité qu'on n'a jamais pourvue et pour une capacité dont il ne
 * manque qu'un `ecurie pull`, alors que la seconde est à une commande de marcher.
 *
 * `sans-modèle` ne décrit plus aucune capacité du parc : les vingt-cinq contrats
 * ont au moins un manifeste, et un test l'exige désormais. Le cas reste ici, et
 * ce n'est pas du code mort — un contrat s'ajoute avant son modèle, et c'est
 * même l'ordre normal du travail. Ce qui aurait été mort, c'est un état qu'on
 * aurait retiré au premier jour où le parc est complet, pour le réécrire au
 * premier contrat suivant.
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
