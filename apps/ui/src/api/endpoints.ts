/**
 * Les sept routes de l'API, une fonction chacune.
 *
 * C'est le seul fichier du front où une chaîne d'URL apparaît, et le seul où
 * apparaît le nom du paramètre `for`. Ce dernier point n'est pas cosmétique :
 * `for` est un mot réservé en JavaScript, il ne peut pas devenir un identifiant,
 * et le nom Python derrière l'alias est `for_ref` — deux occasions de se
 * tromper, réduites à un endroit qu'un test surveille.
 *
 * Il n'y a pas de huitième fonction : `POST /jobs`, le flux SSE et les fichiers
 * de sortie **n'existent pas encore côté serveur**. Ils attendent délibérément
 * le déménagement du superviseur dans le processus de l'API (tâche 4.6), et
 * écrire ici un client qui frapperait des 404 reviendrait à le défaire ensuite.
 */

import { get, post } from "./http";
import type {
  AdmissionResponse,
  CapabilitiesResponse,
  IndexResponse,
  ModelsResponse,
  ResidentsResponse,
  StoreSummaryResponse,
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
