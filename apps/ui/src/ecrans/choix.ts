/**
 * Les deux choix de l'Atelier — capacité, puis variant — sans rendre un écran.
 *
 * Le banc du 4.3 posait les dix-sept capacités dans un `<select>` plat et
 * laissait le variant vide : c'était assez pour prouver que le formulaire
 * s'engendre, pas pour travailler. Sur le parc réel, sept capacités sur
 * dix-sept n'ont aucun modèle et une n'a aucun variant exécutable ; les mêler
 * aux dix qui marchent oblige à lire dix-sept lignes pour en trouver une.
 *
 * D'où deux fonctions, et une règle commune : **on ne cache jamais ce qui ne
 * marche pas.** Une capacité sans modèle reste dans la liste, dans son propre
 * groupe, parce qu'elle dit ce que le parc pourrait faire et ne fait pas
 * encore — c'est la moitié de ce qu'un registre sert à savoir.
 */

import type { Capability, Model, Variant } from "../api/types";
import { etatCapacite, phraseEtat, type EtatCapacite } from "../schema/etat";

export interface GroupeCapacites {
  etat: EtatCapacite;
  titre: string;
  capacites: readonly Capability[];
}

const ORDRE: readonly EtatCapacite[] = ["prête", "sans-variant-prêt", "sans-modèle"];

const TITRES: Record<EtatCapacite, string> = {
  "prête": "Exécutables",
  "sans-variant-prêt": "Déclarées, rien d'exécutable en l'état",
  "sans-modèle": "Aucun modèle au registre",
};

/**
 * Les capacités par état, les exécutables d'abord, les groupes vides omis.
 *
 * L'ordre à l'intérieur d'un groupe est celui du serveur, qui trie par
 * identifiant : le redéfinir ici ferait deux tris à maintenir pour un même
 * affichage.
 */
export function groupesDeCapacites(capacites: readonly Capability[]): GroupeCapacites[] {
  return ORDRE.map((etat) => ({
    etat,
    titre: TITRES[etat] ?? phraseEtat(etat),
    capacites: capacites.filter((c) => etatCapacite(c) === etat),
  })).filter((g) => g.capacites.length > 0);
}

export interface GroupeVariants {
  modele: string;
  /** Titulaire de la capacité : la référence de toute confrontation A/B. */
  titulaire: boolean;
  variants: readonly Variant[];
}

/** Les variants groupés par modèle, le titulaire en tête. */
export function groupesDeVariants(
  models: readonly Model[],
  incumbent: string | null | undefined,
): GroupeVariants[] {
  const groupes = models.map((m) => ({
    modele: m.id,
    titulaire: m.id === incumbent,
    variants: m.variants,
  }));
  return [...groupes].sort((a, b) => Number(b.titulaire) - Number(a.titulaire));
}

/**
 * Le variant que l'écran choisit tout seul à l'ouverture d'une capacité.
 *
 * Le titulaire d'abord — c'est la référence à laquelle tout le reste se
 * compare —, à défaut le premier exécutable. **Jamais un variant qui ne l'est
 * pas** : préselectionner `hunyuan3d-2.1-shape-mlx@mlx-bf16`, titulaire d'une
 * capacité dont les 7,37 Go de poids ne sont pas téléchargés, ouvrirait l'écran
 * sur un formulaire dont rien ne peut sortir.
 *
 * `ready_variants` vient du serveur et porte des `model@variant` complets ; la
 * partie gauche est l'id du modèle, ce qui suffit à reconnaître le titulaire
 * sans attendre `/registry/models`.
 */
export function variantParDefaut(
  capability: Pick<Capability, "incumbent" | "ready_variants">,
): string | null {
  const prêts = capability.ready_variants;
  if (prêts.length === 0) return null;
  const titulaire = capability.incumbent;
  const duTitulaire = titulaire
    ? prêts.find((ref) => ref.slice(0, ref.indexOf("@")) === titulaire)
    : undefined;
  return duTitulaire ?? prêts[0]!;
}
