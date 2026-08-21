/** Les quatre formes d'échec du serveur, ramenées à des phrases. */

import { describe, expect, test } from "vitest";
import { repond } from "../../vitest.setup";
import * as api from "./endpoints";
import { ApiError, detailToMessages, phraseErreur } from "./errors";

describe("la normalisation des erreurs", () => {
  test("un detail en chaine passe tel quel", () => {
    // 404, 409 et les 422 métier : une phrase française qui porte souvent la
    // commande qui répare. La paraphraser ferait perdre le seul renseignement
    // utile.
    const detail = "qwen3-tts-1.7b@8bit-mlx : poids absents du cache — ecurie pull <ref>";
    expect(detailToMessages({ detail })).toEqual([detail]);
  });

  test("un detail en liste est aplati avec le champ fautif", () => {
    // 422 de pydantic. Afficher `detail` tel quel donnerait « [object Object] »,
    // le seul message dont on ne puisse rien faire.
    const corps = {
      detail: [
        { loc: ["body", "ref"], msg: "Field required", type: "missing" },
        { loc: ["query", "unused_after_days"], msg: "Input should be >= 1" },
      ],
    };
    expect(detailToMessages(corps)).toEqual([
      "body.ref : Field required",
      "query.unused_after_days : Input should be >= 1",
    ]);
  });

  test("un 500 garde sa phrase sans son chemin", () => {
    const corps = { detail: "TypeError : objet non appelable", path: "/runtime/admission" };
    expect(detailToMessages(corps)).toEqual(["TypeError : objet non appelable"]);
  });

  test("un corps en texte brut est rendu", () => {
    // Un refus de CORS de Starlette ne traverse pas FastAPI : c'est du texte.
    expect(detailToMessages("Disallowed CORS method")).toEqual(["Disallowed CORS method"]);
  });

  test("un corps vide ne fabrique pas de message", () => {
    expect(detailToMessages(null)).toEqual([]);
    expect(detailToMessages({})).toEqual([]);
    expect(detailToMessages("")).toEqual([]);
  });

  test("aucune forme ne produit object Object", () => {
    const formes: unknown[] = [
      { detail: "x" },
      { detail: [{ loc: ["body"], msg: "y" }] },
      { detail: { inattendu: true } },
      "brut",
      null,
    ];
    for (const forme of formes) {
      for (const message of detailToMessages(forme)) {
        expect(message).not.toContain("[object Object]");
      }
    }
  });
});

describe("les échecs de transport", () => {
  test("un echec reseau nomme ses deux causes", async () => {
    // Côté JavaScript, un refus de CORS et un serveur éteint donnent le même
    // TypeError nu : choisir une seule cause enverrait sur une fausse piste une
    // fois sur deux.
    repond("/healthz", () => {
      throw new TypeError("Failed to fetch");
    });

    const erreur = await api.healthz().catch((e) => e);
    expect(erreur).toBeInstanceOf(ApiError);
    expect(erreur.reseau).toBe(true);
    expect(erreur.status).toBe(0);
    expect(erreur.message).toContain("ecurie serve");
    expect(erreur.message).toContain("CORS");
    expect(erreur.message).toContain("http://localhost:5173");
    expect(erreur.message).toContain("http://127.0.0.1:5173");
  });

  test("un statut sans corps garde son code", async () => {
    repond("/healthz", { status: 503, texte: "" });
    const erreur = await api.healthz().catch((e) => e);
    expect(erreur.status).toBe(503);
    expect(phraseErreur(erreur)).toContain("503");
  });

  test("une phrase est toujours rendue", () => {
    expect(phraseErreur(new Error("cassé"))).toBe("cassé");
    expect(phraseErreur("brut")).toBe("brut");
    expect(phraseErreur(new ApiError("/x", 404, ["introuvable"]))).toBe("introuvable");
  });
});
