import { Cadre } from "./_Cadre";
import { useContenu } from "./useContenu";
import type { ViewerProps } from "../registry";

/**
 * `text/*` — le contenu du fichier, pas son chemin.
 *
 * La sortie déclarée par le contrat est un **chemin** ; l'afficher tel quel dans
 * un cadre de texte montrerait « transcription.txt » à la place de la
 * transcription. Le fichier est donc lu.
 *
 * `CONCEPTION.md §7` range aussi ici le diff de l'OCR. Il n'y a rien à comparer
 * tant qu'une seule sortie existe : le diff appartient à l'écran Confrontation
 * (tâche 5.4), qui met deux variants côte à côte sur la même entrée.
 */
export function TextViewer(props: ViewerProps) {
  const { texte, erreur, en_cours } = useContenu(props.href);
  return (
    <Cadre {...props} chemin={String(props.valeur)}>
      {erreur ? (
        <p className="text-danger">lecture impossible : {erreur}</p>
      ) : (
        <pre className="ecurie-texte" data-viewer="text">
          {en_cours ? "lecture…" : texte}
        </pre>
      )}
    </Cadre>
  );
}
