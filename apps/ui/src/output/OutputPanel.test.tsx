/** L'aplatissement d'une sortie, et le contrat imposé à tout visualiseur. */

import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { flattenOutput } from "./flatten";
import { OutputPanel } from "./OutputPanel";
import { VIEWERS, VIEWER_INCONNU, VIEWER_INLINE } from "./registry";

const PISTES = {
  "tracks.vocals": "audio/wav",
  "tracks.drums": "audio/wav",
  "tracks.bass": "audio/wav",
  "tracks.piano": "audio/wav",
  "tracks.other": "audio/wav",
};

describe("l'aplatissement d'une sortie", () => {
  test("les pistes absentes ne sont pas affichees", () => {
    // audio-separation déclare cinq pistes et n'en produit que deux ou quatre
    // selon `stems`. Itérer le contrat afficherait trois lecteurs pointant vers
    // des fichiers qui n'existent pas.
    const réponse = { tracks: { vocals: "vocals.wav", other: "other.wav" } };
    const rendus = flattenOutput(réponse, PISTES);
    expect(rendus.map((r) => r.chemin)).toEqual(["tracks.vocals", "tracks.other"]);
  });

  test("le chemin pointe retrouve son type media", () => {
    // Les clés d'output_media_types sont des chemins pointés, pas des noms de
    // premier niveau : le serveur descend dans les sorties de type objet.
    const rendus = flattenOutput({ tracks: { vocals: "vocals.wav" } }, PISTES);
    expect(rendus[0]!.mediaType).toBe("audio/wav");
    expect(rendus[0]!.nom).toBe("vocals");
  });

  test("une sortie hors schema est rendue quand meme", () => {
    // Le méta-schéma n'impose additionalProperties: false qu'en ENTRÉE : une
    // sortie supplémentaire est une information, pas une faute.
    const rendus = flattenOutput({ text: "t.txt", surprise: 3 }, { text: "text/plain" });
    expect(rendus.map((r) => r.chemin).sort()).toEqual(["surprise", "text"]);
    expect(rendus.find((r) => r.chemin === "surprise")!.mediaType).toBeNull();
  });

  test("une valeur structuree sans type media est aplatie et non perdue", () => {
    const rendus = flattenOutput({ metrics: { wer: 0.12 } }, {});
    expect(rendus.map((r) => r.chemin)).toEqual(["metrics.wer"]);
  });

  test("une liste reste une valeur unique", () => {
    // tool-use.call_names est un tableau de chaînes : une valeur inline, pas
    // cinq sorties.
    const rendus = flattenOutput({ call_names: ["a", "b"] }, {});
    expect(rendus).toHaveLength(1);
    expect(rendus[0]!.valeur).toEqual(["a", "b"]);
  });

  test("une sortie absente ne rend rien", () => {
    expect(flattenOutput(null, {})).toEqual([]);
  });
});

describe("le panneau de sortie", () => {
  test("un visualiseur sans url affiche le chemin", () => {
    // href: null est l'état NORMAL du 4.3 : aucune route ne sert les fichiers de
    // job. Chacun des visualiseurs doit savoir le rendre, faute de quoi il
    // faudrait tous les rouvrir à la tâche 4.4.
    for (const entrée of VIEWERS) {
      const Vue = entrée.Component;
      const { unmount } = render(
        <Vue nom="sortie" chemin="sortie" valeur="fichier.bin" mediaType="x/y" href={null} />,
      );
      expect(screen.getByText("fichier.bin"), entrée.id).toBeInTheDocument();
      expect(screen.getByText(/fichier non résolu/), entrée.id).toBeInTheDocument();
      unmount();
    }
  });

  test("le repli reste visible et nomme le type", () => {
    const Vue = VIEWER_INCONNU.Component;
    render(
      <Vue nom="truc" chemin="truc" valeur="truc.xyz" mediaType="application/x-inconnu" href={null} />,
    );
    expect(screen.getByText("application/x-inconnu")).toBeInTheDocument();
    expect(screen.getByText(/aucun visualiseur pour ce type/)).toBeInTheDocument();
  });

  test("une valeur inline s_affiche sans cadre de fichier", () => {
    const Vue = VIEWER_INLINE.Component;
    render(<Vue nom="page_count" chemin="page_count" valeur={4} mediaType={null} href={null} />);
    expect(screen.getByText("4")).toBeInTheDocument();
    expect(screen.queryByText(/fichier non résolu/)).not.toBeInTheDocument();
  });

  test("le panneau aiguille chaque sortie vers son visualiseur", () => {
    render(
      <OutputPanel
        sortie={{ text: "t.txt", layout: "l.json", page_count: 4 }}
        mediaTypes={{ text: "text/plain", layout: "application/json" }}
      />,
    );
    expect(document.querySelector('[data-viewer="text"]')).toBeTruthy();
    expect(document.querySelector('[data-viewer="json"]')).toBeTruthy();
    expect(document.querySelector('[data-viewer="inline"]')).toBeTruthy();
  });

  test("un job sans sortie le dit", () => {
    render(<OutputPanel sortie={null} mediaTypes={{}} />);
    expect(screen.getByText("aucune sortie")).toBeInTheDocument();
  });
});
