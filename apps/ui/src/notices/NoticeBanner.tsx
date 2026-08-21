/**
 * Ce que le registre et le front ont à redire — visible, jamais bloquant.
 *
 * Le serveur a un comportement délibéré que ce bandeau prolonge : **il répond
 * malgré un registre en erreur.** La CLI, elle, s'arrête, et c'est le bon geste
 * avant d'exécuter quelque chose ; une UI à qui l'on ne rendrait rien parce
 * qu'un manifeste sur six a une révision non épinglée n'aurait aucun moyen
 * d'apprendre lequel. Les modèles valides sont servis, `issues` transporte le
 * reste — et c'est ici qu'il s'affiche.
 *
 * Aucun chemin de code ne conditionne donc le rendu du formulaire au fait que
 * `issues` soit vide. Le bandeau est replié par défaut : ces constats sont du
 * travail de registre, pas une interruption de ce qu'on est en train de faire.
 */

import { useState } from "react";
import type { Notice } from "./notices";

export interface NoticeBannerProps {
  notices: readonly Notice[];
}

const RANG: Record<Notice["severite"], number> = { error: 0, warning: 1, info: 2 };

export function NoticeBanner({ notices }: NoticeBannerProps) {
  const [ouvert, setOuvert] = useState(false);
  if (notices.length === 0) return null;

  const triés = [...notices].sort((a, b) => RANG[a.severite] - RANG[b.severite]);
  const erreurs = triés.filter((n) => n.severite === "error").length;
  const avertissements = triés.filter((n) => n.severite === "warning").length;
  const avis = triés.length - erreurs - avertissements;

  const résumé = [
    erreurs ? `${erreurs} erreur(s) de registre` : null,
    avertissements ? `${avertissements} avertissement(s)` : null,
    avis ? `${avis} champ(s) rendu(s) autrement qu'annoncé` : null,
  ]
    .filter(Boolean)
    .join(", ");

  return (
    <aside className="ecurie-avis" data-avis={triés.length}>
      <button type="button" onClick={() => setOuvert((o) => !o)} aria-expanded={ouvert}>
        {ouvert ? "▾" : "▸"} {résumé}
      </button>
      {ouvert ? (
        <ul>
          {triés.map((n, i) => (
            <li key={`${n.source}-${i}`} data-severite={n.severite}>
              <code>{n.source}</code> — {n.message}
            </li>
          ))}
        </ul>
      ) : null}
    </aside>
  );
}
