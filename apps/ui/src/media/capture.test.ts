/**
 * La mécanique de capture, sans caméra ni micro.
 *
 * Ce que ces tests éprouvent n'est pas le matériel — il n'y en a pas — mais les
 * décisions prises autour : quels modes un champ autorise, ce qu'on allume pour
 * chacun, quel conteneur on demande à `MediaRecorder`, et le fait qu'un flux
 * ouvert se referme. Cette dernière propriété est la seule dont l'échec survive
 * à la page : une diode de caméra qui reste allumée.
 */

import { describe, expect, test, vi } from "vitest";
import {
  type Materiel,
  TYPES_AUDIO,
  TYPES_VIDEO,
  collecter,
  contraintes,
  extensionPour,
  fermer,
  modesPour,
  nomDeCapture,
  premierTypeSupporte,
  trameEnPng,
  versWav,
} from "./capture";
import { TYPE_WAV } from "./wav";

describe("les modes qu'un champ autorise", () => {
  test("une image ouvre la camera, pas le micro", () => {
    expect(modesPour(["image/*"])).toEqual(["photo"]);
  });

  test("un son ouvre le micro seul", () => {
    expect(modesPour(["audio/*"])).toEqual(["audio"]);
  });

  test("une video ouvre les deux ensemble", () => {
    expect(modesPour(["video/*"])).toEqual(["video"]);
  });

  test("le champ de lecture de document accepte aussi la photo", () => {
    // `document-to-text` déclare « application/pdf,image/* » : photographier une
    // page est exactement l'usage.
    expect(modesPour(["application/pdf", "image/*"])).toEqual(["photo"]);
  });

  test("un pdf seul n_offre aucune capture", () => {
    // Aucun bouton plutôt qu'un bouton qui produirait un fichier refusé.
    expect(modesPour(["application/pdf"])).toEqual([]);
  });

  test("un champ sans type declare les offre tous", () => {
    expect(modesPour([])).toEqual(["photo", "audio", "video"]);
    expect(modesPour(["*/*"])).toEqual(["photo", "audio", "video"]);
  });

  test("un type exact vaut sa famille", () => {
    expect(modesPour(["image/png"])).toEqual(["photo"]);
  });
});

describe("ce qu'on allume", () => {
  test("une photo n_ouvre pas le micro", () => {
    expect(contraintes("photo")).toEqual({ video: true });
  });

  test("un enregistrement sonore n_ouvre pas la camera", () => {
    expect(contraintes("audio")).toEqual({ audio: true });
  });

  test("filmer ouvre les deux", () => {
    expect(contraintes("video")).toEqual({ video: true, audio: true });
  });
});

describe("le conteneur demandé à MediaRecorder", () => {
  test("le premier accepte gagne", () => {
    const supporte = (type: string) => type === "video/webm";
    expect(premierTypeSupporte(TYPES_VIDEO, supporte)).toBe("video/webm");
  });

  test("safari n_accepte que le mp4, et c_est prevu", () => {
    const supporte = (type: string) => type === "video/mp4";
    expect(premierTypeSupporte(TYPES_VIDEO, supporte)).toBe("video/mp4");
  });

  test("aucun candidat accepte laisse le navigateur choisir", () => {
    expect(premierTypeSupporte(TYPES_AUDIO, () => false)).toBeUndefined();
  });

  test("un candidat qui fait lever est un candidat refuse", () => {
    // `isTypeSupported` lève sur certaines chaînes selon les moteurs : la
    // question posée est « puis-je enregistrer avec cela », et une exception y
    // répond non.
    const supporte = (type: string) => {
      if (type.includes(";")) throw new TypeError("chaîne malformée");
      return type === "audio/webm";
    };
    expect(premierTypeSupporte(TYPES_AUDIO, supporte)).toBe("audio/webm");
  });
});

describe("la fermeture du flux", () => {
  test("chaque piste est arretee", () => {
    // La faute la plus visible qu'on puisse commettre ici, et la seule qui
    // survive à l'écran : une caméra qui reste allumée.
    const pistes = [{ stop: vi.fn() }, { stop: vi.fn() }];
    fermer({ getTracks: () => pistes } as unknown as MediaStream);
    for (const piste of pistes) expect(piste.stop).toHaveBeenCalledOnce();
  });

  test("fermer ce qui n_est pas ouvert ne leve pas", () => {
    expect(() => fermer(null)).not.toThrow();
  });
});

