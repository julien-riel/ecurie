import { Cadre } from "./_Cadre";
import { useContenu } from "./useContenu";
import type { ViewerProps } from "../registry";

/**
 * `application/json` — la ligne que la conception oublie.
 *
 * `CONCEPTION.md §7` énumère cinq types et n'en nomme pas le JSON, alors que
 * trois contrats en produisent : la structure de page de l'OCR, les métriques de
 * transcription, et surtout `tool-use.calls`, qui est une sortie **requise** et
 * toujours écrite — « un fichier absent et une liste vide ne disent pas la même
 * chose ». Sans cette entrée, la seule sortie obligatoire d'une capacité du parc
 * tomberait sur le visualiseur de repli.
 *
 * Le JSON est réindenté quand il est lisible, et rendu brut sinon : un fichier
 * tronqué se lit mieux tel quel qu'écrasé par un message d'analyse.
 */
export function JsonViewer(props: ViewerProps) {
  const { texte, erreur, en_cours } = useContenu(props.href);
  let affiche = texte;
  if (texte) {
    try {
      affiche = JSON.stringify(JSON.parse(texte), null, 2);
    } catch {
      affiche = texte;
    }
  }
  return (
    <Cadre {...props} chemin={String(props.valeur)}>
      {erreur ? (
        <p className="text-danger">lecture impossible : {erreur}</p>
      ) : (
        <pre className="ecurie-json" data-viewer="json">
          {en_cours ? "lecture…" : affiche}
        </pre>
      )}
    </Cadre>
  );
}
