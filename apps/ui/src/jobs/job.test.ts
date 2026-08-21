/** Ce qu'on dit d'un job, et ce qu'on refuse d'en dire. */

import { describe, expect, test } from "vitest";
import type { Job } from "../api/types";
import { aUneSortie, dureeSecondes, enCours, formatDuree, phrasesBilan, phrasesMetriques } from "./job";

function job(partiel: Partial<Job> = {}): Job {
  return {
    id: "20260821-160000-abc123",
    ref: "qwen3-tts-1.7b@8bit-mlx",
    model: "qwen3-tts-1.7b",
    variant: "8bit-mlx",
    capability: "text-to-speech",
    state: "done",
    submitted_at: "2026-08-21T16:00:00+00:00",
    started_at: "2026-08-21T16:00:01+00:00",
    finished_at: "2026-08-21T16:00:04.400000+00:00",
    progress: 100,
    note: "",
    error: null,
    reused: false,
    evicted: [],
    warnings: [],
    metrics: {},
    output: {},
    outputs: {},
    files: {},
    input: {},
    seed: null,
    ...partiel,
  } as Job;
}

describe("la durée d'un job", () => {
  test("elle court du demarrage a la fin, pas de la soumission", () => {
    // Attendre son tour derrière un autre job n'est pas du temps de calcul :
    // le compter ferait passer pour lent un job qui a simplement patienté.
    expect(dureeSecondes(job())).toBeCloseTo(3.4, 3);
  });

  test("un job en cours n_a pas encore de duree", () => {
    expect(dureeSecondes(job({ state: "running", finished_at: null }))).toBeNull();
  });

  test("au-dela d_une minute elle se lit en minutes", () => {
    expect(formatDuree(3.4)).toBe("3,4 s");
    expect(formatDuree(72)).toBe("1 min 12 s");
  });
});

describe("le bilan d'un job", () => {
  test("un modele deja chaud et une eviction se disent tous les deux", () => {
    // C'est ce que l'admission décide et que nul autre outil ne dit ; les
    // concaténer en une phrase donnerait une ligne qu'on ne lit plus.
    const lignes = phrasesBilan(job({ reused: true, evicted: ["sdxl-base@fp16"] }));
    expect(lignes).toHaveLength(3);
    expect(lignes[0]).toBe("terminé en 3,4 s");
    expect(lignes[1]).toContain("déjà chaud");
    expect(lignes[2]).toContain("sdxl-base@fp16");
  });

  test("un job en echec est arrete, pas termine", () => {
    const lignes = phrasesBilan(job({ state: "failed" }));
    expect(lignes[0]).toBe("arrêté après 3,4 s");
  });

  test("un job encore en cours n_a rien a dire de son cout", () => {
    expect(phrasesBilan(job({ state: "running", finished_at: null }))).toEqual([]);
  });
});

describe("les métriques", () => {
  test("les cles ne sont pas traduites, les nombres sont arrondis", () => {
    // `rtf` et `audio_seconds` sont écrits par les adaptateurs et varient d'un
    // runtime à l'autre : une table de traduction dans le front mentirait dès
    // qu'un adaptateur en ajouterait une.
    expect(phrasesMetriques(job({ metrics: { rtf: 0.0512345, audio_seconds: 4.64 } }))).toEqual([
      "audio_seconds : 4,64",
      "rtf : 0,051",
    ]);
  });

  test("une taille se lit en gigaoctets, pas en dix chiffres", () => {
    // Le premier job réel a rapporté `peak_memory_bytes: 4794454718`. Le §4 du
    // plan exige qu'un chiffre de mémoire se lise « 4,47 Gio » ; la règle porte
    // sur le suffixe `_bytes`, convention du registre, et non sur une liste de
    // clés qu'un adaptateur neuf prendrait en défaut.
    expect(phrasesMetriques(job({ metrics: { peak_memory_bytes: 4_794_454_718 } }))).toEqual([
      "peak_memory_bytes : 4,47 Gio",
    ]);
    expect(phrasesMetriques(job({ metrics: { total_bytes: 0 } }))).toEqual(["total_bytes : 0 o"]);
  });

  test("une metrique textuelle ou structuree ne se perd pas", () => {
    expect(phrasesMetriques(job({ metrics: { backend: "mlx", stems: ["voix", "reste"] } }))).toEqual(
      ['backend : mlx', 'stems : ["voix","reste"]'],
    );
  });
});

describe("l'état d'un job", () => {
  test("en file et en cours ne sont pas terminaux", () => {
    expect(enCours(job({ state: "queued" }))).toBe(true);
    expect(enCours(job({ state: "running" }))).toBe(true);
    expect(enCours(job())).toBe(false);
    expect(enCours(null)).toBe(false);
  });

  test("un job en echec qui a produit quelque chose le montre quand meme", () => {
    // `run_job` fait échouer un job dont une sortie **requise** manque, ce qui
    // laisse dans `output` ce que le worker avait tout de même écrit. Le cacher
    // priverait du seul indice utile pour comprendre ce qui a manqué.
    expect(aUneSortie(job({ state: "failed", output: { text: "partiel.txt" } }))).toBe(true);
    expect(aUneSortie(job({ output: {} }))).toBe(false);
    expect(aUneSortie(job({ state: "running", output: { a: "b" } }))).toBe(false);
  });
});
