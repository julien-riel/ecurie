/**
 * Ce que le bandeau dit de la mémoire — des fonctions, pas un affichage.
 *
 * Tout ce qui demande une décision vit ici, testable sans rendre un composant :
 * ce qui occupe le budget, ce que « lancer » coûterait, et surtout ce dont il
 * faut se méfier dans les chiffres du serveur.
 *
 * Un point mérite d'être dit une fois pour toutes, parce qu'il traverse tout le
 * fichier : **`free_bytes` est optimiste**. Le serveur le calcule comme
 * `budget − somme des pics résidents`, et un worker devenu injoignable dont le
 * processus vit encore ne figure plus dans cette somme tout en retenant ses
 * gigaoctets. Le budget les croit libres, Metal non. On ne peut pas les
 * chiffrer — `StaleWorkerOut` ne transporte pas de pic —, mais on peut les
 * compter et le dire : annoncer « 9 Gio libres » sans mentionner les deux
 * fantômes ferait lancer un job qui swappe.
 */

import type { Admission, Resident, ResidentsResponse, StaleWorker } from "../api/types";
import { workersHorsBudget } from "../api/residents";
import { formatBytes } from "../format/bytes";

export interface SyntheseMemoire {
  budget: number;
  occupe: number;
  /** Tel que le serveur le calcule : `budget − occupé`. Peut être négatif. */
  libre: number;
  /** Part occupée, bornée à [0, 1] — une jauge ne déborde pas de son cadre. */
  part: number;
  /** Le budget vient de Metal, et non d'une règle de trois sur la RAM physique. */
  mesure: boolean;
  source: string;
  /** Workers vivants hors budget : c'est ce qui rend `libre` optimiste. */
  fantomes: readonly StaleWorker[];
}

export function synthese(réponse: ResidentsResponse | null): SyntheseMemoire | null {
  if (!réponse) return null;
  const budget = réponse.budget_bytes;
  const occupe = réponse.used_bytes;
  return {
    budget,
    occupe,
    libre: réponse.free_bytes,
    part: budget > 0 ? Math.min(1, Math.max(0, occupe / budget)) : 0,
    mesure: réponse.budget_measured,
    source: réponse.budget_source,
    fantomes: workersHorsBudget(réponse.stale),
  };
}

/** « 7,65 Gio occupés sur 17,76 — 10,11 libres ». */
export function phraseBudget(s: SyntheseMemoire): string {
  return `${formatBytes(s.occupe)} occupés sur ${formatBytes(s.budget)} — ${formatBytes(
    s.libre,
  )} libres`;
}

/**
 * L'avertissement des fantômes, quand il y en a — jamais un chiffre.
 *
 * Compter est exact, chiffrer serait une estimation : retrouver le pic d'un
 * worker disparu supposerait de relire le profil de son variant et que le
 * manifeste n'ait pas bougé depuis. Ce projet n'écrit pas d'estimation.
 */
export function phraseFantomes(s: SyntheseMemoire): string | null {
  if (s.fantomes.length === 0) return null;
  const refs = s.fantomes.map((f) => f.ref).join(", ");
  return (
    `${s.fantomes.length} worker(s) hors budget retiennent toujours leur mémoire (${refs}) : ` +
    `le libre annoncé est optimiste d'autant — ecurie unload --force`
  );
}

/** L'état d'un résident, dans les mots de `ecurie ps`. */
export function etatResident(r: Resident): string {
  if (r.busy) return `job en cours (pid ${r.busy_by})`;
  if (r.pinned) return "épinglé";
  return "libérable";
}

export type SeveriteAdmission = "ok" | "attention" | "refus";

export function severiteAdmission(a: Admission | null): SeveriteAdmission {
  if (!a) return "ok";
  if (!a.admitted) return "refus";
  if (a.evict.length > 0 || a.measure_mode || a.peak_note) return "attention";
  return "ok";
}

/**
 * Ce que « lancer » ferait, en une à trois lignes.
 *
 * On rend un tableau plutôt qu'une phrase parce que les cas se cumulent : un
 * pic extrapolé s'ajoute à une éviction, un mode mesure s'ajoute à tout le
 * reste. Les concaténer donnerait une phrase de trois lignes qu'on ne lit plus.
 *
 * `blockers` n'est **pas** repris : la décision les nomme déjà dans sa `reason`,
 * avec le motif qui dit le geste — « épinglé » se désépingle, « en cours de
 * job » s'attend. Les répéter nus perdrait le motif et doublerait la ligne.
 */
export function phrasesAdmission(a: Admission | null): readonly string[] {
  if (!a) return [];
  const lignes: string[] = [];

  if (a.already_resident) {
    lignes.push(`${a.ref} est déjà résident : lancer ne le rechargera pas.`);
  } else if (a.admitted) {
    const décharge = a.evict.length ? ` et déchargera ${a.evict.join(", ")}` : "";
    lignes.push(`lancer chargera ${formatBytes(a.peak_bytes)}${décharge} — ${a.reason}`);
  } else {
    lignes.push(`lancer refusé : ${a.reason}`);
  }

  if (a.measure_mode) {
    lignes.push(
      "aucun profil mesuré : le premier lancement videra le parc pour mesurer, épinglés compris.",
    );
  }
  if (a.peak_note) lignes.push(a.peak_note);
  return lignes;
}

/**
 * Le paramètre dont dépend le pic de ce variant, s'il y en a un.
 *
 * `GET /runtime/residents?for=` chiffre un **variant**, sans rien savoir de la
 * saisie ; `POST /runtime/admission` chiffre l'**entrée**. Tant que le second
 * n'alimente pas le bandeau — c'est la tâche 4.7 —, un variant à `peak_scaling`
 * y affiche un chiffre de base que trente secondes de musique feront doubler.
 * Le taire serait le pire des deux mondes : un chiffre faux qui a l'air sûr.
 */
export function parametreDuPic(profile: { peak_scaling?: { parameter: string } | null } | null): string | null {
  return profile?.peak_scaling?.parameter ?? null;
}
