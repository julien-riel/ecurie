/** Le rail de la mémoire, sans rendre un composant. */

import { describe, expect, test } from "vitest";
import type { Admission, Resident, ResidentsResponse } from "../api/types";
import { synthese } from "./ressources";
import { nommable, railDeMemoire } from "./stalles";

const GIO = 1024 ** 3;

function synth(occupe: number, budget = 16 * GIO) {
  return synthese({
    budget_bytes: budget,
    budget_source: "metal, via le mlx de runtimes/x",
    budget_measured: true,
    used_bytes: occupe,
    free_bytes: budget - occupe,
    policy: { budget_bytes: budget, max_heavy_resident: 1, heavy_threshold_bytes: 8 * GIO },
    residents: [],
    stale: [],
    admission: null,
  } as ResidentsResponse)!;
}

function resident(ref: string, octets: number, partiel: Partial<Resident> = {}): Resident {
  return {
    ref,
    peak_bytes: octets,
    pid: 1,
    busy: false,
    busy_by: 0,
    busy_since: 0,
    pinned: false,
    heavy: false,
    env: "x",
    runtime: "mlx",
    socket: "/tmp/x",
    log: "/tmp/x.log",
    loaded_at: "2026-08-22T00:00:00Z",
    last_used: 0,
    warmup_ms: 0,
    options: {},
    ...partiel,
  } as Resident;
}

function admission(peak: number, partiel: Partial<Admission> = {}): Admission {
  return {
    ref: "sdxl-base@fp16",
    admitted: true,
    reason: "tient dans le budget résiduel",
    evict: [],
    blockers: [],
    peak_bytes: peak,
    peak_note: null,
    already_resident: false,
    measure_mode: false,
    overcommit: false,
    overflow_bytes: 0,
    headroom_bytes: 0,
    ...partiel,
  } as Admission;
}

describe("le rail de la mémoire", () => {
  test("un parc vide ne dessine aucune stalle", () => {
    const rail = railDeMemoire(synth(0), [], null);
    expect(rail.stalles).toHaveLength(0);
    expect(rail.deborde).toBe(false);
    expect(rail.repere).toBe(1);
  });

  test("chaque resident occupe sa part du budget", () => {
    const rail = railDeMemoire(
      synth(12 * GIO),
      [resident("a@x", 8 * GIO), resident("b@y", 4 * GIO)],
      null,
    );
    expect(rail.stalles.map((s) => s.part)).toEqual([0.5, 0.25]);
    expect(rail.stalles.every((s) => s.espece === "resident")).toBe(true);
    expect(rail.deborde).toBe(false);
  });

  test("le titre d_une stalle dit le poids et l_etat, jamais la couleur seule", () => {
    const rail = railDeMemoire(
      synth(8 * GIO),
      [resident("a@x", 8 * GIO, { busy: true, busy_by: 4242 })],
      null,
    );
    expect(rail.stalles[0]!.titre).toBe("a@x — 8 Gio, job en cours (pid 4242)");
  });

  test("un arrivant qui tient ajoute une seule stalle", () => {
    const rail = railDeMemoire(synth(4 * GIO), [resident("a@x", 4 * GIO)], admission(4 * GIO));
    expect(rail.stalles.map((s) => s.espece)).toEqual(["resident", "arrivant"]);
    expect(rail.stalles[1]!.part).toBeCloseTo(0.25);
    expect(rail.deborde).toBe(false);
    expect(rail.repere).toBe(1);
  });

  test("un variant deja resident n_est pas compte deux fois", () => {
    // Le bandeau reçoit son admission avec `already_resident` quand le variant
    // composé est celui qui est chargé : lui ajouter une stalle de plus
    // dessinerait un chargement qui n'aura pas lieu.
    const rail = railDeMemoire(
      synth(4 * GIO),
      [resident("sdxl-base@fp16", 4 * GIO)],
      admission(4 * GIO, { already_resident: true }),
    );
    expect(rail.stalles).toHaveLength(1);
    expect(rail.stalles[0]!.espece).toBe("resident");
  });

  test("un arrivant a cheval sur le budget se coupe en deux stalles", () => {
    // 12 Gio occupés, 8 demandés sur un budget de 16 : 4 rentrent, 4 non.
    // L'échelle devient 20 Gio, si bien que le repère du budget tombe à 80 %.
    const rail = railDeMemoire(
      synth(12 * GIO),
      [resident("a@x", 12 * GIO)],
      admission(8 * GIO, { admitted: false, reason: "au-delà du budget" }),
    );
    expect(rail.stalles.map((s) => s.espece)).toEqual(["resident", "arrivant", "debordement"]);
    expect(rail.stalles[1]!.octets).toBe(4 * GIO);
    expect(rail.stalles[2]!.octets).toBe(4 * GIO);
    expect(rail.repere).toBeCloseTo(0.8);
    expect(rail.deborde).toBe(true);
    // La somme couvre le rail entier : c'est ce qui garantit qu'aucun pixel
    // n'est peint par un arrondi plutôt que par une donnée.
    expect(rail.stalles.reduce((n, s) => n + s.part, 0)).toBeCloseTo(1);
  });

  test("un budget deja plein met tout l_arrivant en debordement", () => {
    const rail = railDeMemoire(
      synth(16 * GIO),
      [resident("a@x", 16 * GIO)],
      admission(4 * GIO, { admitted: false, reason: "au-delà du budget" }),
    );
    expect(rail.stalles.map((s) => s.espece)).toEqual(["resident", "debordement"]);
    expect(rail.stalles[1]!.octets).toBe(4 * GIO);
    expect(rail.repere).toBeCloseTo(0.8);
  });

  test("un pic d_admission inconnu ne dessine pas de stalle d_arrivee", () => {
    // Une stalle de largeur nulle et une stalle absente se ressemblent trop
    // pour qu'on choisisse la première : « inconnu n'est pas zéro » vaut aussi
    // pour une barre. Le cas se dit ailleurs, en toutes lettres.
    const rail = railDeMemoire(
      synth(4 * GIO),
      [resident("a@x", 4 * GIO)],
      admission(null as never, { admitted: false, reason: "aucun profil mesuré" }),
    );
    expect(rail.stalles.map((s) => s.espece)).toEqual(["resident"]);
    expect(rail.deborde).toBe(false);
  });

  test("le nom peint sur la porte est la reference sans son variant", () => {
    const rail = railDeMemoire(synth(4 * GIO), [resident("qwen3-tts-1.7b@8bit-mlx", 4 * GIO)], null);
    expect(rail.stalles[0]!.nom).toBe("qwen3-tts-1.7b");
    expect(rail.stalles[0]!.ref).toBe("qwen3-tts-1.7b@8bit-mlx");
  });

  test("un budget inconnu ne dessine rien plutot que de diviser par zero", () => {
    expect(railDeMemoire(synth(0, 0), [resident("a@x", 4 * GIO)], null).stalles).toHaveLength(0);
  });

  test("le nom ne rentre dans la stalle qu_au-dela d_un sixieme du rail", () => {
    expect(nommable({ part: 0.3 } as never)).toBe(true);
    expect(nommable({ part: 0.05 } as never)).toBe(false);
  });
});
