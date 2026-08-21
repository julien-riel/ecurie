/**
 * Ce que devient le job qu'on vient de lancer.
 *
 * Il n'y a pas de « chargement… » générique ici, et c'est délibéré : le job
 * traverse des étapes que l'utilisateur a des raisons de vouloir distinguer —
 * attendre son tour derrière un autre job, charger un modèle de huit
 * gigaoctets, inférer. Le serveur les nomme dans `note`, et le chargement d'un
 * modèle n'a **pas** de granularité : aucun adaptateur ne rapporte de
 * progression avant l'inférence. Une barre qui n'avancerait pas pendant vingt
 * secondes serait pire que la phrase qui dit ce qui se passe.
 *
 * L'erreur d'un job est rendue telle que le serveur l'écrit. C'est par là
 * qu'arrive le refus d'admission — « admission refusée : ce morceau de 30 s
 * demanderait 24,2 Gio » —, qui n'est pas une panne mais une décision, et dont
 * les deux chiffres sont tout l'intérêt.
 */

import type { Job } from "../api/types";
import { LIBELLE_ETAT, enCours, phrasesBilan, phrasesMetriques } from "./job";

export interface PanneauJobProps {
  job: Job;
  /** Ce qui a empêché de suivre — jamais l'échec du job lui-même. */
  erreurDeSuivi?: readonly string[];
  onReprendre?: () => void;
  onOublier?: () => void;
}

export function PanneauJob({ job, erreurDeSuivi = [], onReprendre, onOublier }: PanneauJobProps) {
  const bilan = phrasesBilan(job);
  const métriques = phrasesMetriques(job);

  return (
    <section className="ecurie-job" aria-label="Job" data-etat={job.state}>
      <p className="ecurie-job-entete">
        <strong>{LIBELLE_ETAT[job.state]}</strong> · <code>{job.ref}</code> ·{" "}
        <code className="ecurie-etat-champ">{job.id}</code>
        {onOublier ? (
          <button type="button" className="ecurie-lien" onClick={onOublier}>
            retirer de l'écran
          </button>
        ) : null}
      </p>

      {enCours(job) ? (
        <>
          <progress className="ecurie-progression" value={job.progress} max={100} aria-label="Progression" />
          <p className="ecurie-etat-champ">
            {job.progress} % — {job.note || "en attente du worker"}
          </p>
        </>
      ) : null}

      {job.error ? <p className="text-danger">{job.error}</p> : null}

      {bilan.length ? (
        <ul className="ecurie-bilan">
          {bilan.map((ligne) => (
            <li key={ligne}>{ligne}</li>
          ))}
        </ul>
      ) : null}

      {job.warnings.length ? (
        <ul className="ecurie-avertissements">
          {job.warnings.map((avis) => (
            <li key={avis}>{avis}</li>
          ))}
        </ul>
      ) : null}

      {métriques.length ? (
        <ul className="ecurie-metriques">
          {métriques.map((ligne) => (
            <li key={ligne}>{ligne}</li>
          ))}
        </ul>
      ) : null}

      {erreurDeSuivi.length ? (
        <p className="text-danger">
          {erreurDeSuivi.join(" · ")}{" "}
          {onReprendre ? (
            <button type="button" className="ecurie-lien" onClick={onReprendre}>
              reprendre le suivi
            </button>
          ) : null}
        </p>
      ) : null}
    </section>
  );
}
