/**
 * Le rangement du sélecteur, et la garde qui l'empêche de vieillir.
 *
 * La catégorie est la seule chose de cet écran qui ne se déduit pas d'un
 * contrat : `image-detect` et `face-detect` prennent une image et rendent des
 * boîtes, et pourtant on ne les cherche pas dans le même geste. Une table écrite
 * à la main vieillit — d'où le test qui refuse qu'une capacité du registre n'y
 * soit pas, et qui fera rougir la CI le lendemain du jour où l'on en ajoute une.
 */

import { describe, expect, test } from "vitest";
import capacités from "../api/__fixtures__/capabilities.json";
import type { Capability } from "../api/types";
import { etatCapacite } from "../schema/etat";
import { entreePrincipale, sortiePrincipale } from "../schema/modalites";
import { CAPACITES_RANGEES, categorieDe, formeEntree, formeSortie, sections } from "./catalogue";

const CAPACITES = (capacités as unknown as { capabilities: Capability[] }).capabilities;

describe("la table des catégories", () => {
  test("chaque capacite du registre est rangee", () => {
    const orphelines = CAPACITES.filter((c) => categorieDe(c.id) === "divers").map((c) => c.id);
    expect(
      orphelines,
      "capacités sans catégorie : les ajouter à PAR_CAPACITE dans catalogue.ts",
    ).toEqual([]);
  });

  test("une capacite inconnue tombe dans Divers plutot que de disparaitre", () => {
    // Le jour où un contrat arrive sans que cette table le sache, il doit
    // s'afficher quand même : on ne cache jamais ce que le registre déclare.
    expect(categorieDe("quelque-chose-de-neuf")).toBe("divers");
  });

  test("la table ne range rien qui n_existe pas", () => {
    const connus = new Set(CAPACITES.map((c) => c.id));
    // `text-to-mesh` est la composite du §11, déclarée à la conception et pas
    // encore au registre : elle est tolérée, tout le reste doit exister.
    const fantômes = CAPACITES_RANGEES.filter((id) => !connus.has(id) && id !== "text-to-mesh");
    expect(fantômes).toEqual([]);
  });

  test("les six capacites du visage sont ensemble", () => {
    const visage = CAPACITES.filter((c) => categorieDe(c.id) === "visage").map((c) => c.id);
    expect(visage.sort()).toEqual([
      "face-detect",
      "face-embed",
      "face-gaze",
      "face-headpose",
      "face-landmark",
      "face-parse",
    ]);
  });
});

describe("les sections", () => {
  test("le texte ouvre, et les familles vides ne s_affichent pas", () => {
    const groupes = sections(CAPACITES, etatCapacite);
    expect(groupes[0]!.categorie.id).toBe("texte");
    expect(groupes.every((g) => g.capacites.length > 0)).toBe(true);
  });

  test("dans une famille, l_executable passe devant", () => {
    const bloquée = { ...CAPACITES.find((c) => c.id === "text-to-speech")!, ready_variants: [] };
    const prête = CAPACITES.find((c) => c.id === "translation")!;
    const groupes = sections([bloquée, prête], etatCapacite);
    const rangées = groupes.flatMap((g) => g.capacites.map((c) => c.id));
    expect(rangées.indexOf("translation")).toBeLessThan(rangées.indexOf("text-to-speech"));
  });

  test("aucune capacite n_est perdue entre l_entree et l_affichage", () => {
    const groupes = sections(CAPACITES, etatCapacite);
    const total = groupes.reduce((n, g) => n + g.capacites.length, 0);
    expect(total).toBe(CAPACITES.length);
  });
});

describe("les formes de la glyphe", () => {
  test("le visage se dessine en visage, pas en image", () => {
    // Les six capacités du visage prennent une image, et le dire ainsi serait
    // exact et inutile : c'est ce qu'elles regardent dans l'image qui les
    // distingue d'`image-detect`.
    const detect = CAPACITES.find((c) => c.id === "face-detect")!;
    expect(formeEntree("face-detect", entreePrincipale(detect))).toBe("visage");
    const objets = CAPACITES.find((c) => c.id === "image-detect")!;
    expect(formeEntree("image-detect", entreePrincipale(objets))).toBe("image");
  });

  test("quatre capacites qui rendent du JSON ne se dessinent pas pareil", () => {
    // Sans la table fine, elles auraient toutes la glyphe « données » et le
    // sélecteur cesserait de dire quoi que ce soit.
    const formes = ["face-detect", "face-landmark", "face-embed", "face-headpose"].map((id) =>
      formeSortie(id, sortiePrincipale(CAPACITES.find((c) => c.id === id)!)),
    );
    expect(new Set(formes).size).toBe(4);
  });

  test("une sortie sans finesse retombe sur sa modalite", () => {
    expect(formeSortie("text-to-image", "image")).toBe("image");
  });
});
