import { Cadre } from "./_Cadre";
import type { ViewerProps } from "../registry";

/**
 * `model/gltf-binary` — le maillage à emporter, pas encore sa visite.
 *
 * `<model-viewer>` de Google est le composant retenu par la conception, et il
 * n'est toujours **pas installé**. La raison a changé de nature mais pas de
 * conclusion : ce n'est plus l'absence d'URL — la route des fichiers en donne
 * une —, c'est qu'aucun maillage n'existe. `image-to-mesh` attend ses 7,37 Go de
 * poids, et `runtimes/hunyuan3d/run.py` n'a jamais tourné (tâche 7.0). Installer
 * quarante mégaoctets de composant pour l'éprouver sur un fichier fabriqué
 * reviendrait à écrire un chemin de code que rien n'exécute — la faute même que
 * le v0.3 a payée trois fois.
 *
 * En attendant, le fichier se télécharge : c'est ce qu'un maillage produit sur
 * cette machine sert à faire, l'ouvrir dans l'outil qui sait déjà l'afficher.
 */
export function MeshViewer(props: ViewerProps) {
  return (
    <Cadre {...props} chemin={String(props.valeur)}>
      <p data-viewer="mesh" className="ecurie-etat-champ">
        maillage produit — le visualiseur 3D attend qu'un modèle en produise un vraiment
        (tâche 7.0)
      </p>
      {props.href ? (
        <a href={props.href} download>
          télécharger {String(props.valeur)}
        </a>
      ) : null}
    </Cadre>
  );
}
