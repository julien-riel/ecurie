/** Les requêtes réellement construites — l'objet `Request`, pas une trace. */

import { describe, expect, test } from "vitest";
import { repond, requetes } from "../../vitest.setup";
import * as api from "./endpoints";
import { ApiError } from "./errors";

describe("les appels de l'API", () => {
  test("le parametre de requete s_appelle for", async () => {
    // `for` est un mot réservé en JavaScript, et le nom Python derrière l'alias
    // est `for_ref` : deux occasions de se tromper, réduites à un seul endroit.
    repond("/runtime/residents", { body: { residents: [] } });
    await api.residents("qwen3-tts-1.7b@8bit-mlx");

    const url = new URL(requetes()[0]!.url);
    expect(url.searchParams.get("for")).toBe("qwen3-tts-1.7b@8bit-mlx");
    expect(url.searchParams.has("for_ref")).toBe(false);
  });

  test("sans reference aucun parametre n_est pose", async () => {
    repond("/runtime/residents", { body: { residents: [] } });
    await api.residents();
    expect(new URL(requetes()[0]!.url).search).toBe("");
  });

  test("aucune requete ne porte de cookie", async () => {
    // `allow_credentials` est faux côté serveur : une requête portant des
    // cookies serait rejetée par le navigateur avant d'atteindre FastAPI, sans
    // message exploitable.
    repond("/registry/capabilities", { body: { capabilities: [], issues: [] } });
    repond("/runtime/admission", { body: {} });
    await api.capabilities();
    await api.admission("m@v", { text: "a" });

    for (const requête of requetes()) {
      expect(requête.credentials, requête.url).toBe("omit");
    }
  });

  test("l_admission ne pose le seed qu_a l_interieur de l_entree", async () => {
    // Le serveur écrit le champ racine `seed` par-dessus la saisie résolue :
    // deux sources en feraient partir une que l'utilisateur n'a pas choisie.
    repond("/runtime/admission", { body: {} });
    await api.admission("m@v", { text: "a", seed: 7 });

    const corps = JSON.parse(await requetes()[0]!.text());
    expect(corps).toEqual({ ref: "m@v", input: { text: "a", seed: 7 } });
    expect(corps.seed).toBeUndefined();
  });

  test("le filtre par capacite se pose quand il est donne", async () => {
    repond("/registry/models", { body: { models: [], issues: [] } });
    await api.models("text-to-speech");
    expect(new URL(requetes()[0]!.url).searchParams.get("capability")).toBe("text-to-speech");
  });

  test("les verbes se limitent a ceux que le serveur autorise", async () => {
    // allow_methods côté serveur : GET, POST, OPTIONS. Un autre verbe échouerait
    // au préflight, avec un message que le navigateur ne transmet pas.
    repond("/registry/capabilities", { body: { capabilities: [], issues: [] } });
    repond("/runtime/admission", { body: {} });
    await api.capabilities();
    await api.admission("m@v", {});
    expect(requetes().map((r) => r.method)).toEqual(["GET", "POST"]);
  });

  test("une capacite inconnue remonte le 404 du serveur", async () => {
    // Le serveur refuse de rendre une liste vide, qui se lirait « aucun modèle
    // pour cette capacité » là où la vraie réponse est « elle n'existe pas ».
    //
    // Le statut et le message sont vérifiés, pas seulement le type de
    // l'exception : un échec de transport produit lui aussi une `ApiError`, et
    // s'en contenter ferait passer ce test même si la requête n'était jamais
    // partie.
    repond("/registry/models", {
      status: 404,
      body: { detail: "capacité inconnue : 'chimere' — connues : text-to-speech" },
    });
    const erreur = await api.models("chimere").catch((e) => e);
    expect(erreur).toBeInstanceOf(ApiError);
    expect(erreur.status).toBe(404);
    expect(erreur.reseau).toBe(false);
    expect(erreur.messages[0]).toContain("capacité inconnue");
  });

  test("l_index et le resume du parc appellent bien leurs routes", async () => {
    // Les sept fonctions de ce fichier sont sa raison d'être : « le seul endroit
    // où une chaîne d'URL apparaît ». Deux d'entre elles n'étaient surveillées
    // par rien.
    repond("/", { body: { service: "ecurie", models: 12, capabilities: 17 } });
    repond("/store/summary", { body: { scanned: false, figures: null } });
    repond("/healthz", { body: { ok: true } });

    const index = await api.index();
    expect(index.capabilities).toBe(17);
    await api.storeSummary(30);
    await api.healthz();

    const chemins = requetes().map((r) => new URL(r.url).pathname);
    expect(chemins).toEqual(["/", "/store/summary", "/healthz"]);
    expect(new URL(requetes()[1]!.url).searchParams.get("unused_after_days")).toBe("30");
  });

  test("le seuil du parc n_est pose que s_il est demande", async () => {
    // Le serveur a son propre défaut (90 jours) et le borne : le poser à tort
    // ferait diverger l'écran Parc de ce que la CLI affiche.
    repond("/store/summary", { body: { scanned: false, figures: null } });
    await api.storeSummary();
    expect(new URL(requetes()[0]!.url).search).toBe("");
  });
});

