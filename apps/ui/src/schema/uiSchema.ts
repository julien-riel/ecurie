/**
 * Du contrat de capacité vers l'`uiSchema` de RJSF.
 *
 * C'est ici que « zéro formulaire manuel » se joue : le schéma d'entrée part
 * chez RJSF **tel quel**, sans être recomposé, et tout ce que le front décide se
 * trouve dans l'`uiSchema` que cette fonction engendre. Aucun identifiant de
 * capacité n'apparaît dans ce fichier, ni dans ceux qu'il appelle — c'est la
 * condition pour qu'un dix-huitième contrat entre dans l'UI sans qu'on y touche.
 *
 * La descente est récursive parce que le méta-schéma l'autorise : un champ peut
 * porter `items` (un tableau) ou `properties` (un objet), et le seul tableau
 * d'objets du parc — `tool-use.tools` — cache justement à sa troisième
 * profondeur le champ que RJSF ne sait pas rendre.
 */

import type { Champ, SchemaEntree } from "./contract";
import type { ChampCtx, Resolution } from "../form/widgets";
import { widgetFor } from "../form/widgets";
import type { Notice } from "../notices/notices";
import { uniques } from "../notices/notices";

export type UiSchema = Record<string, unknown>;

export interface CompilationCtx {
  /** Id du contrat, pour que tout avis nomme sa provenance. */
  capacite: string;
  /** `x-options-from` résolu : « voices » → [« serena », …]. */
  runtime?: Record<string, unknown>;
  /** Le variant choisi est-il chargé ? Change la phrase des suggestions. */
  resident?: boolean;
}

export interface Compilation {
  uiSchema: UiSchema;
  notices: readonly Notice[];
}

/**
 * Les suggestions d'un `x-options-from`.
 *
 * `x-options-from: "runtime.voices"` désigne la clé `voices` de ce que le worker
 * a annoncé. On ne retient que les **tableaux de chaînes** : un worker annonce
 * aussi `max_pages: 12` et un objet `versions`, qui n'ont rien à faire dans une
 * liste de valeurs proposées.
 */
export function suggestionsFor(
  champ: Champ,
  runtime: Record<string, unknown> | undefined,
): string[] {
  const source = champ["x-options-from"];
  if (!source || !runtime) return [];
  const clé = source.startsWith("runtime.") ? source.slice("runtime.".length) : source;
  const valeur = runtime[clé];
  if (!Array.isArray(valeur)) return [];
  return valeur.filter((v): v is string => typeof v === "string");
}

function compilerChamp(champ: Champ, chemin: string, ctx: CompilationCtx): Compilation {
  const champCtx: ChampCtx = {
    capacite: ctx.capacite,
    chemin,
    suggestions: suggestionsFor(champ, ctx.runtime),
    resident: ctx.resident ?? false,
  };
  const resolution: Resolution = widgetFor(champ, champCtx);
  const notices: Notice[] = [...resolution.notices];
  const noeud: UiSchema = {};

  if (resolution.widget) noeud["ui:widget"] = resolution.widget;
  if (resolution.field) noeud["ui:field"] = resolution.field;
  if (Object.keys(resolution.options).length > 0) noeud["ui:options"] = resolution.options;

  // Un objet libre est remplacé en entier par `ui:field` : descendre dedans
  // n'aurait aucun sens, il n'a pas de sous-champs déclarés.
  if (resolution.field === "json") {
    return { uiSchema: noeud, notices };
  }

  if (champ.items) {
    const enfant = compilerChamp(champ.items, `${chemin}.items`, ctx);
    if (Object.keys(enfant.uiSchema).length > 0) noeud["items"] = enfant.uiSchema;
    notices.push(...enfant.notices);
  }

  if (champ.properties) {
    for (const [nom, sous] of Object.entries(champ.properties)) {
      const enfant = compilerChamp(sous, `${chemin}.${nom}`, ctx);
      if (Object.keys(enfant.uiSchema).length > 0) noeud[nom] = enfant.uiSchema;
      notices.push(...enfant.notices);
    }
  }

  return { uiSchema: noeud, notices };
}

/** Compile le schéma d'entrée d'un contrat. Ne lève jamais. */
export function compileUiSchema(entree: SchemaEntree, ctx: CompilationCtx): Compilation {
  const uiSchema: UiSchema = {
    // Le socle ne fournit aucun bouton : `POST /jobs` n'existe pas et attend la
    // tâche 4.6, et un bouton « Lancer » inerte serait un mensonge.
    "ui:submitButtonOptions": { norender: true },
  };
  const notices: Notice[] = [];

  for (const [nom, champ] of Object.entries(entree.properties)) {
    const compile = compilerChamp(champ, nom, ctx);
    if (Object.keys(compile.uiSchema).length > 0) uiSchema[nom] = compile.uiSchema;
    notices.push(...compile.notices);
  }

  return { uiSchema, notices: uniques(notices) };
}