describe("la trame photographiée", () => {
  function scène(largeur: number, hauteur: number) {
    const dessine = vi.fn();
    const toile = {
      width: 0,
      height: 0,
      getContext: () => ({ drawImage: dessine }),
    } as unknown as HTMLCanvasElement;
    const video = { videoWidth: largeur, videoHeight: hauteur } as HTMLVideoElement;
    return { toile, video, dessine };
  }

  test("prend la taille du flux et non celle de l_apercu", async () => {
    // L'aperçu est réduit par la mise en page : photographier ses pixels
    // affichés rendrait une image de 320 pixels à un modèle qui en attend mille.
    const { toile, video, dessine } = scène(1280, 720);
    const materiel = {
      toileVersBlob: vi.fn(async () => new Blob(["png"], { type: "image/png" })),
    } as unknown as Materiel;

    const image = await trameEnPng(video, toile, materiel);

    expect(toile.width).toBe(1280);
    expect(toile.height).toBe(720);
    expect(dessine).toHaveBeenCalledWith(video, 0, 0, 1280, 720);
    expect(image.type).toBe("image/png");
  });

  test("une camera qui n_a rien envoye le dit", async () => {
    const { toile, video } = scène(0, 0);
    await expect(trameEnPng(video, toile, {} as Materiel)).rejects.toThrow(/pas encore envoyé/);
  });

  test("un navigateur qui ne rend pas d_image le dit aussi", async () => {
    const { toile, video } = scène(640, 480);
    const materiel = { toileVersBlob: async () => null } as unknown as Materiel;
    await expect(trameEnPng(video, toile, materiel)).rejects.toThrow(/n'a pas produit d'image/);
  });
});

describe("la collecte d'un enregistrement", () => {
  function enregistreurFactice(type = "audio/webm") {
    return {
      mimeType: type,
      ondataavailable: null as ((e: { data: Blob }) => void) | null,
      onstop: null as (() => void) | null,
      onerror: null as (() => void) | null,
    };
  }

  test("le dernier morceau arrive avant l_arret et n_est pas perdu", async () => {
    // Attendre `dataavailable` plutôt que `stop` amputerait le blob de sa fin
    // dès que l'enregistreur découpe en plusieurs morceaux.
    const enregistreur = enregistreurFactice();
    const attendu = collecter(enregistreur as unknown as MediaRecorder);

    enregistreur.ondataavailable!({ data: new Blob(["aa"]) });
    enregistreur.ondataavailable!({ data: new Blob(["bbb"]) });
    enregistreur.onstop!();

    const blob = await attendu;
    expect(blob.size).toBe(5);
    expect(blob.type).toBe("audio/webm");
  });

  test("les morceaux vides sont ecartes", () => {
    const enregistreur = enregistreurFactice();
    const attendu = collecter(enregistreur as unknown as MediaRecorder);
    enregistreur.ondataavailable!({ data: new Blob([]) });
    enregistreur.onstop!();
    return expect(attendu).resolves.toHaveProperty("size", 0);
  });

  test("une panne du materiel remonte en phrase", async () => {
    const enregistreur = enregistreurFactice();
    const attendu = collecter(enregistreur as unknown as MediaRecorder);
    enregistreur.onerror!();
    await expect(attendu).rejects.toThrow(/débranché/);
  });
});

describe("la conversion en WAV", () => {
  function materielAudio(canaux: Float32Array[], frequence: number) {
    const close = vi.fn();
    const materiel = {
      contexteAudio: () => ({
        close,
        decodeAudioData: async () => ({
          numberOfChannels: canaux.length,
          sampleRate: frequence,
          getChannelData: (i: number) => canaux[i]!,
        }),
      }),
    } as unknown as Materiel;
    return { materiel, close };
  }

  test("le navigateur relit ce qu_il vient d_encoder, et il en sort du wav", async () => {
    const { materiel } = materielAudio([new Float32Array([0, 1, -1])], 16000);

    const wav = await versWav(new Blob(["opus"]), materiel);

    expect(wav.type).toBe(TYPE_WAV);
    expect(wav.size).toBe(44 + 6);
  });

  test("le contexte audio est referme, meme quand le decodage echoue", async () => {
    // Un `AudioContext` par enregistrement, jamais rendu, finit par atteindre la
    // limite du navigateur — sur une page ouverte depuis une heure, c'est-à-dire
    // l'usage même d'un atelier.
    const close = vi.fn();
    const materiel = {
      contexteAudio: () => ({
        close,
        decodeAudioData: async () => {
          throw new Error("format illisible");
        },
      }),
    } as unknown as Materiel;

    await expect(versWav(new Blob(["x"]), materiel)).rejects.toThrow();
    expect(close).toHaveBeenCalledOnce();
  });
});

describe("le nom d'une capture", () => {
  test("porte son origine et son horodatage", () => {
    const quand = new Date(2026, 7, 22, 9, 5, 3);
    expect(nomDeCapture("photo", "image/png", quand)).toBe("photo-20260822-090503.png");
    expect(nomDeCapture("audio", TYPE_WAV, quand)).toBe("micro-20260822-090503.wav");
    expect(nomDeCapture("video", "video/webm", quand)).toBe("camera-20260822-090503.webm");
  });

  test("un type inconnu ne fabrique pas d_extension", () => {
    // Le serveur sait la déduire ; en inventer une ici serait plus faux que de
    // n'en poser aucune.
    expect(extensionPour("application/inconnu")).toBe("");
  });

  test("les parametres du type ne changent pas l_extension", () => {
    expect(extensionPour("video/webm;codecs=vp9,opus")).toBe(".webm");
  });
});
