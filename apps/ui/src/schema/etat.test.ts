/** L'état d'une capacité, sur le parc réel. */

import { describe, expect, test } from "vitest";
import capacités from "../api/__fixtures__/capabilities.json";
import type { Capability } from "../api/types";
import { etatCapacite, phraseEtat } from "./etat";

const CAPACITES = (capacités as unknown as { capabilities: Capability[] }).capabilities;

describe("l'état d'une capacité", () => {
  test("aucune capacite du parc n_est sans modele", () => {
    // L'invariant que le registre tient désormais : les vingt-cinq contrats ont
    // au moins un manifeste. Il vit aussi côté serveur
    // (`test_real_registry.py`), qui est l'autorité ; ici, il garde ce que
    // l'Atelier affiche — un groupe « Aucun modèle au registre » qui n'aurait
    // plus lieu d'être.
    const sansModèle = CAPACITES.filter((c) => etatCapacite(c) === "sans-modèle");
    expect(sansModèle.map((c) => c.id)).toEqual([]);
  });

  test("les deux etats du parc reel restent distingues", () => {
    const par_état = new Map<string, string[]>();
    for (const c of CAPACITES) {
      const état = etatCapacite(c);
      par_état.set(état, [...(par_état.get(état) ?? []), c.id]);
    }
    // Réduire à « prête / pas prête » confondrait une capacité dont les poids
    // ne sont pas téléchargés avec une capacité qui tourne.
    expect(par_état.get("prête")?.length).toBeGreaterThan(0);
    expect(par_état.get("sans-variant-prêt")?.length).toBeGreaterThan(0);
  });

  test("un titulaire n_implique pas un variant pret", () => {
    // image-to-mesh affiche un titulaire et n'a rien d'exécutable : ses 7,37 Go
    // de poids ne sont pas téléchargés.
    const mesh = CAPACITES.find((c) => c.id === "image-to-mesh");
    if (!mesh) return;
    expect(mesh.incumbent).toBeTruthy();
    expect(etatCapacite(mesh)).toBe("sans-variant-prêt");
  });

  test("chaque etat a sa phrase", () => {
    expect(phraseEtat("prête")).toBe("exécutable");
    expect(phraseEtat("sans-modèle")).toContain("aucun modèle");
    expect(phraseEtat("sans-variant-prêt")).toContain("aucun variant exécutable");
  });

  test("une capacite sans modele n_est pas dite sans variant pret", () => {
    expect(etatCapacite({ models: [], ready_variants: [] })).toBe("sans-modèle");
    expect(etatCapacite({ models: ["m"], ready_variants: [] })).toBe("sans-variant-prêt");
    expect(etatCapacite({ models: ["m"], ready_variants: ["m@v"] })).toBe("prête");
  });
});
