/** Ce que le bandeau dit de la mémoire, sans rendre un composant. */

import { describe, expect, test } from "vitest";
import type { Admission, Resident, ResidentsResponse } from "../api/types";
import {
  etatResident,
  parametreDuPic,
  phraseBudget,
  phraseFantomes,
  phrasesAdmission,
  severiteAdmission,
  synthese,
} from "./ressources";

const GIO = 1024 ** 3;

function réponse(partiel: Partial<ResidentsResponse>): ResidentsResponse {
  return {
    budget_bytes: 16 * GIO,
    budget_source: "metal, via le mlx de runtimes/x",
    budget_measured: true,
    used_bytes: 0,
    free_bytes: 16 * GIO,
    policy: { budget_bytes: 16 * GIO, max_heavy_resident: 1, heavy_threshold_bytes: 8 * GIO },
    residents: [],
    stale: [],
    admission: null,
    ...partiel,
  } as ResidentsResponse;
}

function admission(partiel: Partial<Admission>): Admission {
  return {
    ref: "qwen3-tts-1.7b@8bit-mlx",
    admitted: true,
    reason: "tient dans le budget résiduel",
    evict: [],
    blockers: [],
    peak_bytes: 8_209_951_240,
    peak_note: null,
    already_resident: false,
    measure_mode: false,
    headroom_bytes: 0,
    ...partiel,
  } as Admission;
}

describe("la synthèse de la mémoire", () => {
  test("le libre vient du serveur, la part est bornee", () => {
    const s = synthese(réponse({ used_bytes: 8 * GIO, free_bytes: 8 * GIO }))!;
    expect(s.occupe).toBe(8 * GIO);
    expect(s.libre).toBe(8 * GIO);
    expect(s.part).toBeCloseTo(0.5);
    expect(phraseBudget(s)).toBe("8 Gio occupés sur 16 Gio — 8 Gio libres");
  });

  test("un budget depasse ne fait pas deborder la jauge", () => {
    // `free_bytes` passe sous zéro quand les pics résidents dépassent le budget :
    // une jauge à 130 % sortirait de son cadre, mais le chiffre, lui, se dit.
    const s = synthese(réponse({ used_bytes: 20 * GIO, free_bytes: -4 * GIO }))!;
    expect(s.part).toBe(1);
    expect(phraseBudget(s)).toContain("-4 Gio libres");
  });

  test("un budget nul ne divise pas par zero", () => {
    expect(synthese(réponse({ budget_bytes: 0, free_bytes: 0 }))!.part).toBe(0);
  });

  test("aucune reponse ne donne aucune synthese", () => {
    expect(synthese(null)).toBeNull();
  });
});

describe("les workers hors budget", () => {
  test("ils sont comptes et nommes, jamais chiffres", () => {
    // `StaleWorkerOut` ne transporte pas de pic : chiffrer la mémoire retenue
    // supposerait de retrouver le profil du variant, ce qui n'est vrai que si le
    // manifeste n'a pas bougé. Compter est exact, estimer ne l'est pas.
    const s = synthese(
      réponse({
        stale: [
          { ref: "sdxl-base@fp16", pid: 42, holds_memory: true, socket: "/tmp/a.sock" },
          { ref: "mort@v1", pid: 43, holds_memory: false, socket: "/tmp/b.sock" },
        ],
      }),
    )!;
    expect(s.fantomes.map((f) => f.ref)).toEqual(["sdxl-base@fp16"]);
    const phrase = phraseFantomes(s)!;
    expect(phrase).toContain("1 worker(s) hors budget");
    expect(phrase).toContain("sdxl-base@fp16");
    expect(phrase).toContain("optimiste");
    expect(phrase).not.toMatch(/Gio|Mio/);
  });

  test("aucun fantome ne dit rien", () => {
    expect(phraseFantomes(synthese(réponse({}))!)).toBeNull();
  });
});

describe("l'état d'un résident", () => {
  function resident(partiel: Partial<Resident>): Resident {
    return { ref: "a@v", busy: false, busy_by: 0, pinned: false, ...partiel } as Resident;
  }

  test("les trois etats sont ceux de ecurie ps", () => {
    expect(etatResident(resident({ busy: true, busy_by: 4242 }))).toBe("job en cours (pid 4242)");
    expect(etatResident(resident({ pinned: true }))).toBe("épinglé");
    expect(etatResident(resident({}))).toBe("libérable");
  });

  test("un job en cours prime sur l_epinglage", () => {
    // Les deux rendent le résident intouchable, mais les gestes diffèrent :
    // « épinglé » se désépingle, « job en cours » s'attend.
    expect(etatResident(resident({ busy: true, busy_by: 7, pinned: true }))).toContain("job");
  });
});

