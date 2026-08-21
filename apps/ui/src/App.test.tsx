/**
 * L'Atelier monté en entier, sur les réponses réelles du serveur — sans serveur.
 *
 * `App.reel.test.tsx` éprouve la même chaîne contre un `ecurie serve` qui
 * tourne, et c'est lui qui a trouvé la boucle de rendu du 4.3 ; mais il est
 * exclu par défaut, si bien que sans ces tests-ci le seul fichier qui assemble
 * les briques n'aurait aucune couverture dans `npm test`. Les briques peuvent
 * toutes être justes et le montage faux.
 *
 * Les réponses viennent des fixtures capturées sur le vrai registre par
 * `tools/ui_fixtures.py` : ce sont les octets que le serveur envoie. Celles de
 * `/runtime/residents` n'en font pas partie, et ne le peuvent pas — un résident
 * est un processus vivant au moment de la capture, et la capture ne charge
 * aucun modèle. Elles sont donc fabriquées ici, à partir du schéma.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test } from "vitest";
import { repond, requetes } from "../vitest.setup";
import capacités from "./api/__fixtures__/capabilities.json";
import modèles from "./api/__fixtures__/models.json";
import type { Capability, Model, ResidentsResponse } from "./api/types";
import { App } from "./App";
import { Atelier } from "./ecrans/Atelier";

const CAPACITES = capacités as unknown as { capabilities: Capability[]; issues: unknown[] };
const MODELES = modèles as unknown as { models: Model[]; issues: unknown[] };

const GIO = 1024 ** 3;

function modèlesDe(capability: string) {
  return {
    models: MODELES.models.filter((m) => m.capability === capability),
    issues: MODELES.issues,
  };
}

/** Une réponse `/runtime/residents` complète, dans la forme du schéma OpenAPI. */
function parc(partiel: Partial<ResidentsResponse> = {}): ResidentsResponse {
  const résidents = partiel.residents ?? [];
  const occupé = résidents.reduce((n, r) => n + r.peak_bytes, 0);
  return {
    budget_bytes: 16 * GIO,
    budget_source: "metal, via le mlx de runtimes/mlx-audio",
    budget_measured: true,
    used_bytes: occupé,
    free_bytes: 16 * GIO - occupé,
    policy: { budget_bytes: 16 * GIO, max_heavy_resident: 1, heavy_threshold_bytes: 8 * GIO },
    residents: résidents,
    stale: [],
    admission: null,
    ...partiel,
  } as ResidentsResponse;
}

function resident(ref: string, peak_bytes: number, partiel: Record<string, unknown> = {}) {
  return {
    ref,
    pid: 4242,
    peak_bytes,
    heavy: peak_bytes > 8 * GIO,
    runtime: "mlx-audio",
    env: "mlx-audio",
    loaded_at: "2026-08-21T10:00:00+00:00",
    last_used: 1_755_712_345.6,
    pinned: false,
    busy: false,
    busy_by: 0,
    busy_since: 0,
    warmup_ms: 2400,
    options: {},
    socket: "/tmp/a.sock",
    log: "/tmp/a.log",
    ...partiel,
  } as ResidentsResponse["residents"][number];
}

beforeEach(() => {
  repond("/registry/capabilities", { body: CAPACITES });
  repond("/runtime/residents", { body: parc() });
  repond("/registry/models", (requête) => {
    const capability = new URL(requête.url).searchParams.get("capability");
    return { body: capability ? modèlesDe(capability) : MODELES };
  });
});

async function choisir(capability: string) {
  await userEvent.selectOptions(await screen.findByLabelText("Capacité"), capability);
  const variants = (await screen.findByLabelText("Variant")) as HTMLSelectElement;
  await waitFor(() => expect(variants.options.length).toBeGreaterThan(1));
  return variants;
}

