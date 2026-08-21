/**
 * Le flux d'événements d'un job, lu par `fetch` et non par `EventSource`.
 *
 * Le choix mérite d'être écrit, parce que `CONCEPTION.md §6.1` nomme
 * `EventSource` — c'est pour lui que le serveur termine son flux par un `end`,
 * faute de quoi il rouvrirait la connexion indéfiniment sur un job terminé. Deux
 * raisons l'ont écarté, et la seconde est décisive.
 *
 * **Il n'existe pas en jsdom.** Toute la suite du front tournerait alors sur un
 * double d'`EventSource` écrit ici : on éprouverait le double, pas le client.
 *
 * **Il passerait à travers le filet.** `vitest.setup.ts` remplace `fetch` par un
 * double qui refuse toute route non déclarée, précisément pour qu'un test ne
 * parte pas frapper le `ecurie serve` qui tourne sur cette machine. Un
 * `EventSource` n'est pas `fetch` : il ouvrirait une vraie connexion depuis un
 * test, et l'oubli passerait au vert chez son auteur et nulle part ailleurs.
 *
 * Ce qu'on perd en échange est ce qu'on ne voulait pas : la reconnexion
 * automatique, qui sur un job terminé rouvre une connexion pour rien. Ce qu'on
 * gagne : l'annulation par `AbortSignal`, la même que le reste du front, et un
 * 404 qui arrive comme une `ApiError` lisible plutôt qu'un événement `error` nu.
 *
 * L'analyse suit le format : des trames séparées par une ligne vide, `event:`
 * puis `data:`, et des commentaires `:` pour le battement. Les trois fins de
 * ligne que la spécification autorise sont reconnues, bien que ce serveur-ci
 * n'écrive que `\n` — le test qui l'a demandé a trouvé un vrai défaut : chercher
 * `\n\n` ne trouve rien dans un `\r\n\r\n`, si bien qu'un serveur en CRLF
 * n'aurait jamais rendu un seul événement, sans une erreur pour le dire.
 */

import { flux } from "./http";

export interface EvenementSSE {
  /** Le nom de l'événement. `message` par défaut, comme le veut le format. */
  event: string;
  /** Les lignes `data:` recollées par des sauts de ligne, telles quelles. */
  data: string;
}

/**
 * Découpe un flux en événements, morceau par morceau.
 *
 * Un objet à état, et non une fonction pure, parce qu'une trame se fait couper
 * en deux par la taille des paquets : `data: {"id":"2026…` peut arriver sans son
 * accolade fermante. Ce qui reste incomplet est gardé jusqu'au morceau suivant —
 * l'oublier donnerait un `JSON.parse` en échec toutes les quelques centaines
 * d'octets, et seulement sur des jobs assez longs pour que cela arrive.
 */
/** La ligne vide qui sépare deux trames, dans les trois fins de ligne du format. */
const FIN_DE_TRAME = /\r\n\r\n|\n\n|\r\r/;

export class LecteurSSE {
  private reste = "";

  pousser(morceau: string): EvenementSSE[] {
    this.reste += morceau;
    const evenements: EvenementSSE[] = [];
    for (;;) {
      const coupure = FIN_DE_TRAME.exec(this.reste);
      if (!coupure) break;
      const trame = this.reste.slice(0, coupure.index);
      this.reste = this.reste.slice(coupure.index + coupure[0].length);
      const evenement = analyser(trame);
      if (evenement) evenements.push(evenement);
    }
    return evenements;
  }
}

/** Une trame complète → un événement, ou rien si elle ne portait pas de données. */
function analyser(trame: string): EvenementSSE | null {
  let event = "message";
  const donnees: string[] = [];
  for (const ligne of trame.split(/\r\n|\n|\r/)) {
    if (!ligne || ligne.startsWith(":")) continue; // ligne vide ou battement
    const separateur = ligne.indexOf(":");
    const champ = separateur < 0 ? ligne : ligne.slice(0, separateur);
    const valeur = separateur < 0 ? "" : ligne.slice(separateur + 1).replace(/^ /, "");
    if (champ === "event") event = valeur;
    else if (champ === "data") donnees.push(valeur);
  }
  // Une trame sans `data:` n'est pas un événement — c'est un battement, ou un
  // `retry:` que ce client n'utilise pas.
  return donnees.length ? { event, data: donnees.join("\n") } : null;
}

/**
 * Suit un flux jusqu'à sa fin, en appelant `surEvenement` à chaque trame.
 *
 * Rend la main quand le serveur a fermé le flux — ce qui, pour un job, arrive
 * après son `end`. Une annulation par le signal fait lever, comme n'importe
 * quelle requête abandonnée : c'est à l'appelant de vérifier `signal.aborted`
 * avant d'en faire une erreur affichée.
 */
export async function suivreFlux(
  chemin: string,
  surEvenement: (evenement: EvenementSSE) => void,
  signal?: AbortSignal,
): Promise<void> {
  const reponse = await flux(chemin, signal);
  if (!reponse.body) {
    throw new Error(`le flux ${chemin} n'a pas de corps lisible`);
  }
  const lecteur = reponse.body.getReader();
  const decodeur = new TextDecoder();
  const analyseur = new LecteurSSE();
  try {
    for (;;) {
      const { done, value } = await lecteur.read();
      if (done) break;
      // `stream: true` : un caractère accentué coupé entre deux paquets se
      // reconstitue au suivant au lieu de devenir un losange.
      for (const evenement of analyseur.pousser(decodeur.decode(value, { stream: true }))) {
        surEvenement(evenement);
      }
    }
  } finally {
    // Le verrou du lecteur survit à la boucle : sans lui, un abandon laisserait
    // la connexion ouverte jusqu'au ramassage des ordures.
    await lecteur.cancel().catch(() => {});
  }
}
