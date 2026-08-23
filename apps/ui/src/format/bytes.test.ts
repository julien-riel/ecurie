/** Le formatage des octets, et l'inconnu qui ne devient pas zéro. */

import { describe, expect, test } from "vitest";
import { formatBytes, formatOctetsDisque, phraseLourdeur } from "./bytes";

describe("le formatage des octets", () => {
  test("un pic inconnu ne s_affiche pas comme zero", () => {
    // Un pic non mesuré interdit l'exécution — « l'admission refuse par
    // principe » — quand un pic nul, lui, n'existe pas. Les confondre ferait
    // lire « ce modèle ne coûte rien » là où il faut lire « on ne sait pas ».
    expect(formatBytes(null)).toBe("pic inconnu");
    expect(formatBytes(undefined)).toBe("pic inconnu");
    expect(formatBytes(0)).toBe("0 o");
  });

  test("les unites sont binaires comme le budget", () => {
    expect(formatBytes(8589934592)).toContain("Gio");
    expect(formatBytes(1024)).toContain("Kio");
    expect(formatBytes(512)).toBe("512 o");
  });

  test("le pic mesure du tts se lit", () => {
    // 8 209 951 240 octets, mesurés le 20 août 2026 sur qwen3-tts-1.7b@8bit-mlx.
    expect(formatBytes(8209951240)).toBe("7,65 Gio");
  });

  test("une valeur non finie reste inconnue", () => {
    expect(formatBytes(Number.NaN)).toBe("pic inconnu");
    expect(formatBytes(Number.POSITIVE_INFINITY)).toBe("pic inconnu");
  });
});

describe("le formatage des octets du disque", () => {
  test("l_unite est celle de la CLI du parc, decimale et non binaire", () => {
    // `ecurie store status` affiche « 4.90 Go » pour ces octets-là. L'écran Parc
    // vise la parité avec lui : afficher « 4,56 Gio » du même fichier ferait
    // douter du chiffre plutôt que de l'unité.
    expect(formatOctetsDisque(4_900_000_000)).toBe("4,90 Go");
    expect(formatOctetsDisque(1_500_000)).toBe("1,5 Mo");
    expect(formatOctetsDisque(2_000)).toBe("2 ko");
    expect(formatOctetsDisque(512)).toBe("512 o");
  });

  test("l_inconnu du disque n_est pas un pic", () => {
    // Le poste « variants jamais utilisés » reste indéterminé tant que la
    // télémétrie est trop jeune, et le mot « pic » n'a rien à faire là.
    expect(formatOctetsDisque(null)).toBe("inconnu");
    expect(formatOctetsDisque(undefined)).toBe("inconnu");
    expect(formatOctetsDisque(Number.NaN)).toBe("inconnu");
    expect(formatOctetsDisque(0)).toBe("0 o");
  });
});

describe("la lourdeur d'un variant", () => {
  test("l_inconnu n_est pas leger", () => {
    // `heavy` vaut null — et non false — quand aucun profil n'est mesuré.
    // Un `?? false` ferait passer un modèle non mesuré pour cohabitable.
    expect(phraseLourdeur(null)).toContain("inconnue");
    expect(phraseLourdeur(undefined)).toContain("inconnue");
    expect(phraseLourdeur(false)).toBe("modèle léger");
    expect(phraseLourdeur(true)).toBe("modèle lourd");
  });
});
