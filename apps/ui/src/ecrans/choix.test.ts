/**
 * Les deux choix de l'Atelier, éprouvés sur le parc réel.
 *
 * Les capacités et les modèles viennent des fixtures capturées par
 * `tools/ui_fixtures.py` : ce sont les octets que le serveur envoie, avec ses
 * vingt-cinq contrats désormais tous pourvus d'au moins un modèle, son titulaire
 * de `text-to-speech` et son `image-to-mesh` qui affiche un titulaire sans rien
 * d'exécutable.
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

/** La même capacité, mais dont plus aucun variant n'est exécutable. */
function bloquée(c: Capability): Capability {
  return { ...c, ready_variants: [] };
}

describe("les capacités groupées par état", () => {
  test("les executables viennent en premier", () => {
    const groupes = groupesDeCapacites(CAPACITES);
    expect(groupes[0]!.etat).toBe("prête");
    expect(groupes[0]!.capacites.map((c) => c.id)).toContain("text-to-speech");
  });

  test("aucune capacite n_est perdue en chemin", () => {
    // Une capacité qui ne peut pas tourner reste dans la liste : elle dit ce que
    // le parc pourrait faire et ne fait pas encore, ce qui est la moitié de ce
    // qu'un registre sert à savoir.
    const groupes = groupesDeCapacites(CAPACITES);
    const total = groupes.reduce((n, g) => n + g.capacites.length, 0);
    expect(total).toBe(CAPACITES.length);
  });

  test("le groupe des capacites sans modele a disparu du parc", () => {
    // Il n'est pas retiré du code — un contrat s'ajoute avant son modèle — mais
    // il n'a plus rien à contenir, et un groupe vide ne s'affiche pas.
    const groupes = groupesDeCapacites(CAPACITES);
    expect(groupes.map((g) => g.etat)).not.toContain("sans-modèle");
  });

  test("une capacite pourvue mais rien de telecharge a ses propres mots", () => {
    // Des modèles déclarés, un titulaire, et rien de lançable : la ranger avec
    // les capacités qu'on n'a jamais pourvues afficherait la même phrase pour
    // deux situations dont l'une est à un `ecurie pull` de marcher.
    //
    // Le cas est fabriqué, non cueilli : la version d'avant prenait
    // `image-to-mesh` dans les fixtures et est tombée le jour où quelqu'un a
    // téléchargé Hunyuan3D. Un test qui dépend du disque d'un poste ne dit pas
    // ce qu'il prétend dire.
    const groupes = groupesDeCapacites([bloquée(capacité("image-to-mesh"))]);
    const sansVariant = groupes.find((g) => g.etat === "sans-variant-prêt")!;
    expect(sansVariant.capacites.map((c) => c.id)).toContain("image-to-mesh");
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
    // `image-upscale` a deux modèles depuis SeedVR2 : on vise swin2sr, le seul
    // des deux qui ait plusieurs variants, puisque c'est le groupement qu'on
    // éprouve ici et non le nombre de modèles de la capacité.
    const groupes = groupesDeVariants(modèlesDe("image-upscale"), null);
    const swin = groupes.find((g) => g.modele === "swin2sr");
    expect(swin).toBeDefined();
    expect(swin!.variants.map((v) => v.id)).toEqual(["classique-x2", "reel-x4"]);
  });
});

describe("le variant préselectionné", () => {
  test("le titulaire d_abord", () => {
    expect(variantParDefaut(capacité("text-to-speech"))).toBe("qwen3-tts-1.7b@8bit-mlx");
  });

  test("a defaut, le premier executable", () => {
    // `image-upscale` n'a pas de titulaire et deux variants prêts.
    // Le premier exécutable de la capacité, dans l'ordre du registre.
    expect(variantParDefaut(capacité("image-upscale"))).toBe("seedvr2-3b@fp16");
  });

  test("jamais un variant qui ne peut pas tourner", () => {
    // Une capacité qui affiche un titulaire dont les poids ne sont pas
    // téléchargés : le préselectionner ouvrirait l'écran sur un formulaire dont
    // rien ne peut sortir.
    expect(variantParDefaut(bloquée(capacité("image-to-mesh")))).toBeNull();
  });

  test("un titulaire declare mais non pret cede la place a un autre", () => {
    expect(
      variantParDefaut({ incumbent: "absent", ready_variants: ["autre@v1", "encore@v2"] }),
    ).toBe("autre@v1");
  });

  test("un modele declare mais rien de telecharge, aucun choix", () => {
    // `audio-denoise` a désormais son manifeste ; ce que la préselection lit est
    // `ready_variants`, et lui seul. Un modèle au registre n'est pas un variant
    // exécutable.
    const denoise = capacité("audio-denoise");
    expect(denoise.models.length).toBeGreaterThan(0);
    expect(variantParDefaut(denoise)).toBeNull();
  });
});
