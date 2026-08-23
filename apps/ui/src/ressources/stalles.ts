/**
 * Le rail de la mémoire, découpé en stalles — des nombres, pas un affichage.
 *
 * Le bandeau du 4.4 rendait la mémoire en une jauge de six pixels : une part
 * unique, grise, qui disait « c'est plein aux deux tiers » sans dire par qui.
 * Or la question de l'écran n'est pas « combien reste-t-il », c'est **« que
 * faudra-t-il décharger »** — et la réponse tient dans la répartition, pas dans
 * le total. Un rail segmenté par résident la donne d'un coup d'œil, et il la
 * donne pour de bon : `used_bytes` est exactement `Σ peak_bytes` des résidents
 * côté serveur, si bien que les segments couvrent l'occupé sans reste ni
 * arrondi à cacher.
 *
 * Deux décisions valent d'être écrites, parce qu'aucune ne se devine du rendu.
 *
 * **Le rail change d'échelle quand la demande dépasse le budget.** Une jauge qui
 * borne à 100 % — ce que faisait `SyntheseMemoire.part` — répond « plein » aussi
 * bien à un dépassement de 200 Mio qu'à un dépassement de douze gigaoctets, et
 * ces deux situations n'appellent pas le même geste. Ici l'échelle devient
 * `occupé + demandé`, et un **repère** marque où tombe le budget : ce qui est à
 * sa droite est ce qui ne rentre pas, à l'échelle du reste. Tant que tout tient,
 * le repère est à l'extrémité et le rail se lit comme une jauge ordinaire.
 *
 * **L'arrivant est coupé au repère, en deux segments.** Un variant dont la
 * moitié rentre porte une part admissible et une part de trop, et les peindre
 * d'une seule couleur perdrait justement la seule chose qu'on regarde. Le
 * découpage se fait sur les octets et non sur les pourcentages, pour que la
 * somme des parts fasse exactement un.
 *
 * Ce module ne connaît ni couleur ni pixel. Il rend des parts et des mots ; la
 * feuille de style décide de la matière, et le composant du balisage.
 */

import type { Admission, Resident } from "../api/types";
import { formatBytes } from "../format/bytes";
import { etatResident, type SyntheseMemoire } from "./ressources";

/**
 * Ce qu'un segment représente. Jamais une couleur : une couleur seule ne se lit
 * pas, et chaque espèce porte ici le mot qui la nomme.
 */
export type Espece =
  /** Un modèle chargé, qui occupe sa place maintenant. */
  | "resident"
  /** La part de l'arrivant qui tient dans le budget. */
  | "arrivant"
  /** La part de l'arrivant qui n'y tient pas. */
  | "debordement";

export interface Stalle {
  espece: Espece;
  /** La part du rail, dans [0, 1]. La somme des stalles ne dépasse jamais 1. */
  part: number;
  /** Le variant que la stalle nomme. */
  ref: string;
  /**
   * Le nom peint sur la porte du box — la référence sans son variant.
   *
   * `qwen3-tts-1.7b@8bit-mlx` ne tient pas dans une plaque de dix rem, et
   * tronqué à `qwen3-tts-1.7b@8bit…` il n'identifie rien de plus que sa moitié
   * gauche. C'est donc la moitié gauche qu'on peint, et la légende porte la
   * référence entière, comme un registre à côté d'une porte.
   */
  nom: string;
  /** Ce que la stalle vaut en octets. */
  octets: number;
  /**
   * Ce que la stalle fait — « libérable », « à charger », « au-delà du budget ».
   *
   * C'est le mot qui double la couleur, et il est affiché : une barre où seule
   * la teinte distinguerait un modèle chargé d'un modèle qu'on envisage ne se
   * lirait ni en niveaux de gris, ni par un daltonien, ni à voix haute.
   */
  etat: string;
  /** La phrase entière — c'est elle que lit un lecteur d'écran, pas la couleur. */
  titre: string;
}

export interface Rail {
  stalles: readonly Stalle[];
  /**
   * Où tombe le budget sur le rail, dans [0, 1]. Vaut 1 tant que rien ne
   * dépasse — et le composant ne dessine alors pas de repère, l'extrémité du
   * rail disant déjà la même chose.
   */
  repere: number;
  /** Vrai quand l'échelle a dû s'étendre : le budget n'est plus l'extrémité. */
  deborde: boolean;
}

/**
 * Le rail tel qu'il se dessine — résidents d'abord, arrivant ensuite.
 *
 * `admission` n'est prise en compte que si elle chiffre un chargement à venir :
 * un variant déjà résident occupe déjà sa stalle, et lui en ajouter une seconde
 * le compterait deux fois. Un refus, en revanche, est bien dessiné — c'est même
 * le cas où le rail sert le plus, puisqu'il montre de combien on dépasse.
 */
export function railDeMemoire(
  synthese: SyntheseMemoire,
  residents: readonly Resident[],
  admission: Admission | null,
): Rail {
  const budget = synthese.budget;
  if (budget <= 0) return { stalles: [], repere: 1, deborde: false };

  // Un pic d'admission inconnu ne dessine **rien** plutôt qu'une stalle de
  // largeur nulle : c'est la règle « inconnu n'est pas zéro » appliquée à une
  // barre, où une stalle absente et une stalle de zéro pixel se ressemblent trop
  // pour qu'on choisisse la seconde. Le cas se dit ailleurs — `phrasesAdmission`
  // écrit « aucun profil mesuré » sous le rail.
  const attendu = admission?.peak_bytes;
  const demande =
    admission && !admission.already_resident && typeof attendu === "number" && attendu > 0
      ? attendu
      : 0;
  const occupe = residents.reduce((somme, r) => somme + Math.max(0, r.peak_bytes), 0);
  const echelle = Math.max(budget, occupe + demande);

  const stalles: Stalle[] = residents.map((r) => stalle("resident", r.ref, r.peak_bytes, echelle, etatResident(r)));

  if (demande > 0 && admission) {
    // Le découpage porte sur les octets : couper en pourcentages ferait dériver
    // la somme des parts du fait de deux arrondis successifs.
    const dedans = Math.max(0, Math.min(occupe + demande, budget) - occupe);
    const dehors = demande - dedans;
    if (dedans > 0) {
      stalles.push(stalle("arrivant", admission.ref, dedans, echelle, "à charger"));
    }
    if (dehors > 0) {
      stalles.push(stalle("debordement", admission.ref, dehors, echelle, "au-delà du budget"));
    }
  }

  const repere = Math.min(1, budget / echelle);
  return { stalles, repere, deborde: repere < 1 };
}

function stalle(
  espece: Espece,
  ref: string,
  octets: number,
  echelle: number,
  etat: string,
): Stalle {
  return {
    espece,
    part: Math.max(0, octets) / echelle,
    ref,
    nom: ref.split("@")[0] ?? ref,
    octets,
    etat,
    titre: `${ref} — ${formatBytes(octets)}, ${etat}`,
  };
}

/**
 * Une stalle est-elle assez large pour porter son nom à l'intérieur ?
 *
 * Le seuil est empirique et assumé : sous un sixième du rail, une référence
 * comme `hunyuan3d-2.1-shape-mlx@mlx-bf16` sort tronquée à trois lettres, ce qui
 * n'identifie rien et salit la barre. En dessous, le nom vit dans la légende,
 * qui le porte de toute façon.
 */
export function nommable(stalle: Stalle): boolean {
  return stalle.part >= 0.17;
}
