/**
 * Du clic sur *Lancer* au fichier affiché — l'Atelier monté en entier.
 *
 * C'est le parcours que le jalon v0.4 doit rendre quotidien : choisir, remplir,
 * lancer, voir avancer, écouter le résultat. Les briques ont chacune leurs
 * tests ; ce qui se joue ici est le montage, et deux choses qu'aucune brique ne
 * peut prouver seule.
 *
 * **Un flux arrive en morceaux.** Le job passe par `queued`, `running`, une
 * progression, un état final, et l'écran doit bouger à chaque fois — pas
 * seulement afficher le dernier. Les trames sont donc poussées une à une, entre
 * deux assertions, comme le ferait un vrai serveur.
 *
 * **Un échec de job n'est pas un échec de requête.** Le refus d'admission arrive
 * en 202 puis `failed`, avec sa phrase en gigaoctets ; le variant non exécutable
 * arrive en 409 avec la commande qui répare. Les deux mènent à un écran très
 * différent, et confondre les chemins était le défaut le plus facile à écrire.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test } from "vitest";
import { fluxManuel, repond, requetes } from "../vitest.setup";
import { choisir, poserLeParc } from "./__essais__/parc";
import { App } from "./App";
import type { Job } from "./api/types";

const ID = "20260821-160000-abc123";
const REF = "qwen3-tts-1.7b@8bit-mlx";

function job(partiel: Partial<Job> = {}): Job {
  return {
    id: ID,
    ref: REF,
    model: "qwen3-tts-1.7b",
    variant: "8bit-mlx",
    capability: "text-to-speech",
    state: "queued",
    submitted_at: "2026-08-21T16:00:00+00:00",
    started_at: null,
    finished_at: null,
    progress: 0,
    note: "",
    error: null,
    reused: false,
    evicted: [],
    warnings: [],
    metrics: {},
    output: {},
    outputs: {},
    files: {},
    input: { text: "Bonjour.", speed: 1.0 },
    seed: null,
    ...partiel,
  } as Job;
}

const TERMINE = job({
  state: "done",
  started_at: "2026-08-21T16:00:01+00:00",
  finished_at: "2026-08-21T16:00:04.400000+00:00",
  progress: 100,
  metrics: { rtf: 0.05, audio_seconds: 4.64 },
  output: { audio: "audio.wav" },
  outputs: { audio: "audio.wav" },
  files: { audio: `/jobs/${ID}/files/audio.wav` },
});

/** Déclare `POST /jobs` et le flux du job, et rend de quoi le pousser. */
function serveurDeJob(premier: Job = job()) {
  const flux = fluxManuel();
  repond("/jobs", { status: 202, body: premier });
  repond(`/jobs/${ID}/events`, { flux: flux.corps, type: "text/event-stream" });
  return flux;
}

async function lancer(capability = "text-to-speech") {
  render(<App />);
  await choisir(capability);
  await userEvent.click(screen.getByRole("button", { name: /^Lancer/ }));
}

beforeEach(poserLeParc);

