/**
 * Ce qu'une capacité prend et ce qu'elle rend, déduit de son contrat.
 *
 * Rien n'est écrit à la main ici : les modalités sortent du JSON Schema d'entrée
 * et de `output_media_types`, c'est-à-dire des deux choses que le serveur émet
 * déjà. C'est la règle du §4 de l'architecture appliquée jusqu'au filtre — « le
 * front rend un formulaire à partir du JSON Schema d'entrée » —, et elle a une
 * conséquence pratique : une capacité qui entre au registre demain est filtrable
 * le jour même, sans toucher au front.
 *
 * **Une capacité accepte souvent plus d'une modalité, et n'en a qu'une
 * principale.** `image-to-text` prend une image et, facultativement, une
 * question. Les deux comptent pour le filtre — quelqu'un qui cherche « ce qui
 * accepte du texte » veut la voir — mais une seule décrit ce qu'elle fait, et
 * c'est celle de son champ **obligatoire**. Sans cette distinction, la glyphe de
 * `image-to-text` dirait « texte vers texte ».
 */

import type { Capability } from "../api/types";

export type Modalite = "texte" | "image" | "son" | "video" | "document" | "maillage" | "donnees";

/** Le libellé montré sur une pastille de filtre. */
export const LIBELLE_MODALITE: Record<Modalite, string> = {
  texte: "Texte",
  image: "Image",
  son: "Son",
  video: "Vidéo",
  document: "Document",
  maillage: "Maillage",
  donnees: "Données",
};

/** L'ordre des pastilles, du plus courant au plus rare. Stable, jamais trié à l'exécution. */
export const ORDRE_ENTREE: readonly Modalite[] = ["texte", "image", "son", "video", "document"];
export const ORDRE_SORTIE: readonly Modalite[] = [
  "texte",
  "image",
  "son",
  "video",
  "maillage",
  "donnees",
];

/**
 * Le type de média d'un champ vers une modalité.
 *
 * `document-to-text` déclare « application/pdf,image/\* » — la graphie de
 * l'attribut `accept` d'un `<input type="file">`, que le contrat garde telle
 * quelle. On lit donc la liste entière, et le PDF l'emporte sur l'image : une
 * capacité qui accepte les deux est une capacité de document.
 */
export function modaliteDuMedia(media: string): Modalite | null {
  const types = media.split(",").map((t) => t.trim().toLowerCase());
  if (types.some((t) => t.startsWith("application/pdf"))) return "document";
  if (types.some((t) => t.startsWith("image/"))) return "image";
  if (types.some((t) => t.startsWith("audio/"))) return "son";
  if (types.some((t) => t.startsWith("video/"))) return "video";
  if (types.some((t) => t.startsWith("model/"))) return "maillage";
  if (types.some((t) => t.startsWith("application/json"))) return "donnees";
  if (types.some((t) => t.startsWith("text/"))) return "texte";
  return null;
}

interface Champ {
  type?: string;
  contentMediaType?: string;
  "x-ui"?: string;
  /** Le schéma des éléments, quand le champ est un tableau. */
  items?: Champ;
}

function champs(capability: Pick<Capability, "input">): Record<string, Champ> {
  const schema = capability.input as { properties?: Record<string, Champ> };
  return schema?.properties ?? {};
}

function requis(capability: Pick<Capability, "input">): readonly string[] {
  const schema = capability.input as { required?: string[] };
  return schema?.required ?? [];
}

