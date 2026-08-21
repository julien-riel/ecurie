import { Cadre } from "./_Cadre";
import type { ViewerProps } from "../registry";

/**
 * `model/gltf-binary` — l'emplacement du maillage, pas encore sa visite.
 *
 * `<model-viewer>` de Google est le composant retenu par la conception, et il
 * n'est **pas installé** : quarante mégaoctets pour la seule capacité dont
 * aucun variant n'est exécutable — `image-to-mesh` attend ses 7,37 Go de poids —
 * et, sans route de fichiers, il n'aurait de toute façon aucune URL à charger.
 * L'entrée reste dans la table ; l'import arrive avec le résolveur de fichiers,
 * à la tâche 4.4.
 */
export function MeshViewer(props: ViewerProps) {
  return (
    <Cadre {...props} chemin={String(props.valeur)}>
      <p data-viewer="mesh" className="ecurie-etat-champ">
        maillage prêt à être affiché — le composant model-viewer est chargé avec le résolveur
        de fichiers (tâche 4.4)
      </p>
    </Cadre>
  );
}
