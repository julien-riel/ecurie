/**
 * La lecture d'une sortie textuelle, avec une URL — l'état du 4.4.
 *
 * Ces tests passent un `href`, ce qu'aucun autre ne fait : au 4.3 le résolveur
 * rend toujours `null`, si bien que la branche de lecture ne serait jamais
 * exécutée. Un point d'injection qu'on déclare prêt sans l'avoir fait tourner
 * est exactement ce que le v0.3 a appris à ne pas croire.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { repond } from "../../../vitest.setup";
import { JsonViewer } from "./JsonViewer";
import { TextViewer } from "./TextViewer";

const URL_TEXTE = "http://127.0.0.1:8765/jobs/abc/files/t.txt";
const URL_JSON = "http://127.0.0.1:8765/jobs/abc/files/l.json";

describe("la lecture d'une sortie textuelle", () => {
  test("le contenu du fichier s_affiche et non son chemin", async () => {
    repond("/jobs/abc/files/t.txt", { texte: "la transcription", type: "text/plain" });
    render(
      <TextViewer nom="text" chemin="text" valeur="t.txt" mediaType="text/plain" href={URL_TEXTE} />,
    );
    await waitFor(() => expect(screen.getByText("la transcription")).toBeInTheDocument());
    expect(screen.queryByText("t.txt")).not.toBeInTheDocument();
  });

  test("le json est reindente quand il est lisible", async () => {
    repond("/jobs/abc/files/l.json", { texte: '{"blocs":[1,2]}' });
    render(
      <JsonViewer
        nom="layout"
        chemin="layout"
        valeur="l.json"
        mediaType="application/json"
        href={URL_JSON}
      />,
    );
    await waitFor(() =>
      expect(document.querySelector('[data-viewer="json"]')!.textContent).toContain('"blocs"'),
    );
    expect(document.querySelector('[data-viewer="json"]')!.textContent).toContain("\n");
  });

  test("un json tronque est rendu brut plutot qu_ecrase par un message", async () => {
    repond("/jobs/abc/files/l.json", { texte: '{"blocs":[1,' });
    render(
      <JsonViewer
        nom="layout"
        chemin="layout"
        valeur="l.json"
        mediaType="application/json"
        href={URL_JSON}
      />,
    );
    await waitFor(() =>
      expect(document.querySelector('[data-viewer="json"]')!.textContent).toBe('{"blocs":[1,'),
    );
  });

  test("une lecture impossible se dit", async () => {
    repond("/jobs/abc/files/t.txt", { status: 404, texte: "" });
    render(
      <TextViewer nom="text" chemin="text" valeur="t.txt" mediaType="text/plain" href={URL_TEXTE} />,
    );
    await waitFor(() => expect(screen.getByText(/lecture impossible/)).toBeInTheDocument());
  });

  test("un href qui redevient nul ne laisse pas lecture en cours", async () => {
    // Défaut trouvé en revue : la branche du href nul sortait sans remettre
    // `en_cours` à faux, et le `finally` de la requête précédente était déjà
    // neutralisé par le nettoyage de l'effet. L'écran affichait « lecture… »
    // pour toujours, juste à côté de « fichier non résolu ».
    repond("/jobs/abc/files/t.txt", { texte: "contenu" });
    const { rerender } = render(
      <TextViewer nom="text" chemin="text" valeur="t.txt" mediaType="text/plain" href={URL_TEXTE} />,
    );
    rerender(
      <TextViewer nom="text" chemin="text" valeur="t.txt" mediaType="text/plain" href={null} />,
    );
    await waitFor(() => expect(screen.getByText(/fichier non résolu/)).toBeInTheDocument());
    expect(screen.queryByText("lecture…")).not.toBeInTheDocument();
  });

  test("aucune requete ne part sans url", () => {
    // Au 4.3, le résolveur rend toujours null : rien ne doit sortir sur le
    // réseau. Le double de fetch lèverait sur une route non déclarée.
    render(
      <TextViewer nom="text" chemin="text" valeur="t.txt" mediaType="text/plain" href={null} />,
    );
    expect(screen.getByText("t.txt")).toBeInTheDocument();
  });
});
