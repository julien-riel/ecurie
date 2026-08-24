/**
 * Comment ranger trente-deux capacités pour qu'on en trouve une.
 *
 * Deux tables, et une seule est un choix de présentation.
 *
 * **La catégorie** groupe par famille de travail — le son avec le son, le visage
 * avec le visage. Elle n'est pas déductible du contrat : `image-detect` et
 * `face-detect` prennent une image et rendent des boîtes, et pourtant on ne les
 * cherche pas dans le même geste. Elle vit donc ici, côté écran, avec un test
 * qui refuse qu'une capacité du registre n'en ait pas — une capacité neuve
 * tombe dans « Divers » à l'écran plutôt que de disparaître, et fait rougir la
 * CI le lendemain.
 *
 * **La nature de sortie** raffine ce que `modalites.ts` déduit. Le contrat dit
 * qu'`image-detect` rend du JSON ; il ne dit pas que ce JSON est une liste de
 * boîtes, ni que celui de `face-embed` est un vecteur de 512 nombres. Quatre
 * capacités qui rendent toutes « des données » n'auraient sans cela qu'une seule
 * et même glyphe, et le sélecteur cesserait de dire quoi que ce soit.
 *
 * Ce que ces tables ne font jamais : décider qu'une capacité ne mérite pas
 * d'être montrée. Tout ce que le serveur envoie est rendu, y compris ce qui ne
 * peut pas tourner — c'est la règle de `choix.ts`, et la moitié de ce qu'un
 * registre sert à savoir.
 */

import type { Capability } from "../api/types";
import type { Modalite } from "../schema/modalites";

export type Categorie =
  | "texte"
  | "son"
  | "image"
  | "video"
  | "visage"
  | "volume"
  | "terrain"
  | "series"
  | "science"
  | "commande"
  | "divers";

export interface DescriptionCategorie {
  id: Categorie;
  titre: string;
  /** Ce que la catégorie rassemble, en une ligne, sous son titre. */
  sous_titre: string;
}

/** L'ordre des sections. Le texte d'abord : c'est là qu'on entre dans un parc de modèles. */
export const CATEGORIES: readonly DescriptionCategorie[] = [
  { id: "texte", titre: "Texte", sous_titre: "Écrire, traduire, appeler un outil, lire une page" },
  { id: "son", titre: "Son et parole", sous_titre: "Dire, transcrire, séparer, nettoyer, composer" },
  { id: "image", titre: "Image", sous_titre: "Produire, retoucher, agrandir, découper, décrire" },
  { id: "video", titre: "Vidéo", sous_titre: "Animer, décrire, suivre un mouvement" },
  { id: "visage", titre: "Visage", sous_titre: "Situer, décrire, découper, reconnaître un visage" },
  {
    id: "volume",
    titre: "Volume et profondeur",
    sous_titre: "Sortir une image de son plan, et en refaire une pièce",
  },
  // Les quatre sections arrivées le 24 août 2026. Ce qui les rassemble n'est pas
  // une modalité mais une intention : elles ne produisent pas de contenu, elles
  // transforment une donnée en mesure, en prévision, en géométrie ou en action.
  // Chacune n'a qu'une ou deux capacités aujourd'hui, et c'est assumé — une
  // section courte se lit très bien, alors qu'un fourre-tout « données » aurait
  // rangé une protéine avec une orthophoto et n'aurait rien dit de ni l'une ni
  // l'autre. Le backlog annonce leur suite : météo, cristaux, anticipation.
  {
    id: "terrain",
    titre: "Terrain",
    sous_titre: "Lire une scène satellite, bande par bande",
  },
  {
    id: "series",
    titre: "Séries et prévision",
    sous_titre: "Prolonger une mesure, et dire ce qu'on ignore d'elle",
  },
  { id: "science", titre: "Science", sous_titre: "Encoder ce que la nature a écrit" },
  { id: "commande", titre: "Commande", sous_titre: "Transformer une consigne en gestes" },
  { id: "divers", titre: "Divers", sous_titre: "Ce que cet écran ne sait pas encore ranger" },
];