function modaliteDuChamp(champ: Champ | undefined): Modalite | null {
  if (!champ) return null;
  if (champ.contentMediaType) return modaliteDuMedia(champ.contentMediaType);
  // Un champ **tableau** de fichiers porte son type sur `items`, parce que c'est
  // chaque élément qui est un fichier — le tableau, lui, n'en est pas un.
  // `multiview-to-3d` reçoit N photos d'une même scène, et sans ce cas elle
  // n'annonçait aucune modalité d'entrée : invisible du filtre, et une glyphe
  // qui disait « texte vers nuage ».
  if (champ.type === "array" && champ.items) return modaliteDuChamp(champ.items);
  // Un champ fichier sans type déclaré n'annonce aucune restriction : il ne dit
  // rien de sa modalité, et deviner « image » parce que c'est le cas fréquent
  // ferait mentir un filtre.
  if (champ["x-ui"] === "file") return null;
  if (champ.type === "string") return "texte";
  return null;
}

/** Toutes les modalités qu'une capacité sait recevoir, obligatoires ou non. */
export function modalitesEntree(capability: Pick<Capability, "input">): Set<Modalite> {
  const trouvées = new Set<Modalite>();
  for (const champ of Object.values(champs(capability))) {
    const modalité = modaliteDuChamp(champ);
    if (modalité) trouvées.add(modalité);
  }
  return trouvées;
}

/** Toutes les modalités qu'une capacité sait produire. */
export function modalitesSortie(
  capability: Pick<Capability, "output_media_types">,
): Set<Modalite> {
  const trouvées = new Set<Modalite>();
  for (const média of Object.values(capability.output_media_types)) {
    const modalité = modaliteDuMedia(média);
    if (modalité) trouvées.add(modalité);
  }
  return trouvées;
}

/**
 * La modalité qui décrit ce que la capacité prend — celle de son champ requis.
 *
 * Les champs obligatoires sont parcourus dans l'ordre du contrat, qui est celui
 * dans lequel il les déclare : `document-to-text` exige `document`, `tool-use`
 * exige `task` puis `tools`. Le premier qui porte une modalité gagne.
 */
export function entreePrincipale(capability: Pick<Capability, "input">): Modalite {
  const propriétés = champs(capability);
  for (const nom of requis(capability)) {
    const modalité = modaliteDuChamp(propriétés[nom]);
    if (modalité) return modalité;
  }
  // Aucun champ requis ne porte de modalité : on retombe sur l'ensemble, puis
  // sur le texte — une capacité sans entrée typée reçoit forcément des mots.
  const [première] = modalitesEntree(capability);
  return première ?? "texte";
}

/**
 * La modalité qui décrit ce que la capacité rend — celle de sa sortie exigée.
 *
 * Le contrat distingue les sorties **toujours produites** des facultatives, et
 * la distinction fait le sens : `video-to-motion` rend des trajectoires en JSON
 * et, si on le demande, une vidéo de contrôle. La décrire comme « rend une
 * vidéo » serait exactement l'inverse de ce qu'elle fait.
 */
export function sortiePrincipale(
  capability: Pick<Capability, "output" | "output_media_types">,
): Modalite {
  const schema = capability.output as { required?: string[] };
  for (const nom of schema?.required ?? []) {
    const modalité = modaliteDuMedia(mediaDeLaSortie(capability, nom) ?? "");
    if (modalité) return modalité;
  }
  const [première] = modalitesSortie(capability);
  return première ?? "donnees";
}

/**
 * Le type de média d'une sortie exigée, y compris quand elle en contient d'autres.
 *
 * `audio-separation` exige `tracks`, et `output_media_types` ne connaît que
 * `tracks.vocals`, `tracks.drums`… — les cinq pistes vivent sous l'objet, pas à
 * côté. Chercher la clé exacte ne trouverait rien, et la capacité qui rend cinq
 * fichiers audio passerait pour une capacité sans sortie typée.
 */
function mediaDeLaSortie(
  capability: Pick<Capability, "output_media_types">,
  nom: string,
): string | null {
  const direct = capability.output_media_types[nom];
  if (direct) return direct;
  const préfixe = `${nom}.`;
  for (const [clé, média] of Object.entries(capability.output_media_types)) {
    if (clé.startsWith(préfixe)) return média;
  }
  return null;
}
