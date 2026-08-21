/** La table d'aiguillage des visualiseurs, confrontée aux sorties du parc. */

import { describe, expect, test } from "vitest";
import capacitésServies from "../api/__fixtures__/capabilities.json";
import type { Capability } from "../api/types";
import { mediaTypesDe, tousLesContrats } from "../schema/__fixtures__/contrats";
import { famille, matches, normalize } from "./mediaType";
import { VIEWERS, VIEWER_INCONNU, VIEWER_INLINE, viewerFor } from "./registry";

describe("la comparaison de types de média", () => {
  test("la casse et les parametres sont ignores", () => {
    expect(normalize("Text/Plain; charset=utf-8")).toBe("text/plain");
    expect(matches("Text/Plain; charset=utf-8", "text/*")).toBe(true);
  });

  test("une famille couvre ses types", () => {
    expect(matches("audio/wav", "audio/*")).toBe(true);
    expect(matches("image/png", "audio/*")).toBe(false);
  });

  test("un type exact ne couvre que lui", () => {
    expect(matches("model/gltf-binary", "model/gltf-binary")).toBe(true);
    expect(matches("model/gltf+json", "model/gltf-binary")).toBe(false);
  });

  test("la famille se lit", () => {
    expect(famille("audio/wav")).toBe("audio");
  });
});

describe("les chemins de sortie du contrat", () => {
  test("notre recopie egale les chemins que le serveur produit", () => {
    // `mediaTypesDe` recopie `output_media_types()` de capabilities.py, pour que
    // les tests n'aient pas à passer par le serveur. La recopie n'a de valeur
    // que confrontée : ce test compare, contrat par contrat, à ce que le serveur
    // a réellement produit sur le vrai registre.
    const servies = (capacitésServies as unknown as { capabilities: Capability[] }).capabilities;
    expect(servies.length).toBeGreaterThanOrEqual(17);

    for (const servie of servies) {
      const local = mediaTypesDe(tousLesContrats().find((c) => c.id === servie.id)!);
      expect(local, servie.id).toEqual(servie.output_media_types);
    }
  });
});

describe("l'aiguillage des visualiseurs", () => {
  test("les media types du parc ont tous un visualiseur", () => {
    // Ce test échoue si l'on n'implémente que les cinq lignes de CONCEPTION §7 :
    // application/json y manque, alors que trois contrats en produisent et que
    // tool-use.calls est une sortie REQUISE.
    const types = new Set<string>();
    for (const brut of tousLesContrats()) {
      for (const t of Object.values(mediaTypesDe(brut))) types.add(t);
    }
    expect(types.size).toBeGreaterThan(0);

    for (const type of types) {
      const entrée = viewerFor(type);
      expect(entrée.id, `${type} tombe sur le repli`).not.toBe(VIEWER_INCONNU.id);
    }
  });

  test("application_json a bien son visualiseur", () => {
    expect(viewerFor("application/json").id).toBe("json");
  });

  test("un media type inconnu reste visible", () => {
    // Une zone blanche est le seul échec que l'utilisateur ne peut pas
    // diagnostiquer : viewerFor ne rend jamais undefined.
    const entrée = viewerFor("application/x-inconnu");
    expect(entrée).toBeTruthy();
    expect(entrée.id).toBe(VIEWER_INCONNU.id);
  });

  test("une valeur sans type media va au visualiseur inline", () => {
    // page_count, call_names et finish_reason ne sont pas des fichiers, et rien
    // dans leur nom ne le dit : c'est l'absence de contentMediaType qui tranche.
    expect(viewerFor(null).id).toBe(VIEWER_INLINE.id);
  });

  test("chaque entree de la table a un composant", () => {
    for (const entrée of [...VIEWERS, VIEWER_INLINE, VIEWER_INCONNU]) {
      expect(typeof entrée.Component, entrée.id).toBe("function");
    }
  });

  test("l_ordre va du precis au general", () => {
    // model/gltf-binary est un type exact ; s'il passait après une famille trop
    // large, il ne serait jamais atteint.
    expect(viewerFor("model/gltf-binary").id).toBe("mesh");
    expect(viewerFor("audio/wav").id).toBe("audio");
    expect(viewerFor("text/plain").id).toBe("text");
    expect(viewerFor("image/png").id).toBe("image");
    expect(viewerFor("video/mp4").id).toBe("video");
  });
});
