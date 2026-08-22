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

  test("le schema fige annonce les routes que le front appelle", () => {
    // Les routes de job sont arrivées avec le reste de la tâche 4.1 : le
    // superviseur vit dans le processus de l'API depuis la 4.6, donc le serveur
    // sait quel job tourne. C'est ce qui a donné son bouton *Lancer* à
    // l'Atelier.
    //
    // `/uploads` est venue après, et pour une autre raison : un champ fichier
    // se remplissait à la main faute de route, alors qu'une image choisie dans
    // une page, une photo de la caméra et un son du micro n'ont jamais eu de
    // chemin à saisir.
    const schéma = JSON.parse(readFileSync(resolve(DOSSIER, "openapi.json"), "utf8"));
    expect(Object.keys(schéma.paths).sort()).toEqual([
      "/",
      "/healthz",
      "/jobs",
      "/jobs/{job_id}",
      "/jobs/{job_id}/events",
      "/jobs/{job_id}/files/{chemin}",
      "/registry/capabilities",
      "/registry/models",
      "/runtime/admission",
      "/runtime/residents",
      "/store/summary",
      "/uploads",
    ]);
  });

  test("le depot annonce du multipart et rend un chemin", () => {
    // C'est ce que le front programme contre : un `FormData` à l'aller, une
    // chaîne `path` au retour. Un jour où le serveur rendrait un identifiant
    // opaque à la place, le champ du formulaire ne porterait plus un chemin et
    // le worker ne saurait plus l'ouvrir.
    const schéma = JSON.parse(readFileSync(resolve(DOSSIER, "openapi.json"), "utf8"));
    const dépôt = schéma.paths["/uploads"].post;

    expect(Object.keys(dépôt.requestBody.content)).toEqual(["multipart/form-data"]);
    expect(schéma.components.schemas.UploadOut.properties.path.type).toBe("string");
  });
});
