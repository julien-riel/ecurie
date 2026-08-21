import { Cadre } from "./_Cadre";
import type { ViewerProps } from "../registry";

/**
 * Le repli — visible, jamais muet.
 *
 * Un type de média qu'aucune entrée de la table ne reconnaît ne fait pas
 * disparaître la sortie : le chemin et le type restent à l'écran, et le fichier
 * reste accessible dès qu'une URL existe. Une zone blanche serait le seul échec
 * que l'utilisateur ne peut pas diagnostiquer.
 */
export function UnknownViewer(props: ViewerProps) {
  return (
    <Cadre {...props} chemin={String(props.valeur)}>
      <p data-viewer="unknown" className="ecurie-etat-champ">
        aucun visualiseur pour ce type de média — le fichier reste téléchargeable
      </p>
      {props.href ? <a href={props.href}>{String(props.valeur)}</a> : null}
    </Cadre>
  );
}