describe("lancer un job", () => {
  test("le clic envoie la reference et l_entree projetee", async () => {
    serveurDeJob();
    render(<App />);
    await choisir("text-to-speech");
    await userEvent.type(await screen.findByLabelText(/^text/i), "Bonjour.");

    await userEvent.click(screen.getByRole("button", { name: /^Lancer/ }));

    const envoyée = requetes().find((r) => new URL(r.url).pathname === "/jobs");
    expect(envoyée, "aucun POST /jobs n'est parti").toBeTruthy();
    const corps = JSON.parse(await envoyée!.clone().text());
    expect(corps.ref).toBe(REF);
    expect(corps.input.text).toBe("Bonjour.");
    // `voice: serena` vient du manifeste et `speed: 1.0` du contrat : ce que
    // l'écran envoie est l'entrée complète, pas la seule saisie.
    expect(corps.input.voice).toBe("serena");
  });

  test("l_ecran suit le job de la file a la sortie", async () => {
    const flux = serveurDeJob();
    await lancer();

    const panneau = await screen.findByLabelText("Job");
    expect(within(panneau).getByText("en file")).toBeInTheDocument();

    flux.evenement("state", job({ state: "running", note: "admission et chargement du modèle" }));
    expect(await within(panneau).findByText(/admission et chargement/)).toBeInTheDocument();

    flux.evenement("progress", job({ state: "running", progress: 40, note: "synthèse" }));
    await waitFor(() =>
      expect(screen.getByRole("progressbar")).toHaveAttribute("value", "40"),
    );

    flux.evenement("state", TERMINE);
    flux.evenement("end", { id: ID, state: "done" });
    flux.fermer();

    expect(await within(panneau).findByText("terminé")).toBeInTheDocument();
    expect(within(panneau).getByText("terminé en 3,4 s")).toBeInTheDocument();
    expect(within(panneau).getByText("rtf : 0,05")).toBeInTheDocument();
  });

  test("la fin du flux n_efface pas ce que le job a rendu", async () => {
    // L'événement `end` porte `{id, state}` et rien d'autre : le poser dans
    // l'état laisserait un job sans sortie, sans métrique et sans référence.
    const flux = serveurDeJob();
    await lancer();
    flux.evenement("state", TERMINE);
    flux.evenement("end", { id: ID, state: "done" });
    flux.fermer();

    const panneau = await screen.findByLabelText("Job");
    await within(panneau).findByText("terminé");
    expect(within(panneau).getByText(REF)).toBeInTheDocument();
    expect(await screen.findByText(/Ce que ce job a produit/)).toBeInTheDocument();
  });

  test("un second job attend le premier, et l_ecran dit ou aller sans attendre", async () => {
    serveurDeJob();
    await lancer();

    await screen.findByLabelText("Job");
    expect(screen.getByRole("button", { name: /^Lancer/ })).toBeDisabled();
    expect(screen.getByText(/ne suit qu'un job à la fois/)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`ecurie run ${REF}`))).toBeInTheDocument();
  });
});

describe("ce que le job a produit", () => {
  test("le wav pointe l_url que le serveur a composee", async () => {
    // Le front ne fabrique aucune URL : il lit `files`, composé par le serveur,
    // et n'y ajoute que l'hôte de l'API — la page vient de Vite en 5173.
    const flux = serveurDeJob();
    await lancer();
    flux.evenement("state", TERMINE);
    flux.fermer();

    await screen.findByText(/Ce que ce job a produit/);
    const lecteur = document.querySelector('[data-viewer="audio"]') as HTMLAudioElement;
    expect(lecteur.getAttribute("src")).toBe(`http://127.0.0.1:8765/jobs/${ID}/files/audio.wav`);
    expect(screen.queryByText(/fichier non résolu/)).toBeNull();
  });

  test("avant le premier job, l_ecran annonce ce que le contrat promet", async () => {
    serveurDeJob();
    render(<App />);
    await choisir("text-to-speech");

    expect(await screen.findByText(/Ce que ce job produirait/)).toBeInTheDocument();
    expect(document.querySelector(".ecurie-sorties")).toBeNull();
  });

  test("relancer remplace la sortie precedente, sans la melanger a la neuve", async () => {
    // Le geste le plus courant de l'écran : on écoute, on change un mot, on
    // relance. Deux jobs, deux flux, et rien du premier ne doit survivre —
    // ni sa sortie, ni son identifiant, ni son bilan.
    const premier = fluxManuel();
    const second = fluxManuel();
    const ID2 = "20260821-161500-def456";
    let soumissions = 0;
    repond("/jobs", () => {
      soumissions += 1;
      return { status: 202, body: soumissions === 1 ? job() : job({ id: ID2 }) };
    });
    repond(`/jobs/${ID}/events`, { flux: premier.corps, type: "text/event-stream" });
    repond(`/jobs/${ID2}/events`, { flux: second.corps, type: "text/event-stream" });

    await lancer();
    premier.evenement("state", TERMINE);
    premier.fermer();
    await screen.findByText(/Ce que ce job a produit/);

    await userEvent.click(screen.getByRole("button", { name: /^Lancer/ }));
    const panneau = await screen.findByLabelText("Job");
    await waitFor(() => expect(within(panneau).getByText(ID2)).toBeInTheDocument());
    // Le second job n'a encore rien rendu : l'écran revient à la promesse.
    expect(within(panneau).queryByText(ID)).toBeNull();
    expect(await screen.findByText(/Ce que ce job produirait/)).toBeInTheDocument();

    second.evenement("state", { ...TERMINE, id: ID2, files: { audio: `/jobs/${ID2}/files/audio.wav` } });
    second.fermer();

    await screen.findByText(/Ce que ce job a produit/);
    const lecteur = document.querySelector('[data-viewer="audio"]')!;
    expect(lecteur.getAttribute("src")).toContain(ID2);
  });

  test("changer de capacite retire le job de l_ecran", async () => {
    // La sortie s'aiguille sur les `output_media_types` du contrat qui l'a
    // produite : la garder pendant qu'on compose une autre capacité
    // l'aplatirait avec la mauvaise table.
    const flux = serveurDeJob();
    await lancer();
    flux.evenement("state", TERMINE);
    flux.fermer();
    await screen.findByText(/Ce que ce job a produit/);

    await choisir("image-upscale");

    expect(screen.queryByLabelText("Job")).toBeNull();
    expect(await screen.findByText(/Ce que ce job produirait/)).toBeInTheDocument();
  });
});

describe("ce qui refuse un job", () => {
  test("un refus d_admission arrive par le flux, en gigaoctets", async () => {
    // L'admission ne se tranche qu'au moment de charger : d'ici là, un autre job
    // a pu libérer la place. Le job existe donc, et il échoue en portant la
    // phrase du contrôle d'admission — un écran qui ne regarderait que le code
    // HTTP croirait ce job parti.
    const flux = serveurDeJob();
    await lancer();

    flux.evenement(
      "state",
      job({
        state: "failed",
        started_at: "2026-08-21T16:00:01+00:00",
        finished_at: "2026-08-21T16:00:01.200000+00:00",
        error:
          "admission refusée : minimax-music3@4bit demande 24,21 Gio, " +
          "le budget entier est de 17,76 Gio",
      }),
    );
    flux.evenement("end", { id: ID, state: "failed" });
    flux.fermer();

    const panneau = await screen.findByLabelText("Job");
    expect(await within(panneau).findByText(/admission refusée/)).toBeInTheDocument();
    expect(within(panneau).getByText(/24,21 Gio/)).toBeInTheDocument();
    expect(panneau).toHaveAttribute("data-etat", "failed");
  });

  test("un variant non executable est refuse avec la commande qui repare", async () => {
    // Le 409 porte un `detail` **objet** — une phrase, et un blocker par cause.
    // Le sérialiser en JSON brut ferait lire des accolades là où l'utilisateur
    // attend la commande à copier.
    repond("/jobs", {
      status: 409,
      body: {
        detail: {
          ref: "hunyuan3d-2.1-shape-mlx@mlx-bf16",
          message: "hunyuan3d-2.1-shape-mlx@mlx-bf16 n'est pas exécutable en l'état",
          blockers: [
            "poids absents du cache — ecurie pull hunyuan3d-2.1-shape-mlx@mlx-bf16",
            "environnement non synchronisé — ecurie env sync hunyuan3d",
          ],
        },
      },
    });
    render(<App />);
    const variants = await choisir("image-to-mesh");
    await userEvent.selectOptions(variants, "hunyuan3d-2.1-shape-mlx@mlx-bf16");

    await userEvent.click(screen.getByRole("button", { name: /^Lancer/ }));

    expect(await screen.findByText(/n'est pas exécutable en l'état/)).toBeInTheDocument();
    const actions = document.querySelector(".ecurie-actions ul.text-danger")!;
    expect(actions.textContent).toContain("ecurie pull hunyuan3d-2.1-shape-mlx@mlx-bf16");
    expect(actions.textContent).toContain("ecurie env sync hunyuan3d");
    expect(actions.textContent).not.toContain("{");
    // Rien n'a été lancé : aucun panneau de job ne doit apparaître.
    expect(screen.queryByLabelText("Job")).toBeNull();
  });

  test("une entree refusee par le contrat se lit champ par champ", async () => {
    repond("/jobs", { status: 422, body: { detail: "entrée refusée : text : trop court" } });
    render(<App />);
    await choisir("text-to-speech");

    await userEvent.click(screen.getByRole("button", { name: /^Lancer/ }));

    expect(await screen.findByText(/text : trop court/)).toBeInTheDocument();
  });
});

describe("quand le suivi lâche", () => {
  test("un flux coupe se dit, et se reprend sans rien perdre", async () => {
    // Le flux rejoue depuis le début : reprendre ne rattrape rien, il redemande
    // tout. C'est ce qui rend la reprise sûre après un serveur redémarré.
    const flux = serveurDeJob();
    await lancer();
    flux.evenement("state", job({ state: "running", progress: 20, note: "synthèse" }));
    await screen.findByText(/20 %/);

    flux.fermer(); // le serveur s'arrête sans avoir dit « end »

    const panneau = await screen.findByLabelText("Job");
    expect(await within(panneau).findByText(/le suivi s'est interrompu/)).toBeInTheDocument();

    repond(`/jobs/${ID}`, { body: { ...TERMINE, manifest: { ok: true } } });
    await userEvent.click(screen.getByRole("button", { name: /reprendre le suivi/ }));

    expect(await within(panneau).findByText("terminé")).toBeInTheDocument();
    expect(within(panneau).queryByText(/interrompu/)).toBeNull();
  });

  test("un job oublie pendant que sa soumission vole ne revient pas bloquer l_ecran", async () => {
    // Trouvé en revue, et le symptôme était pire que la cause : le `POST` que
    // rien n'annule revenait poser son job **après** le changement de capacité.
    // La garde de capacité cachait le panneau — donc aussi le bouton « retirer
    // de l'écran » — pendant que « un job est en cours » gardait *Lancer* grisé.
    // L'écran restait bloqué sur un job qu'il refusait de montrer.
    const flux = fluxManuel();
    let libérer!: () => void;
    const attente = new Promise<void>((r) => {
      libérer = r;
    });
    repond("/jobs", async () => {
      await attente;
      return { status: 202, body: job() };
    });
    repond(`/jobs/${ID}/events`, { flux: flux.corps, type: "text/event-stream" });

    await lancer();
    await choisir("image-upscale");
    libérer();

    await waitFor(() => expect(screen.getByRole("button", { name: /^Lancer/ })).toBeEnabled());
    expect(screen.queryByLabelText("Job")).toBeNull();
    expect(screen.queryByText(/ne suit qu'un job à la fois/)).toBeNull();
    flux.fermer();
  });

  test("retirer un job pendant sa reprise ne le fait pas revenir", async () => {
    const flux = fluxManuel();
    repond("/jobs", { status: 202, body: job() });
    repond(`/jobs/${ID}/events`, { flux: flux.corps, type: "text/event-stream" });
    await lancer();
    await screen.findByLabelText("Job");
    flux.evenement("state", job({ state: "running", progress: 20, note: "synthèse" }));
    await screen.findByText(/20 %/);
    flux.fermer();
    await screen.findByText(/le suivi s'est interrompu/);

    let libérer!: () => void;
    const attente = new Promise<void>((r) => {
      libérer = r;
    });
    repond(`/jobs/${ID}`, async () => {
      await attente;
      return { body: { ...TERMINE, manifest: null } };
    });

    await userEvent.click(screen.getByRole("button", { name: /reprendre le suivi/ }));
    await userEvent.click(screen.getByRole("button", { name: /retirer de l'écran/ }));
    libérer();

    await waitFor(() => expect(screen.getByRole("button", { name: /^Lancer/ })).toBeEnabled());
    expect(screen.queryByLabelText("Job")).toBeNull();
  });

  test("le flux d_un job inconnu se dit sans effacer le job", async () => {
    repond("/jobs", { status: 202, body: job() });
    repond(`/jobs/${ID}/events`, { status: 404, body: { detail: `job inconnu : ${ID}` } });
    await lancer();

    const panneau = await screen.findByLabelText("Job");
    expect(await within(panneau).findByText(/job inconnu/)).toBeInTheDocument();
    expect(within(panneau).getByText("en file")).toBeInTheDocument();
  });
});
