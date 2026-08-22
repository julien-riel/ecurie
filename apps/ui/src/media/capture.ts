/**
 * La caméra et le micro, sans React et sans écran.
 *
 * Trois raisons de séparer cette mécanique du composant qui l'emploie.
 *
 * **Rien de tout cela n'existe dans jsdom.** `getUserMedia`, `MediaRecorder`,
 * `AudioContext`, `canvas.toBlob` : aucune de ces API n'est fournie par
 * l'environnement de test, et aucune ne peut l'être — ce sont des accès au
 * matériel. Les faire passer par un objet `Materiel` explicite est ce qui permet
 * d'en éprouver l'usage sans caméra ; les cacher derrière des `globalThis`
 * rendrait la moitié de ce fichier invérifiable.
 *
 * **Le choix du conteneur n'est pas le même selon le navigateur.** Chrome
 * enregistre en `video/webm`, Safari en `video/mp4`, et demander le mauvais fait
 * lever `MediaRecorder` à la construction. On interroge donc `isTypeSupported`
 * dans l'ordre des préférences plutôt que de coder un nom en dur.
 *
 * **Un flux qu'on oublie de fermer laisse la caméra allumée.** C'est la faute
 * la plus visible qu'on puisse commettre ici — une diode verte qui reste après
 * que l'écran a changé de sujet. `fermer` existe pour cela, et le composant
 * l'appelle sur chaque chemin de sortie, y compris l'échec.
 */

import { TYPE_WAV, encoderWav, versMono } from "./wav";

/** Ce qu'on demande au matériel, déduit de ce que le champ accepte. */
export type ModeCapture = "photo" | "audio" | "video";

export const TYPE_PHOTO = "image/png";

/**
 * Conteneurs proposés à `MediaRecorder`, par ordre de préférence.
 *
 * Le premier accepté gagne. VP9 avant VP8 pour la taille ; `video/mp4` en
 * dernier parce que c'est le seul que Safari propose et qu'aucun navigateur ne
 * l'accepte à moitié.
 */
export const TYPES_VIDEO = [
  "video/webm;codecs=vp9,opus",
  "video/webm;codecs=vp8,opus",
  "video/webm",
  "video/mp4",
] as const;

/**
 * Pour le son, le conteneur ne survit pas au dépôt : ce qui sort d'ici est
 * toujours du WAV. Cette liste ne sert qu'à obtenir des octets que le navigateur
 * saura ensuite se relire à lui-même.
 */
export const TYPES_AUDIO = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"] as const;

/** L'accès au matériel, en un objet — pour qu'un test puisse le remplacer. */
export interface Materiel {
  flux(contraintes: MediaStreamConstraints): Promise<MediaStream>;
  typeSupporte(type: string): boolean;
  enregistreur(flux: MediaStream, type: string | undefined): MediaRecorder;
  contexteAudio(): Pick<AudioContext, "decodeAudioData" | "close">;
  toileVersBlob(toile: HTMLCanvasElement, type: string): Promise<Blob | null>;
}

/**
 * Le matériel réel. N'est jamais construit dans un test.
 *
 * `canvas.toBlob` est promisifié ici plutôt qu'à l'appel : c'est la seule des
 * cinq fonctions qui rende encore par rappel, et l'asymétrie se paierait à
 * chaque usage.
 */
export function materielDuNavigateur(): Materiel {
  return {
    flux: (contraintes) => navigator.mediaDevices.getUserMedia(contraintes),
    typeSupporte: (type) => MediaRecorder.isTypeSupported(type),
    enregistreur: (flux, type) => new MediaRecorder(flux, type ? { mimeType: type } : undefined),
    contexteAudio: () => new AudioContext(),
    toileVersBlob: (toile, type) =>
      new Promise((resoudre) => toile.toBlob((blob) => resoudre(blob), type)),
  };
}

/** Le matériel est-il seulement joignable ? Un test, un `file://`, un vieux navigateur : non. */
export function captureDisponible(): boolean {
  return (
    typeof navigator !== "undefined" &&
    typeof navigator.mediaDevices?.getUserMedia === "function" &&
    typeof MediaRecorder !== "undefined"
  );
}

/**
 * Les modes qu'un champ autorise, d'après les types qu'il accepte.
 *
 * L'ordre rendu est celui des boutons à l'écran, et il va du plus courant au
 * plus rare — dix champs du parc attendent une image, six un son, deux une
 * vidéo. Un champ qui n'accepte rien de tout cela (`application/pdf` seul) rend
 * une liste vide, et le composant ne propose alors aucune capture plutôt qu'un
 * bouton qui produirait un fichier refusé.
 */
export function modesPour(accept: readonly string[]): ModeCapture[] {
  const accepte = (famille: string) =>
    accept.some((motif) => motif === "*/*" || motif.toLowerCase().startsWith(famille));
  // Un champ sans type déclaré accepte tout : le serveur le vérifiera contre le
  // registre, mais rien ici ne justifie de masquer les boutons.
  if (accept.length === 0) return ["photo", "audio", "video"];
  const modes: ModeCapture[] = [];
  if (accepte("image/")) modes.push("photo");
  if (accepte("audio/")) modes.push("audio");
  if (accepte("video/")) modes.push("video");
  return modes;
}

/** Ce qu'il faut allumer pour ce mode. Une photo n'a pas besoin du micro. */
export function contraintes(mode: ModeCapture): MediaStreamConstraints {
  if (mode === "audio") return { audio: true };
  if (mode === "photo") return { video: true };
  return { video: true, audio: true };
}

