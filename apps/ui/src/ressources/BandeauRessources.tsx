/**
 * Le bandeau de ressources — un rang de boxes, la place qui reste, et ce que lancer déchargera.
 *
 * Il ne charge rien lui-même : **l'écran lui passe le sondage**. Ce n'est pas de
 * l'injection par principe, c'est la conséquence d'un doublon découvert en
 * branchant l'Atelier. Les deux ont besoin de la même réponse — le bandeau pour
 * les chiffres, l'écran pour savoir si le variant est résident et pour peupler
 * les `x-options-from` du contrat, qui n'ont pas d'autre source que le champ
 * `options` d'un worker chargé. Deux sondages sur la même route doubleraient les
 * requêtes, et le second n'aurait pas la même cadence : les voix d'un modèle
 * chargé après l'ouverture de l'écran n'y seraient jamais apparues.
 *
 * Il reste global par sa nature — n'importe quel écran peut le poser — et pas
 * par sa place dans l'arbre : le chiffre qui compte est « ce que lancer *ce
 * variant-là* coûterait », et seul l'écran sait lequel est choisi.
 *
 * **Ce qu'il montre a changé, et pas seulement sa peinture.** Le 4.4 rendait la
 * mémoire en une part unique — une jauge grise de six pixels — sous laquelle
 * s'empilait une liste à puces des résidents. Cette forme répondait à « c'est
 * plein aux deux tiers » et à rien d'autre, alors que la question de l'écran est
 * **« que faudra-t-il décharger »**. Le rail segmenté la prend en charge : une
 * plaque par modèle chargé, à sa largeur réelle, et la part de l'arrivant
 * hachurée à la suite. Le calcul vit dans `stalles.ts`, testé sans rendre un
 * composant, comme tout ce qui décide ici.
 *
 * Le chiffre mis en gros est le **libre**, non l'occupé. C'est celui qu'on
 * regarde avant de lancer ; l'occupé se lit dans le rail, qui le montre réparti,
 * et une seconde fois en toutes lettres sous la barre pour qui veut la somme.
 *
 * Deux prudences qui ne se voient pas à la lecture du rendu :
 *
 * **L'admission affichée est vérifiée contre le variant demandé.** Un changement
 * de variant laisse en place, le temps d'une requête, la réponse du précédent ;
 * afficher son chiffre annoncerait le coût d'un autre modèle. Le serveur
 * normalise la référence — il accepte un id de modèle seul quand le choix est
 * évident —, mais le front n'envoie que des `variant.ref` complets, si bien que
 * l'égalité est exacte.
 *
 * **Un échec ne vide pas le bandeau.** Les derniers chiffres restent, datés de
 * l'heure où ils étaient vrais. Un bandeau vide pendant qu'on redémarre le
 * serveur ferait perdre ce qu'on regardait ; des chiffres non datés qui ont
 * cessé d'être vrais seraient pires que les deux.
 */

import type { CSSProperties } from "react";
import type { ResidentsResponse } from "../api/types";
import { phraseErreur } from "../api/errors";
import type { Sondage } from "../api/useSondage";
import { formatBytes } from "../format/bytes";
import { heureLocale } from "../format/dates";
import {
  phraseBudget,
  phraseFantomes,
  phrasesAdmission,
  severiteAdmission,
  synthese,
} from "./ressources";
import { nommable, railDeMemoire, type Stalle } from "./stalles";

export interface BandeauRessourcesProps {
  /** Le sondage de `/runtime/residents`, tenu par l'écran. */
  sondage: Sondage<ResidentsResponse>;
  /** Le variant en cours de composition — celui que le sondage a demandé en `?for=`. */
  pour?: string | null;
  /**
   * Le paramètre dont dépend le pic de ce variant, s'il en a un. Le bandeau
   * chiffre le variant seul : il doit dire quand ce chiffre bougera avec la
   * saisie, faute de quoi il annonce 13,8 Gio pour un job qui en demande 23,9.
   */
  picParametre?: string | null;
}

