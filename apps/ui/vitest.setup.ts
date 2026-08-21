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
type Reponse = {
  status?: number;
  body?: unknown;
  texte?: string;
  type?: string;
  /** Un corps qui arrive en morceaux — le flux d'événements d'un job. */
  flux?: ReadableStream<Uint8Array>;
};
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
  const { status = 200, body, texte, type, flux } = await route(requete);
  // Le signal est branché sur le flux, et pas seulement accepté : `fetch` fait
  // échouer la lecture en cours quand on abandonne une requête, et un double qui
  // l'ignorerait laisserait passer un composant qui ne coupe jamais rien — le
  // défaut se verrait en production, sur un onglet ouvert des heures.
  const corps =
    flux && requete.signal
      ? flux.pipeThrough(new TransformStream(), { signal: requete.signal })
      : (flux ?? texte ?? (body === undefined ? "" : JSON.stringify(body)));
  return new Response(corps, {
    status,
    headers: { "content-type": type ?? "application/json" },
  });
}) as typeof fetch;

/**
 * Un flux qu'un test remplit à la main, morceau par morceau.
 *
 * Un job n'arrive pas d'un coup : `queued`, `running`, une progression, une
 * sortie. Servir toutes ses trames d'un bloc éprouverait l'analyse et rien
 * d'autre — or ce qui compte à l'écran est qu'une barre bouge *pendant* que le
 * flux dure, et qu'un abandon coupe la connexion au lieu de la laisser ouverte.
 * Le test pousse donc lui-même, entre deux assertions.
 */
export function fluxManuel() {
  const encodeur = new TextEncoder();
  let controle!: ReadableStreamDefaultController<Uint8Array>;
  let ferme = false;
  const corps = new ReadableStream<Uint8Array>({
    start(c) {
      controle = c;
    },
  });
  return {
    corps,
    /** Une trame SSE — `event:` puis `data:`, suivies de la ligne vide. */
    evenement(nom: string, donnee: unknown) {
      controle.enqueue(encodeur.encode(`event: ${nom}\ndata: ${JSON.stringify(donnee)}\n\n`));
    },
    /** Du texte brut, pour éprouver une trame coupée en deux. */
    brut(morceau: string) {
      controle.enqueue(encodeur.encode(morceau));
    },
    /** Des octets, pour couper au milieu d'un caractère et non entre deux. */
    brutOctets(octets: Uint8Array) {
      controle.enqueue(octets);
    },
    fermer() {
      if (ferme) return;
      ferme = true;
      try {
        controle.close();
      } catch {
        // Le consommateur a pu fermer le flux avant le test : un composant
        // démonté annule son lecteur, ce qui clôt le stream par l'autre bout.
        // Un test qui range ses affaires après coup ne doit pas échouer là-dessus.
      }
    },
  };
}

afterEach(() => {
  // Le démontage n'est pas laissé à la détection automatique de Testing
  // Library : sans lui, deux rendus successifs cohabitent dans le même document
  // et une recherche par texte trouve deux fois le même élément.
  cleanup();
  routes.clear();
  vues.length = 0;
});