const PAR_CAPACITE: Record<string, Categorie> = {
  "text-generation": "texte",
  "translation": "texte",
  "tool-use": "texte",
  "document-to-text": "texte",

  "text-to-speech": "son",
  "speech-to-text": "son",
  "audio-to-text": "son",
  "voice-clone": "son",
  "speaker-diarization": "son",
  "audio-denoise": "son",
  "audio-separation": "son",
  "text-to-music": "son",

  "text-to-image": "image",
  "image-to-image": "image",
  "image-to-text": "image",
  "image-upscale": "image",
  "image-inpaint": "image",
  "image-matting": "image",
  "image-segment": "image",
  "image-detect": "image",

  "text-to-video": "video",
  "image-to-video": "video",
  "video-to-text": "video",
  "video-to-motion": "video",

  "face-detect": "visage",
  "face-landmark": "visage",
  "face-parse": "visage",
  "face-embed": "visage",
  "face-headpose": "visage",
  "face-gaze": "visage",

  "image-to-mesh": "volume",
  "text-to-mesh": "volume",
  "depth-estimation": "volume",
  // Deux voisines de `image-to-mesh`, et elles ne s'y confondent pas : l'une
  // rend le **programme** qui construit la pièce plutôt que la pièce, l'autre
  // relie plusieurs vues entre elles au lieu d'en refermer une seule.
  "pointcloud-to-cad": "volume",
  "multiview-to-3d": "volume",

  "image-embed": "image",
  "audio-align": "son",

  "geo-segment": "terrain",
  "geo-embed": "terrain",

  "time-series-forecast": "series",

  "protein-embed": "science",

  "robot-action": "commande",
};

export function categorieDe(id: string): Categorie {
  return PAR_CAPACITE[id] ?? "divers";
}

/** Les capacités que cet écran sait ranger. Exporté pour le test qui garde la table à jour. */
export const CAPACITES_RANGEES: readonly string[] = Object.keys(PAR_CAPACITE);

/**
 * La forme dessinée pour une entrée ou une sortie.
 *
 * Le vocabulaire est fermé, et c'est ce qui fait tenir la glyphe : douze formes
 * pour trente-deux capacités, donc des formes qu'on reconnaît d'une capacité à
 * l'autre plutôt que trente-deux dessins qu'il faudrait apprendre.
 */
export type Forme =
  | "texte"
  | "parole"
  | "musique"
  | "image"
  | "video"
  | "document"
  | "maillage"
  | "profondeur"
  | "visage"
  | "boites"
  | "masque"
  | "points"
  | "vecteur"
  | "angles"
  | "squelette"
  | "donnees"
  // Les huit formes du 24 août 2026. Chacune existe parce qu'aucune des seize
  // précédentes ne disait ce que la capacité fait : une série n'est pas « des
  // données », un nuage de points n'est pas un maillage, et un programme CAO
  // n'est ni l'un ni l'autre.
  | "serie"
  | "eventail"
  | "scene"
  | "nuage"
  | "programme"
  | "molecule"
  | "commande"
  | "vues";

const FORME_DE_LA_MODALITE: Record<Modalite, Forme> = {
  texte: "texte",
  image: "image",
  son: "parole",
  video: "video",
  document: "document",
  maillage: "maillage",
  donnees: "donnees",
};

/**
 * Ce que la sortie est vraiment, quand « des données » ne suffit pas à le dire.
 *
 * Une entrée par capacité dont la sortie mérite mieux que sa modalité. Les
 * absentes retombent sur la forme de leur modalité, ce qui est déjà juste :
 * `text-to-image` rend une image, et le dessin d'une image est le bon dessin.
 */
