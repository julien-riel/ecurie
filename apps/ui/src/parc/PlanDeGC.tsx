/**
 * Le plan de récupération, tel qu'il serait — et la commande qui l'exécute.
 *
 * **L'écran ne l'applique pas, et ce n'est pas une lacune.** Le §4.3 de la
 * conception veut qu'appliquer un plan re-vérifie le sha256 de chaque fichier
 * *au moment d'agir*, jamais sur la foi du scan : c'est relire des giga-octets,
 * refuser tout ce qui a bougé, et déplacer le reste en quarantaine. Une
 * opération qu'on lance en tapant une commande et en confirmant, pas d'un clic
 * dans un onglet resté ouvert depuis la veille. L'écran montre ce qu'il y a à
 * gagner et par où le prendre ; `ecurie store apply` fait le reste.
 *
 * Ce qu'il montre est le plan **entier** : gain par poste, mais aussi chaque
 * action et chaque chemin écarté. Un total sans son détail demanderait de faire
 * confiance à un outil qui propose de toucher trente giga-octets de poids, ce
 * qui est exactement l'inverse de ce que ce format cherche — « le document qu'on
 * relit avant ».
 *
 * `verified_only` est ici plutôt qu'ailleurs parce que c'est la seule décision
 * de l'écran qui change ce que le plan propose : dédupliquer sur un hash annoncé
 * par un gestionnaire, ou seulement sur un contenu qu'on a relu. La CLI en fait
 * une option ; en faire une case cochable est ce qui la rend visible.
 */

import type { Plan, PlanAction } from "../api/types";
import { formatOctetsDisque } from "../format/bytes";
import { cheminsDeLAction, gainParPoste, libelleMotif } from "./parc";

export interface PlanDeGCProps {
  plan: Plan;
  labels: Record<string, string>;
  /** La commande qui écrit ce plan pour de bon — le serveur la compose. */
  command: string | null | undefined;
  verifiedOnly: boolean;
  onVerifiedOnly: (valeur: boolean) => void;
  enCours: boolean;
}

export function PlanDeGC({
  plan,
  labels,
  command,
  verifiedOnly,
  onVerifiedOnly,
  enCours,
}: PlanDeGCProps) {
  const postes = gainParPoste(plan, labels);

  return (
    <section className="ecurie-carte">
      <p className="ecurie-plaque">ce qu'on pourrait reprendre</p>
      <h2>Plan de récupération</h2>
      <p className="ecurie-etat-champ">
        À blanc : rien n'est écrit, rien n'est déplacé. Chaque action re-vérifie le contenu du
        fichier au moment de l'exécuter, et refuse tout ce qui a bougé depuis le scan.
      </p>

      <label className="ecurie-case">
        <input
          type="checkbox"
          checked={verifiedOnly}
          onChange={(e) => onVerifiedOnly(e.target.checked)}
          disabled={enCours}
        />{" "}
        Ne dédupliquer que sur des sha256 relus
        <span className="ecurie-etat-champ">
          {" "}
          — un hash annoncé par un gestionnaire suffit à compter, jamais à effacer
        </span>
      </label>

      <p className="ecurie-total">
        Total récupérable : <strong>{formatOctetsDisque(plan.total_bytes_reclaimed)}</strong> en{" "}
        {plan.actions.length} action(s)
      </p>

      {postes.length === 0 ? (
        /*
          « Rien à récupérer » et « rien à récupérer *sous cette contrainte* »
          sont deux constats différents, et le second est celui qu'on lit le
          plus souvent : sur un parc dont les hash viennent tous d'un nom de
          blob, cocher la case ramène le gain à zéro sans rien dire de l'état du
          disque. Les confondre ferait conclure « le parc est propre » d'une
          option qu'on vient soi-même de poser.
        */
        <p className="ecurie-etat-champ">
          {verifiedOnly && plan.ignored.length ? (
            <>
              Rien à récupérer sur des sha256 relus — ce que le plan proposait repose sur des hash
              annoncés. <code>ecurie store verify</code> les relit, et le plan revient.
            </>
          ) : (
            "Rien à récupérer sur l'état observé — le parc est déjà au plus court."
          )}
        </p>
      ) : (
        <table className="ecurie-table">
          <thead>
            <tr>
              <th scope="col">Poste</th>
              <th scope="col">Actions</th>
              <th scope="col">Gain</th>
            </tr>
          </thead>
          <tbody>
            {postes.map((poste) => (
              <tr key={poste.motif}>
                <th scope="row">{poste.titre}</th>
                <td>{poste.actions}</td>
                <td>{formatOctetsDisque(poste.octets)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {plan.actions.length ? (
        <details>
          <summary>Les {plan.actions.length} action(s), une par une</summary>
          <ul className="ecurie-actions-plan">
            {plan.actions.map((action, rang) => (
              <li key={`${action.kind}-${cheminsDeLAction(action)[0] ?? rang}`}>
                <Action action={action} labels={labels} />
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {plan.ignored.length ? (
        <details>
          <summary>
            {plan.ignored.reduce((n, é) => n + é.paths.length, 0)} chemin(s) écarté(s)
          </summary>
          <ul className="ecurie-ecartes">
            {plan.ignored.map((écarté) => (
              <li key={`${écarté.reason}-${écarté.paths[0] ?? ""}`}>
                <p>{libelleMotif(écarté.reason, labels)}</p>
                <ul>
                  {écarté.paths.map((chemin) => (
                    <li key={chemin}>
                      <code>{chemin}</code>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {command ? (
        <p className="ecurie-etat-champ">
          Pour l'obtenir en fichier et l'appliquer : <code>{command}</code>, puis{" "}
          <code>ecurie store apply &lt;plan&gt;</code>.
        </p>
      ) : null}
    </section>
  );
}

/**
 * Une action, dans la forme de son `kind`.
 *
 * L'aiguillage se fait sur `kind` et non sur les clés présentes : le plan est
 * une union que le serveur n'étiquette pas autrement, et deviner la forme
 * d'après « il y a un `keep` » se romprait au premier `kind` ajouté. Le repli
 * n'est pas décoratif — il affiche le `kind` inconnu et ses chemins plutôt que
 * de laisser une ligne vide, comme les deux autres tables d'aiguillage du front.
 */
function Action({ action, labels }: { action: PlanAction; labels: Record<string, string> }) {
  const gain = formatOctetsDisque(action.bytes_reclaimed);
  const motif = libelleMotif(action.reason, labels);

  if (action.kind === "hardlink") {
    return (
      <>
        <p>
          <strong>lier en dur</strong> — {motif} · {gain}
          {action.hash_source === "announced" ? (
            <span className="ecurie-etat-champ"> · hash annoncé, jamais relu</span>
          ) : null}
        </p>
        <p className="ecurie-etat-champ">
          garde <code>{action.keep}</code>
        </p>
        <ul>
          {(action.replace ?? []).map((chemin) => (
            <li key={chemin}>
              remplace <code>{chemin}</code>
            </li>
          ))}
        </ul>
      </>
    );
  }

  if (action.kind === "trash") {
    return (
      <p>
        <strong>quarantaine</strong> — {motif} · {gain} · <code>{action.path}</code>
        {action.variant_refs?.length ? (
          <span className="ecurie-etat-champ"> · sert à {action.variant_refs.join(", ")}</span>
        ) : null}
      </p>
    );
  }

  return (
    <p>
      <strong>{action.kind}</strong> — {motif} · {gain}
      {cheminsDeLAction(action).map((chemin) => (
        <span key={chemin}>
          {" "}
          · <code>{chemin}</code>
        </span>
      ))}
    </p>
  );
}
