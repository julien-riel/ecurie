/**
 * Les treize routes de l'API, une fonction chacune.
 *
 * C'est le seul fichier du front où une chaîne d'URL apparaît, et le seul où
 * apparaît le nom du paramètre `for`. Ce dernier point n'est pas cosmétique :
 * `for` est un mot réservé en JavaScript, il ne peut pas devenir un identifiant,
 * et le nom Python derrière l'alias est `for_ref` — deux occasions de se
 * tromper, réduites à un endroit qu'un test surveille.
 *
 * Trois d'entre elles sont celles des jobs, et elles ont attendu que le serveur
 * les serve : le déménagement du superviseur dans le processus de l'API (tâche
 * 4.6) était le préalable, écrire ici un client qui aurait frappé des 404
 * revenait à le défaire ensuite.
 *
 * La onzième est `deposerFichier`, et elle a attendu plus longtemps encore : le
 * front savait depuis le 4.3 qu'un champ fichier ne peut pas être rempli par un
 * sélecteur de fichier, et s'en tenait à l'annoncer. Ce qui manquait n'était pas
 * une idée mais une route ; elle existe.
 *
 * Les deux dernières sont celles du Parc (4.5), et elles ont ceci de commun
 * qu'elles décrivent une action sans la faire : un plan de récupération qui
 * n'est pas écrit, un déport qui n'est pas exécuté. Chacune rend la commande à
 * taper — ce qui n'est pas un aveu de faiblesse du front mais la conséquence du
 * §4.3 de la conception, où l'exécution relit chaque fichier sur le disque avant
 * d'y toucher.
 *
 * **Il n'y en a pas de quatorzième** : l'URL d'un fichier de sortie n'est pas
 * composée ici, elle est *lue* dans la réponse du job. Le serveur la donne déjà
 * faite, parce qu'une sortie imbriquée — `tracks/vocals.wav` sous la clé
 * `tracks.vocals` — est un chemin à plusieurs segments que le client n'a pas à
 * savoir recomposer.
 */

import { get, post, postFichier, urlAbsolue } from "./http";
import { suivreFlux, type EvenementSSE } from "./sse";
import type {
  AdmissionResponse,
  CapabilitiesResponse,
  IndexResponse,
  Job,
  JobDetail,
  ModelsResponse,
  ResidentsResponse,
  StorePlanResponse,
  StoreSummaryResponse,
  TieringResponse,
  Upload,
} from "./types";

export function index(signal?: AbortSignal): Promise<IndexResponse> {
  return get<IndexResponse>("/", undefined, signal);
}

export function healthz(signal?: AbortSignal): Promise<{ ok: boolean }> {
  return get<{ ok: boolean }>("/healthz", undefined, signal);
}

export function capabilities(signal?: AbortSignal): Promise<CapabilitiesResponse> {
  return get<CapabilitiesResponse>("/registry/capabilities", undefined, signal);
}

/**
 * Les manifestes, éventuellement filtrés.
 *
 * Une capacité inconnue rend un **404**, pas une liste vide : le serveur refuse
 * de laisser lire « aucun modèle pour cette capacité » là où la vraie réponse
 * est « cette capacité n'existe pas ». L'appelant ne doit donc jamais composer
 * ce paramètre à la main — seulement le reprendre d'un `id` rendu par
 * `capabilities()`.
 */
export function models(capability?: string, signal?: AbortSignal): Promise<ModelsResponse> {
  const params = new URLSearchParams();
  if (capability) params.set("capability", capability);
  return get<ModelsResponse>("/registry/models", params, signal);
}

export function storeSummary(
  unusedAfterDays?: number,
  signal?: AbortSignal,
): Promise<StoreSummaryResponse> {
  const params = new URLSearchParams();
  if (unusedAfterDays !== undefined) params.set("unused_after_days", String(unusedAfterDays));
  return get<StoreSummaryResponse>("/store/summary", params, signal);
}

/**
 * Le plan de récupération **à blanc** — ce qui serait fait, jamais ce qui est fait.
 *
 * La route ne dépose aucun fichier, alors que `ecurie store plan` en dépose un.
 * Ce n'est pas une omission : `ecurie store apply` exige un chemin de plan, et
 * la réponse porte donc `command`, la commande qui l'écrit pour de bon. Rien
 * dans le front n'applique un plan, et rien ne devrait : chaque action
 * re-vérifie le sha256 du fichier au moment d'agir, ce qui prend le temps de
 * relire des giga-octets.
 */
export function storePlan(
  options: { unusedAfterDays?: number; verifiedOnly?: boolean } = {},
  signal?: AbortSignal,
): Promise<StorePlanResponse> {
  const params = new URLSearchParams();
  if (options.unusedAfterDays !== undefined) {
    params.set("unused_after_days", String(options.unusedAfterDays));
  }
  if (options.verifiedOnly) params.set("verified_only", "true");
  return get<StorePlanResponse>("/store/plan", params, signal);
}

/** Les volumes déclarés, les variants déjà déportés, et ce que chacun pèse. */
export function storeTiering(signal?: AbortSignal): Promise<TieringResponse> {
  return get<TieringResponse>("/store/tiering", undefined, signal);
}

/**
 * Les résidents, et l'admission simulée d'un variant si on la demande.
 *
 * `ref` doit être un `model@variant` complet. Le serveur tolère un id de modèle
 * seul quand le choix est évident, mais rend un 404 dès qu'un modèle a plusieurs
 * variants — `swin2sr` en a deux. Cette tolérance est une commodité de la ligne
 * de commande, pas un contrat : le front envoie toujours `variant.ref`.
 */
