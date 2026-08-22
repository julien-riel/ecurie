/**
 * Les deux verbes que l'API accepte, et rien d'autre.
 *
 * `allow_methods` du serveur énumère `GET`, `POST` et `OPTIONS` : un `PUT` ne
 * serait pas refusé par une route, il serait refusé par le préflight, avec un
 * message que le navigateur ne transmet pas au code. Il n'y a donc ici que `get`
 * et `post` — plus `flux`, qui est un `GET` dont on n'attend pas la fin, et
 * `urlAbsolue`, qui ne fait partir aucune requête.
 *
 * `credentials: "omit"` est posé sur chaque requête parce que `allow_credentials`
 * est faux côté serveur : une requête portant un cookie serait rejetée par le
 * navigateur avant d'atteindre FastAPI, sans explication lisible.
 *
 * Aucun proxy Vite : il masquerait le CORS que le serveur configure exprès, et
 * la configuration réelle ne serait éprouvée qu'en production — c'est-à-dire
 * jamais, sur un outil local.
 */

import { ApiError, detailToMessages, messageReseau } from "./errors";

export const BASE_URL: string =
  (import.meta.env?.VITE_ECURIE_API as string | undefined) ?? "http://127.0.0.1:8765";

async function corpsDe(reponse: Response): Promise<unknown> {
  const texte = await reponse.text();
  if (!texte) return null;
  try {
    return JSON.parse(texte);
  } catch {
    // Un refus de CORS de Starlette (« Disallowed CORS method ») est du texte
    // brut : le rendre tel quel vaut mieux que de le perdre.
    return texte;
  }
}

/**
 * Envoie la requête et rend la réponse **sans toucher à son corps**.
 *
 * La distinction avec `lire` n'est pas de la décomposition pour la forme : un
 * flux d'événements se lit morceau par morceau, et un `await reponse.text()`
 * posé ici attendrait la fin du job avant de rendre la main. Un échec, lui, a
 * toujours un corps fini et court, qu'on consomme pour en tirer la phrase.
 */
async function envoyer(requete: Request): Promise<Response> {
  let reponse: Response;
  try {
    reponse = await fetch(requete);
  } catch {
    throw new ApiError(requete.url, 0, [messageReseau(BASE_URL)]);
  }
  if (!reponse.ok) {
    const messages = detailToMessages(await corpsDe(reponse));
    throw new ApiError(
      requete.url,
      reponse.status,
      messages.length ? messages : [`${reponse.status} ${reponse.statusText}`],
    );
  }
  return reponse;
}

async function lire<T>(requete: Request): Promise<T> {
  return (await corpsDe(await envoyer(requete))) as T;
}

export function get<T>(chemin: string, params?: URLSearchParams, signal?: AbortSignal): Promise<T> {
  const requete = params?.size ? `${chemin}?${params}` : chemin;
  return lire<T>(
    new Request(`${BASE_URL}${requete}`, {
      method: "GET",
      credentials: "omit",
      headers: { accept: "application/json" },
      signal: signal ?? null,
    }),
  );
}

export function post<T>(chemin: string, corps: unknown, signal?: AbortSignal): Promise<T> {
  return lire<T>(
    new Request(`${BASE_URL}${chemin}`, {
      method: "POST",
      credentials: "omit",
      headers: { accept: "application/json", "content-type": "application/json" },
      body: JSON.stringify(corps),
      signal: signal ?? null,
    }),
  );
}

/**
 * Envoie un fichier en `multipart/form-data`.
 *
 * Aucun `content-type` n'est posé, et c'est **obligatoire** : ce format exige une
 * frontière (`boundary`) que seul le navigateur peut engendrer, et qu'il écrit
 * lui-même dans l'en-tête à partir du `FormData`. En poser un à la main donne un
 * en-tête sans frontière, que Starlette refuse d'analyser — un 400 dont le
 * message ne dit pas ce qui manque.
 *
 * Le nom est passé au troisième argument d'`append` plutôt que laissé au `Blob` :
 * une capture n'a pas de nom propre, et sans lui le navigateur envoie « blob » —
 * ce qui prive le serveur de l'extension. Le serveur sait la redéduire du type
 * de média (`ecurie_api.uploads`), et c'est heureux : l'implémentation de
 * `fetch` de Node sérialise un `Blob` en `filename="blob"` quoi qu'on lui passe,
 * si bien que le nom ne peut pas être éprouvé hors d'un vrai navigateur. Il est
 * envoyé parce que c'est ce que la spécification prévoit, et rien du chemin ne
 * dépend de son arrivée.
 */
export function postFichier<T>(
  chemin: string,
  fichier: Blob,
  nom: string,
  signal?: AbortSignal,
): Promise<T> {
  const corps = new FormData();
  corps.append("file", fichier, nom);
  return lire<T>(
    new Request(`${BASE_URL}${chemin}`, {
      method: "POST",
      credentials: "omit",
      headers: { accept: "application/json" },
      body: corps,
      signal: signal ?? null,
    }),
  );
}

/**
 * Ouvre un flux d'événements et rend la réponse, corps non lu.
 *
 * C'est un `GET` comme les autres jusqu'à l'en-tête ; ce qui change est qu'on ne
 * l'attend pas — le corps arrive au fur et à mesure que le job avance, et c'est
 * `sse.ts` qui le découpe.
 */
export function flux(chemin: string, signal?: AbortSignal): Promise<Response> {
  return envoyer(
    new Request(`${BASE_URL}${chemin}`, {
      method: "GET",
      credentials: "omit",
      headers: { accept: "text/event-stream" },
      signal: signal ?? null,
    }),
  );
}

/**
 * Le chemin qu'une réponse porte, rendu chargeable par le navigateur.
 *
 * Le serveur compose `/jobs/{id}/files/…` sans hôte : il ne sait pas d'où la
 * page a été servie, et l'inventer serait une hypothèse. C'est donc au front de
 * le préfixer — la page vient de Vite en 5173, l'API écoute en 8765, et un
 * chemin nu pointerait sur Vite. Une URL déjà absolue est rendue telle quelle,
 * pour qu'un jour où le serveur servirait aussi la page rien n'ait à bouger.
 */
export function urlAbsolue(chemin: string): string {
  return /^[a-z][a-z0-9+.-]*:/i.test(chemin) ? chemin : `${BASE_URL}${chemin}`;
}
