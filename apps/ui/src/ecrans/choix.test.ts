/**
 * Les deux choix de l'Atelier, éprouvés sur le parc réel.
 *
 * Les capacités et les modèles viennent des fixtures capturées par
 * `tools/ui_fixtures.py` : ce sont les octets que le serveur envoie, avec ses
 * dix-sept contrats dont sept sans modèle, son titulaire de `text-to-speech` et
 * son `image-to-mesh` qui affiche un titulaire sans rien d'exécutable.
 */

import { describe, expect, test } from "vitest";
import capacités from "../api/__fixtures__/capabilities.json";
import modèles from "../api/__fixtures__/models.json";
import type { Capability, Model } from "../api/types";
import { groupesDeCapacites, groupesDeVariants, variantParDefaut } from "./choix";

const CAPACITES = (capacités as unknown as { capabilities: Capability[] }).capabilities;
const MODELES = (modèles as unknown as { models: Model[] }).models;

function capacité(id: string): Capability {
  const c = CAPACITES.find((x) => x.id === id);
  if (!c) throw new Error(`capacité absente de la fixture : ${id}`);
  return c;
}

function modèlesDe(capability: string): Model[] {
  return MODELES.filter((m) => m.capability === capability);
}

describe("les capacités groupées par état", () => {
  test("les executables viennent en premier", () => {
    const groupes = groupesDeCapacites(CAPACITES);
    expect(groupes[0]!.etat).toBe("prête");
    expect(groupes[0]!.capacites.map((c) => c.id)).toContain("text-to-speech");
  });

  test("aucune capacite n_est perdue en chemin", () => {
    // Une capacité sans modèle reste dans la liste : elle dit ce que le parc
    // pourrait faire et ne fait pas encore, ce qui est la moitié de ce qu'un
    // registre sert à savoir.
    const groupes = groupesDeCapacites(CAPACITES);
    const total = groupes.reduce((n, g) => n + g.capacites.length, 0);
    expect(total).toBe(CAPACITES.length);
    expect(groupes.map((g) => g.etat)).toContain("sans-modèle");
  });

  test("image-to-mesh a ses propres mots", () => {
    // Elle a deux modèles déclarés, un titulaire, et rien de lançable : la
    // ranger avec les capacités qu'on n'a jamais pourvues afficherait la même
    // phrase pour deux situations dont l'une est à un `ecurie pull` de marcher.
    const groupes = groupesDeCapacites(CAPACITES);
    const sansVariant = groupes.find((g) => g.etat === "sans-variant-prêt")!;
    expect(sansVariant.capacites.map((c) => c.id)).toEqual(["image-to-mesh"]);
  });

  test("un groupe vide ne s_affiche pas", () => {
    const groupes = groupesDeCapacites([capacité("text-to-speech")]);
    expect(groupes).toHaveLength(1);
  });
});

describe("les variants groupés par modèle", () => {
  test("le titulaire passe devant", () => {
    // `image-to-mesh` porte deux modèles, dont le titulaire est le second par
    // ordre alphabétique : c'est le seul cas du parc qui prouve le tri.
    const groupes = groupesDeVariants(modèlesDe("image-to-mesh"), "hunyuan3d-2.1-shape-mlx");
    expect(groupes.map((g) => g.modele)).toEqual(["hunyuan3d-2.1-shape-mlx", "trellis2"]);
    expect(groupes[0]!.titulaire).toBe(true);
  });

  test("sans titulaire, l_ordre du serveur est conserve", () => {
    const groupes = groupesDeVariants(modèlesDe("image-to-mesh"), null);
    expect(groupes.map((g) => g.modele)).toEqual(["hunyuan3d-2.1-shape-mlx", "trellis2"]);
    expect(groupes.every((g) => !g.titulaire)).toBe(true);
  });

  test("les deux variants d_un meme modele restent ensemble", () => {
    const groupes = groupesDeVariants(modèlesDe("image-upscale"), null);
    expect(groupes).toHaveLength(1);
    expect(groupes[0]!.variants.map((v) => v.id)).toEqual(["classique-x2", "reel-x4"]);
  });
});

describe("le variant préselectionné", () => {
  test("le titulaire d_abord", () => {
    expect(variantParDefaut(capacité("text-to-speech"))).toBe("qwen3-tts-1.7b@8bit-mlx");
  });

  test("a defaut, le premier executable", () => {
    // `image-upscale` n'a pas de titulaire et deux variants prêts.
    expect(variantParDefaut(capacité("image-upscale"))).toBe("swin2sr@classique-x2");
  });

  test("jamais un variant qui ne peut pas tourner", () => {
    // `image-to-mesh` affiche un titulaire dont les 7,37 Go de poids ne sont pas
    // téléchargés : le préselectionner ouvrirait l'écran sur un formulaire dont
    // rien ne peut sortir.
    expect(variantParDefaut(capacité("image-to-mesh"))).toBeNull();
  });

  test("un titulaire declare mais non pret cede la place a un autre", () => {
    expect(
      variantParDefaut({ incumbent: "absent", ready_variants: ["autre@v1", "encore@v2"] }),
    ).toBe("autre@v1");
  });

  test("aucun modele, aucun choix", () => {
    expect(variantParDefaut(capacité("audio-denoise"))).toBeNull();
  });
});
