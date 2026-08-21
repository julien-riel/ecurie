/**
 * Le socle, branché sur un vrai `ecurie serve` — l'essai que rien ne remplace.
 *
 * Le dépôt tient de son v0.3 une leçon qu'il vaut mieux ne pas réapprendre :
 * « un adaptateur non exécuté est un adaptateur faux ». Les trois adaptateurs
 * écrits proprement avaient chacun un défaut sérieux, invisible aux tests, et
 * découvert au premier lancement. Un front qui n'aurait jamais parlé au serveur
 * n'a aucune raison de faire exception — et de fait, celui-ci a livré au premier
 * essai une **boucle de rendu infinie** que la suite en jsdom ne voyait pas :
 * les avis de compilation étaient remontés pendant le rendu, ce qu'aucun test ne
 * faisait faute de passer `onNotices`. Le correctif durable est le test jsdom
 * qui garde ce cas ; cet essai-ci est ce qui l'a révélé.
 *
 * Il est exclu par défaut, comme le marqueur `real` de la suite pytest, et pour
 * la même raison : il demande un serveur qui tourne sur cette machine, avec le
 * vrai parc et son budget Metal.
 *
 *     uv run ecurie serve &
 *     ECURIE_ESSAI_REEL=1 npx vitest run src/App.reel.test.tsx
 *
 * Il utilise le `fetch` du navigateur, pas le double de `vitest.setup.ts` — ce
 * qui est tout l'intérêt. La couverture du parc, elle, reste à la suite en
 * jsdom, qui lit les mêmes contrats sur le disque sans dépendre d'un serveur.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, describe, expect, test } from "vitest";
import { fetchReel } from "../vitest.setup";
import { App } from "./App";
import { BASE_URL } from "./api/http";

const REEL = process.env["ECURIE_ESSAI_REEL"] === "1";

describe.skipIf(!REEL)("le socle contre un vrai ecurie serve", () => {
  beforeAll(async () => {
    // Le double de fetch du fichier de configuration refuse toute route non
    // déclarée : ici, on veut justement le réseau.
    globalThis.fetch = fetchReel;
    const santé = await fetch(`${BASE_URL}/healthz`);
    expect(santé.ok, `aucun serveur sur ${BASE_URL} — lancer « uv run ecurie serve »`).toBe(true);
  });

  test("le parc reel engendre son formulaire et son chiffre d_admission", async () => {
    render(<App />);

    const capacités = (await screen.findByLabelText("Capacité")) as HTMLSelectElement;
    await waitFor(() => expect(capacités.options.length).toBeGreaterThan(1));
    expect(capacités.options.length - 1).toBeGreaterThanOrEqual(17);

    await userEvent.selectOptions(capacités, "text-to-speech");

    // Le formulaire vient du contrat : `text` porte x-ui textarea, `voice` un
    // x-options-from, `speed` des bornes. Rien de tout cela n'est écrit ici.
    const texte = (await screen.findByLabelText(/^text/i)) as HTMLTextAreaElement;
    expect(texte.tagName).toBe("TEXTAREA");
    expect(document.getElementById("root_voice")).toBeTruthy();

    const variants = (await screen.findByLabelText("Variant")) as HTMLSelectElement;
    await waitFor(() => expect(variants.options.length).toBeGreaterThan(1));
    await userEvent.selectOptions(variants, "qwen3-tts-1.7b@8bit-mlx");

    // Le variant pose ses défauts : `voice: serena` vient du manifeste, `speed`
    // du contrat. C'est la fusion que `initialValues` recopie du serveur.
    await waitFor(() =>
      expect((document.getElementById("root_voice") as HTMLInputElement).value).toBe("serena"),
    );

    await userEvent.type(texte, "Bonjour.");

    // Le serveur chiffre ce que coûterait ce job, pour cette entrée-là.
    await userEvent.click(screen.getByRole("button", { name: /Chiffrer/ }));
    const coût = await screen.findByText(/Gio —/, {}, { timeout: 15_000 });
    expect(coût.textContent).toMatch(/7,65 Gio/);
  }, 60_000);
});
