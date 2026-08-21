/** Ce qu'on tire du registre des résidents. */

import { describe, expect, test } from "vitest";
import type { Resident, StaleWorker } from "./types";
import { optionsFor, workersHorsBudget } from "./residents";

function resident(ref: string, options: Record<string, unknown>): Resident {
  return { ref, options } as unknown as Resident;
}

describe("les options annoncées par un worker", () => {
  test("les options viennent du resident et de nul autre", () => {
    // CONCEPTION.md promet /runtime/residents/{ref}/options, qui n'existe pas.
    // La seule source est le champ `options` d'un résident.
    const résidents = [
      resident("a@v", { voices: ["serena"] }),
      resident("b@v", { languages: ["fr", "en"] }),
    ];
    expect(optionsFor(résidents, "b@v")).toEqual({ languages: ["fr", "en"] });
  });

  test("un modele jamais charge n_annonce rien", () => {
    // C'est l'état du parc au premier lancement, c'est-à-dire au moment précis
    // où l'on veut essayer une voix.
    expect(optionsFor([resident("a@v", { voices: ["serena"] })], "b@v")).toEqual({});
    expect(optionsFor([], "a@v")).toEqual({});
    expect(optionsFor([], null)).toEqual({});
  });
});

describe("les workers hors budget", () => {
  function stale(ref: string, holds_memory: boolean): StaleWorker {
    return { ref, pid: 1, holds_memory, socket: "/tmp/x.sock" };
  }

  test("seul un processus vivant retient de la memoire", () => {
    // Un processus mort ne laisse qu'une ligne à balayer ; un processus vivant
    // sans socket retient toujours ses gigaoctets sans figurer au budget.
    const périmés = [stale("a@v", true), stale("b@v", false)];
    expect(workersHorsBudget(périmés).map((w) => w.ref)).toEqual(["a@v"]);
  });

  test("aucun perime ne donne aucune retenue", () => {
    expect(workersHorsBudget([])).toEqual([]);
  });
});
