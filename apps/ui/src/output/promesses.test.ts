/**
 * Ce qu'une capacité promet de produire, éprouvé sur les dix-sept contrats du disque.
 *
 * Les contrats sont lus sur le disque au moment du test, comme pour les
 * formulaires : un dix-huitième entre dans la suite tout seul. La propriété
 * qu'on vérifie contrat par contrat est celle qui coûterait cher à rater —
 * **aucun fichier de sortie déclaré ne manque à l'appel**, y compris quand il
 * est imbriqué.
 */

import { describe, expect, test } from "vitest";
import { commeCapability, contrat, tousLesContrats } from "../schema/__fixtures__/contrats";
import { promessesDeSortie } from "./promesses";

describe("les promesses de sortie du parc", () => {
  test.each(tousLesContrats().map((c) => [c.id, c] as const))(
    "%s annonce chacun de ses fichiers",
    (_id, brut) => {
      const capability = commeCapability(brut);
      const promesses = promessesDeSortie(capability);
      const fichiers = promesses.filter((p) => p.mediaType !== null).map((p) => p.chemin);
      expect(fichiers.sort()).toEqual(Object.keys(capability.output_media_types).sort());
    },
  );

  test.each(tousLesContrats().map((c) => [c.id, c] as const))(
    "%s ne perd aucune sortie scalaire",
    (_id, brut) => {
      // Les sorties qui ne sont pas des fichiers comptent aussi : `page_count`,
      // `detected_source_language`, `finish_reason`. Les omettre laisserait
      // croire qu'un OCR ne rend qu'un fichier de texte.
      const promesses = promessesDeSortie(commeCapability(brut));
      const racine = Object.keys(brut.output.properties ?? {});
      const vues = new Set(promesses.map((p) => p.chemin.split(".")[0]));
      expect([...vues].sort()).toEqual(racine.sort());
    },
  );
});

describe("les cas que le parc impose", () => {
  test("les sorties imbriquees sont annoncees une par une", () => {
    // `audio-separation` est la seule capacité à sorties imbriquées : ses cinq
    // pistes vivent sous `tracks.*`, et s'arrêter au premier niveau annoncerait
    // « tracks » comme un fichier unique.
    const promesses = promessesDeSortie(commeCapability(contrat("audio-separation")));
    expect(promesses.map((p) => p.chemin)).toContain("tracks.vocals");
    expect(promesses.map((p) => p.chemin)).not.toContain("tracks");
    expect(promesses.filter((p) => p.mediaType === "audio/wav")).toHaveLength(5);
  });

  test("le requis est celui du niveau ou il est declare", () => {
    // `audio-separation` déclare `tracks` requis à la racine et `vocals` requis
    // à l'intérieur : la voix sort d'une séparation en deux pistes comme d'une
    // séparation en quatre, la basse non. Lire le `required` de la racine pour
    // tout l'arbre promettrait un fichier de basse qui n'existera pas.
    const promesses = promessesDeSortie(commeCapability(contrat("audio-separation")));
    const requises = promesses.filter((p) => p.requis).map((p) => p.chemin);
    expect(requises).toEqual(["tracks.vocals"]);

    const tts = promessesDeSortie(commeCapability(contrat("text-to-speech")));
    expect(tts.find((p) => p.nom === "audio")!.requis).toBe(true);
  });

  test("un json requis est annonce comme fichier", () => {
    // `tool-use.calls` est une sortie **requise** de type `application/json` :
    // c'est la ligne que la conception avait oubliée dans sa table.
    const promesses = promessesDeSortie(commeCapability(contrat("tool-use")));
    const calls = promesses.find((p) => p.nom === "calls")!;
    expect(calls.mediaType).toBe("application/json");
    expect(calls.requis).toBe(true);
  });

  test("la description du contrat est reprise telle quelle", () => {
    // Elle est en français dans le JSON, et c'est là sa bonne place : une table
    // de traduction dans le front dirait autre chose que le registre.
    const promesses = promessesDeSortie(commeCapability(contrat("text-to-speech")));
    expect(promesses[0]!.description).toContain("relatif au dossier du job");
  });

  test("un contrat sans sortie ne casse pas", () => {
    expect(promessesDeSortie({ output: {}, output_media_types: {} })).toEqual([]);
  });
});
