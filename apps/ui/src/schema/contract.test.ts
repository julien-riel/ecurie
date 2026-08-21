/** Le vocabulaire du contrat, confronté au registre réel. */

import { describe, expect, test } from "vitest";
import { tousLesChamps, tousLesContrats } from "./__fixtures__/contrats";
import { accept, estChampFichier, estObjetLibre } from "./contract";

describe("la reconnaissance d'un champ fichier", () => {
  test("un champ fichier se reconnait comme cote serveur", () => {
    // Le serveur teste « x-ui vaut file OU contentMediaType est présent »
    // (ecurie_runtime/runner.py). Les deux vont ensemble sur le parc actuel ;
    // rien dans le méta-schéma ne l'impose. Le jour où un contrat les
    // dissociera, cette divergence doit devenir un échec de test et non un champ
    // silencieusement mal rendu.
    const champs = tousLesChamps();
    const parXui = champs.filter(({ champ }) => champ["x-ui"] === "file");
    const parType = champs.filter(({ champ }) => champ["contentMediaType"] !== undefined);

    expect(parXui.length).toBeGreaterThan(0);
    expect(parXui.map((c) => `${c.capacite}.${c.chemin}`).sort()).toEqual(
      parType.map((c) => `${c.capacite}.${c.chemin}`).sort(),
    );
    for (const { champ } of [...parXui, ...parType]) {
      expect(estChampFichier(champ as never)).toBe(true);
    }
  });

  test("un champ ordinaire n_est pas un fichier", () => {
    expect(estChampFichier({ type: "string" })).toBe(false);
    expect(estChampFichier({ type: "string", "x-ui": "textarea" })).toBe(false);
  });

  test("un contentMediaType seul suffit", () => {
    expect(estChampFichier({ type: "string", contentMediaType: "image/png" })).toBe(true);
  });
});

describe("le decoupage des types de media", () => {
  test("une chaine a deux types donne deux entrees", () => {
    expect(accept("application/pdf,image/*")).toEqual(["application/pdf", "image/*"]);
  });

  test("les espaces autour des virgules sont absorbes", () => {
    expect(accept("audio/wav , audio/mpeg")).toEqual(["audio/wav", "audio/mpeg"]);
  });

  test("un type absent donne une liste vide", () => {
    expect(accept(undefined)).toEqual([]);
    expect(accept("")).toEqual([]);
  });

  test("tous les types du registre se decoupent", () => {
    for (const { champ } of tousLesChamps()) {
      const type = champ["contentMediaType"] as string | undefined;
      if (!type) continue;
      const liste = accept(type);
      expect(liste.length).toBeGreaterThan(0);
      expect(liste.every((t) => t.includes("/"))).toBe(true);
    }
  });
});

describe("la reconnaissance d'un objet libre", () => {
  test("un objet sans properties est libre", () => {
    expect(estObjetLibre({ type: "object" })).toBe(true);
  });

  test("un objet avec properties ne l_est pas", () => {
    expect(estObjetLibre({ type: "object", properties: { a: { type: "string" } } })).toBe(false);
  });

  test("le parc n_a qu_un seul objet libre et il est requis", () => {
    // Si ce compte change, c'est que le registre a gagné un champ que RJSF ne
    // rend pas : le savoir vaut mieux que de le découvrir sur un formulaire.
    const libres = tousLesChamps().filter(({ champ }) => estObjetLibre(champ as never));
    expect(libres.map((c) => `${c.capacite}.${c.chemin}`)).toEqual(["tool-use.tools.items.parameters"]);

    const toolUse = tousLesContrats().find((c) => c.id === "tool-use")!;
    const items = toolUse.input.properties["tools"]!.items!;
    expect(items.required).toContain("parameters");
  });
});
