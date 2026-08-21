/**
 * Les valeurs qu'un formulaire montre avant qu'on y touche.
 *
 * Recopie de `merge_defaults` du serveur (`ecurie_runtime/runner.py`), et la
 * recopie est assumée : le serveur ne rend cette fusion nulle part avant qu'on
 * lui pose une question d'admission, or le formulaire doit s'afficher rempli au
 * premier rendu, sans aller-retour. Un test compare les deux sur les treize
 * variants du parc — c'est lui qui rend la duplication tenable.
 *
 * L'ordre est celui du serveur, et il n'est pas arbitraire : **le contrat pose la
 * valeur générique, le variant la corrige.** `qwen25-coder` déclare
 * `temperature: 0.2` là où `text-generation` propose 0.7, parce qu'un modèle de
 * code n'échantillonne pas comme un modèle de prose. Montrer 0.7 afficherait une
 * valeur qui ne partira pas.
 *
 * Deux règles négatives, aussi importantes que la fusion :
 *
 * **Un défaut du variant hors contrat est ignoré**, comme côté serveur. Ce que le
 * manifeste appelle `options` est un réglage propre au moteur, transporté à part
 * dans `params` ; l'injecter dans le formulaire ferait partir un paramètre que le
 * contrat ne déclare pas, et le serveur le refuserait — même en mode non strict.
 *
 * **On n'invente aucune valeur.** Un champ borné sans `default` reste absent :
 * `image-matting.threshold` à 0 binariserait le masque, et les onze `seed` du
 * parc à 0 figeraient l'aléa sans que personne l'ait demandé.
 */

import type { Capability, Variant } from "../api/types";

export function initialValues(
  capability: Pick<Capability, "defaults" | "input">,
  variant: Pick<Variant, "defaults"> | null,
): Record<string, unknown> {
  const valeurs: Record<string, unknown> = { ...(capability.defaults ?? {}) };
  const propriétés = (capability.input as { properties?: Record<string, unknown> }).properties ?? {};

  for (const [nom, valeur] of Object.entries(variant?.defaults ?? {})) {
    if (nom in propriétés) valeurs[nom] = valeur;
  }
  return valeurs;
}
