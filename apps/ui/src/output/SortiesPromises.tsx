/**
 * Ce que le job produirait, tant qu'aucun job ne peut se lancer.
 *
 * L'Atelier du 4.4 est complet sauf son bouton *Lancer* : `POST /jobs` attend le
 * déménagement du superviseur dans le processus de l'API. La question est donc
 * ce qu'on met à la place de la zone de sortie, et il n'y a que trois réponses
 * possibles.
 *
 * Rien du tout — l'écran ne dit pas ce qu'on obtiendrait, alors que le contrat
 * le sait. Une sortie fabriquée passée à `OutputPanel`, comme le faisait le banc
 * du 4.3 — huit visualiseurs sur des fichiers qui n'existent pas, ce qui prouve
 * l'aiguillage mais ment sur l'état du job. Ou bien la promesse du contrat,
 * annoncée pour ce qu'elle est.
 *
 * C'est la troisième, et elle a survécu à l'arrivée du bouton *Lancer* : ce
 * composant reste ce que l'écran montre **avant** le premier job, et
 * `OutputPanel` prend sa place dès qu'il y a une réponse. Le titre change avec
 * eux — « ce que ce job produirait », puis « ce que ce job a produit ».
 */

import type { Capability } from "../api/types";
import { promessesDeSortie } from "./promesses";

export interface SortiesPromisesProps {
  capability: Pick<Capability, "output" | "output_media_types">;
}

export function SortiesPromises({ capability }: SortiesPromisesProps) {
  const promesses = promessesDeSortie(capability);
  if (promesses.length === 0) {
    return <p className="ecurie-etat-champ">ce contrat ne déclare aucune sortie</p>;
  }
  return (
    <ul className="ecurie-promesses">
      {promesses.map((p) => (
        <li key={p.chemin} data-fichier={p.mediaType ? "oui" : "non"}>
          <code className="ecurie-sortie-nom">{p.chemin}</code>{" "}
          <span className="ecurie-sortie-type">
            {p.mediaType ?? "valeur inline"}
            {p.requis ? " · toujours produit" : ""}
          </span>
          {p.description ? <p className="ecurie-etat-champ">{p.description}</p> : null}
        </li>
      ))}
    </ul>
  );
}