const SORTIE_FINE: Record<string, Forme> = {
  "text-to-music": "musique",
  "text-to-speech": "parole",
  "voice-clone": "parole",
  "audio-denoise": "parole",
  "audio-separation": "musique",
  "speaker-diarization": "parole",
  "image-detect": "boites",
  "image-segment": "masque",
  "image-matting": "masque",
  "image-inpaint": "image",
  "depth-estimation": "profondeur",
  "video-to-motion": "squelette",
  "face-detect": "boites",
  "face-landmark": "points",
  "face-parse": "masque",
  "face-embed": "vecteur",
  "face-headpose": "angles",
  "face-gaze": "angles",
  // Ce que rend une prévision n'est pas une courbe, c'est un éventail : la
  // médiane et l'écart qui dit ce que le modèle ignore. Dessiner une simple
  // courbe effacerait la seule chose qui distingue cette capacité.
  "time-series-forecast": "eventail",
  "image-embed": "vecteur",
  "protein-embed": "vecteur",
  "geo-embed": "vecteur",
  "geo-segment": "masque",
  "pointcloud-to-cad": "programme",
  "multiview-to-3d": "nuage",
  "robot-action": "commande",
  // Un texte horodaté, pas une transcription : la sortie dit **quand**, et le
  // « quoi » lui était donné. Deux ondes côte à côte annuleraient la frontière
  // que ce contrat existe pour poser face à `speech-to-text`.
  "audio-align": "serie",
};

/**
 * Ce que l'entrée est vraiment, quand sa modalité ne suffit pas à le dire.
 *
 * Le symétrique de `SORTIE_FINE`, et il a fallu attendre les capacités de
 * mesure pour en avoir besoin. Une scène satellite à six bandes et une photo
 * sont toutes deux des `image/*` ; un nuage de points et un maillage sont tous
 * deux des `model/*` ; une séquence protéique et une invite sont toutes deux du
 * texte. Dans les trois cas, la modalité dit le format et tait le sujet.
 */
const ENTREE_FINE: Record<string, Forme> = {
  "time-series-forecast": "serie",
  "geo-segment": "scene",
  "geo-embed": "scene",
  "pointcloud-to-cad": "nuage",
  "multiview-to-3d": "vues",
  "protein-embed": "molecule",
  "audio-align": "parole",
};

/** L'entrée telle qu'elle se dessine : la modalité, sauf quand elle ment. */
export function formeEntree(id: string, modalite: Modalite): Forme {
  // Les six capacités du visage prennent une image, et le dire ainsi serait
  // exact et inutile : ce qu'elles regardent dans l'image est un visage, et
  // c'est ce que la glyphe doit montrer pour qu'on distingue `image-detect` de
  // `face-detect` d'un coup d'œil.
  if (categorieDe(id) === "visage") return "visage";
  return ENTREE_FINE[id] ?? FORME_DE_LA_MODALITE[modalite];
}

export function formeSortie(id: string, modalite: Modalite): Forme {
  return SORTIE_FINE[id] ?? FORME_DE_LA_MODALITE[modalite];
}

/**
 * Les capacités d'une catégorie, les exécutables d'abord.
 *
 * L'ordre à l'intérieur d'un état est celui du serveur, qui trie par
 * identifiant : le redéfinir ici ferait deux tris à maintenir pour un même
 * affichage.
 */
export interface Section {
  categorie: DescriptionCategorie;
  capacites: readonly Capability[];
}

const RANG: Record<string, number> = { "prête": 0, "sans-variant-prêt": 1, "sans-modèle": 2 };

export function sections(
  capacites: readonly Capability[],
  etat: (c: Capability) => string,
): Section[] {
  return CATEGORIES.map((categorie) => ({
    categorie,
    capacites: capacites
      .filter((c) => categorieDe(c.id) === categorie.id)
      .sort((a, b) => (RANG[etat(a)] ?? 9) - (RANG[etat(b)] ?? 9)),
  })).filter((s) => s.capacites.length > 0);
}
