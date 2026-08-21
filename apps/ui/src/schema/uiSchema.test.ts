/** Le compilateur de formulaire, éprouvé sur les contrats réels du dépôt. */

import { describe, expect, test } from "vitest";
import { CHEMIN_LOCAL, OPTIONS_RUNTIME } from "../form/widgets";
import { idsContrats, tousLesChamps, tousLesContrats } from "./__fixtures__/contrats";
import type { Champ, SchemaEntree } from "./contract";
import { compileUiSchema, suggestionsFor } from "./uiSchema";

describe("le compilateur d'uiSchema", () => {
  test("compile tous les contrats du depot sans lever", () => {
    const ids = idsContrats();
    expect(ids.length).toBeGreaterThanOrEqual(17);

    for (const brut of tousLesContrats()) {
      const { uiSchema, notices } = compileUiSchema(brut.input, { capacite: brut.id });
      expect(uiSchema, brut.id).toBeTruthy();
      // Aucune dégradation sur le parc réel : chaque x-ui du registre est
      // honoré. Ce test échouera le jour où un contrat en portera un que l'UI ne
      // sait pas rendre — et c'est exactement le signal qu'on veut.
      expect(notices, `${brut.id} : ${notices.map((n) => n.message).join(" | ")}`).toHaveLength(0);
    }
  });

  test("chaque champ x_ui recoit le widget annonce", () => {
    // Les décomptes sont calculés depuis le registre, jamais recopiés : la table
    // doit couvrir le parc tel qu'il est, pas tel qu'il était.
    const champs = tousLesChamps();
    const parXui = new Map<string, number>();
    for (const { champ } of champs) {
      const xui = champ["x-ui"] as string | undefined;
      if (xui) parXui.set(xui, (parXui.get(xui) ?? 0) + 1);
    }
    expect(parXui.size).toBeGreaterThan(0);

    for (const brut of tousLesContrats()) {
      const { uiSchema } = compileUiSchema(brut.input, { capacite: brut.id });
      for (const [nom, champ] of Object.entries(brut.input.properties)) {
        const noeud = uiSchema[nom] as Record<string, unknown> | undefined;
        const xui = champ["x-ui"];
        if (xui === "textarea" && !champ.enum) {
          expect(noeud?.["ui:widget"], `${brut.id}.${nom}`).toBe("textarea");
        }
        if (xui === "file") {
          expect(noeud?.["ui:widget"], `${brut.id}.${nom}`).toBe(CHEMIN_LOCAL);
        }
        if (xui === "select" && !champ.enum) {
          expect(noeud?.["ui:widget"], `${brut.id}.${nom}`).toBe(OPTIONS_RUNTIME);
        }
      }
    }
  });

  test("un enum l_emporte sur le widget impose", () => {
    // Le SelectWidget natif encode ses options par index et préserve donc le
    // type : 44100 reste un entier. Un widget imposé lirait une chaîne, que le
    // validateur Draft 2020-12 du serveur refuserait.
    const entree: SchemaEntree = {
      type: "object",
      required: ["taux"],
      additionalProperties: false,
      properties: {
        taux: { type: "integer", enum: [16000, 44100], "x-ui": "textarea" },
      },
    };
    const { uiSchema, notices } = compileUiSchema(entree, { capacite: "essai" });
    expect(uiSchema["taux"]).toBeUndefined();
    expect(notices).toHaveLength(1);
    expect(notices[0]!.message).toContain("enum");
  });

  test("un x_ui inconnu retombe sur le widget du type", () => {
    const entree: SchemaEntree = {
      type: "object",
      required: ["titre"],
      additionalProperties: false,
      properties: { titre: { type: "string", "x-ui": "carrousel" } },
    };
    const { uiSchema, notices } = compileUiSchema(entree, { capacite: "essai" });

    expect(uiSchema["titre"]).toBeUndefined(); // RJSF déduit du type : le champ reste rendu
    expect(notices).toHaveLength(1);
    expect(notices[0]!.source).toBe("essai.titre");
    expect(notices[0]!.message).toContain("carrousel");
    expect(notices[0]!.message).toContain("inconnu");
  });

  test("un slider sans bornes degrade avec un avis", () => {
    const entree: SchemaEntree = {
      type: "object",
      required: ["poids"],
      additionalProperties: false,
      properties: { poids: { type: "number", "x-ui": "slider" } },
    };
    const { uiSchema, notices } = compileUiSchema(entree, { capacite: "essai" });
    expect(uiSchema["poids"]).toBeUndefined();
    expect(notices[0]!.message).toContain("minimum");
  });

  test("un slider borne devient un curseur", () => {
    const entree: SchemaEntree = {
      type: "object",
      required: ["poids"],
      additionalProperties: false,
      properties: {
        poids: { type: "number", minimum: 0, maximum: 1, multipleOf: 0.1, "x-ui": "slider" },
      },
    };
    const { uiSchema, notices } = compileUiSchema(entree, { capacite: "essai" });
    expect((uiSchema["poids"] as Record<string, unknown>)["ui:widget"]).toBe("range");
    expect(notices).toHaveLength(0);
  });

  test("le type media double est decoupe", () => {
    // document-to-text.document déclare "application/pdf,image/*" : deux types
    // dans une seule chaîne, que l'attribut accept doit recevoir en liste.
    const brut = tousLesContrats().find((c) => c.id === "document-to-text")!;
    const { uiSchema } = compileUiSchema(brut.input, { capacite: brut.id });
    const noeud = uiSchema["document"] as Record<string, unknown>;
    const options = noeud["ui:options"] as Record<string, unknown>;
    expect(options["accept"]).toEqual(["application/pdf", "image/*"]);
  });

  test("le json schema libre recoit un champ dedie", () => {
    // tool-use.tools[].parameters : le seul objet sans properties du parc, et il
    // est requis. RJSF ne rend rien pour lui.
    const brut = tousLesContrats().find((c) => c.id === "tool-use")!;
    const { uiSchema } = compileUiSchema(brut.input, { capacite: brut.id });
    const tools = uiSchema["tools"] as Record<string, unknown>;
    const items = tools["items"] as Record<string, unknown>;
    const parameters = items["parameters"] as Record<string, unknown>;
    expect(parameters["ui:field"]).toBe("json");
  });

  test("la descente atteint les champs imbriques", () => {
    const brut = tousLesContrats().find((c) => c.id === "tool-use")!;
    const { uiSchema } = compileUiSchema(brut.input, { capacite: brut.id });
    // `system` porte x-ui: textarea au premier niveau, `parameters` est à trois
    // niveaux de profondeur : les deux doivent être compilés.
    expect((uiSchema["system"] as Record<string, unknown>)["ui:widget"]).toBe("textarea");
    expect(uiSchema["tools"]).toBeTruthy();
  });

  test("aucun bouton de soumission n_est rendu", () => {
    // Aucune route de job n'existe avant la tâche 4.6 : un bouton « Lancer »
    // serait inerte, donc mensonger.
    const brut = tousLesContrats()[0]!;
    const { uiSchema } = compileUiSchema(brut.input, { capacite: brut.id });
    expect(uiSchema["ui:submitButtonOptions"]).toEqual({ norender: true });
  });
});

