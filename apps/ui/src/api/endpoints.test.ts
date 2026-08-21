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
