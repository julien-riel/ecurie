/**
 * L'Atelier monté en entier, sur les réponses réelles du serveur — sans serveur.
 *
 * `App.reel.test.tsx` éprouve la même chaîne contre un `ecurie serve` qui
 * tourne, et c'est lui qui a trouvé la boucle de rendu du 4.3 ; mais il est
 * exclu par défaut, si bien que sans ces tests-ci le seul fichier qui assemble
 * les briques n'aurait aucune couverture dans `npm test`. Les briques peuvent
 * toutes être justes et le montage faux.
 *
 * Ce fichier couvre le **choix** et le **chiffrage** ; `App.jobs.test.tsx`
 * couvre ce qui se passe après le clic sur *Lancer*. Le parc que les deux
 * montent est le même, et il vit dans `__essais__/parc.ts`.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test } from "vitest";
import { repond, requetes } from "../vitest.setup";
import { CAPACITES, GIO, choisir, parc, poserLeParc, resident } from "./__essais__/parc";
import { App } from "./App";
import { Atelier } from "./ecrans/Atelier";

beforeEach(poserLeParc);

describe("le choix de la capacité et du variant", () => {
  test("les capacites du parc sont groupees, les executables d_abord", async () => {
    render(<App />);
    const sélecteur = (await screen.findByLabelText("Capacité")) as HTMLSelectElement;
    await waitFor(() => expect(sélecteur.options.length).toBeGreaterThan(1));

    expect(sélecteur.options.length - 1).toBe(CAPACITES.capabilities.length);
    const groupes = [...sélecteur.querySelectorAll("optgroup")].map((g) => g.label);
    expect(groupes[0]).toBe("Exécutables");
    // Le parc n'a plus de capacité sans modèle, et un groupe vide ne s'affiche
    // pas : ce qui reste à part, ce sont celles dont rien n'est téléchargé.
    expect(groupes).toContain("Déclarées, rien d'exécutable en l'état");
    expect(groupes).not.toContain("Aucun modèle au registre");
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
    expect(await within(bandeau).findByText(/7,65 Gio occupés/)).toBeInTheDocument();
    expect(within(bandeau).getByText(/budget mesuré/)).toBeInTheDocument();
    expect(within(bandeau).getByText(/libérable/)).toBeInTheDocument();
    // Le libre est en gros au-dessus du rail ; les trois nombres d'un coup
    // restent dans l'étiquette du rail, qui est ce qu'un lecteur d'écran entend
    // au lieu de parcourir douze segments.
    expect(
      within(bandeau).getByRole("img", { name: "7,65 Gio occupés sur 16 Gio — 8,35 Gio libres" }),
    ).toBeInTheDocument();
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
    await within(bandeau).findByText(/occupés ·/);
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

  test("un chiffrage en vol n_atterrit jamais sous un autre variant", async () => {
    // Le bandeau garde déjà cette règle — « l'admission affichée est vérifiée
    // contre le variant demandé ». Le chiffrage l'a apprise en revue : changer
    // de variant efface bien le résultat précédent, mais ne peut rien contre
    // une requête déjà partie, qui revenait annoncer le coût de `classique-x2`
    // sous le nom de `reel-x4`.
    let libérer!: () => void;
    const attente = new Promise<void>((r) => {
      libérer = r;
    });
    repond("/runtime/admission", async (requête) => {
      const corps = JSON.parse(await requête.clone().text());
      if (corps.ref === "swin2sr@classique-x2") await attente;
      return {
        body: {
          ref: corps.ref,
          admission: {
            ref: corps.ref,
            admitted: true,
            reason: `chiffre de ${corps.ref}`,
            evict: [],
            blockers: [],
            peak_bytes: 3 * GIO,
            peak_note: null,
          },
          input: {},
          input_errors: [],
          ready: true,
          blockers: [],
        },
      };
    });
    render(<App />);
    const variants = await choisir("image-upscale");
    await waitFor(() => expect(variants.value).toBe("swin2sr@classique-x2"));

    await userEvent.click(screen.getByRole("button", { name: /Chiffrer/ }));
    await userEvent.selectOptions(variants, "swin2sr@reel-x4");
    libérer();

    await waitFor(() => expect(variants.value).toBe("swin2sr@reel-x4"));
    expect(screen.queryByText(/Pour l'entrée saisie/)).toBeNull();
    expect(screen.queryByText(/chiffre de swin2sr@classique-x2/)).toBeNull();
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
  test("le bouton lancer n_est jamais grise pour un variant qu_on croit incapable", async () => {
    // « ce morceau de 30 s demanderait 24,2 Gio » vaut mieux qu'un bouton mort :
    // c'est l'exigence du §4 du plan. `image-to-mesh` n'a aucun variant
    // exécutable — poids absents, environnement non synchronisé — et le bouton
    // part quand même ; c'est le serveur qui refuse, avec la commande qui répare.
    render(<App />);
    const variants = await choisir("image-to-mesh");
    await userEvent.selectOptions(variants, "hunyuan3d-2.1-shape-mlx@mlx-bf16");

    const lancer = screen.getByRole("button", { name: /^Lancer/ });
    expect(lancer).toBeEnabled();
  });

  test("sans variant choisi, il n_y a rien a lancer", async () => {
    render(<App />);
    await userEvent.selectOptions(await screen.findByLabelText("Capacité"), "image-to-mesh");

    expect(await screen.findByRole("button", { name: /^Lancer/ })).toBeDisabled();
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