describe("le choix de la capacité et du variant", () => {
  test("les capacites du parc sont groupees, les executables d_abord", async () => {
    render(<App />);
    const sélecteur = (await screen.findByLabelText("Capacité")) as HTMLSelectElement;
    await waitFor(() => expect(sélecteur.options.length).toBeGreaterThan(1));

    expect(sélecteur.options.length - 1).toBe(CAPACITES.capabilities.length);
    const groupes = [...sélecteur.querySelectorAll("optgroup")].map((g) => g.label);
    expect(groupes[0]).toBe("Exécutables");
    expect(groupes).toContain("Aucun modèle au registre");
  });

  test("le titulaire est preselectionne, et ses defauts avec", async () => {
    // `voice: serena` vient du manifeste, `speed: 1.0` du contrat : c'est la
    // fusion que `initialValues` recopie du serveur, et elle doit être posée
    // par la préselection comme elle l'est par un choix à la main.
    render(<App />);
    const variants = await choisir("text-to-speech");

    await waitFor(() => expect(variants.value).toBe("qwen3-tts-1.7b@8bit-mlx"));
    await waitFor(() =>
      expect((document.getElementById("root_voice") as HTMLInputElement).value).toBe("serena"),
    );
  });

  test("changer de capacite ne selectionne jamais dans l_ancienne liste", async () => {
    // `useResource` garde les dernières données pendant qu'il recharge, pour ne
    // pas vider l'écran : entre le clic et l'arrivée des nouveaux modèles, la
    // liste affichée est encore celle d'avant. La préselection y cherchait une
    // référence de la bonne capacité, ne la trouvait pas, et posait un
    // formulaire sans les défauts du manifeste.
    render(<App />);
    await choisir("text-to-speech");
    await waitFor(() =>
      expect((document.getElementById("root_voice") as HTMLInputElement).value).toBe("serena"),
    );

    const variants = await choisir("image-upscale");
    await waitFor(() => expect(variants.value).toBe("swin2sr@classique-x2"));
    expect([...variants.options].map((o) => o.value)).not.toContain("qwen3-tts-1.7b@8bit-mlx");
    // `scale: 2` vient du manifeste de `classique-x2`, pas du contrat. On le lit
    // dans la projection et non dans le champ : RJSF encode les enums **par
    // index**, si bien que le `<select>` porte « 0 » pour la valeur 2.
    await waitFor(() => {
      const entrée = JSON.parse(document.querySelector("pre.ecurie-json")!.textContent!);
      expect(entrée["scale"]).toBe(2);
    });
  });

  test("une capacite sans variant executable n_en preselectionne aucun", async () => {
    // `image-to-mesh` affiche un titulaire dont les poids ne sont pas
    // téléchargés : ouvrir l'écran dessus donnerait un formulaire dont rien ne
    // peut sortir. Ce qui manque est écrit sous le sélecteur, pas dans l'option.
    render(<App />);
    const variants = await choisir("image-to-mesh");
    expect(variants.value).toBe("");

    await userEvent.selectOptions(variants, "hunyuan3d-2.1-shape-mlx@mlx-bf16");
    expect(await screen.findByText(/ecurie pull hunyuan3d/)).toBeInTheDocument();
    expect(screen.getByText(/ecurie env sync hunyuan3d/)).toBeInTheDocument();
  });

  test("choisir une capacite rend son formulaire depuis le contrat", async () => {
    render(<App />);
    await userEvent.selectOptions(await screen.findByLabelText("Capacité"), "text-to-speech");

    const texte = (await screen.findByLabelText(/^text/i)) as HTMLTextAreaElement;
    expect(texte.tagName).toBe("TEXTAREA");
    expect(document.getElementById("root_voice")).toBeTruthy();
    expect(document.getElementById("root_speed")).toBeTruthy();
  });

  test("ce qui partirait ne contient que des cles du contrat", async () => {
    render(<App />);
    await userEvent.selectOptions(await screen.findByLabelText("Capacité"), "text-to-speech");
    await userEvent.type(await screen.findByLabelText(/^text/i), "bonjour");

    const projeté = document.querySelector("pre.ecurie-json")!;
    const entrée = JSON.parse(projeté.textContent!);
    expect(entrée["text"]).toBe("bonjour");
    expect(Object.keys(entrée).every((k) => ["text", "voice", "speed", "seed"].includes(k))).toBe(
      true,
    );
  });
});

