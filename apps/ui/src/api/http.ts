/**
 * Les deux verbes que l'API accepte, et rien d'autre.
 *
 * `allow_methods` du serveur énumère `GET`, `POST` et `OPTIONS` : un `PUT` ne
 * serait pas refusé par une route, il serait refusé par le préflight, avec un
 * message que le navigateur ne transmet pas au code. Il n'y a donc ici que `get`
 * et `post`.
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

async function lire<T>(requete: Request): Promise<T> {
  let reponse: Response;
  try {
    reponse = await fetch(requete);
  } catch {
    throw new ApiError(requete.url, 0, [messageReseau(BASE_URL)]);
  }
  const corps = await corpsDe(reponse);
  if (!reponse.ok) {
    const messages = detailToMessages(corps);
    throw new ApiError(
      requete.url,
      reponse.status,
      messages.length ? messages : [`${reponse.status} ${reponse.statusText}`],
    );
  }
  return corps as T;
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