export function residents(ref?: string, signal?: AbortSignal): Promise<ResidentsResponse> {
  const params = new URLSearchParams();
  if (ref) params.set("for", ref);
  return get<ResidentsResponse>("/runtime/residents", params, signal);
}

/**
 * Ce que coûterait ce job, pour cette entrée-là.
 *
 * `POST` par la forme, lecture par l'effet : rien n'est chargé, rien n'est
 * écrit. Le verbe vient de ce que la question porte une entrée complète, qu'on
 * n'écrit pas dans une chaîne de requête.
 *
 * Le `seed` n'est **pas** passé au niveau racine, bien que le schéma l'accepte :
 * il appartient à l'entrée comme n'importe quel paramètre du contrat, et le
 * serveur écrit le champ racine par-dessus après avoir résolu la saisie. Deux
 * sources pour une même valeur en feraient partir une que l'utilisateur n'a pas
 * choisie.
 */
export function admission(
  ref: string,
  input: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<AdmissionResponse> {
  return post<AdmissionResponse>("/runtime/admission", { ref, input }, signal);
}

/**
 * Soumet un job. Rend **202** et l'identifiant : rien n'est encore produit.
 *
 * Ce que la réponse garantit, c'est qu'un job existe et qu'il est suivable. Ce
 * qu'elle ne dit pas, et ne peut pas dire, c'est qu'il tiendra en mémoire :
 * l'admission ne se tranche qu'au moment de charger, après le tour de rôle, et
 * son refus arrive donc par le flux, en état `failed`, avec la phrase que le
 * contrôle d'admission compose. Un client qui n'écouterait que le code HTTP
 * croirait tous ses jobs partis.
 *
 * Le `seed` n'est pas passé au niveau racine, comme pour `admission()` : il
 * appartient à l'entrée comme n'importe quel paramètre du contrat.
 */
export function lancerJob(
  ref: string,
  input: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<Job> {
  return post<Job>("/jobs", { ref, input }, signal);
}

/**
 * Arrête un job en cours, et rend son état après l'arrêt.
 *
 * Le job rendu est en `failed` avec `cancelled` à vrai : l'arrêt passe par la
 * mort du worker, il n'y a pas de sortie partielle à ramasser. Ce qui reste est
 * `stream_text` — le texte déjà reçu —, et c'est précisément ce qu'un écran doit
 * garder à l'affichage plutôt que de le remplacer par un message d'échec.
 *
 * Appeler sur un job déjà terminé ne lève pas : le bouton cliqué une seconde
 * trop tard est un geste normal, pas une erreur à signaler.
 */
export function arreterJob(id: string, signal?: AbortSignal): Promise<Job> {
  return post<Job>(`/jobs/${encodeURIComponent(id)}/cancel`, undefined, signal);
}

/**
 * L'état d'un job et son manifeste.
 *
 * Répond aussi pour un job d'une session précédente : la table du serveur ne
 * survit pas à son redémarrage, le dossier du job si, et c'est le manifeste qui
 * fait foi sur ce qui a été exécuté.
 */
export function job(id: string, signal?: AbortSignal): Promise<JobDetail> {
  return get<JobDetail>(`/jobs/${encodeURIComponent(id)}`, undefined, signal);
}

/**
 * Suit un job en direct. Rend la main quand le flux se termine.
 *
 * Le flux **rejoue depuis le début** : s'abonner en retard, ou se rabonner après
 * une coupure, redonne l'histoire entière et non le silence jusqu'au prochain
 * changement. C'est ce qui rend la reprise du suivi triviale — il n'y a rien à
 * rattraper.
 */
export function evenementsDuJob(
  id: string,
  surEvenement: (evenement: EvenementSSE) => void,
  signal?: AbortSignal,
): Promise<void> {
  return suivreFlux(`/jobs/${encodeURIComponent(id)}/events`, surEvenement, signal);
}

/**
 * Dépose un fichier sur la machine du serveur et rend le chemin qu'il a écrit.
 *
 * C'est la contrepartie exacte de ce que `FilePathWidget` disait depuis le
 * 4.3 : *« le navigateur ne donne pas le chemin réel d'un fichier choisi —
 * seulement son nom et son contenu »*. Il a le contenu ; il suffit de l'envoyer
 * pour qu'un chemin existe. Ce que la fonction rend va **dans le champ**, tel
 * quel : c'est la même chaîne qu'on aurait tapée.
 *
 * Elle sert les trois sources d'un même besoin — un fichier choisi sur le
 * disque, une image déposée depuis une page web, une capture de la caméra ou du
 * micro — parce que les trois arrivent au même endroit : un `Blob` sans chemin.
 */
export function deposerFichier(fichier: Blob, nom: string, signal?: AbortSignal): Promise<Upload> {
  return postFichier<Upload>("/uploads", fichier, nom, signal);
}

/**
 * L'URL d'un fichier de sortie, telle que le navigateur peut la charger.
 *
 * Une **lecture**, pas une construction : `job.files` porte déjà le chemin
 * composé par le serveur, et tout ce qui reste à faire est de lui donner
 * l'hôte de l'API — la page vient de Vite en 5173, l'API écoute en 8765.
 */
export function fichierDuJob(url: string): string {
  return urlAbsolue(url);
}
