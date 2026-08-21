/**
 * Trois formats de date dans la même surface d'API, trois fonctions — plus une du navigateur.
 *
 * Ce n'est pas un désordre à corriger côté front : chaque format vient d'un
 * usage. `loaded_at` est un instant ISO écrit par le superviseur, `last_used` un
 * flottant POSIX comparable sans analyse, `measured_at` un jour sans heure parce
 * qu'une mesure de profil date d'un jour, pas d'une seconde.
 *
 * Un `new Date(x)` générique les avalerait tous les trois et se tromperait sur
 * le deuxième — `new Date(1755712345.6)` donne 1970.
 *
 * La quatrième ne vient pas de l'API : c'est l'horloge du navigateur, celle qui
 * date le dernier sondage réussi du bandeau. Elle rend l'heure seule, parce que
 * des chiffres de mémoire qui datent de plus d'une journée n'existent pas — la
 * page aurait été rechargée.
 */

const FR = "fr-CA";

/** `2026-08-20T12:00:00+00:00` → date et heure locales. */
export function isoUtc(valeur: string | null | undefined): string {
  if (!valeur) return "—";
  const d = new Date(valeur);
  return Number.isNaN(d.getTime()) ? valeur : d.toLocaleString(FR);
}

/** Un flottant POSIX en secondes → date et heure locales. */
export function posix(valeur: number | null | undefined): string {
  if (valeur === null || valeur === undefined || !Number.isFinite(valeur)) return "—";
  return new Date(valeur * 1000).toLocaleString(FR);
}

/** Un instant du navigateur, en millisecondes → l'heure locale, sans le jour. */
export function heureLocale(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  return new Date(ms).toLocaleTimeString(FR);
}

/** `2026-08-20` → le jour, sans heure inventée. */
export function jourMesure(valeur: string | null | undefined): string {
  if (!valeur) return "—";
  const [a, m, j] = valeur.split("-");
  return a && m && j ? `${j}/${m}/${a}` : valeur;
}
