/**
 * La sortie d'un job, aplatie puis aiguillée — un composant par type de média.
 *
 * Écrit au 4.3 sur des sorties fabriquées, il reçoit depuis la fin du 4.4 la
 * vraie réponse d'un vrai job, et **rien n'a bougé** sauf ce qui devait : le
 * résolveur, qui n'était plus qu'un point d'injection, en est un vrai.
 *
 * Ce qu'on résout est le chemin **pointé** de la sortie et non sa valeur. Les
 * deux se ressemblent à s'y méprendre — `tracks.vocals` contre
 * `tracks/vocals.wav` — mais seul le premier est une clé de `files`, la table
 * que le serveur compose. Passer la seconde obligerait à l'inverser pour
 * retrouver la première.
 */

import type { FileResolver } from "./files";
import { NO_FILE } from "./files";
import { flattenOutput } from "./flatten";
import { viewerFor } from "./registry";

export interface OutputPanelProps {
  sortie: Record<string, unknown> | null;
  /** `output_media_types` du contrat : chemins pointés vers types de média. */
  mediaTypes: Record<string, string>;
  /** Par défaut `NO_FILE` — une sortie montrée sans job derrière n'a pas d'URL. */
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
        const href = r.mediaType && typeof r.valeur === "string" ? resoudre(r.chemin) : null;
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
