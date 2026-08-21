/**
 * Le vocabulaire fermé du contrat de capacité, côté TypeScript.
 *
 * Ce fichier type le **méta-schéma** — ce qu'un contrat a le droit de déclarer —
 * et jamais les valeurs saisies. La distinction porte tout le socle : le schéma
 * d'entrée d'une capacité est une **donnée** que le serveur recharge à chaud, pas
 * un type qu'on fige au build. `formData` reste un `Record<string, unknown>` de
 * bout en bout, et aucun code n'est engendré depuis `registry/capabilities/`.
 *
 * Les clés listées dans `Champ` sont exactement celles que
 * `registry/schema/capability.schema.json` autorise sous `$defs/champ`, avec son
 * `additionalProperties: false`. En ajouter une ici sans l'ajouter au
 * méta-schéma laisserait croire qu'un contrat peut la porter.
 */

/** Les cinq widgets que le méta-schéma énumère. */
export type XUi = "textarea" | "select" | "file" | "slider" | "hidden";

export const X_UI_CONNUS: readonly XUi[] = ["textarea", "select", "file", "slider", "hidden"];

export interface Champ {
  type: "string" | "number" | "integer" | "boolean" | "array" | "object";
  title?: string;
  description?: string;
  default?: unknown;
  const?: unknown;
  enum?: unknown[];
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  multipleOf?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  /**
   * Déclaré parce que le méta-schéma l'autorise, et **jamais interprété**.
   * Attention au faux ami : `format` est aussi le nom d'un champ du contrat
   * `document-to-text` (« text » ou « markdown »). Aucun code ne doit conclure
   * quoi que ce soit de la présence de cette clé.
   */
  format?: string;
  minItems?: number;
  maxItems?: number;
  items?: Champ;
  properties?: Record<string, Champ>;
  required?: string[];
  additionalProperties?: boolean;
  contentMediaType?: string;
  /**
   * `string` et non `XUi` : le serveur sert le contrat tel quel, et le
   * méta-schéma peut gagner une sixième valeur avant que ce front la connaisse.
   * C'est `widgetFor` qui décide quoi en faire, et il dégrade au lieu de lever.
   */
  "x-ui"?: string;
  "x-options-from"?: string;
}

export interface SchemaEntree {
  type: "object";
  required: string[];
  additionalProperties: false;
  properties: Record<string, Champ>;
  description?: string;
}

/**
 * Un champ qui désigne un fichier.
 *
 * Recopie littérale de la règle du serveur (`ecurie_runtime/runner.py`) : un
 * `x-ui: "file"` **ou** un `contentMediaType`. Les deux vont ensemble sur les dix
 * champs du parc actuel, mais rien dans le méta-schéma ne l'impose, et le jour
 * où un contrat les dissociera, c'est la règle du serveur qui fera foi — pas la
 * coïncidence d'aujourd'hui.
 */
export function estChampFichier(champ: Champ): boolean {
  return champ["x-ui"] === "file" || champ.contentMediaType !== undefined;
}

/**
 * `"application/pdf,image/*"` → `["application/pdf", "image/*"]`.
 *
 * Le contrat `document-to-text` porte deux types dans une seule chaîne. Passée
 * telle quelle à l'attribut `accept` d'un `<input type="file">`, elle
 * fonctionnerait par accident — la virgule y est aussi le séparateur — mais rien
 * ne le garantit et l'appelant a besoin de la liste, pas de la chaîne.
 */
export function accept(contentMediaType: string | undefined): string[] {
  if (!contentMediaType) return [];
  return contentMediaType
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
}

/**
 * Un `type: "object"` sans `properties` : un JSON Schema arbitraire.
 *
 * Le cas existe pour de bon — `tool-use.tools[].parameters` déclare le schéma
 * des arguments d'un outil, il est **requis**, et son contenu est libre par
 * nature. Mesuré sur RJSF 6.8 : un tel champ rend un `<fieldset>` vide, sans
 * aucun contrôle. Le formulaire paraît complet et ne l'est pas ; le contrat le
 * plus riche du parc serait cassé de bout en bout, en silence.
 */
export function estObjetLibre(champ: Champ): boolean {
  return champ.type === "object" && !champ.properties;
}

/** Le schéma d'entrée d'un contrat, quand il en a la forme attendue. */
export function estSchemaEntree(valeur: unknown): valeur is SchemaEntree {
  return (
    typeof valeur === "object" &&
    valeur !== null &&
    (valeur as SchemaEntree).type === "object" &&
    typeof (valeur as SchemaEntree).properties === "object"
  );
}
