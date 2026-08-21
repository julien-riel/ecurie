/**
 * Ce que tout visualiseur montre autour de la sortie : son nom, son type, son chemin.
 *
 * Le cadre existe parce que `href: null` reste un **état normal**, même depuis
 * qu'une route sert les fichiers de job : une sortie facultative que le worker
 * n'a pas produite n'a pas d'URL, et `audio-separation` en déclare cinq pour
 * n'en rendre que deux ou quatre. Sans lui, chaque composant devrait répéter la
 * même retenue, et l'un d'eux finirait par afficher une zone blanche, qui est le
 * seul échec que l'utilisateur ne peut pas diagnostiquer.
 *
 * Il n'escamote pas son contenu pour autant : le visualiseur est **toujours**
 * rendu, et c'est lui qui sait quoi faire d'une URL absente — un `<audio>` sans
 * source est inerte mais présent, un message de repli reste lisible. Masquer les
 * enfants rendrait les huit visualiseurs identiques dès qu'une URL manque, et
 * l'aiguillage ne se verrait jamais.
 */

import type { ReactNode } from "react";

export interface CadreProps {
  nom: string;
  chemin: string;
  mediaType: string | null;
  href: string | null;
  children?: ReactNode;
}

export function Cadre({ nom, chemin, mediaType, href, children }: CadreProps) {
  return (
    <figure className="ecurie-sortie">
      <figcaption>
        <span className="ecurie-sortie-nom">{nom}</span>
        {mediaType ? <code className="ecurie-sortie-type">{mediaType}</code> : null}
      </figcaption>
      {children}
      {href ? null : (
        <>
          <p className="ecurie-sortie-absente">{chemin}</p>
          <p className="ecurie-etat-champ">fichier non résolu — aucune URL pour cette sortie</p>
        </>
      )}
    </figure>
  );
}