describe("les suggestions d'un x-options-from", () => {
  const champVoix: Champ = { type: "string", "x-options-from": "runtime.voices" };

  test("un runtime absent ne suggere rien", () => {
    expect(suggestionsFor(champVoix, undefined)).toEqual([]);
  });

  test("une liste vide est un etat normal", () => {
    // Le worker mlx_vlm annonce languages: [] volontairement — il les accepte
    // toutes. Ce n'est pas une absence de réponse.
    expect(suggestionsFor(champVoix, { voices: [] })).toEqual([]);
  });

  test("seuls les tableaux de chaines entrent dans les suggestions", () => {
    const runtime = { voices: ["serena", "ethan"], max_pages: 12, versions: { mlx: "0.3" } };
    expect(suggestionsFor(champVoix, runtime)).toEqual(["serena", "ethan"]);
    const champAutre: Champ = { type: "string", "x-options-from": "runtime.max_pages" };
    expect(suggestionsFor(champAutre, runtime)).toEqual([]);
  });

  test("les valeurs non textuelles d_une liste sont ecartees", () => {
    expect(suggestionsFor(champVoix, { voices: ["serena", 3, null] })).toEqual(["serena"]);
  });

  test("un champ sans x_options_from ne suggere rien", () => {
    expect(suggestionsFor({ type: "string" }, { voices: ["serena"] })).toEqual([]);
  });
});

describe("l'ordre des règles de widgetFor", () => {
  test("un objet libre garde son editeur meme avec un x_ui", () => {
    // Défaut trouvé en revue : la règle de l'objet libre venait APRÈS celles du
    // x-ui, si bien qu'une seule clé — légale au méta-schéma, qui ne contraint
    // pas x-ui par type — retirait au champ son unique contrôle. Le formulaire
    // affichait un fieldset vide sur un champ requis, avec un avis prétendant
    // qu'il était « rendu par le widget du type ».
    for (const xui of ["textarea", "select", "slider", "carrousel"]) {
      const entree: SchemaEntree = {
        type: "object",
        required: ["params"],
        additionalProperties: false,
        properties: { params: { type: "object", "x-ui": xui } },
      };
      const { uiSchema, notices } = compileUiSchema(entree, { capacite: "essai" });
      const noeud = uiSchema["params"] as Record<string, unknown>;
      expect(noeud["ui:field"], xui).toBe("json");
      expect(notices[0]!.message, xui).toContain("schéma libre");
    }
  });

  test("un champ fichier qui n_est pas une chaine degrade avec un avis", () => {
    // La règle du fichier court-circuitait le garde `accepts` de la table : le
    // même x-ui refusé par la règle du x-ui était accepté en silence par
    // celle-ci, posant un contrôle de saisie de chemin sur un tableau.
    const entree: SchemaEntree = {
      type: "object",
      required: ["images"],
      additionalProperties: false,
      properties: {
        images: { type: "array", "x-ui": "file", contentMediaType: "image/*" },
      },
    };
    const { uiSchema, notices } = compileUiSchema(entree, { capacite: "essai" });
    expect(uiSchema["images"]).toBeUndefined();
    expect(notices[0]!.message).toContain("chaîne");
  });

  test("les champs fichier du parc restent des chaines", () => {
    // Le garde ci-dessus ne doit rien changer au parc réel.
    for (const brut of tousLesContrats()) {
      const { uiSchema, notices } = compileUiSchema(brut.input, { capacite: brut.id });
      expect(notices, brut.id).toHaveLength(0);
      for (const [nom, champ] of Object.entries(brut.input.properties)) {
        if (champ["x-ui"] !== "file") continue;
        expect((uiSchema[nom] as Record<string, unknown>)["ui:widget"], nom).toBe(CHEMIN_LOCAL);
      }
    }
  });
});
