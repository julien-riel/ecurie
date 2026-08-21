/**
 * Le câblage du banc, sur les réponses réelles du serveur — sans serveur.
 *
 * `App.reel.test.tsx` éprouve la même chaîne contre un `ecurie serve` qui
 * tourne, et c'est lui qui a trouvé la boucle de rendu du 4.3 ; mais il est
 * exclu par défaut, si bien que sans ces tests-ci le seul fichier qui assemble
 * les briques n'aurait aucune couverture dans `npm test`. Les briques peuvent
 * toutes être justes et le montage faux.
 *
 * Les réponses viennent des fixtures capturées sur le vrai registre par
 * `tools/ui_fixtures.py` : ce sont les octets que le serveur envoie.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test } from "vitest";
import { repond } from "../vitest.setup";
import capacités from "./api/__fixtures__/capabilities.json";
import modèles from "./api/__fixtures__/models.json";
import type { Capability, Model } from "./api/types";
import { App } from "./App";

const CAPACITES = (capacités as unknown as { capabilities: Capability[]; issues: unknown[] });
const MODELES = (modèles as unknown as { models: Model[]; issues: unknown[] });

function modèlesDe(capability: string) {
  return {
    models: MODELES.models.filter((m) => m.capability === capability),
    issues: MODELES.issues,
  };
}

beforeEach(() => {
  repond("/registry/capabilities", { body: CAPACITES });
  repond("/runtime/residents", { body: { residents: [], stale: [], budget_bytes: 1 } });
  repond("/registry/models", (requête) => {
    const capability = new URL(requête.url).searchParams.get("capability");
    return { body: capability ? modèlesDe(capability) : MODELES };
  });
});

describe("le banc de rendu", () => {
  test("les capacites du parc sont proposees avec leur etat", async () => {
    render(<App />);
    const sélecteur = (await screen.findByLabelText("Capacité")) as HTMLSelectElement;
    await waitFor(() => expect(sélecteur.options.length).toBeGreaterThan(1));

    expect(sélecteur.options.length - 1).toBe(CAPACITES.capabilities.length);
    const libellés = [...sélecteur.options].map((o) => o.textContent ?? "");
    expect(libellés.some((t) => t.includes("exécutable"))).toBe(true);
    expect(libellés.some((t) => t.includes("aucun modèle"))).toBe(true);
  });

  test("choisir une capacite rend son formulaire depuis le contrat", async () => {
    render(<App />);
    const sélecteur = await screen.findByLabelText("Capacité");
    await userEvent.selectOptions(sélecteur, "text-to-speech");

    const texte = (await screen.findByLabelText(/^text/i)) as HTMLTextAreaElement;
    expect(texte.tagName).toBe("TEXTAREA");
    expect(document.getElementById("root_voice")).toBeTruthy();
    expect(document.getElementById("root_speed")).toBeTruthy();
  });

  test("choisir un variant pose les defauts fusionnes", async () => {
    // `voice: serena` vient du manifeste, `speed: 1.0` du contrat : c'est la
    // fusion que `initialValues` recopie du serveur.
    render(<App />);
    await userEvent.selectOptions(await screen.findByLabelText("Capacité"), "text-to-speech");

    const variants = (await screen.findByLabelText("Variant")) as HTMLSelectElement;
    await waitFor(() => expect(variants.options.length).toBeGreaterThan(1));
    await userEvent.selectOptions(variants, "qwen3-tts-1.7b@8bit-mlx");

    await waitFor(() =>
      expect((document.getElementById("root_voice") as HTMLInputElement).value).toBe("serena"),
    );
  });

  test("ce qui partirait ne contient que des cles du contrat", async () => {
    render(<App />);
    await userEvent.selectOptions(await screen.findByLabelText("Capacité"), "text-to-speech");
    await userEvent.type(await screen.findByLabelText(/^text/i), "bonjour");

    const projeté = document.querySelectorAll("pre.ecurie-json")[0]!;
    const entrée = JSON.parse(projeté.textContent!);
    expect(entrée["text"]).toBe("bonjour");
    expect(Object.keys(entrée).every((k) => ["text", "voice", "speed", "seed"].includes(k))).toBe(
      true,
    );
  });

  test("le cout du job vient du serveur et se lit", async () => {
    repond("/runtime/admission", {
      body: {
        ref: "qwen3-tts-1.7b@8bit-mlx",
        admission: {
          ref: "qwen3-tts-1.7b@8bit-mlx",
          admitted: true,
          reason: "tient dans le budget résiduel",
          evict: [],
          blockers: [],
          peak_bytes: 8209951240,
          peak_note: null,
        },
        input: {},
        input_errors: [],
        ready: true,
        blockers: [],
      },
    });
    render(<App />);
    await userEvent.selectOptions(await screen.findByLabelText("Capacité"), "text-to-speech");
    const variants = (await screen.findByLabelText("Variant")) as HTMLSelectElement;
    await waitFor(() => expect(variants.options.length).toBeGreaterThan(1));
    await userEvent.selectOptions(variants, "qwen3-tts-1.7b@8bit-mlx");

    await userEvent.click(screen.getByRole("button", { name: /Chiffrer/ }));
    expect(await screen.findByText(/7,65 Gio — tient dans le budget résiduel/)).toBeInTheDocument();
  });

  test("un refus du serveur est rendu en toutes lettres", async () => {
    // « ce morceau de 30 s demanderait 24,2 Gio » vaut mieux qu'un bouton grisé :
    // c'est l'exigence écrite du §4 du plan.
    repond("/runtime/admission", {
      body: {
        ref: "minimax-music3@4bit",
        admission: {
          ref: "minimax-music3@4bit",
          admitted: false,
          reason: "pic de 24,2 Gio au-delà des 19,07 du budget",
          evict: ["qwen3-tts-1.7b@8bit-mlx"],
          blockers: [],
          peak_bytes: 25990000000,
          peak_note: "durée hors de l'intervalle mesuré : le pic est extrapolé",
        },
        input: {},
        input_errors: ["duration_seconds : doit être <= 60"],
        ready: true,
        blockers: [],
      },
    });
    render(<App />);
    await userEvent.selectOptions(await screen.findByLabelText("Capacité"), "text-to-music");
    const variants = (await screen.findByLabelText("Variant")) as HTMLSelectElement;
    await waitFor(() => expect(variants.options.length).toBeGreaterThan(1));
    await userEvent.selectOptions(variants, "minimax-music3@4bit");

    await userEvent.click(screen.getByRole("button", { name: /Chiffrer/ }));
    expect(await screen.findByText(/au-delà des 19,07 du budget/)).toBeInTheDocument();
    expect(screen.getByText(/déchargerait : qwen3-tts-1.7b@8bit-mlx/)).toBeInTheDocument();
    expect(screen.getByText(/extrapolé/)).toBeInTheDocument();
    // Les reproches du contrat sont rendus tels quels, dans leur liste : le
    // nom du champ apparaît aussi dans la prose du contrat, d'où le ciblage.
    const reproches = document.querySelector("ul.text-danger")!;
    expect(reproches.textContent).toContain("duration_seconds : doit être <= 60");
  });

  test("un echec du serveur ne vide pas l_ecran", async () => {
    repond("/runtime/admission", { status: 404, body: { detail: "variant inconnu : chimere" } });
    render(<App />);
    await userEvent.selectOptions(await screen.findByLabelText("Capacité"), "text-to-speech");
    const variants = (await screen.findByLabelText("Variant")) as HTMLSelectElement;
    await waitFor(() => expect(variants.options.length).toBeGreaterThan(1));
    await userEvent.selectOptions(variants, "qwen3-tts-1.7b@8bit-mlx");

    await userEvent.click(screen.getByRole("button", { name: /Chiffrer/ }));
    expect(await screen.findByText(/variant inconnu/)).toBeInTheDocument();
    expect(document.querySelector("form.rjsf")).toBeTruthy();
  });

  test("les constats du registre s_affichent sans bloquer le formulaire", async () => {
    render(<App />);
    await userEvent.selectOptions(await screen.findByLabelText("Capacité"), "text-to-speech");
    await screen.findByLabelText(/^text/i);

    // Le parc réel porte deux avertissements sur trellis2.
    if (CAPACITES.issues.length > 0) {
      expect(screen.getByText(/avertissement\(s\)/)).toBeInTheDocument();
    }
    expect(document.querySelector("form.rjsf")).toBeTruthy();
  });

  test("les sorties imbriquees affichent chacune leur visualiseur", async () => {
    // audio-separation est la seule capacité à sorties imbriquées : ses pistes
    // vivent sous `tracks.*`, et prendre les seules clés de premier niveau
    // n'afficherait aucun lecteur.
    render(<App />);
    await userEvent.selectOptions(await screen.findByLabelText("Capacité"), "audio-separation");
    await waitFor(() =>
      expect(document.querySelectorAll('[data-viewer="audio"]').length).toBeGreaterThan(1),
    );
  });
});
