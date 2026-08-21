/** Trois formats de date dans la même surface d'API, trois fonctions. */

import { describe, expect, test } from "vitest";
import { isoUtc, jourMesure, posix } from "./dates";

describe("les trois formats de date de l'API", () => {
  test("un flottant posix n_est pas lu comme une date iso", () => {
    // `new Date(1755712345.6)` donne 1970 : c'est l'erreur qu'un formateur
    // générique commettrait sur `last_used`.
    const lu = posix(1755712345.6);
    expect(lu).not.toContain("1970");
    expect(lu).toContain("2025");
  });

  test("un instant iso se lit", () => {
    expect(isoUtc("2026-08-20T12:00:00+00:00")).toContain("2026");
  });

  test("un jour de mesure n_invente pas d_heure", () => {
    // `measured_at` date d'un jour, pas d'une seconde.
    expect(jourMesure("2026-08-20")).toBe("20/08/2026");
  });

  test("l_absence se dit", () => {
    expect(isoUtc(null)).toBe("—");
    expect(posix(null)).toBe("—");
    expect(jourMesure(null)).toBe("—");
  });

  test("une valeur illisible est rendue telle quelle", () => {
    expect(isoUtc("pas une date")).toBe("pas une date");
    expect(jourMesure("2026")).toBe("2026");
  });
});
