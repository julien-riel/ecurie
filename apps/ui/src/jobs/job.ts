/**
 * Ce qu'on dit d'un job — des fonctions, pas un affichage.
 *
 * Comme pour le bandeau de ressources, tout ce qui demande une décision vit ici,
 * testable sans rendre un composant. Trois points valent d'être dits, parce
 * qu'ils viennent de ce que le serveur transporte et non d'un choix de mise en
 * page.
 *
 * **Un échec de job n'est pas une erreur de requête.** Un refus d'admission
 * arrive en `state: "failed"` avec une phrase déjà écrite pour être lue — « ce
 * morceau de 30 s demanderait 24,2 Gio, au-delà des 17,8 disponibles ». On la
 * rend telle quelle : la paraphraser perdrait les deux chiffres qui comptent.
 *
 * **`reused` et `evicted` sont le sujet du projet.** « Le modèle était déjà
 * chaud » et « il a fallu décharger sdxl-base » sont exactement ce que
 * l'admission décide et ce qu'aucun autre outil ne dit. Les taire ferait de
 * l'écran un lanceur de commandes de plus.
 *
 * **La durée se calcule sur les deux instants du job, pas sur `metrics`.** Un
 * `duration_ms` existe au manifeste, mais le manifeste n'arrive qu'à la fin et
 * seulement par `GET /jobs/{id}` ; `started_at` et `finished_at` voyagent dans
 * chaque événement du flux.
 */

import type { Job, JobState } from "../api/types";
import { formatBytes } from "../format/bytes";

/** Le libellé court de chaque état. Le serveur en a quatre, tous nommés ici. */
export const LIBELLE_ETAT: Record<JobState, string> = {
  queued: "en file",
  running: "en cours",
  done: "terminé",
  failed: "échec",
};

/** Vrai tant que le job n'a pas fini — dans un sens ou dans l'autre. */
export function enCours(job: Job | null): boolean {
  return job !== null && (job.state === "queued" || job.state === "running");
}

/**
 * La durée d'exécution en secondes, ou `null` si le job n'a pas encore fini.
 *
 * Bornée à `started_at`, non à `submitted_at` : l'attente du tour de rôle n'est
 * pas du temps de calcul, et la compter ferait passer pour lent un job qui a
 * simplement attendu qu'un autre libère le worker.
 */
export function dureeSecondes(job: Job): number | null {
  if (!job.started_at || !job.finished_at) return null;
  const début = Date.parse(job.started_at);
  const fin = Date.parse(job.finished_at);
  if (Number.isNaN(début) || Number.isNaN(fin)) return null;
  return Math.max(0, (fin - début) / 1000);
}

/** « 3,4 s », « 1 min 12 s ». */
export function formatDuree(secondes: number): string {
  if (secondes < 60) return `${Number(secondes.toFixed(1)).toLocaleString("fr-CA")} s`;
  const minutes = Math.floor(secondes / 60);
  return `${minutes} min ${Math.round(secondes - minutes * 60)} s`;
}

/**
 * Ce que le job a coûté, en une à trois lignes.
 *
 * Un tableau plutôt qu'une phrase, pour la raison qui vaut déjà pour
 * l'admission : les cas se cumulent — un modèle chaud *et* une éviction *et*
 * une durée — et les concaténer donnerait une ligne qu'on ne lit plus.
 */
export function phrasesBilan(job: Job): readonly string[] {
  const lignes: string[] = [];
  const durée = dureeSecondes(job);
  if (durée !== null) {
    lignes.push(
      job.state === "done" ? `terminé en ${formatDuree(durée)}` : `arrêté après ${formatDuree(durée)}`,
    );
  }
  if (job.reused) {
    lignes.push("le modèle était déjà chaud : son chargement n'a pas été repayé");
  }
  if (job.evicted.length) {
    lignes.push(`a déchargé ${job.evicted.join(", ")} pour faire de la place`);
  }
  return lignes;
}

/**
 * Les métriques du worker, une phrase chacune.
 *
 * Les clés ne sont pas traduites, et ce n'est pas un oubli : `rtf`,
 * `audio_seconds` et `tokens_per_second` sont écrits par les adaptateurs, ils
 * varient d'un runtime à l'autre, et une table de traduction dans le front
 * mentirait dès qu'un adaptateur en ajouterait une. La bonne place du français
 * est là où la clé naît.
 *
 * Les **valeurs**, en revanche, se formatent, et le premier job réel l'a montré :
 * `mlx_audio` rapporte `peak_memory_bytes: 4794454718`, dix chiffres d'affilée
 * dans un écran dont le §4 du plan exige qu'un chiffre de mémoire se lise en
 * gigaoctets. La règle porte sur le **suffixe** et non sur le nom — tout ce qui
 * finit par `_bytes` est une taille —, ce qui est une convention du registre
 * (`disk_bytes`, `peak_unified_memory_bytes`, `budget_bytes`) et non une liste
 * de clés qu'un adaptateur neuf prendrait en défaut.
 */
export function phrasesMetriques(job: Job): readonly string[] {
  return Object.entries(job.metrics)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([clé, valeur]) => `${clé} : ${formatValeur(clé, valeur)}`);
}

function formatValeur(clé: string, valeur: unknown): string {
  if (typeof valeur === "number") {
    if (clé.endsWith("_bytes")) return formatBytes(valeur);
    return Number(valeur.toFixed(3)).toLocaleString("fr-CA");
  }
  return typeof valeur === "string" ? valeur : JSON.stringify(valeur);
}

/**
 * Ce job a-t-il quelque chose à montrer ?
 *
 * Un job échoué peut avoir produit une sortie — `run_job` refuse un job dont
 * une sortie **requise** manque, ce qui laisse dans `output` ce que le worker
 * avait tout de même écrit. Le cacher parce que le job est marqué en échec
 * priverait du seul indice utile pour comprendre ce qui a manqué.
 */
export function aUneSortie(job: Job | null): boolean {
  return job !== null && !enCours(job) && Object.keys(job.output).length > 0;
}
