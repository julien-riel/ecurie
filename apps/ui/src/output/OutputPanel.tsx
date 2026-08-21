/**
 * La sortie d'un job, aplatie puis aiguillée — un composant par type de média.
 *
 * Aucun job n'est encore soumis au 4.3 : ce panneau existe et se prouve sur des
 * sorties fabriquées, parce que c'est la moitié « visualiseurs par media type »
 * du livrable. La tâche 4.4 lui passera une vraie réponse et un vrai résolveur
 * de fichiers ; rien d'autre ne changera ici.
 */

import type { FileResolver } from "./files";
import { NO_FILE } from "./files";
import { flattenOutput } from "./flatten";
import { viewerFor } from "./registry";

export interface OutputPanelProps {
  sortie: Record<string, unknown> | null;
  /** `output_media_types` du contrat : chemins pointés vers types de média. */
  mediaTypes: Record<string, string>;
  /** Par défaut `NO_FILE` — aucune route ne sert les fichiers avant le 4.4. */
  resoudre?: FileResolver;
}

export function OutputPanel({ sortie, mediaTypes, resoudre = NO_FILE }: OutputPanelProps) {
  const rendus = flattenOutput(sortie, mediaTypes);
  if (rendus.length === 0) {
    return <p className="ecurie-etat-champ">aucune sortie</p>;
  }
  return (
    <div className="ecurie-sorties">
      {rendus.map((r) => {
        const entree = viewerFor(r.mediaType);
        const Vue = entree.Component;
        const href = r.mediaType && typeof r.valeur === "string" ? resoudre(r.valeur) : null;
        return (
          <Vue
            key={r.chemin}
            nom={r.nom}
            chemin={r.chemin}
            valeur={r.valeur}
            mediaType={r.mediaType}
            href={href}
          />
        );
      })}
    </div>
  );
}
