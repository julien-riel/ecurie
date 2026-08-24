import { Cadre } from "./_Cadre";
import type { ViewerProps } from "../registry";

/**
 * `model/*` — une géométrie qui n'est pas un maillage à regarder.
 *
 * Arrivé avec `pointcloud-to-cad`, qui rend un fichier STEP : le format
 * d'échange de la CAO, lisible par tous les modeleurs et par aucun navigateur.
 * Il n'y a rien à afficher, et c'est **la bonne réponse** plutôt qu'un défaut —
 * un STEP porte des surfaces exactes et un arbre de construction, pas des
 * triangles ; le rendre en image reviendrait à montrer le maillage qu'on en
 * déduit, c'est-à-dire à effacer ce qui le distingue.
 *
 * La même capacité rend d'ailleurs les deux : le STEP pour l'outil de CAO, le
 * GLB pour l'œil. C'est le second qui tombe sur `MeshViewer`, et le premier ici.
 *
 * Distinct du repli `UnknownViewer` : celui-ci dit « je ne sais pas ce que
 * c'est », celui-là dit « je sais exactement ce que c'est, et cela s'ouvre
 * ailleurs ». La différence se voit à l'écran, et elle évite de faire chercher
 * un visualiseur manquant qui n'a aucune raison d'exister.
 */
export function GeometryViewer(props: ViewerProps) {
  return (
    <Cadre {...props} chemin={String(props.valeur)}>
      <p data-viewer="geometrie" className="ecurie-etat-champ">
        géométrie d'échange ({props.mediaType}) — se lit dans un modeleur, pas dans un
        navigateur
      </p>
      {props.href ? (
        <a href={props.href} download>
          télécharger {String(props.valeur)}
        </a>
      ) : null}
    </Cadre>
  );
}
