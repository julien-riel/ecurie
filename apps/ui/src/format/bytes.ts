/**
 * Des octets vers une phrase, et l'inconnu qui reste inconnu.
 *
 * `null` ne devient jamais « 0 o ». Un pic non mesuré et un pic nul ne disent
 * pas la même chose : le premier interdit l'exécution — « l'admission refuse par
 * principe » —, le second n'existe pas. Le champ `heavy` d'un variant a la même
 * propriété tri-état, pour la même raison.
 *
 * L'unité est binaire (Gio, Mio) parce que c'est celle du budget mémoire, du
 * seuil de lourdeur et de tout ce que la CLI affiche.
 */

const UNITES = ["o", "Kio", "Mio", "Gio", "Tio"] as const;

export function formatBytes(octets: number | null | undefined): string {
  if (octets === null || octets === undefined) return "pic inconnu";
  if (!Number.isFinite(octets)) return "pic inconnu";
  if (octets === 0) return "0 o";

  let valeur = Math.abs(octets);
  let rang = 0;
  while (valeur >= 1024 && rang < UNITES.length - 1) {
    valeur /= 1024;
    rang += 1;
  }
  const arrondi = valeur >= 100 || rang === 0 ? Math.round(valeur) : Number(valeur.toFixed(2));
  return `${octets < 0 ? "-" : ""}${arrondi.toLocaleString("fr-CA")} ${UNITES[rang]}`;
}

/** L'état de lourdeur d'un variant, en trois cas. `null` n'est pas « léger ». */
export function phraseLourdeur(heavy: boolean | null | undefined): string {
  if (heavy === null || heavy === undefined) return "lourdeur inconnue, aucun profil mesuré";
  return heavy ? "modèle lourd" : "modèle léger";
}