export function BandeauRessources({
  sondage,
  pour = null,
  picParametre = null,
}: BandeauRessourcesProps) {
  const parc = sondage.données;
  const état = synthese(parc);
  const admission = parc?.admission && parc.admission.ref === pour ? parc.admission : null;
  const lignes = phrasesAdmission(admission);
  const sévérité = severiteAdmission(admission);

  if (état === null || parc === null) {
    return (
      <aside className="ecurie-bandeau" aria-label="Ressources">
        <p className="ecurie-etat-champ">
          {sondage.erreur ? phraseErreur(sondage.erreur) : "lecture de la mémoire…"}
        </p>
      </aside>
    );
  }

  const fantômes = phraseFantomes(état);
  const rail = railDeMemoire(état, parc.residents, admission);

  return (
    <aside className="ecurie-bandeau" aria-label="Ressources">
      <p className="ecurie-budget">
        <span className="ecurie-budget-libre">{formatBytes(état.libre)}</span>
        <span className="ecurie-budget-sur">libres sur {formatBytes(état.budget)}</span>
      </p>

      {/*
        Le rail entier porte une seule étiquette : la phrase du budget. Un
        lecteur d'écran qui parcourrait douze segments annoncerait douze fois
        « image » sans jamais dire combien il reste ; la légende juste en dessous
        redit chaque stalle en texte, dans l'ordre, pour qui veut le détail.
      */}
      <div className="ecurie-rail" role="img" aria-label={phraseBudget(état)}>
        {rail.stalles.map((stalle) => (
          <span
            key={clefDeStalle(stalle)}
            className="ecurie-stalle"
            data-espece={stalle.espece}
            style={{ "--part": `${(stalle.part * 100).toFixed(2)}%` } as CSSProperties}
            title={stalle.titre}
          >
            {nommable(stalle) ? <span className="ecurie-stalle-nom">{stalle.nom}</span> : null}
          </span>
        ))}
        {rail.deborde ? (
          <span
            className="ecurie-repere"
            style={{ "--repere": `${(rail.repere * 100).toFixed(2)}%` } as CSSProperties}
            aria-hidden="true"
          />
        ) : null}
      </div>

      {rail.stalles.length === 0 ? (
        <p className="ecurie-vide">aucun modèle résident — l'écurie est vide</p>
      ) : (
        <ul className="ecurie-legende">
          {rail.stalles.map((stalle) => (
            <li key={clefDeStalle(stalle)} data-espece={stalle.espece}>
              <span className="ecurie-legende-pastille" aria-hidden="true" />
              <code className="ecurie-legende-ref">{stalle.ref}</code>
              <span className="ecurie-legende-poids">{formatBytes(stalle.octets)}</span>
              <span className="ecurie-legende-etat">{stalle.etat}</span>
            </li>
          ))}
        </ul>
      )}

      {/*
        L'occupé se dit ici en chiffre, et le libre n'y est pas répété : il est
        déjà en gros au-dessus. `phraseBudget` porte les trois nombres d'un coup
        et reste l'étiquette du rail, où un lecteur d'écran les obtient dans la
        même phrase.
      */}
      <p className="ecurie-source">
        {formatBytes(état.occupe)} occupés · {état.mesure ? "budget mesuré" : "budget estimé"} ·{" "}
        {état.source}
      </p>

      {fantômes ? <p className="ecurie-fantomes">{fantômes}</p> : null}

      {lignes.length ? (
        <ul className="ecurie-admission" data-severite={sévérité}>
          {lignes.map((ligne) => (
            <li key={ligne}>{ligne}</li>
          ))}
        </ul>
      ) : null}

      {picParametre ? (
        <p className="ecurie-etat-champ">
          Ce pic est celui du variant seul : il dépend de « {picParametre} », et le chiffre ne
          suivra la saisie qu'à la tâche 4.7. Chiffrer le job donne la valeur exacte pour l'entrée
          en cours.
        </p>
      ) : null}

      {sondage.erreur ? (
        <p className="text-danger">
          contact perdu avec le serveur — chiffres de {heureLocale(sondage.vu)} ·{" "}
          {phraseErreur(sondage.erreur)}
        </p>
      ) : null}
      {!sondage.actif ? (
        <p className="ecurie-etat-champ">
          onglet en arrière-plan : le sondage dort, chiffres de {heureLocale(sondage.vu)}
        </p>
      ) : null}
    </aside>
  );
}

/**
 * La référence ne suffit pas comme clé : un arrivant qui déborde donne deux
 * stalles du même variant, l'une dans le budget et l'autre au-delà.
 */
function clefDeStalle(stalle: Stalle): string {
  return `${stalle.espece}-${stalle.ref}`;
}