describe("le bandeau de ressources", () => {
  test("il dit le budget, sa source et ce qui l_occupe", async () => {
    repond("/runtime/residents", {
      body: parc({ residents: [resident("qwen3-tts-1.7b@8bit-mlx", 8_209_951_240)] }),
    });
    render(<App />);

    const bandeau = await screen.findByLabelText("Ressources");
    expect(await within(bandeau).findByText(/7,65 Gio occupés sur 16 Gio/)).toBeInTheDocument();
    expect(within(bandeau).getByText(/budget mesuré/)).toBeInTheDocument();
    expect(within(bandeau).getByText(/libérable/)).toBeInTheDocument();
  });

  test("il annonce ce que lancer le variant choisi couterait", async () => {
    // C'est la promesse du §9 de l'architecture : « ce qui sera déchargé si tu
    // lances », en permanence, sans avoir rien demandé.
    repond("/runtime/residents", (requête) => {
      const pour = new URL(requête.url).searchParams.get("for");
      return {
        body: parc({
          residents: [resident("sdxl-base@fp16", 17_123_246_080, { heavy: true })],
          admission: pour
            ? {
                ref: pour,
                admitted: true,
                reason: "décharge sdxl-base@fp16 (moins récemment utilisés)",
                evict: ["sdxl-base@fp16"],
                blockers: [],
                peak_bytes: 8_209_951_240,
                peak_note: null,
                already_resident: false,
                measure_mode: false,
                headroom_bytes: 0,
              }
            : null,
        }),
      };
    });
    render(<App />);
    await choisir("text-to-speech");

    const bandeau = await screen.findByLabelText("Ressources");
    expect(
      await within(bandeau).findByText(/lancer chargera 7,65 Gio et déchargera sdxl-base@fp16/),
    ).toBeInTheDocument();
  });

  test("l_admission d_un autre variant n_est jamais affichee", async () => {
    // Un changement de variant laisse en place, le temps d'une requête, la
    // réponse du précédent : afficher son chiffre annoncerait le coût d'un autre
    // modèle sous le nom de celui qu'on vient de choisir.
    repond("/runtime/residents", {
      body: parc({
        admission: {
          ref: "un-autre@variant",
          admitted: true,
          reason: "tient dans le budget résiduel",
          evict: [],
          blockers: [],
          peak_bytes: 999_999_999,
          peak_note: null,
          already_resident: false,
          measure_mode: false,
          headroom_bytes: 0,
        },
      }),
    });
    render(<App />);
    await choisir("text-to-speech");

    const bandeau = await screen.findByLabelText("Ressources");
    await within(bandeau).findByText(/occupés sur/);
    expect(within(bandeau).queryByText(/lancer chargera/)).toBeNull();
  });

  test("il previent quand le pic depend de l_entree", async () => {
    // `minimax-music3@4bit` est le seul variant du parc à `peak_scaling` :
    // `?for=` chiffre le variant sans rien savoir de la durée demandée, et
    // trente secondes coûtent le double de quinze.
    render(<App />);
    await choisir("text-to-music");

    const bandeau = await screen.findByLabelText("Ressources");
    expect(await within(bandeau).findByText(/duration_seconds/)).toBeInTheDocument();
  });

  test("un worker hors budget est nomme, jamais chiffre", async () => {
    repond("/runtime/residents", {
      body: parc({
        stale: [{ ref: "sdxl-base@fp16", pid: 99, holds_memory: true, socket: "/tmp/x.sock" }],
      }),
    });
    render(<App />);

    const bandeau = await screen.findByLabelText("Ressources");
    const alerte = await within(bandeau).findByText(/hors budget/);
    expect(alerte.textContent).toContain("sdxl-base@fp16");
    expect(alerte.textContent).toContain("ecurie unload --force");
  });

  test("les voix d_un modele charge en cours de route finissent par arriver", async () => {
    // Les `x-options-from` n'ont pas d'autre source que le champ `options` d'un
    // worker chargé : tant qu'un modèle n'a pas tourné une fois, on ne connaît
    // pas ses voix. Une lecture unique au montage les figerait à « aucune » pour
    // toute la session, y compris après un `ecurie run` lancé dans un terminal.
    let tours = 0;
    repond("/runtime/residents", () => {
      tours += 1;
      if (tours < 2) return { body: parc() };
      return {
        body: parc({
          residents: [
            resident("qwen3-tts-1.7b@8bit-mlx", 8_209_951_240, {
              options: { voices: ["serena", "ethan", "chelsie"] },
            }),
          ],
        }),
      };
    });
    render(<Atelier periodeBandeau={10} />);
    await choisir("text-to-speech");

    // Le premier tour rend « valeurs connues après le premier chargement » —
    // c'est l'état que garde `OptionsWidget.test.tsx`. Ce qui se joue ici est le
    // suivant : que le sondage le remplace sans qu'on recharge la page.
    expect(await screen.findByText(/3 valeur\(s\) annoncée\(s\)/)).toBeInTheDocument();
    expect(document.querySelectorAll("#root_voice__suggestions option")).toHaveLength(3);
  });

  test("un serveur muet garde les derniers chiffres et les date", async () => {
    // Seul cas monté sur `Atelier` plutôt que sur `App` : il lui faut un second
    // tour de sondage, et la cadence réelle est de deux secondes. Attendre
    // deux secondes pour prouver une seconde ligne de texte est un test qu'on
    // finit par désactiver.
    let tours = 0;
    repond("/runtime/residents", () => {
      tours += 1;
      if (tours === 1) return { body: parc({ residents: [resident("a@v1", 4 * GIO)] }) };
      return { status: 503, body: { detail: "superviseur indisponible" } };
    });
    render(<Atelier periodeBandeau={10} />);

    const bandeau = await screen.findByLabelText("Ressources");
    await within(bandeau).findByText(/4 Gio occupés/);
    expect(await within(bandeau).findByText(/contact perdu/)).toBeInTheDocument();
    expect(within(bandeau).getByText(/4 Gio occupés/)).toBeInTheDocument();
  });
});

