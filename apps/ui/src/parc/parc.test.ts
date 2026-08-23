/** Ce que l'écran Parc dit du disque — la logique, sans DOM. */

import { describe, expect, test } from "vitest";
import type { Plan, Recoverable, Telemetry } from "../api/types";
import { plan as unPlan } from "../__essais__/disque";
import {
  cheminsDeLAction,
  gainParPoste,
  libelleMotif,
  phraseTelemetrie,
  phraseVolume,
  postesRecuperables,
} from "./parc";

const RECUPERABLE: Recoverable = {
  duplication_bytes: 4_900_000_000,
  hf_stale_bytes: 300_000_000,
  orphan_bytes: 0,
  unused_known: false,
  unused_bytes: 0,
  total_known_bytes: 5_200_000_000,
};

const LABELS = { duplication: "duplication inter-gestionnaires" };

describe("les quatre postes du récupérable", () => {
  test("le poste jamais utilise est indetermine, pas nul", () => {
    // Zéro se lirait « il n'y a rien à y gagner », ce qui est une conclusion.
    // Tant que la télémétrie n'observe pas depuis assez longtemps, il n'y en a
    // aucune à tirer.
    const postes = postesRecuperables(RECUPERABLE, null);

    const inutilisés = postes.find((p) => p.clef === "unused")!;
    expect(inutilisés.octets).toBeNull();
    expect(inutilisés.note).toContain("aucune exécution notée");
  });

  test("une telemetrie qui a conclu donne un chiffre", () => {
    const postes = postesRecuperables(
      { ...RECUPERABLE, unused_known: true, unused_bytes: 12_000_000_000 },
      { conclusive: true, first_run_at: "2025-01-01T00:00:00+00:00", unused_after_days: 90 },
    );

    const inutilisés = postes.find((p) => p.clef === "unused")!;
    expect(inutilisés.octets).toBe(12_000_000_000);
    expect(inutilisés.note).toBeUndefined();
  });

  test("les trois autres postes sont toujours des chiffres", () => {
    const postes = postesRecuperables(RECUPERABLE, null);

    expect(postes.slice(0, 3).map((p) => p.octets)).toEqual([4_900_000_000, 300_000_000, 0]);
  });

  test("les deux raisons de ne pas savoir appellent deux gestes differents", () => {
    // Sans exécution notée, il n'y a rien à attendre ; avec un journal trop
    // jeune, il suffit d'attendre. Un « inconnu » unique les confondrait.
    const jeune: Telemetry = {
      conclusive: false,
      first_run_at: "2026-08-01T09:00:00+00:00",
      unused_after_days: 90,
    };

    expect(phraseTelemetrie(null)).toBe("aucune exécution notée");
    expect(phraseTelemetrie(jeune)).toContain("2026-08-01");
    expect(phraseTelemetrie(jeune)).toContain("90 jours");
  });
});

describe("le gain par poste du plan", () => {
  test("les postes sont ordonnes du plus gros gain au plus petit", () => {
    const plan: Plan = unPlan({
      actions: [
        { kind: "trash", path: "/a", reason: "orphan-blob", bytes_reclaimed: 200 },
        { kind: "trash", path: "/b", reason: "hf-stale-revision", bytes_reclaimed: 900 },
        { kind: "trash", path: "/c", reason: "hf-stale-revision", bytes_reclaimed: 0 },
      ],
      by_reason: { "orphan-blob": 200, "hf-stale-revision": 900 },
      total_bytes_reclaimed: 1100,
    });

    const postes = gainParPoste(plan, LABELS);

    expect(postes.map((p) => p.motif)).toEqual(["hf-stale-revision", "orphan-blob"]);
    expect(postes[0]!.actions).toBe(2);
  });

  test("un poste dont toutes les actions rendent zero octet reste visible", () => {
    // Un instantané HF détaché ne libère rien — ce ne sont que des liens — mais
    // le laisser sur place transforme le cache en champ de liens cassés. Le
    // taire ferait disparaître une action réelle du décompte.
    const plan: Plan = unPlan({
      actions: [{ kind: "trash", path: "/snap", reason: "detached-snapshot", bytes_reclaimed: 0 }],
      by_reason: {},
      total_bytes_reclaimed: 0,
    });

    const postes = gainParPoste(plan);

    expect(postes).toHaveLength(1);
    expect(postes[0]!.actions).toBe(1);
    expect(postes[0]!.octets).toBe(0);
  });

  test("un motif que le serveur n_a pas traduit garde sa cle", () => {
    // Table totale, comme les deux autres tables d'aiguillage du front : un
    // poste ajouté demain s'affiche sous sa clé plutôt que de disparaître.
    expect(libelleMotif("duplication", LABELS)).toBe("duplication inter-gestionnaires");
    expect(libelleMotif("un-poste-de-demain", LABELS)).toBe("un-poste-de-demain");
    expect(libelleMotif("duplication")).toBe("duplication");
  });
});

describe("les chemins d'une action", () => {
  test("un lien dur en touche deux, une quarantaine un seul", () => {
    expect(
      cheminsDeLAction({
        kind: "hardlink",
        keep: "/a",
        replace: ["/b", "/c"],
        reason: "duplication",
        bytes_reclaimed: 10,
      }),
    ).toEqual(["/a", "/b", "/c"]);
    expect(
      cheminsDeLAction({ kind: "trash", path: "/a", reason: "orphan-blob", bytes_reclaimed: 10 }),
    ).toEqual(["/a"]);
  });

  test("un kind inconnu ne fait rien lever", () => {
    expect(cheminsDeLAction({ kind: "verglas", reason: "?", bytes_reclaimed: 0 })).toEqual([]);
  });
});

describe("l'état d'un volume de tiering", () => {
  test("un volume demonte explique ce qu_il rend indisponible", () => {
    expect(
      phraseVolume({ path: "/Volumes/Parc", mounted: false, free_bytes: null, total_bytes: null }),
    ).toContain("démonté");
  });

  test("un volume monte annonce sa place, en unites de disque", () => {
    const phrase = phraseVolume({
      path: "/Volumes/Parc",
      mounted: true,
      free_bytes: 320_000_000_000,
      total_bytes: 2_000_000_000_000,
    });

    expect(phrase).toContain("320,00 Go libres");
    // Le séparateur de milliers de `fr-CA` est une espace insécable étroite, pas
    // une espace ordinaire : comparer au caractère près échouerait pour la
    // mauvaise raison. Comme la CLI, le Go est la plus grande unité — un volume
    // de deux téraoctets s'annonce « 2 000,00 Go ».
    expect(phrase).toMatch(/2\s000,00 Go/);
  });

  test("une place libre inconnue n_est pas un disque plein", () => {
    // `0 o libre` proposerait de déporter ailleurs ce qui est déjà là.
    expect(
      phraseVolume({ path: "/x", mounted: true, free_bytes: null, total_bytes: null }),
    ).toContain("inconnue");
  });
});
