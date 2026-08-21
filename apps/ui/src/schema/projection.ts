/**
 * Du `formData` de RJSF vers l'entrée que le contrat accepte.
 *
 * Le serveur refuse **un paramètre qu'il ne reconnaît ni au contrat ni au
 * manifeste**, y compris en mode non strict — « ce n'est pas une saisie
 * incomplète, c'est un client qui parle d'autre chose que la capacité qu'il a
 * nommée ». Un champ de travail qui traînerait dans `formData` donnerait donc un
 * 422 à chaque frappe, et le bandeau de coût s'éteindrait au moment précis où on
 * le regarde. La projection est ce qui garantit que cela n'arrive pas.
 *
 * Le serveur laisse une seconde porte, que le front n'emprunte pas : une clé
 * déclarée dans les `options:` du manifeste est acceptée et part dans le sac
 * `params`, à côté de l'entrée. C'est un réglage propre au moteur — une seule
 * clé du parc entier est concernée —, il n'a aucun schéma, donc rien à rendre
 * dans un formulaire engendré. Ne garder que les propriétés du contrat est donc
 * plus strict que le serveur, à dessein.
 *
 * Le sort réservé aux valeurs vides mérite d'être écrit, parce qu'il n'est pas
 * symétrique : `undefined`, `null` et la chaîne vide sont **jetés**, `0` et
 * `false` sont **gardés**. RJSF émet `""` pour un champ texte qu'on a vidé, et
 * l'envoyer échouerait sur le `minLength: 1` d'un `text` que l'utilisateur n'a
 * pourtant pas encore rempli — alors qu'un `0` sur `temperature` et un `false`
 * sur `parallel_calls` sont des choix, et des choix qui changent la sortie.
 */

import type { Capability } from "../api/types";

export function toContractInput(
  formData: Record<string, unknown> | undefined,
  capability: Pick<Capability, "input">,
): Record<string, unknown> {
  const propriétés = (capability.input as { properties?: Record<string, unknown> }).properties ?? {};
  const entrée: Record<string, unknown> = {};

  for (const [nom, valeur] of Object.entries(formData ?? {})) {
    if (!(nom in propriétés)) continue;
    if (valeur === undefined || valeur === null || valeur === "") continue;
    entrée[nom] = valeur;
  }
  return entrée;
}
