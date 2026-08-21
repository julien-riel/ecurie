/** Les types engendrés suivent-ils encore le schéma figé ? */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import openapiTS, { astToString } from "openapi-typescript";
import { describe, expect, test } from "vitest";

const DOSSIER = resolve(process.cwd(), "src/api");

/** Retire la bannière de tête, propre à la ligne de commande. */
function sansEntete(source: string): string {
  return source.replace(/^\/\*\*[\s\S]*?\*\/\s*/, "").trim();
}

describe("les types engendrés depuis l'OpenAPI", () => {
  test("le type genere ne derive pas du schema fige", async () => {
    // Deux artefacts sont committés : le schéma et les types qui en sortent.
    // Régénérer l'un sans l'autre laisserait le front programmer contre une
    // forme qui n'est plus servie — sans qu'aucune compilation ne s'en plaigne,
    // puisque le .ts, lui, resterait cohérent avec lui-même.
    const schéma = JSON.parse(readFileSync(resolve(DOSSIER, "openapi.json"), "utf8"));
    const attendu = astToString(await openapiTS(schéma));
    const committé = readFileSync(resolve(DOSSIER, "openapi.gen.ts"), "utf8");

    // La ligne de commande ajoute un en-tête « auto-generated » que l'appel
    // programmatique n'émet pas : on compare les déclarations, pas la bannière.
    expect(sansEntete(committé), "types périmés — relancer : npm run gen:api").toBe(
      sansEntete(attendu),
    );
  }, 30_000);

  test("le schema fige n_annonce aucune route de job", () => {
    // POST /jobs et le flux SSE attendent délibérément la tâche 4.6 : le front
    // ne doit pas se mettre à les appeler par inadvertance.
    const schéma = JSON.parse(readFileSync(resolve(DOSSIER, "openapi.json"), "utf8"));
    expect(Object.keys(schéma.paths).sort()).toEqual([
      "/",
      "/healthz",
      "/registry/capabilities",
      "/registry/models",
      "/runtime/admission",
      "/runtime/residents",
      "/store/summary",
    ]);
  });
});
