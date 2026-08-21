/**
 * Ce qu'on tire du registre des résidents sans afficher d'écran.
 *
 * Deux fonctions pures, testées, que la tâche 4.4 branchera sur son bandeau.
 */

import type { Resident, StaleWorker } from "./types";

/**
 * Les options annoncées par un worker au chargement — voix, langues, versions.
 *
 * C'est la **seule** source des `x-options-from` du contrat de capacité.
 * `CONCEPTION.md` promet un endpoint `/runtime/residents/{ref}/options` qui
 * n'existe pas et n'a jamais existé ; ce qui existe est le champ `options` de
 * chaque résident de `GET /runtime/residents`. Conséquence directe, et qu'il
 * faut assumer plutôt que masquer : **tant qu'un modèle n'a pas été chargé une
 * fois, on ne connaît pas ses voix.** Un champ qui se bloquerait faute de les
 * connaître serait inutilisable au premier lancement, qui est justement le
 * moment où l'on veut essayer.
 */
export function optionsFor(
  residents: readonly Resident[],
  ref: string | null | undefined,
): Record<string, unknown> {
  if (!ref) return {};
  return residents.find((r) => r.ref === ref)?.options ?? {};
}

/**
 * Les workers que le budget ne compte plus mais que la machine paie encore.
 *
 * Une entrée périmée dont le processus vit toujours retient sa mémoire sans
 * plus figurer dans `used_bytes` : le budget la croit libre, Metal non. Le
 * bandeau du 4.4 ne pourra donc pas annoncer cette place comme disponible.
 *
 * On rend les entrées, pas une somme d'octets, et c'est une limite du serveur
 * plutôt qu'un choix : `StaleWorkerOut` ne transporte que `ref`, `pid`,
 * `holds_memory` et `socket` — le pic du worker disparu n'y figure pas. Chiffrer
 * la mémoire retenue demanderait de retrouver le profil du variant par son
 * `ref`, ce qui n'est vrai que si le manifeste n'a pas changé depuis le
 * chargement. Compter les fantômes est exact ; les chiffrer serait une
 * estimation, et ce projet n'en écrit pas.
 */
export function workersHorsBudget(stale: readonly StaleWorker[]): readonly StaleWorker[] {
  return stale.filter((e) => e.holds_memory);
}
