/**
 * Le Parc — ce que le disque contient, ce qu'on pourrait en reprendre. Deuxième écran.
 *
 * Il porte la comptabilité disque que le §6.2 de l'architecture décrit, et il la
 * porte pour la même raison que l'Atelier porte les jobs : tant qu'elle n'existe
 * qu'en ligne de commande, le critère de sortie du v0.4 — « une semaine d'usage
 * où l'UI est le chemin par défaut, sans retomber sur les scripts d'origine » —
 * n'est pas atteignable.
 *
 * Trois décisions le gouvernent, et aucune n'était écrite au plan.
 *
 * **Il ne sonde pas.** Le bandeau de ressources se rafraîchit toutes les deux
 * secondes parce que la mémoire bouge sans qu'on touche à l'écran — un `ecurie
 * run` dans un terminal charge un modèle, un worker meurt. Le disque observé,
 * lui, ne bouge qu'après un `ecurie store scan`, qui est une commande qu'on
 * tape ; et derrière chaque lecture il y a une classification de tout le parc
 * par contenu. Sonder ferait tourner ce calcul en boucle pour rendre trois fois
 * le même chiffre. Un bouton *Relire* remplace le sondage, et il suffit.
 *
 * **Rien ici ne touche au disque, et c'est un choix, pas une étape suivante.**
 * Appliquer un plan relit chaque fichier pour prouver son contenu avant d'y
 * toucher ; déporter un variant copie des giga-octets et laisse un `tier: cold`
 * à committer à la main. Deux opérations longues, irréversibles à l'échelle
 * d'une quarantaine, et dont la seconde n'est de toute façon pas complète sans
 * un passage par Git. L'écran montre ce qu'elles donneraient et rend la commande
 * qui les lance.
 *
 * **Trois lectures indépendantes, et non une.** `/store/summary`, `/store/plan`
 * et `/store/tiering` répondent à trois questions séparées, et une qui échoue ne
 * doit pas emporter les deux autres : un parc sans volume de tiering déclaré n'a
 * aucune raison de perdre ses trois chiffres. Chacune affiche son propre échec à
 * sa place.
 *
 * Le bandeau de ressources n'est pas ici. Il est global par sa nature — n'importe
 * quel écran peut le poser —, mais il parle de mémoire unifiée, et cet écran
 * parle de disque. Les deux se comptent même dans des unités différentes : Gio
 * binaires pour le budget Metal, Go décimaux pour le disque, comme la CLI.
 */

import { useState } from "react";
import * as api from "../api/endpoints";
import { phraseErreur } from "../api/errors";
import { useResource } from "../api/useResource";
import { isoUtc } from "../format/dates";
import { Occupation } from "../parc/Occupation";
import { PlanDeGC } from "../parc/PlanDeGC";
import { Tiering } from "../parc/Tiering";

export function Parc() {
  const [verifiedOnly, setVerifiedOnly] = useState(false);
  const résumé = useResource((s) => api.storeSummary(undefined, s), []);
  const plan = useResource((s) => api.storePlan({ verifiedOnly }, s), [verifiedOnly]);
  const tiering = useResource((s) => api.storeTiering(s), []);

  function relire() {
    résumé.recharger();
    plan.recharger();
    tiering.recharger();
  }

  const figures = résumé.données?.figures ?? null;
  const enCours = résumé.en_cours || plan.en_cours || tiering.en_cours;

  return (
    <section className="ecurie-parc" aria-label="Parc">
      <div className="ecurie-parc-entete">
        <button type="button" onClick={relire} disabled={enCours}>
          {enCours ? "lecture…" : "Relire"}
        </button>
        {résumé.données?.last_scan_at ? (
          <p className="ecurie-etat-champ">
            État observé du {isoUtc(résumé.données.last_scan_at)} — il ne change qu'après un{" "}
            <code>ecurie store scan</code>.
          </p>
        ) : null}
      </div>

      {/*
        Le premier avis est celui qui commande : des chiffres périmés décrivent
        un disque qui n'existe plus, et planifier dessus reviendrait à proposer
        de récupérer des octets déjà repris.
      */}
      {résumé.données?.stale ? <p className="text-danger">{résumé.données.hint}</p> : null}

      {résumé.erreur ? <p className="text-danger">{phraseErreur(résumé.erreur)}</p> : null}

      {résumé.données && !résumé.données.scanned ? <PasDeScan hint={résumé.données.hint} /> : null}

      {figures ? <Occupation figures={figures} telemetry={résumé.données?.telemetry} /> : null}

      {plan.erreur ? (
        <div className="ecurie-carte">
          <p className="ecurie-plaque">ce qu'on pourrait reprendre</p>
          <h2>Plan de récupération</h2>
          <p className="text-danger">{phraseErreur(plan.erreur)}</p>
        </div>
      ) : null}
      {plan.données?.plan ? (
        <PlanDeGC
          plan={plan.données.plan}
          labels={plan.données.labels}
          command={plan.données.command}
          verifiedOnly={verifiedOnly}
          onVerifiedOnly={setVerifiedOnly}
          enCours={plan.en_cours}
        />
      ) : null}

      {tiering.erreur ? (
        <div className="ecurie-carte">
          <p className="ecurie-plaque">ce qu'on pourrait mettre au pré</p>
          <h2>Tiering</h2>
          <p className="text-danger">{phraseErreur(tiering.erreur)}</p>
        </div>
      ) : null}
      {tiering.données ? (
        <Tiering
          volumes={tiering.données.volumes}
          cold={tiering.données.cold}
          variants={tiering.données.variants}
        />
      ) : null}
    </section>
  );
}

/**
 * Aucun scan n'a eu lieu — et la réponse le dit, plutôt que d'afficher des zéros.
 *
 * C'est la règle « inconnu n'est pas zéro », appliquée à l'écran entier : des
 * chiffres nuls se liraient « le parc est vide », ce qui est faux et décourage
 * précisément la commande qui les remplirait. La route ne scanne pas d'elle-même,
 * un scan prenant des dizaines de secondes et écrivant dans la base.
 */
function PasDeScan({ hint }: { hint: string | null | undefined }) {
  return (
    <div className="ecurie-avis">
      <p>Le parc n'a jamais été regardé.</p>
      <p className="ecurie-etat-champ">
        {hint ??
          "lancer ecurie store scan pour remplir ~/.ecurie/state.db (lecture seule, rien n'est modifié sur le disque scanné)"}
      </p>
    </div>
  );
}
