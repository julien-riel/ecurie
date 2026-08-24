/**
 * Les formes du sélecteur — et pourquoi une capacité se dessine en deux temps.
 *
 * Un contrat de capacité est une entrée typée et une sortie typée ; c'est toute
 * l'architecture du projet tenant en une phrase (§4). La glyphe dit donc la même
 * chose : **une forme, une flèche, une forme**. `text-to-speech` montre des
 * lignes de texte devenant une onde ; `face-embed` montre un visage devenant une
 * suite de nombres. On lit ce que la capacité fait avant d'avoir lu son nom.
 *
 * C'est aussi ce qui évite trente-deux dessins arbitraires à apprendre : seize
 * formes se recombinent, et celle qu'on a reconnue chez l'une se reconnaît chez
 * les autres. Une icône par capacité aurait demandé d'inventer un symbole pour
 * « débruitage » et un autre pour « séparation de pistes », que personne
 * n'aurait distingués.
 *
 * Les formes sont tracées, jamais remplies, et n'emploient que `currentColor` :
 * elles suivent la couleur de leur contexte, donc le thème sombre et l'état
 * d'une carte sans qu'aucune règle ne s'en occupe.
 */

import type { Forme } from "./catalogue";

const TRAITS: Record<Forme, React.ReactNode> = {
  // Trois lignes inégales : du texte, et non un paragraphe justifié.
  texte: (
    <>
      <path d="M4 7h16" />
      <path d="M4 12h16" />
      <path d="M4 17h9" />
    </>
  ),
  // Une onde, pas une bulle : ce qui sort d'un moteur de parole est un signal.
  parole: (
    <>
      <path d="M3 12h2" />
      <path d="M7.5 8.5v7" />
      <path d="M12 5v14" />
      <path d="M16.5 9.5v5" />
      <path d="M21 11.5v1" />
    </>
  ),
  musique: (
    <>
      <path d="M9 17V6l10-2v11" />
      <circle cx="6.5" cy="17" r="2.5" />
      <circle cx="16.5" cy="15" r="2.5" />
    </>
  ),
  image: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <circle cx="8.5" cy="10" r="1.6" />
      <path d="M4 17l5-4.5 4 3.5 3-2.5 4 3.5" />
    </>
  ),
  video: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="M10.5 9.5l5 2.5-5 2.5z" />
    </>
  ),
  document: (
    <>
      <path d="M6 3h8l4 4v14H6z" />
      <path d="M14 3v4h4" />
      <path d="M9 12h6" />
      <path d="M9 16h6" />
    </>
  ),
  // Un volume en fil de fer, arêtes cachées comprises : c'est un maillage, pas
  // une photo de cube.
  maillage: (
    <>
      <path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z" />
      <path d="M12 12l8-4.5" />
      <path d="M12 12v9" />
      <path d="M12 12L4 7.5" />
    </>
  ),
  // Des plans qui s'éloignent : le proche épais, le lointain fin.
  profondeur: (
    <>
      <path d="M3 18h18" strokeWidth="2.6" />
      <path d="M5 13h14" strokeWidth="1.6" />
      <path d="M7 9h10" strokeWidth="1" />
      <path d="M9 6h6" strokeWidth="0.7" />
    </>
  ),
  visage: (
    <>
      <path d="M12 3c4 0 6.5 3 6.5 7.5S15.5 21 12 21s-6.5-6-6.5-10.5S8 3 12 3z" />
      <path d="M9.5 10.5h.01" strokeWidth="2.4" />
      <path d="M14.5 10.5h.01" strokeWidth="2.4" />
      <path d="M10 15.5c1.3.9 2.7.9 4 0" />
    </>
  ),
  // Une boîte pleine et une amorcée : ce qu'un détecteur rend, c'est plusieurs
  // rectangles, et l'un d'eux est toujours le bon.
  boites: (
    <>
      <rect x="3" y="6" width="10" height="9" rx="1" />
      <path d="M15 11h6" strokeDasharray="2 2.5" />
      <path d="M18 11v8" strokeDasharray="2 2.5" />
      <path d="M11 17h10v-2" opacity="0.55" />
    </>
  ),
  // Une silhouette découpée dans son cadre — le sujet gardé, le fond rendu.
  masque: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" strokeDasharray="3 2.5" />
      <path d="M12 8.5c2.3 0 3.6 1.8 3.6 4.2S14 17.5 12 17.5s-3.6-2.4-3.6-4.8S9.7 8.5 12 8.5z" fill="currentColor" stroke="none" />
    </>
  ),
  // Un semis qui suit un contour : des points clés, pas une constellation.
  points: (
    <>
      <path d="M6 8.5h.01M9 6h.01M12 5.5h.01M15 6h.01M18 8.5h.01" strokeWidth="2.2" />
      <path d="M6.5 13h.01M9.5 11.5h.01M14.5 11.5h.01M17.5 13h.01" strokeWidth="2.2" />
      <path d="M9 16.5h.01M12 18h.01M15 16.5h.01" strokeWidth="2.2" />
    </>
  ),
  // Des nombres alignés, de hauteurs quelconques : un vecteur se lit comme un
  // relevé, pas comme une image.
  vecteur: (
    <>
      <path d="M4 14v4" />
      <path d="M7.4 9v9" />
      <path d="M10.8 12v6" />
      <path d="M14.2 6v12" />
      <path d="M17.6 11v7" />
      <path d="M21 8v10" />
    </>
  ),
  // Un arc et son rayon : un angle, ce qui n'est ni une flèche ni une boussole.
  angles: (
    <>
      <path d="M5 19h14" />
      <path d="M5 19L17 7" />
      <path d="M13 19a8 8 0 0 0-2.4-5.7" />
    </>
  ),
  squelette: (
    <>
      <circle cx="12" cy="5" r="2.2" />
      <path d="M12 7.5v6" />
      <path d="M7.5 10h9" />
      <path d="M12 13.5L8.5 20" />
      <path d="M12 13.5L15.5 20" />
    </>
  ),
  // Deux accolades : ce que rend une capacité dont la sortie est une structure.
  donnees: (
    <>
      <path d="M9 4c-2 0-2.5 1-2.5 3v2c0 1.6-.6 2.6-2 3 1.4.4 2 1.4 2 3v2c0 2 .5 3 2.5 3" />
      <path d="M15 4c2 0 2.5 1 2.5 3v2c0 1.6.6 2.6 2 3-1.4.4-2 1.4-2 3v2c0 2-.5 3-2.5 3" />
    </>
  ),
};

export function Glyphe({ forme, taille = 22 }: { forme: Forme; taille?: number }) {
  return (
    <svg
      className="ecurie-glyphe"
      width={taille}
      height={taille}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {TRAITS[forme]}
    </svg>
  );
}

/**
 * La transformation entière : ce qui entre, ce qui sort.
 *
 * `aria-hidden` parce que rien ici n'ajoute d'information à qui n'y voit pas :
 * la carte porte déjà le titre de la capacité et sa description. Une glyphe
 * décrite à voix haute — « texte flèche onde » — ferait perdre du temps sans
 * rien apprendre.
 */
export function GlypheFlux({ entree, sortie }: { entree: Forme; sortie: Forme }) {
  return (
    <span className="ecurie-transformation" aria-hidden="true">
      <Glyphe forme={entree} />
      <svg
        className="ecurie-transformation-fleche"
        width="14"
        height="14"
        viewBox="0 0 14 14"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
        focusable="false"
      >
        <path d="M2 7h9" />
        <path d="M8 4l3 3-3 3" />
      </svg>
      <Glyphe forme={sortie} />
    </span>
  );
}
