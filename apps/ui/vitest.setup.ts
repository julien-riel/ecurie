import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

/**
 * Aucune requête réseau ne part d'un test.
 *
 * Le double de `fetch` posé ici refuse tout ce qui n'a pas été déclaré par le
 * test en cours : une route oubliée échoue en nommant l'URL au lieu de partir
 * vers un serveur qui, sur cette machine, existe pour de bon. Un test qui
 * frapperait `ecurie serve` passerait au vert chez son auteur et nulle part
 * ailleurs.
 */
type Reponse = { status?: number; body?: unknown; texte?: string; type?: string };
type Route = (requete: Request) => Reponse | Promise<Reponse>;

const routes = new Map<string, Route>();
const vues: Request[] = [];

/**
 * Le `fetch` du navigateur, capturé avant d'être remplacé.
 *
 * L'essai de bout en bout (`App.reel.test.tsx`) en a besoin : il parle à un
 * vrai `ecurie serve`, et c'est tout son intérêt. Le capturer ici est la seule
 * façon d'y arriver — ce fichier de configuration s'exécute avant les modules de
 * test, si bien qu'un test qui lirait `globalThis.fetch` récupérerait le double.
 */
export const fetchReel = globalThis.fetch;

/** Déclare la réponse d'une route pour la durée du test. */
export function repond(chemin: string, route: Route | Reponse): void {
  routes.set(chemin, typeof route === "function" ? route : () => route);
}

/** Les requêtes réellement construites, dans l'ordre — l'objet, pas une trace. */
export function requetes(): readonly Request[] {
  return vues;
}

globalThis.fetch = (async (entree: RequestInfo | URL, init?: RequestInit) => {
  const requete = entree instanceof Request ? entree : new Request(entree, init);
  vues.push(requete);
  const url = new URL(requete.url);
  const route = routes.get(url.pathname);
  if (!route) {
    throw new Error(
      `route non déclarée dans ce test : ${requete.method} ${url.pathname}${url.search} — ` +
        "appeler repond() avant de rendre le composant",
    );
  }
  const { status = 200, body, texte, type } = await route(requete);
  const corps = texte ?? (body === undefined ? "" : JSON.stringify(body));
  return new Response(corps, {
    status,
    headers: { "content-type": type ?? "application/json" },
  });
}) as typeof fetch;

afterEach(() => {
  // Le démontage n'est pas laissé à la détection automatique de Testing
  // Library : sans lui, deux rendus successifs cohabitent dans le même document
  // et une recherche par texte trouve deux fois le même élément.
  cleanup();
  routes.clear();
  vues.length = 0;
});
