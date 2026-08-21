/**
 * L'analyse d'un flux d'événements, sur les formes que le serveur produit vraiment.
 *
 * Ce qui est éprouvé ici ne se voit pas dans un test d'écran : une trame coupée
 * en deux par la taille d'un paquet, un battement de quinze secondes qui n'est
 * pas un événement, un job dont la sortie porte des accents. Aucun de ces cas ne
 * se produit sur un job de trois trames servi d'un bloc — ils arrivent tous sur
 * un vrai job, et jamais deux fois de la même façon.
 */

import { describe, expect, test } from "vitest";
import { fluxManuel, repond } from "../../vitest.setup";
import { ApiError } from "./errors";
import { LecteurSSE, suivreFlux } from "./sse";

describe("le découpage d'un flux", () => {
  test("une trame complete devient un evenement", () => {
    const lecteur = new LecteurSSE();
    expect(lecteur.pousser('event: state\ndata: {"id":"j1"}\n\n')).toEqual([
      { event: "state", data: '{"id":"j1"}' },
    ]);
  });

  test("une trame coupee en deux attend son reste", () => {
    // Le cas qui casse tout et qu'aucun test d'écran ne produit : un paquet
    // réseau tombe au milieu d'un JSON. Rendre la première moitié donnerait un
    // `JSON.parse` en échec, et seulement sur les jobs assez longs pour que la
    // trame dépasse la taille d'un paquet.
    const lecteur = new LecteurSSE();
    expect(lecteur.pousser('event: progress\ndata: {"pro')).toEqual([]);
    expect(lecteur.pousser('gress":40}\n\n')).toEqual([
      { event: "progress", data: '{"progress":40}' },
    ]);
  });

  test("deux trames dans le meme morceau sortent toutes les deux", () => {
    const lecteur = new LecteurSSE();
    const vus = lecteur.pousser("event: state\ndata: 1\n\nevent: end\ndata: 2\n\n");
    expect(vus.map((e) => e.event)).toEqual(["state", "end"]);
  });

  test("un battement n_est pas un evenement", () => {
    // Le serveur écrit `: battement` toutes les quinze secondes pour qu'une
    // connexion muette ne passe pas pour morte. Le rendre comme un événement
    // ferait remplacer l'état du job par une charge vide.
    const lecteur = new LecteurSSE();
    expect(lecteur.pousser(": battement\n\n")).toEqual([]);
    expect(lecteur.pousser("event: state\ndata: 1\n\n")).toHaveLength(1);
  });

  test("un evenement sans nom est un message, comme le veut le format", () => {
    expect(new LecteurSSE().pousser("data: bonjour\n\n")).toEqual([
      { event: "message", data: "bonjour" },
    ]);
  });

  test("plusieurs lignes de donnees se recollent avec un saut de ligne", () => {
    expect(new LecteurSSE().pousser("data: une\ndata: deux\n\n")).toEqual([
      { event: "message", data: "une\ndeux" },
    ]);
  });

  test("le retour chariot d_un serveur en CRLF ne colle pas au nom", () => {
    // Celui-ci écrit `\n`, mais un `\r` laissé en fin de nom donnerait
    // « state\r », qu'aucune comparaison ne reconnaîtrait — et l'écran
    // n'afficherait plus rien sans une seule erreur.
    expect(new LecteurSSE().pousser("event: state\r\ndata: 1\r\n\r\n")).toEqual([
      { event: "state", data: "1" },
    ]);
  });
});

describe("le suivi d'un flux", () => {
  test("les evenements arrivent au fur et a mesure, pas a la fin", async () => {
    const flux = fluxManuel();
    repond("/jobs/j1/events", { flux: flux.corps, type: "text/event-stream" });

    const vus: string[] = [];
    const fini = suivreFlux("/jobs/j1/events", (e) => vus.push(`${e.event}:${e.data}`));

    flux.evenement("state", { state: "queued" });
    await attendre(() => vus.length === 1);
    flux.evenement("progress", { progress: 40 });
    await attendre(() => vus.length === 2);
    flux.fermer();
    await fini;

    expect(vus).toEqual(['state:{"state":"queued"}', 'progress:{"progress":40}']);
  });

  test("un accent coupe entre deux paquets se reconstitue", async () => {
    // `TextDecoder` sans `stream: true` rendrait un losange à la place du « é » :
    // deux octets d'un caractère UTF-8 peuvent tomber dans deux paquets.
    const flux = fluxManuel();
    repond("/jobs/j1/events", { flux: flux.corps, type: "text/event-stream" });
    const vus: string[] = [];
    const fini = suivreFlux("/jobs/j1/events", (e) => vus.push(e.data));

    const octets = new TextEncoder().encode('data: "déchargé"\n\n');
    flux.brutOctets(octets.slice(0, 9));
    flux.brutOctets(octets.slice(9));
    await attendre(() => vus.length === 1);
    flux.fermer();
    await fini;

    expect(JSON.parse(vus[0]!)).toBe("déchargé");
  });

  test("un job inconnu leve une erreur lisible, pas un evenement muet", async () => {
    repond("/jobs/inconnu/events", { status: 404, body: { detail: "job inconnu : inconnu" } });

    await expect(suivreFlux("/jobs/inconnu/events", () => {})).rejects.toThrow(ApiError);
  });

  test("un abandon coupe le flux sans attendre sa fin", async () => {
    const flux = fluxManuel();
    repond("/jobs/j1/events", { flux: flux.corps, type: "text/event-stream" });
    const abandon = new AbortController();
    const suivi = suivreFlux("/jobs/j1/events", () => {}, abandon.signal);

    abandon.abort();

    await expect(suivi).rejects.toThrow();
  });
});

/** Attend qu'une condition devienne vraie, sans minuterie ni sommeil fixe. */
async function attendre(condition: () => boolean, tours = 200): Promise<void> {
  for (let i = 0; i < tours; i += 1) {
    if (condition()) return;
    await new Promise((r) => setTimeout(r, 1));
  }
  throw new Error("condition jamais atteinte");
}