describe("ce que lancer ferait", () => {
  test("le cas courant chiffre et cite la raison du serveur", () => {
    expect(phrasesAdmission(admission({}))).toEqual([
      "lancer chargera 7,65 Gio — tient dans le budget résiduel",
    ]);
  });

  test("une eviction est nommee dans la meme phrase", () => {
    const lignes = phrasesAdmission(
      admission({ evict: ["qwen3-tts-1.7b@8bit-mlx", "birefnet@fp16"] }),
    );
    expect(lignes[0]).toContain("déchargera qwen3-tts-1.7b@8bit-mlx, birefnet@fp16");
  });

  test("un refus se lit en toutes lettres, sans repeter les bloquants", () => {
    // La raison nomme déjà les bloquants **avec leur motif** — « épinglé » se
    // désépingle, « en cours de job » s'attend. Les répéter nus perdrait le motif.
    const lignes = phrasesAdmission(
      admission({
        admitted: false,
        reason: "minimax-music3@4bit demande 23.9 Gio, le budget entier est de 17.76 Gio",
        blockers: ["sdxl-base@fp16"],
        peak_bytes: 25_704_234_348,
      }),
    );
    expect(lignes).toHaveLength(1);
    expect(lignes[0]).toContain("lancer refusé");
    expect(lignes[0]).toContain("23.9 Gio");
    expect(lignes.join(" ")).not.toContain("sdxl-base@fp16");
  });

  test("un variant deja resident ne se recharge pas", () => {
    const lignes = phrasesAdmission(admission({ already_resident: true, reason: "déjà résident" }));
    expect(lignes[0]).toContain("est déjà résident");
    // Aucun chiffre : ces gigaoctets sont déjà payés, les annoncer laisserait
    // croire qu'il faudra les trouver une seconde fois.
    expect(lignes[0]).not.toMatch(/Gio|Mio/);
  });

  test("le mode mesure previent qu_il videra le parc", () => {
    // Un profil se mesure parc vidé, épinglés compris : mesurer avec d'autres
    // modèles en mémoire ne mesure pas le modèle, il mesure la machine.
    const lignes = phrasesAdmission(
      admission({ measure_mode: true, peak_bytes: null, evict: ["birefnet@fp16"] }),
    );
    expect(lignes).toHaveLength(2);
    expect(lignes[0]).toContain("pic inconnu");
    expect(lignes[1]).toContain("videra le parc");
  });

  test("un pic extrapole est dit sur sa propre ligne", () => {
    const lignes = phrasesAdmission(
      admission({ peak_note: "durée hors de l'intervalle mesuré : le pic est extrapolé" }),
    );
    expect(lignes).toHaveLength(2);
    expect(lignes[1]).toContain("extrapolé");
  });

  test("aucune admission ne donne aucune ligne", () => {
    expect(phrasesAdmission(null)).toEqual([]);
  });
});

describe("la sévérité d'une admission", () => {
  test("un refus, une eviction et un pic extrapole ne se valent pas", () => {
    expect(severiteAdmission(admission({}))).toBe("ok");
    expect(severiteAdmission(admission({ evict: ["a@v"] }))).toBe("attention");
    expect(severiteAdmission(admission({ peak_note: "extrapolé" }))).toBe("attention");
    expect(severiteAdmission(admission({ measure_mode: true }))).toBe("attention");
    expect(severiteAdmission(admission({ admitted: false }))).toBe("refus");
    expect(severiteAdmission(null)).toBe("ok");
  });
});

describe("le paramètre dont dépend le pic", () => {
  test("un profil a pente nomme son parametre", () => {
    // `minimax-music3@4bit` est le seul du parc : trente secondes coûtent le
    // double de quinze, et `?for=` chiffre le variant sans le savoir.
    expect(
      parametreDuPic({
        peak_scaling: { parameter: "duration_seconds" },
      } as never),
    ).toBe("duration_seconds");
  });

  test("un profil sans pente, ou pas de profil du tout, ne dit rien", () => {
    expect(parametreDuPic({ peak_scaling: null } as never)).toBeNull();
    expect(parametreDuPic(null)).toBeNull();
  });
});