describe("le chiffrage du job", () => {
  test("le cout vient du serveur et se lit", async () => {
    repond("/runtime/admission", {
      body: {
        ref: "qwen3-tts-1.7b@8bit-mlx",
        admission: {
          ref: "qwen3-tts-1.7b@8bit-mlx",
          admitted: true,
          reason: "tient dans le budget résiduel",
          evict: [],
          blockers: [],
          peak_bytes: 8_209_951_240,
          peak_note: null,
        },
        input: {},
        input_errors: [],
        ready: true,
        blockers: [],
      },
    });
    render(<App />);
    await choisir("text-to-speech");

    await userEvent.click(screen.getByRole("button", { name: /Chiffrer/ }));
    expect(
      await screen.findByText(/lancer chargera 7,65 Gio — tient dans le budget résiduel/),
    ).toBeInTheDocument();
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
          reason: "minimax-music3@4bit demande 24.2 Gio, le budget entier est de 17.76 Gio",
          evict: [],
          blockers: [],
          peak_bytes: 25_990_000_000,
          peak_note: "durée hors de l'intervalle mesuré : le pic est extrapolé",
        },
        input: {},
        input_errors: ["duration_seconds : doit être <= 60"],
        ready: true,
        blockers: [],
      },
    });
    render(<App />);
    await choisir("text-to-music");

    await userEvent.click(screen.getByRole("button", { name: /Chiffrer/ }));
    expect(await screen.findByText(/lancer refusé/)).toBeInTheDocument();
    expect(screen.getByText(/le budget entier est de 17.76 Gio/)).toBeInTheDocument();
    expect(screen.getByText(/extrapolé/)).toBeInTheDocument();
    const reproches = document.querySelector(".ecurie-chiffrage ul.text-danger")!;
    expect(reproches.textContent).toContain("duration_seconds : doit être <= 60");
  });

  test("un echec du serveur ne vide pas l_ecran", async () => {
    repond("/runtime/admission", { status: 404, body: { detail: "variant inconnu : chimere" } });
    render(<App />);
    await choisir("text-to-speech");

    await userEvent.click(screen.getByRole("button", { name: /Chiffrer/ }));
    expect(await screen.findByText(/variant inconnu/)).toBeInTheDocument();
    expect(document.querySelector("form.rjsf")).toBeTruthy();
  });

  test("le chiffrage ne part que sur demande", async () => {
    // Le recalcul à chaque frappe est nommément la tâche 4.7 : l'installer
    // maintenant enverrait une requête par caractère tapé.
    render(<App />);
    await choisir("text-to-speech");
    await userEvent.type(await screen.findByLabelText(/^text/i), "bonjour");

    expect(requetes().some((r) => new URL(r.url).pathname === "/runtime/admission")).toBe(false);
  });
});

describe("ce que l'écran dit de lui-même", () => {
  test("il n_y a pas de bouton lancer, et l_ecran dit pourquoi", async () => {
    render(<App />);
    await choisir("text-to-speech");

    expect(screen.queryByRole("button", { name: /^Lancer/ })).toBeNull();
    expect(screen.getByText(/attendent le déménagement du superviseur/)).toBeInTheDocument();
    expect(screen.getByText(/ecurie run qwen3-tts-1.7b@8bit-mlx/)).toBeInTheDocument();
  });

  test("les sorties promises viennent du contrat, pas d_un job", async () => {
    // `audio-separation` est la seule capacité à sorties imbriquées : s'arrêter
    // au premier niveau annoncerait « tracks » comme un fichier unique.
    render(<App />);
    await userEvent.selectOptions(await screen.findByLabelText("Capacité"), "audio-separation");

    await screen.findByText(/Ce que ce job produirait/);
    const liste = document.querySelector("ul.ecurie-promesses")!;
    expect(liste.textContent).toContain("tracks.vocals");
    expect(liste.textContent).toContain("audio/wav");
    expect(document.querySelector(".ecurie-sorties")).toBeNull();
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
});
