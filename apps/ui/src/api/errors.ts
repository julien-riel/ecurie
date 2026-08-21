/**
 * Ramener les quatre formes d'échec de l'API à des phrases lisibles.
 *
 * Le serveur ne parle pas d'une seule voix, et il a ses raisons. Un 404 métier
 * porte `{detail: "…"}`, une phrase française qui énumère ce qui existe — « 
 * capacité inconnue : 'chimere' — connues : … » ; un 422 de pydantic porte
 * `{detail: [{loc, msg}]}`, une liste ; le gestionnaire d'imprévu de `app.py`
 * ajoute `path` ; et un refus de CORS ne traverse même pas FastAPI — c'est du
 * texte brut, quand ce n'est pas un échec de `fetch` sans corps du tout.
 *
 * Afficher `detail` tel quel donnerait « [object Object] » sur la moitié des 422,
 * ce qui est le seul message dont on ne peut rien faire. On les normalise donc
 * toutes en `string[]`, sans jamais réécrire les phrases du serveur : elles sont
 * en français et portent ce qui manque, et les paraphraser ferait perdre le seul
 * renseignement utile. Les commandes de réparation, elles, ne voyagent pas dans
 * les erreurs : elles arrivent dans les `blockers` d'une réponse **200** —
 * « poids absents du cache — ecurie pull <ref> » —, parce qu'un variant qu'on ne
 * peut pas lancer n'est pas un échec de requête.
 *
 * Le cas du réseau mérite son traitement : côté JavaScript, **un refus de CORS
 * est indiscernable d'un serveur éteint** — les deux donnent un `TypeError` nu.
 * Le message nomme donc les deux causes, avec les deux graphies de l'origine
 * autorisée, plutôt que de choisir la mauvaise.
 */

export class ApiError extends Error {
  readonly status: number;
  readonly messages: readonly string[];
  readonly url: string;

  constructor(url: string, status: number, messages: readonly string[]) {
    super(messages.join(" · ") || `échec de ${url}`);
    this.name = "ApiError";
    this.url = url;
    this.status = status;
    this.messages = messages;
  }

  /** Vrai quand la requête n'a jamais atteint le serveur (statut 0). */
  get reseau(): boolean {
    return this.status === 0;
  }
}

interface ErreurPydantic {
  loc?: unknown[];
  msg?: string;
}

function estErreurPydantic(valeur: unknown): valeur is ErreurPydantic {
  return typeof valeur === "object" && valeur !== null && "msg" in valeur;
}

/**
 * Toutes les formes de `detail` du serveur, ramenées à des phrases.
 *
 * La forme liste est aplatie en « body.ref : Field required » : `loc` est le
 * chemin du champ fautif et c'est le seul moyen de savoir lequel des paramètres
 * a été refusé.
 */
export function detailToMessages(corps: unknown): string[] {
  if (typeof corps === "string") {
    return corps.trim() ? [corps.trim()] : [];
  }
  if (typeof corps !== "object" || corps === null) {
    return [];
  }
  const detail = (corps as { detail?: unknown }).detail;
  if (typeof detail === "string") {
    return [detail];
  }
  if (Array.isArray(detail)) {
    return detail.map((entrée) => {
      if (!estErreurPydantic(entrée)) return String(entrée);
      const chemin = Array.isArray(entrée.loc) ? entrée.loc.join(".") : "";
      const message = entrée.msg ?? "entrée refusée";
      return chemin ? `${chemin} : ${message}` : message;
    });
  }
  if (detail !== undefined) {
    return [JSON.stringify(detail)];
  }
  return [];
}

/** Le message d'un `fetch` qui n'a jamais abouti — deux causes, jamais une seule. */
export function messageReseau(base: string): string {
  return (
    `aucune réponse de ${base}. Deux causes donnent la même erreur au navigateur : ` +
    "le serveur n'écoute pas (lancer « ecurie serve » depuis le dépôt), ou l'origine " +
    "de cette page n'est pas dans la liste CORS du serveur — qui n'autorise que " +
    "http://localhost:5173 et http://127.0.0.1:5173."
  );
}

/** Ce que l'utilisateur lit quand une requête échoue, sans jamais de forme vide. */
export function phraseErreur(erreur: unknown): string {
  if (erreur instanceof ApiError) {
    return erreur.messages.length ? erreur.messages.join(" · ") : `échec ${erreur.status}`;
  }
  if (erreur instanceof Error) {
    return erreur.message;
  }
  return String(erreur);
}
