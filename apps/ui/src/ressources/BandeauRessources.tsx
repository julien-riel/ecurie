/**
 * Le bandeau de ressources — mémoire résidente, budget restant, ce que lancer déchargera.
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
 * Il reste global par sa nature — n'importe quel écran peut le poser, le Parc le
 * fera au 4.5 — et pas par sa place dans l'arbre : le chiffre qui compte est
 * « ce que lancer *ce variant-là* coûterait », et seul l'écran sait lequel est
 * choisi.
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

import type { ResidentsResponse } from "../api/types";
import { phraseErreur } from "../api/errors";
import type { Sondage } from "../api/useSondage";
import { formatBytes } from "../format/bytes";
import { heureLocale } from "../format/dates";
import {
  etatResident,
  phraseBudget,
  phraseFantomes,
  phrasesAdmission,
  severiteAdmission,
  synthese,
} from "./ressources";

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
  const fantômes = état ? phraseFantomes(état) : null;

  return (
    <aside className="ecurie-bandeau" aria-label="Ressources">
      {état === null || parc === null ? (
        <p className="ecurie-etat-champ">
          {sondage.erreur ? phraseErreur(sondage.erreur) : "lecture de la mémoire…"}
        </p>
      ) : (
        <>
          <div className="ecurie-jauge" role="img" aria-label={phraseBudget(état)}>
            <span style={{ width: `${Math.round(état.part * 100)}%` }} />
          </div>
          <p className="ecurie-budget">
            <strong>{phraseBudget(état)}</strong>{" "}
            <span className="ecurie-etat-champ">
              {état.mesure ? "budget mesuré" : "budget estimé"} · {état.source}
            </span>
          </p>

          {parc.residents.length === 0 ? (
            <p className="ecurie-etat-champ">aucun modèle résident</p>
          ) : (
            <ul className="ecurie-residents">
              {parc.residents.map((r) => (
                <li key={r.ref} data-occupe={r.busy || r.pinned ? "oui" : "non"}>
                  <code>{r.ref}</code> {formatBytes(r.peak_bytes)} · {etatResident(r)}
                </li>
              ))}
            </ul>
          )}

          {fantômes ? <p className="text-danger">{fantômes}</p> : null}

          {lignes.length ? (
            <ul className="ecurie-admission" data-severite={sévérité}>
              {lignes.map((ligne) => (
                <li key={ligne}>{ligne}</li>
              ))}
            </ul>
          ) : null}

          {picParametre ? (
            <p className="ecurie-etat-champ">
              Ce pic est celui du variant seul : il dépend de « {picParametre} », et le
              chiffre ne suivra la saisie qu'à la tâche 4.7. Chiffrer le job donne la
              valeur exacte pour l'entrée en cours.
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
        </>
      )}
    </aside>
  );
}
