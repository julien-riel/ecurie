/**
 * Ce que l'écran Parc dit du disque — la logique, séparée de son affichage.
 *
 * Le calcul, lui, n'est pas ici et n'y sera jamais : il vit dans
 * `ecurie_store.figures` et `ecurie_store.plan`, et c'est délibéré. Refaire
 * l'addition des quatre postes dans le front rendrait possible qu'un écran
 * annonce un gain que `ecurie store status` n'annonce pas — le genre d'écart
 * qu'on ne remarque qu'en comptant à la main. Ce module ne fait que trois
 * choses : ordonner, nommer, et distinguer l'inconnu du zéro.
 *
 * Cette dernière est la règle qui gouverne tout le reste. Le poste « variants
 * jamais utilisés » n'est pas nul tant que la télémétrie n'observe pas depuis
 * assez longtemps : il est **indéterminé**, et l'afficher « 0 o » annoncerait
 * qu'il n'y a rien à y gagner. Un `Poste` porte donc `octets: number | null`, et
 * `null` traverse jusqu'à la phrase affichée.
 */

import type { Plan, Recoverable, Telemetry, TierVolume } from "../api/types";
import { formatOctetsDisque } from "../format/bytes";

export interface Poste {
  clef: string;
  titre: string;
  /** `null` = indéterminé. Ce n'est jamais zéro, et jamais un défaut de calcul. */
  octets: number | null;
  /** Pourquoi il est indéterminé, quand il l'est. */
  note?: string;
}

/**
 * Les quatre postes du récupérable, dans l'ordre du rapport de la CLI.
 *
 * Les trois premiers sont toujours des chiffres — ils se déduisent du seul état
 * observé. Le quatrième demande un journal d'exécutions, et c'est le seul qui
 * peut ne pas répondre.
 */
export function postesRecuperables(
  recoverable: Recoverable,
  telemetry: Telemetry | null | undefined,
): Poste[] {
  return [
    {
      clef: "duplication",
      titre: "duplication inter-gestionnaires",
      octets: recoverable.duplication_bytes,
    },
    { clef: "hf_stale", titre: "révisions HF obsolètes", octets: recoverable.hf_stale_bytes },
    { clef: "orphan", titre: "blobs orphelins", octets: recoverable.orphan_bytes },
    {
      clef: "unused",
      titre: "variants jamais utilisés",
      octets: recoverable.unused_known ? recoverable.unused_bytes : null,
      note: recoverable.unused_known ? undefined : phraseTelemetrie(telemetry),
    },
  ];
}

/**
 * Pourquoi le poste « jamais utilisés » ne répond pas — miroir de la CLI.
 *
 * Les deux cas appellent deux gestes différents : sans aucune exécution notée,
 * il n'y a rien à attendre ; avec un journal trop jeune, il suffit d'attendre.
 * Un « inconnu » unique les confondrait.
 */
export function phraseTelemetrie(telemetry: Telemetry | null | undefined): string {
  if (!telemetry || !telemetry.first_run_at) return "aucune exécution notée";
  return (
    `télémétrie trop jeune : elle observe depuis le ${telemetry.first_run_at.slice(0, 10)}, ` +
    `et le seuil est de ${telemetry.unused_after_days} jours`
  );
}

/**
 * Le libellé français d'un motif d'action, avec repli sur la clé brute.
 *
 * La table vient du serveur — c'est `REASON_LABELS` de la CLI, qui voyage avec
 * le plan — précisément pour qu'un poste ajouté demain arrive avec son nom sans
 * qu'une ligne de front bouge. Le repli est ce qui rend cet aiguillage **total**,
 * comme les deux autres tables du front : un motif inconnu s'affiche sous sa
 * clé, il ne disparaît pas et ne fait rien lever.
 */
export function libelleMotif(motif: string, labels: Record<string, string> = {}): string {
  return labels[motif] ?? motif;
}

export interface GainParPoste {
  motif: string;
  titre: string;
  actions: number;
  octets: number;
}

/**
 * Le gain par poste, du plus gros au plus petit — la table de `ecurie store plan`.
 *
 * Le nombre d'actions se compte ici parce que le plan ne le porte pas :
 * `by_reason` donne les octets, les actions sont dans la liste. Une action à
 * zéro octet existe et compte — un instantané HF détaché ne libère rien mais
 * évite de laisser un champ de liens cassés —, si bien qu'un poste peut
 * apparaître avec un gain nul et un nombre d'actions non nul.
 */
export function gainParPoste(plan: Plan, labels: Record<string, string> = {}): GainParPoste[] {
  const compte = new Map<string, number>();
  for (const action of plan.actions) {
    compte.set(action.reason, (compte.get(action.reason) ?? 0) + 1);
  }
  const motifs = new Set([...Object.keys(plan.by_reason), ...compte.keys()]);
  return [...motifs]
    .map((motif) => ({
      motif,
      titre: libelleMotif(motif, labels),
      actions: compte.get(motif) ?? 0,
      octets: plan.by_reason[motif] ?? 0,
    }))
    .sort((a, b) => b.octets - a.octets || a.motif.localeCompare(b.motif));
}

/** Les chemins qu'une action du plan touche, quelle que soit sa forme. */
export function cheminsDeLAction(action: Plan["actions"][number]): string[] {
  if (action.kind === "hardlink") return [action.keep ?? "", ...(action.replace ?? [])];
  return action.path ? [action.path] : [];
}

/**
 * L'état d'un volume de tiering, en une phrase.
 *
 * Un volume démonté n'a pas une place libre inconnue **par accident** : c'est
 * l'information utile, celle qui explique qu'un variant froid soit
 * indisponible. Dire « 0 o libre » en ferait un disque plein, et proposerait de
 * déporter ailleurs un variant qui est déjà là.
 */
export function phraseVolume(volume: TierVolume): string {
  if (!volume.mounted) return "démonté — ce qui y est déporté est indisponible";
  if (volume.free_bytes === null || volume.free_bytes === undefined) {
    return "monté, place libre inconnue";
  }
  const libre = formatOctetsDisque(volume.free_bytes);
  return volume.total_bytes
    ? `monté — ${libre} libres sur ${formatOctetsDisque(volume.total_bytes)}`
    : `monté — ${libre} libres`;
}
