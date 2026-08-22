/**
 * L'encodage WAV, octet par octet.
 *
 * C'est le seul fichier du front dont la sortie est lue par autre chose qu'un
 * navigateur : un worker Python l'ouvre avec une bibliothèque qui n'a aucune
 * tolérance pour un en-tête approximatif. Un test qui se contenterait de
 * vérifier la taille du blob laisserait passer une fréquence écrite en
 * gros-boutiste, et l'erreur ne se verrait qu'à l'écoute — trois fois trop lent,
 * dans un job qui a coûté trente secondes.
 */

import { describe, expect, test } from "vitest";
import { TYPE_WAV, dureeSecondes, encoderWav, versMono, versPcm16 } from "./wav";

async function octets(blob: Blob): Promise<DataView> {
  return new DataView(await blob.arrayBuffer());
}

function ascii(vue: DataView, position: number, longueur: number): string {
  return Array.from({ length: longueur }, (_, i) =>
    String.fromCharCode(vue.getUint8(position + i)),
  ).join("");
}

describe("l'en-tête WAV", () => {
  test("porte les quatre marqueurs du format", async () => {
    const vue = await octets(encoderWav(new Float32Array(4), 48000));

    expect(ascii(vue, 0, 4)).toBe("RIFF");
    expect(ascii(vue, 8, 4)).toBe("WAVE");
    expect(ascii(vue, 12, 4)).toBe("fmt ");
    expect(ascii(vue, 36, 4)).toBe("data");
  });

  test("annonce du PCM mono a la frequence recue", async () => {
    const vue = await octets(encoderWav(new Float32Array(10), 44100));

    expect(vue.getUint16(20, true)).toBe(1); // 1 = PCM entier, sans compression
    expect(vue.getUint16(22, true)).toBe(1); // un seul canal
    expect(vue.getUint32(24, true)).toBe(44100);
    expect(vue.getUint16(34, true)).toBe(16);
  });

  test("les tailles annoncees sont celles des donnees", async () => {
    const blob = encoderWav(new Float32Array(100), 16000);
    const vue = await octets(blob);

    expect(blob.size).toBe(44 + 200);
    expect(vue.getUint32(4, true)).toBe(36 + 200); // taille RIFF = tout sauf les 8 premiers
    expect(vue.getUint32(40, true)).toBe(200); // taille du bloc de données
  });

  test("les entiers sont en petit-boutiste", async () => {
    // Le piège du format : `RIFF` est du petit-boutiste, `RIFX` du gros. Une
    // fréquence de 48000 écrite à l'envers se lit 130 048 512, et le fichier
    // s'ouvre quand même — plus vite qu'un magnétophone en avance rapide.
    const vue = await octets(encoderWav(new Float32Array(1), 48000));

    expect(vue.getUint32(24, true)).toBe(48000);
    expect(vue.getUint32(24, false)).not.toBe(48000);
  });

  test("le type du blob est celui que le parc sait lire", () => {
    expect(encoderWav(new Float32Array(1), 8000).type).toBe(TYPE_WAV);
  });
});

describe("la conversion des échantillons", () => {
  test("le silence reste le silence", () => {
    expect(versPcm16(0)).toBe(0);
  });

  test("les extremes vont aux bornes du format", () => {
    expect(versPcm16(1)).toBe(32767);
    expect(versPcm16(-1)).toBe(-32768);
  });

  test("ce qui deborde est borne, jamais reboucle", () => {
    // Sans le bornage, 1,5 donnerait 49 150, qui reboucle en -16 386 : un
    // claquement au lieu d'une saturation.
    expect(versPcm16(1.5)).toBe(32767);
    expect(versPcm16(-2)).toBe(-32768);
  });

  test("l_ecriture repasse par le meme chemin", async () => {
    const vue = await octets(encoderWav(new Float32Array([0, 1, -1, 0.5]), 8000));

    expect(vue.getInt16(44, true)).toBe(0);
    expect(vue.getInt16(46, true)).toBe(32767);
    expect(vue.getInt16(48, true)).toBe(-32768);
    expect(vue.getInt16(50, true)).toBe(16384);
  });
});

describe("le mixage en mono", () => {
  test("un seul canal traverse sans copie", () => {
    const canal = new Float32Array([0.1, 0.2]);
    expect(versMono([canal])).toBe(canal);
  });

  test("deux canaux sont moyennes, jamais sommes", () => {
    // Sommer deux canaux identiques sature à la moindre crête, et l'écrêtage
    // s'entend là où l'atténuation ne s'entend pas.
    const mixé = versMono([new Float32Array([1, 0]), new Float32Array([1, 0.5])]);
    expect(Array.from(mixé)).toEqual([1, 0.25]);
  });

  test("aucun canal donne aucun echantillon", () => {
    expect(versMono([]).length).toBe(0);
  });
});

describe("la durée", () => {
  test("se lit du nombre d_echantillons et de la frequence", () => {
    expect(dureeSecondes(new Float32Array(48000), 48000)).toBe(1);
    expect(dureeSecondes(new Float32Array(24000), 48000)).toBe(0.5);
  });

  test("une frequence nulle ne divise rien", () => {
    expect(dureeSecondes(new Float32Array(10), 0)).toBe(0);
  });
});
