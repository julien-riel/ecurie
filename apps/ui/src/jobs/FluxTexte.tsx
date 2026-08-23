/**
 * Le texte d'un job pendant qu'il s'écrit — le raisonnement et la réponse.
 *
 * Ce composant n'affiche **jamais** le résultat final : celui-ci est un fichier,
 * il passe par `OutputPanel`, et il fait foi. Ce qu'on montre ici est le flux,
 * c'est-à-dire ce qu'on a vu arriver. Les deux coexistent sans se contredire,
 * et l'un disparaît quand l'autre paraît — sauf dans le seul cas où le flux est
 * tout ce qui reste : un job arrêté en cours de route, dont aucun fichier n'a
 * été écrit.
 *
 * **Le raisonnement est replié par défaut, et il est là.** Le replier revient à
 * dire ce qu'il est : un brouillon, que le modèle s'adresse à lui-même, et dont
 * la place n'est pas au même niveau que la réponse. Le supprimer reviendrait à
 * dire qu'il n'existe pas — or c'est lui qui explique une réponse surprenante,
 * et c'est souvent la seule chose à lire quand la réponse déçoit. Il s'ouvre
 * d'un clic et reste ouvert.
 *
 * **Il s'ouvre tout seul tant que la réponse n'a pas commencé.** Un modèle qui
 * raisonne trente secondes avant d'écrire son premier mot laisserait sinon un
 * écran vide sous une barre de progression — exactement l'attente que le flux
 * existe pour supprimer.
 */

import { useEffect, useRef, useState } from "react";
import type { Job } from "../api/types";

export interface FluxTexteProps {
  job: Job;
}

export function FluxTexte({ job }: FluxTexteProps) {
  const raisonnement = job.stream_reasoning ?? "";
  const reponse = job.stream_text ?? "";
  const [ouvertParChoix, setOuvertParChoix] = useState<boolean | null>(null);

  // Tant que la réponse n'a rien donné, le raisonnement est la seule chose à
  // voir : on l'ouvre. Dès qu'elle commence, on le referme — mais jamais contre
  // un choix explicite de l'utilisateur, qui l'emporte à partir du premier clic.
  const ouvert = ouvertParChoix ?? (raisonnement !== "" && reponse === "");

  const finReponse = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    // Suivre le texte qui s'écrit, sans emporter la page avec lui : `nearest`
    // ne fait défiler que si la fin est sortie du cadre.
    //
    // Le test d'existence n'est pas une précaution de style : jsdom ne fournit
    // pas `scrollIntoView`, et l'appeler sans lui faisait tomber le composant
    // entier — donc l'écran — sur un confort d'affichage. Un défilement qui
    // n'arrive pas ne coûte rien ; une exception pendant le rendu coûte tout.
    finReponse.current?.scrollIntoView?.({ block: "nearest" });
  }, [reponse]);

  if (!raisonnement && !reponse) return null;

  return (
    <div className="ecurie-flux" aria-label="Texte produit">
      {raisonnement ? (
        <details
          className="ecurie-flux-raisonnement"
          open={ouvert}
          onToggle={(evenement) => setOuvertParChoix(evenement.currentTarget.open)}
        >
          <summary>
            Raisonnement du modèle
            <span className="ecurie-flux-compte"> · {raisonnement.length} caractères</span>
          </summary>
          {/*
            `pre-wrap` et non un rendu Markdown : c'est un brouillon, il est
            souvent mal formé, et le mettre en forme lui donnerait une autorité
            qu'il n'a pas.
          */}
          <p className="ecurie-flux-texte">{raisonnement}</p>
        </details>
      ) : null}

      {reponse ? (
        <div className="ecurie-flux-reponse">
          <p className="ecurie-flux-texte">{reponse}</p>
          <div ref={finReponse} />
        </div>
      ) : null}
    </div>
  );
}
