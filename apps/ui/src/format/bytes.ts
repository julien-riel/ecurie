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

/**
 * Le disque se compte en Go décimaux, la mémoire en Gio binaires.
 *
 * Deux fonctions plutôt qu'une, et la divergence est volontaire des deux côtés.
 * `ecurie store status` affiche « 4.90 Go » là où `ecurie ps` affiche
 * « 7,65 Gio », parce que ce ne sont pas les mêmes octets : un disque s'annonce,
 * se vend et s'affiche dans le Finder en puissances de dix, une mémoire unifiée
 * et un budget Metal en puissances de deux. Un écran Parc qui dirait « 4,56 Gio »
 * de ce que la CLI appelle 4,90 Go ferait douter du chiffre plutôt que de
 * l'unité — et la tâche 4.5 demande la **parité avec la CLI**.
 *
 * Le miroir est celui de `ecurie_store.figures.fmt_bytes`, à la virgule
 * décimale près, qui est française ici et anglaise là-bas.
 *
 * `null` reste inconnu, comme partout ailleurs : le poste « jamais utilisés »
 * du plan de récupération est indéterminé tant que la télémétrie est trop jeune,
 * et l'afficher « 0 o » annoncerait qu'il n'y a rien à y gagner.
 */
export function formatOctetsDisque(octets: number | null | undefined): string {
  if (octets === null || octets === undefined || !Number.isFinite(octets)) return "inconnu";
  const signe = octets < 0 ? "-" : "";
  const valeur = Math.abs(octets);
  if (valeur >= 1e9) return `${signe}${(valeur / 1e9).toLocaleString("fr-CA", DEUX)} Go`;
  if (valeur >= 1e6) return `${signe}${(valeur / 1e6).toLocaleString("fr-CA", UNE)} Mo`;
  if (valeur >= 1e3) return `${signe}${Math.round(valeur / 1e3).toLocaleString("fr-CA")} ko`;
  return `${signe}${valeur.toLocaleString("fr-CA")} o`;
}

const DEUX = { minimumFractionDigits: 2, maximumFractionDigits: 2 } as const;
const UNE = { minimumFractionDigits: 1, maximumFractionDigits: 1 } as const;
