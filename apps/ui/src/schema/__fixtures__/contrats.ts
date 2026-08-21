/**
 * Les contrats du dépôt, lus sur le disque au moment du test.
 *
 * Ils ne sont **pas recopiés** dans une fixture écrite à la main, et c'est le
 * point : le livrable de la tâche 4.3 est « zéro formulaire manuel », donc la
 * preuve doit porter sur les contrats réels, tous, y compris ceux qui n'existent
 * pas encore. Un dix-huitième contrat entre dans la suite tout seul ; une copie
 * figée aurait laissé passer exactement le cas qu'on cherche à éviter.
 *
 * Les tests tournent depuis `apps/ui`, d'où le chemin relatif — et non un
 * `new URL(…, import.meta.url)`, que Vite prend pour une référence d'asset et
 * refuse de servir hors de la racine du projet.
 */

import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import type { Capability } from "../../api/types";
import type { SchemaEntree } from "../contract";

const DOSSIER = resolve(process.cwd(), "../../registry/capabilities");

export interface ContratBrut {
  id: string;
  title: string;
  description?: string;
  input: SchemaEntree;
  output: { type: "object"; properties: Record<string, unknown>; required?: string[] };
  composite?: boolean;
}

export function idsContrats(): string[] {
  return readdirSync(DOSSIER)
    .filter((f) => f.endsWith(".json"))
    .map((f) => f.slice(0, -".json".length))
    .sort();
}

export function contrat(id: string): ContratBrut {
  return JSON.parse(readFileSync(resolve(DOSSIER, `${id}.json`), "utf8")) as ContratBrut;
}

export function tousLesContrats(): ContratBrut[] {
  return idsContrats().map(contrat);
}

/**
 * Un contrat en `Capability`, comme l'API le sert.
 *
 * `output_media_types` est reconstruit ici de la même façon que le serveur le
 * calcule (`ecurie_core/capabilities.py`) : descente récursive dans les sorties
 * de type objet, clés en chemins pointés. C'est une recopie, et
 * `output/registry.test.ts` la confronte contrat par contrat aux chemins que le
 * serveur a réellement produits, capturés dans `api/__fixtures__/capabilities.json`.
 */
export function commeCapability(brut: ContratBrut, partiel: Partial<Capability> = {}): Capability {
  return {
    id: brut.id,
    title: brut.title,
    description: brut.description ?? null,
    composite: brut.composite ?? false,
    steps: [],
    input: brut.input as unknown as Record<string, unknown>,
    output: brut.output as unknown as Record<string, unknown>,
    output_media_types: mediaTypesDe(brut),
    required: brut.input.required,
    defaults: defautsDe(brut),
    models: [],
    ready_variants: [],
    incumbent: null,
    ...partiel,
  };
}

/** Les `default` déclarés par le schéma d'entrée, comme `contract.defaults()`. */
export function defautsDe(brut: ContratBrut): Record<string, unknown> {
  const défauts: Record<string, unknown> = {};
  for (const [nom, champ] of Object.entries(brut.input.properties)) {
    if (champ.default !== undefined) défauts[nom] = champ.default;
  }
  return défauts;
}

/** Les chemins pointés portant un `contentMediaType`, comme `output_media_types()`. */
export function mediaTypesDe(brut: ContratBrut): Record<string, string> {
  const types: Record<string, string> = {};
  function visiter(propriétés: Record<string, unknown> | undefined, préfixe: string): void {
    for (const [nom, brutChamp] of Object.entries(propriétés ?? {})) {
      const champ = brutChamp as { contentMediaType?: string; properties?: Record<string, unknown> };
      const chemin = préfixe ? `${préfixe}.${nom}` : nom;
      if (champ.contentMediaType) types[chemin] = champ.contentMediaType;
      if (champ.properties) visiter(champ.properties, chemin);
    }
  }
  visiter(brut.output.properties, "");
  return types;
}

/** Tous les champs d'entrée du parc, chemin pointé compris — 100 et quelques. */
export function tousLesChamps(): { capacite: string; chemin: string; champ: Record<string, unknown> }[] {
  const champs: { capacite: string; chemin: string; champ: Record<string, unknown> }[] = [];
  for (const brut of tousLesContrats()) {
    function visiter(propriétés: Record<string, unknown> | undefined, préfixe: string): void {
      for (const [nom, valeur] of Object.entries(propriétés ?? {})) {
        const champ = valeur as { items?: unknown; properties?: Record<string, unknown> };
        const chemin = préfixe ? `${préfixe}.${nom}` : nom;
        champs.push({ capacite: brut.id, chemin, champ: champ as Record<string, unknown> });
        if (champ.properties) visiter(champ.properties, chemin);
        if (champ.items) {
          const items = champ.items as { properties?: Record<string, unknown> };
          champs.push({ capacite: brut.id, chemin: `${chemin}.items`, champ: items as Record<string, unknown> });
          if (items.properties) visiter(items.properties, `${chemin}.items`);
        }
      }
    }
    visiter(brut.input.properties, "");
  }
  return champs;
}
