/**
 * Ce qu'une capacité prend et rend, éprouvé sur les contrats réels.
 *
 * Les filtres du sélecteur en dépendent entièrement : si la déduction se trompe,
 * une capacité disparaît d'un filtre qui devrait la montrer, et rien ne le dit.
 */

import { describe, expect, test } from "vitest";
import capacités from "../api/__fixtures__/capabilities.json";
import type { Capability } from "../api/types";
import {
  entreePrincipale,
  modaliteDuMedia,
  modalitesEntree,
  modalitesSortie,
  sortiePrincipale,
} from "./modalites";

const CAPACITES = (capacités as unknown as { capabilities: Capability[] }).capabilities;

function capacité(id: string): Capability {
  const c = CAPACITES.find((x) => x.id === id);
  if (!c) throw new Error(`capacité absente de la fixture : ${id}`);
  return c;
}

describe("le type de média vers la modalité", () => {
  test("le PDF l_emporte sur l_image quand les deux sont acceptés", () => {
    // `document-to-text` déclare « application/pdf,image/* », qui est la graphie
    // de l'attribut `accept` d'un `<input type="file">`. Une capacité qui prend
    // les deux est une capacité de document.
    expect(modaliteDuMedia("application/pdf,image/*")).toBe("document");
  });

  test("les familles usuelles se reconnaissent", () => {
    expect(modaliteDuMedia("image/png")).toBe("image");
    expect(modaliteDuMedia("audio/wav")).toBe("son");
    expect(modaliteDuMedia("video/mp4")).toBe("video");
    expect(modaliteDuMedia("model/gltf-binary")).toBe("maillage");
    expect(modaliteDuMedia("application/json")).toBe("donnees");
    expect(modaliteDuMedia("text/plain")).toBe("texte");
  });

  test("un type inconnu ne devine rien", () => {
    expect(modaliteDuMedia("application/x-inconnu")).toBeNull();
  });
});

describe("l'entrée principale", () => {
  test("c_est celle du champ obligatoire, pas de tous les champs", () => {
    // `image-to-text` prend une image et, facultativement, une question. Les
    // deux comptent pour le filtre ; une seule décrit ce qu'elle fait, sans quoi
    // sa glyphe dirait « texte vers texte ».
    expect(entreePrincipale(capacité("image-to-text"))).toBe("image");
    expect(modalitesEntree(capacité("image-to-text"))).toContain("texte");
  });

  test("une capacite qui ne prend que des mots prend du texte", () => {
    expect(entreePrincipale(capacité("text-generation"))).toBe("texte");
    expect(entreePrincipale(capacité("tool-use"))).toBe("texte");
  });

  test("un document est un document, pas une image", () => {
    expect(entreePrincipale(capacité("document-to-text"))).toBe("document");
  });

  test.each([
    ["speech-to-text", "son"],
    ["video-to-motion", "video"],
    ["image-to-mesh", "image"],
    ["face-embed", "image"],
  ])("%s prend du %s", (id, attendu) => {
    expect(entreePrincipale(capacité(id))).toBe(attendu);
  });
});

describe("la sortie principale", () => {
  test("c_est la sortie exigée, pas la plus visible", () => {
    // `video-to-motion` rend des trajectoires en JSON et, si on le demande, une
    // vidéo de contrôle. La décrire comme « rend une vidéo » serait l'inverse de
    // ce qu'elle fait.
    expect(sortiePrincipale(capacité("video-to-motion"))).toBe("donnees");
  });

  test("une sortie exigee qui en contient d_autres est suivie", () => {
    // `audio-separation` exige `tracks`, et les cinq pistes vivent dessous :
    // chercher la clé exacte ne trouverait rien.
    expect(sortiePrincipale(capacité("audio-separation"))).toBe("son");
  });

  test.each([
    ["text-to-speech", "son"],
    ["text-to-image", "image"],
    ["image-to-mesh", "maillage"],
    ["face-embed", "donnees"],
    ["translation", "texte"],
  ])("%s rend du %s", (id, attendu) => {
    expect(sortiePrincipale(capacité(id))).toBe(attendu);
  });
});

describe("toutes les modalités d'une capacité", () => {
  test("un champ fichier sans type declare ne devine pas sa modalite", () => {
    // Deviner « image » parce que c'est le cas fréquent ferait mentir un filtre.
    expect([...modalitesEntree({ input: { properties: { f: { "x-ui": "file" } } } })]).toEqual([]);
  });

  test("chaque capacite du parc sait au moins recevoir et rendre quelque chose", () => {
    for (const c of CAPACITES) {
      expect(modalitesEntree(c).size, `entrées de ${c.id}`).toBeGreaterThan(0);
      expect(modalitesSortie(c).size, `sorties de ${c.id}`).toBeGreaterThan(0);
    }
  });
});