describe("les routes des jobs", () => {
  test("lancer un job ne pose le seed qu_a l_interieur de l_entree", async () => {
    // Même règle que pour l'admission : le serveur écrit le champ racine par
    // -dessus la saisie résolue, et deux sources en feraient partir une que
    // l'utilisateur n'a pas choisie.
    repond("/jobs", { status: 202, body: { id: "j1" } });
    await api.lancerJob("m@v", { text: "a", seed: 7 });

    const requête = requetes()[0]!;
    expect(requête.method).toBe("POST");
    expect(new URL(requête.url).pathname).toBe("/jobs");
    const corps = JSON.parse(await requête.text());
    expect(corps).toEqual({ ref: "m@v", input: { text: "a", seed: 7 } });
  });

  test("un identifiant de job est encode dans le chemin", async () => {
    // Il compose une URL. Le serveur en vérifie la forme avant de toucher au
    // disque ; le front n'a pas à lui envoyer un chemin qu'il n'a pas voulu.
    repond("/jobs/x%2Fy", { body: { id: "x/y" } });
    await api.job("x/y").catch(() => {});
    expect(new URL(requetes()[0]!.url).pathname).toBe("/jobs/x%2Fy");
  });

  test("l_url d_un fichier est lue, jamais composee", async () => {
    // Le serveur donne `files` déjà fait — chemin à plusieurs segments compris.
    // Tout ce que le front y ajoute est l'hôte de l'API.
    expect(api.fichierDuJob("/jobs/j1/files/tracks/vocals.wav")).toBe(
      "http://127.0.0.1:8765/jobs/j1/files/tracks/vocals.wav",
    );
  });

  test("le flux annonce qu_il attend des evenements", async () => {
    repond("/jobs/j1/events", { texte: "event: end\ndata: {}\n\n", type: "text/event-stream" });
    await api.evenementsDuJob("j1", () => {});
    expect(requetes()[0]!.headers.get("accept")).toBe("text/event-stream");
  });
});

describe("le dépôt de fichier", () => {
  const RÉPONSE = {
    status: 201,
    body: { path: "/tmp/u/a.wav", name: "a.wav", media_type: "audio/wav", size_bytes: 44 },
  };

  test("part en multipart, avec la frontiere que seul le navigateur sait ecrire", async () => {
    // Poser un `content-type` à la main donnerait un en-tête sans `boundary`,
    // que Starlette refuse d'analyser — un 400 dont le message ne dit pas ce qui
    // manque.
    repond("/uploads", RÉPONSE);
    await api.deposerFichier(new Blob(["RIFF"], { type: "audio/wav" }), "micro.wav");

    const requête = requetes()[0]!;
    expect(requête.method).toBe("POST");
    expect(new URL(requête.url).pathname).toBe("/uploads");
    expect(requête.headers.get("content-type")).toMatch(/^multipart\/form-data; boundary=/);
  });

  test("le champ s_appelle file, et le type de media voyage avec les octets", async () => {
    // Le nom, lui, n'est pas vérifiable ici : l'implémentation de `fetch` de
    // Node sérialise un `Blob` en `filename="blob"` quel que soit le nom passé à
    // `append`. C'est pourquoi le serveur redéduit l'extension du type de média
    // plutôt que de compter sur le nom reçu — et c'est ce type-là qu'il faut
    // donc voir partir.
    repond("/uploads", RÉPONSE);
    await api.deposerFichier(new Blob(["RIFF"], { type: "audio/wav" }), "micro-20260822.wav");

    const corps = await requetes()[0]!.text();
    expect(corps).toContain('name="file"');
    expect(corps).toContain("Content-Type: audio/wav");
  });

  test("le chemin rendu est celui du serveur, tel quel", async () => {
    repond("/uploads", RÉPONSE);
    const écrit = await api.deposerFichier(new Blob(["x"]), "a.wav");
    expect(écrit.path).toBe("/tmp/u/a.wav");
  });
});