/** Le premier conteneur que le navigateur accepte, ou `undefined` pour son défaut. */
export function premierTypeSupporte(
  candidats: readonly string[],
  supporte: (type: string) => boolean,
): string | undefined {
  return candidats.find((type) => {
    try {
      return supporte(type);
    } catch {
      // `isTypeSupported` lève sur certaines chaînes malformées selon les
      // moteurs : un candidat qui fait lever est un candidat refusé.
      return false;
    }
  });
}

/** Coupe toutes les pistes. Sans cela, la diode de la caméra reste allumée. */
export function fermer(flux: MediaStream | null): void {
  for (const piste of flux?.getTracks() ?? []) piste.stop();
}

/**
 * Une trame de l'aperçu, en PNG.
 *
 * La taille est celle du **flux** (`videoWidth`), pas celle de l'élément à
 * l'écran : l'aperçu est réduit par la mise en page, et photographier ses pixels
 * affichés rendrait une image de 320 pixels de large à un modèle qui en attend
 * mille.
 *
 * PNG et non JPEG : les capacités image du parc travaillent sur du PNG en
 * sortie, la compression avec pertes n'apporte rien à une image qu'on va
 * transformer, et le détourage tolère mal les artefacts de bloc.
 */
export async function trameEnPng(
  video: HTMLVideoElement,
  toile: HTMLCanvasElement,
  materiel: Materiel,
): Promise<Blob> {
  const largeur = video.videoWidth;
  const hauteur = video.videoHeight;
  if (!largeur || !hauteur) {
    throw new Error("la caméra n'a pas encore envoyé d'image — réessayer dans un instant");
  }
  toile.width = largeur;
  toile.height = hauteur;
  const contexte = toile.getContext("2d");
  if (!contexte) throw new Error("le navigateur n'a pas rendu de contexte de dessin");
  contexte.drawImage(video, 0, 0, largeur, hauteur);
  const blob = await materiel.toileVersBlob(toile, TYPE_PHOTO);
  if (!blob) throw new Error("le navigateur n'a pas produit d'image à partir de la trame");
  return blob;
}

/**
 * Ce que `MediaRecorder` a enregistré, une fois qu'il s'est arrêté.
 *
 * L'attente porte sur `stop` et non sur `dataavailable` : le dernier morceau
 * arrive **avant** l'arrêt, et se réveiller sur les données rendrait un blob
 * amputé de sa fin dès que l'enregistreur découpe en plusieurs morceaux.
 */
export function collecter(enregistreur: MediaRecorder): Promise<Blob> {
  const morceaux: Blob[] = [];
  return new Promise<Blob>((resoudre, rejeter) => {
    enregistreur.ondataavailable = (evenement) => {
      if (evenement.data?.size) morceaux.push(evenement.data);
    };
    enregistreur.onerror = () =>
      rejeter(new Error("l'enregistrement s'est interrompu — le matériel a-t-il été débranché ?"));
    enregistreur.onstop = () =>
      resoudre(new Blob(morceaux, { type: enregistreur.mimeType || morceaux[0]?.type || "" }));
  });
}

/**
 * Les octets d'un enregistrement audio, convertis en WAV mono.
 *
 * Le navigateur décode ici ce qu'il vient lui-même d'encoder : c'est le même
 * moteur, la conversion ne peut donc pas échouer sur un format qu'il ne
 * connaîtrait pas. Ce qui suit est du calcul pur, dans `wav.ts`.
 *
 * Le contexte est fermé dans tous les cas : un `AudioContext` par
 * enregistrement, jamais rendu, finit par atteindre la limite du navigateur et
 * fait échouer le suivant — sur une page qu'on n'a pas rechargée depuis une
 * heure, c'est-à-dire exactement l'usage d'un atelier.
 */
export async function versWav(enregistre: Blob, materiel: Materiel): Promise<Blob> {
  const contexte = materiel.contexteAudio();
  try {
    const octets = await enregistre.arrayBuffer();
    const buffer = await contexte.decodeAudioData(octets);
    const canaux = Array.from({ length: buffer.numberOfChannels }, (_, i) =>
      buffer.getChannelData(i),
    );
    return encoderWav(versMono(canaux), buffer.sampleRate);
  } finally {
    void contexte.close();
  }
}

/** Le nom du fichier déposé — horodaté, parce que dix captures se ressemblent. */
export function nomDeCapture(mode: ModeCapture, type: string, quand: Date): string {
  const horodatage = [
    quand.getFullYear(),
    String(quand.getMonth() + 1).padStart(2, "0"),
    String(quand.getDate()).padStart(2, "0"),
    "-",
    String(quand.getHours()).padStart(2, "0"),
    String(quand.getMinutes()).padStart(2, "0"),
    String(quand.getSeconds()).padStart(2, "0"),
  ].join("");
  const base = { photo: "photo", audio: "micro", video: "camera" }[mode];
  return `${base}-${horodatage}${extensionPour(type)}`;
}

/**
 * L'extension d'un type de média, quand on la connaît.
 *
 * Le serveur sait la déduire lui aussi ; la poser ici évite qu'un nom déposé
 * sans suffixe traverse le sas et arrive au worker sans rien qui l'annonce.
 */
export function extensionPour(type: string): string {
  const nu = type.split(";")[0]?.trim().toLowerCase() ?? "";
  const connues: Record<string, string> = {
    [TYPE_WAV]: ".wav",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "video/webm": ".webm",
    "video/mp4": ".mp4",
    "audio/webm": ".weba",
    "audio/mp4": ".m4a",
  };
  return connues[nu] ?? "";
}
