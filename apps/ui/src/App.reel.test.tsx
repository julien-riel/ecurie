/**
 * L'Atelier, branché sur un vrai `ecurie serve` — l'essai que rien ne remplace.
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
 * Le 4.4 lui a donné un second sujet : le **bandeau de ressources**, qui sonde
 * toutes les deux secondes. C'est le seul endroit du front qui répète une
 * requête, donc le seul qui puisse empiler des appels, faire clignoter des
 * chiffres ou tourner après un démontage — et rien de tout cela ne se voit sur
 * un double de `fetch` qui répond en une microseconde.
 *
 * Sa fin lui en donne un troisième, et c'est le seul qui prouve le jalon : un
 * **vrai job**, du clic au wav qu'on écoute. Ce qui ne peut s'éprouver
 * qu'ici : un flux d'événements servi par Starlette et découpé par le
 * navigateur, une progression qui vient d'un modèle MLX et non d'un compteur,
 * un fichier téléchargé depuis un autre port que celui de la page — donc soumis
 * au CORS que le serveur configure exprès.
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

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, describe, expect, test } from "vitest";
import { fetchReel } from "../vitest.setup";
import { App } from "./App";
import { BASE_URL } from "./api/http";

const REEL = process.env["ECURIE_ESSAI_REEL"] === "1";

describe.skipIf(!REEL)("l'Atelier contre un vrai ecurie serve", () => {
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

    // Le titulaire est préselectionné, et il pose ses défauts : `voice: serena`
    // vient du manifeste, `speed` du contrat. C'est la fusion que
    // `initialValues` recopie du serveur.
    const variants = (await screen.findByLabelText("Variant")) as HTMLSelectElement;
    await waitFor(() => expect(variants.value).toBe("qwen3-tts-1.7b@8bit-mlx"));
    await waitFor(() =>
      expect((document.getElementById("root_voice") as HTMLInputElement).value).toBe("serena"),
    );

    await userEvent.type(texte, "Bonjour.");

    // Le serveur chiffre ce que coûterait ce job, pour cette entrée-là. On cible
    // le bloc du chiffrage : le bandeau affiche la même phrase, mot pour mot,
    // parce que le pic de ce variant ne dépend pas de l'entrée. C'est cet essai
    // qui l'a montré, et l'intitulé « Pour l'entrée saisie » qui les sépare.
    await userEvent.click(screen.getByRole("button", { name: /Chiffrer/ }));
    await waitFor(() => expect(document.querySelector(".ecurie-chiffrage")).toBeTruthy(), {
      timeout: 15_000,
    });
    const coût = document.querySelector(".ecurie-chiffrage")!;
    // Les trois formes, et la troisième n'est pas une tolérance de confort :
    // l'essai du job qui suit **charge le modèle**, et il reste résident après
    // la fin de la suite. Le prochain lancement de ce fichier voit donc un parc
    // chaud, où la bonne réponse est « déjà résident : lancer ne le rechargera
    // pas ». Un essai réel change l'état de la machine qu'il éprouve.
    expect(coût.textContent).toMatch(/lancer (chargera|refusé)|déjà résident/);
    expect(coût.textContent).toMatch(/7,65 Gio|déjà résident/);
  }, 60_000);

  test("le bandeau sonde le budget reel et le tient a jour", async () => {
    render(<App />);
    const bandeau = await screen.findByLabelText("Ressources");

    // Le budget vient de Metal, mesuré au démarrage du serveur par un
    // sous-processus lancé dans le venv d'un runtime : sur cette machine il
    // vaut 17,76 Gio, et il n'est pas une règle de trois sur la RAM physique.
    const budget = await within(bandeau).findByText(/occupés sur/, {}, { timeout: 15_000 });
    expect(budget.textContent).toMatch(/Gio/);
    expect(within(bandeau).getByText(/budget mesuré/)).toBeInTheDocument();

    // Le sondage repasse : deux tours au moins, sans que l'écran clignote ni
    // qu'une erreur apparaisse. C'est ce que le double de fetch ne peut pas dire.
    const avant = document.querySelectorAll("aside[aria-label='Ressources']").length;
    await new Promise((r) => setTimeout(r, 5_000));
    expect(document.querySelectorAll("aside[aria-label='Ressources']").length).toBe(avant);
    expect(within(bandeau).queryByText(/contact perdu/)).toBeNull();

    // Et il chiffre le variant choisi, sans qu'on ait rien demandé.
    await userEvent.selectOptions(await screen.findByLabelText("Capacité"), "text-to-speech");
    await within(bandeau).findByText(/lancer /, {}, { timeout: 15_000 });
  }, 60_000);

  test("un refus du parc reel se lit en gigaoctets, pas en octets bruts", async () => {
    // `minimax-music3@4bit` demande 23,9 Gio pour trente secondes, au-delà des
    // 17,76 du budget : c'est le seul refus que le parc réel sait produire sans
    // rien charger, et l'exigence du §4 du plan porte précisément dessus.
    render(<App />);
    const capacités = (await screen.findByLabelText("Capacité")) as HTMLSelectElement;
    await waitFor(() => expect(capacités.options.length).toBeGreaterThan(1));
    await userEvent.selectOptions(capacités, "text-to-music");
    const variants = (await screen.findByLabelText("Variant")) as HTMLSelectElement;
    await waitFor(() => expect(variants.value).toBe("minimax-music3@4bit"));

    const durée = document.getElementById("root_duration_seconds") as HTMLInputElement;
    if (durée) {
      await userEvent.clear(durée);
      await userEvent.type(durée, "30");
    }
    await userEvent.click(screen.getByRole("button", { name: /Chiffrer/ }));
    await waitFor(() => expect(document.querySelector(".ecurie-chiffrage")).toBeTruthy(), {
      timeout: 15_000,
    });

    const verdict = document.querySelector(".ecurie-chiffrage")!;
    expect(verdict.textContent).toMatch(/lancer (chargera|refusé)/);
    expect(verdict.textContent).toMatch(/Gio/);
    // Onze chiffres d'affilée : c'est ce que la raison portait avant que
    // `plan_admission` passe ses tailles par `fmt_memory`.
    expect(verdict.textContent).not.toMatch(/\d{10}/);
  }, 60_000);

  test("un vrai job va du clic au wav qu_on peut ecouter", async () => {
    // Le critère de sortie du jalon, vu depuis l'écran : « l'UI est le chemin
    // par défaut pour lancer TTS et image, sans retomber sur les scripts
    // d'origine ». Tout y est réel — le modèle, le flux d'événements, le
    // fichier servi depuis un autre port que celui de la page.
    render(<App />);
    await choisirTTS();
    await userEvent.type(await screen.findByLabelText(/^text/i), "L'atelier lance ses jobs.");

    await userEvent.click(screen.getByRole("button", { name: /^Lancer/ }));

    const panneau = await screen.findByLabelText("Job", {}, { timeout: 30_000 });
    // Un job qui traverse un vrai modèle passe par le chargement puis
    // l'inférence : l'écran doit dire ce qui se passe, et non se figer.
    await within(panneau).findAllByText(/en file|en cours|terminé/, {}, { timeout: 30_000 });

    // L'état se lit sur l'attribut et non sur le texte : « terminé » apparaît
    // deux fois dans un job réussi — le libellé d'état et le bilan — et
    // `getByText` refuse, à juste titre, de choisir entre deux éléments.
    await waitFor(
      () => expect(["done", "failed"]).toContain(panneau.getAttribute("data-etat")),
      { timeout: 240_000 },
    );
    expect(panneau.getAttribute("data-etat"), panneau.textContent ?? "").toBe("done");
    expect(within(panneau).getByText(/rtf/)).toBeInTheDocument();

    // La sortie réelle a remplacé la promesse du contrat.
    await screen.findByText(/Ce que ce job a produit/);
    const lecteur = document.querySelector('[data-viewer="audio"]') as HTMLAudioElement;
    const source = lecteur.getAttribute("src");
    expect(source, "le lecteur n'a pas d'URL : le résolveur n'a rien trouvé").toBeTruthy();
    expect(source).toContain("/files/");

    // Et le fichier se télécharge vraiment, depuis l'origine de l'API — ce que
    // le CORS du serveur doit autoriser, et qu'aucun test en jsdom ne dit.
    const fichier = await fetch(source!);
    expect(fichier.status).toBe(200);
    expect(fichier.headers.get("content-type")).toBe("audio/wav");
    expect((await fichier.arrayBuffer()).byteLength).toBeGreaterThan(1000);
  }, 360_000);

  test("un refus d_admission arrive par le flux, pas par un code HTTP", async () => {
    // Le chemin le plus subtil de toute la route des jobs : l'admission ne se
    // tranche qu'au moment de charger, après le tour de rôle, donc le job est
    // **accepté** puis échoue. Trente secondes de musique demandent 24,2 Gio là
    // où le budget entier en fait 17,76 : aucun modèle n'est chargé, rien n'est
    // déchargé, et la phrase du refus est celle du contrôle d'admission.
    render(<App />);
    const capacités = (await screen.findByLabelText("Capacité")) as HTMLSelectElement;
    await waitFor(() => expect(capacités.options.length).toBeGreaterThan(1));
    await userEvent.selectOptions(capacités, "text-to-music");
    const variants = (await screen.findByLabelText("Variant")) as HTMLSelectElement;
    await waitFor(() => expect(variants.value).toBe("minimax-music3@4bit"));

    // `prompt` est le seul champ requis du contrat, et `duration_seconds` vaut
    // 30 par défaut — c'est-à-dire la durée qui dépasse le budget. Sans le
    // prompt, le serveur refuserait l'entrée en 422 et aucun job n'existerait :
    // ce serait l'autre chemin, celui qu'éprouve la suite en jsdom.
    await userEvent.type(await screen.findByLabelText(/^prompt/i), "Une valse pour le parc.");
    await userEvent.click(screen.getByRole("button", { name: /^Lancer/ }));

    const panneau = await screen.findByLabelText("Job", {}, { timeout: 30_000 });
    await waitFor(() => expect(panneau.getAttribute("data-etat")).toBe("failed"), {
      timeout: 60_000,
    });
    const refus = within(panneau).getByText(/admission refusée/);
    expect(refus.textContent).toMatch(/Gio/);
    expect(refus.textContent).not.toMatch(/\d{10}/);
  }, 120_000);
});

/** Choisit la synthèse vocale et attend son titulaire préselectionné. */
async function choisirTTS(): Promise<void> {
  const capacités = (await screen.findByLabelText("Capacité")) as HTMLSelectElement;
  await waitFor(() => expect(capacités.options.length).toBeGreaterThan(1));
  await userEvent.selectOptions(capacités, "text-to-speech");
  const variants = (await screen.findByLabelText("Variant")) as HTMLSelectElement;
  await waitFor(() => expect(variants.value).toBe("qwen3-tts-1.7b@8bit-mlx"));
}
