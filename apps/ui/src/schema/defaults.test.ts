/** La fusion des défauts, comparée à celle du serveur sur tous les variants. */

import { describe, expect, test } from "vitest";
import fusionnés from "../api/__fixtures__/merged_defaults.json";
import type { Capability, Variant } from "../api/types";
import { commeCapability, contrat, idsContrats } from "./__fixtures__/contrats";
import { initialValues } from "./defaults";

interface Fusion {
  capability: string;
  variant_defaults: Record<string, unknown>;
  merged: Record<string, unknown>;
}

const CAS = fusionnés as unknown as Record<string, Fusion>;

describe("les valeurs de départ d'un formulaire", () => {
  test("la fusion egale celle du serveur pour tous les variants du parc", () => {
    // `merge_defaults` du serveur est recopié côté front, parce que le
    // formulaire doit s'afficher rempli avant toute question au serveur. Ce test
    // est ce qui rend la recopie tenable : si l'ordre de fusion change côté
    // Python, la suite du front le dit.
    const refs = Object.keys(CAS);
    expect(refs.length).toBeGreaterThanOrEqual(13);

    const connues = idsContrats();
    let comparés = 0;

    for (const ref of refs) {
      const cas = CAS[ref]!;
      // Pas de `continue` silencieux : une capacité absente du disque veut dire
      // que la fixture a vieilli, et sauter le cas ferait tourner à vide la
      // comparaison la plus importante du socle sans que rien n'échoue.
      expect(connues, `${ref} : capacité ${cas.capability} absente du registre`).toContain(
        cas.capability,
      );
      comparés += 1;
      const capability = commeCapability(contrat(cas.capability));
      const variant = { defaults: cas.variant_defaults } as unknown as Variant;

      expect(initialValues(capability, variant), ref).toEqual(cas.merged);
    }
    expect(comparés).toBe(refs.length);
  });

  test("le variant corrige le contrat et non l_inverse", () => {
    // qwen25-coder déclare temperature: 0.2 là où text-generation propose 0.7 :
    // un modèle de code n'échantillonne pas comme un modèle de prose. Montrer
    // 0.7 afficherait une valeur qui ne partira pas.
    const cas = CAS["qwen25-coder-7b@4bit"];
    if (!cas) return;
    expect(cas.merged["temperature"]).toBe(0.2);
    expect(contrat("text-generation").input.properties["temperature"]!.default).toBe(0.7);
  });

  test("un defaut du variant hors contrat est ignore", () => {
    // Ce que le manifeste appelle `options` est un réglage propre au moteur,
    // transporté à part dans `params` côté serveur. L'injecter ferait partir un
    // paramètre que le contrat ne déclare pas, et le serveur le refuserait.
    const capability = commeCapability(contrat("text-to-speech"));
    const variant = {
      defaults: { voice: "serena", reglage_moteur: 3 },
    } as unknown as Variant;

    const départ = initialValues(capability, variant);
    expect(départ["voice"]).toBe("serena");
    expect("reglage_moteur" in départ).toBe(false);
  });

  test("sans variant seuls les defauts du contrat sont poses", () => {
    const capability = commeCapability(contrat("text-to-speech"));
    expect(initialValues(capability, null)).toEqual({ speed: 1.0 });
  });

  test("aucune valeur n_est inventee", () => {
    // Un champ borné sans `default` reste absent : threshold à 0 binariserait le
    // masque, un seed à 0 figerait l'aléa.
    for (const id of idsContrats()) {
      const brut = contrat(id);
      const capability: Capability = commeCapability(brut);
      const départ = initialValues(capability, null);
      for (const nom of Object.keys(départ)) {
        expect(brut.input.properties[nom]!.default, `${id}.${nom}`).not.toBeUndefined();
      }
    }
  });
});
