/**
 * Comparer deux types de média sans se tromper sur les cas du parc.
 *
 * Trois pièges, tous présents dans le registre : la casse (`Image/PNG` est le
 * même type que `image/png`), les paramètres (`text/plain; charset=utf-8` est du
 * texte), et les familles (`image/*` couvre `image/png`, et c'est la forme
 * qu'emploient les **entrées** de `image-to-mesh` et `document-to-text`).
 */

/** Type de média sans paramètre ni casse : `text/plain; charset=utf-8` → `text/plain`. */
export function normalize(mediaType: string): string {
  const sans_parametre = mediaType.split(";")[0] ?? "";
  return sans_parametre.trim().toLowerCase();
}

/** `matches("audio/wav", "audio/*")` → vrai. Le motif peut être exact ou une famille. */
export function matches(mediaType: string, motif: string): boolean {
  const valeur = normalize(mediaType);
  const attendu = normalize(motif);
  if (attendu === valeur) return true;
  if (attendu.endsWith("/*")) {
    return valeur.startsWith(attendu.slice(0, -1));
  }
  return false;
}

/** La famille d'un type : `audio/wav` → `audio`. */
export function famille(mediaType: string): string {
  return normalize(mediaType).split("/")[0] ?? "";
}
