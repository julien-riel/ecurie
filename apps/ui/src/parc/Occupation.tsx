/**
 * Les trois chiffres, leur répartition, et l'arbre de duplication.
 *
 * C'est la moitié « comptabilité » de l'écran Parc : ce que le disque contient,
 * sans encore rien proposer d'en faire. Elle vient entière de `/store/summary`,
 * qui appelle le `compute_figures` de la CLI — les chiffres affichés ici sont
 * ceux de `ecurie store status`, aux mêmes octets près.
 *
 * L'arbre de duplication est déplié par défaut jusqu'à dix groupes, comme la
 * table de la CLI, et le reste se déplie. Un parc dupliqué peut compter des
 * centaines de groupes ; les poser tous d'emblée ferait d'un écran de décision
 * une liste de chemins.
 */

import type { DuplicateGroup, Figures, Telemetry } from "../api/types";
import { formatOctetsDisque } from "../format/bytes";
import { postesRecuperables } from "./parc";

/** Le nombre de groupes que la CLI affiche, et qu'on montre sans déplier. */
const PREMIERS_GROUPES = 10;

export function Occupation({
  figures,
  telemetry,
}: {
  figures: Figures;
  telemetry: Telemetry | null | undefined;
}) {
  const recoverable = figures.recoverable;
  const total = recoverable.total_known_bytes;
  const postes = postesRecuperables(recoverable, telemetry);
  const duplicates = figures.duplicates;
  const gestionnaires = Object.entries(figures.by_manager);

  return (
    <>
      {/*
        Deux cartes et non une : la comptabilité — ce que le disque contient —
        et la duplication — ce qu'il en détient en double — sont deux questions,
        et la seconde peut compter des centaines de groupes de chemins. Les
        laisser dans la même surface obligerait à traverser l'une pour relire
        l'autre.
      */}
      <section className="ecurie-carte">
        <p className="ecurie-plaque">ce que le disque contient</p>
        <h2>Trois chiffres</h2>
        <dl className="ecurie-chiffres">
          <div>
            <dt>Apparent</dt>
            <dd>{formatOctetsDisque(figures.apparent_bytes)}</dd>
            <dd className="ecurie-etat-champ">
              somme naïve des tailles — ce que <code>du</code> donnerait
            </dd>
          </div>
          <div>
            <dt>Réel unique</dt>
            <dd>{formatOctetsDisque(figures.real_unique_bytes)}</dd>
            <dd className="ecurie-etat-champ">par contenu distinct, un lien dur compté une fois</dd>
          </div>
          <div>
            <dt>Récupérable</dt>
            {/*
              Le total ne se recalcule pas ici : la route l'injecte, parce que
              `total_known_bytes` est une propriété qui ne sort pas d'`asdict`.
              Refaire l'addition exposerait à annoncer un total qui ne serait pas
              celui de la CLI.
            */}
            <dd>{formatOctetsDisque(total ?? null)}</dd>
            <dd className="ecurie-etat-champ">connu — les postes indéterminés n'y sont pas</dd>
          </div>
        </dl>

        <ul className="ecurie-postes">
          {postes.map((poste) => (
            <li key={poste.clef} data-connu={poste.octets === null ? "non" : "oui"}>
              <span className="ecurie-poste-titre">{poste.titre}</span>{" "}
              <span className="ecurie-poste-valeur">{formatOctetsDisque(poste.octets)}</span>
              {/* La note vient sous la valeur et non entre les deux : elle n'a
                  plus le tiret qui la raccrochait à la phrase de la ligne. */}
              {poste.note ? <span className="ecurie-etat-champ">{poste.note}</span> : null}
            </li>
          ))}
        </ul>

        {figures.mismatched.length ? (
          <div className="text-danger">
            <p>
              {figures.mismatched.length} fichier(s) dont le contenu ne correspond pas au hash
              annoncé par leur gestionnaire — ne rien planifier dessus :
            </p>
            <ul>
              {figures.mismatched.slice(0, 5).map((chemin) => (
                <li key={chemin}>
                  <code>{chemin}</code>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        <h3>Par gestionnaire</h3>
        <table className="ecurie-table">
          <thead>
            <tr>
              <th scope="col">Gestionnaire</th>
              <th scope="col">Apparent</th>
              <th scope="col">Fichiers</th>
            </tr>
          </thead>
          <tbody>
            {gestionnaires.map(([nom, [octets, fichiers]]) => (
              <tr key={nom}>
                <th scope="row">{nom}</th>
                <td>{formatOctetsDisque(octets)}</td>
                <td>{fichiers}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <HorsRegistre figures={figures} />
      </section>

      <section className="ecurie-carte">
        <p className="ecurie-plaque">ce qu'il détient en double</p>
        <h2>Duplications ({duplicates.length})</h2>
        {duplicates.length === 0 ? (
          <p className="ecurie-etat-champ">
            Aucun contenu détenu en double sur un même volume — rien à lier en dur.
          </p>
        ) : (
          <Arbre groupes={duplicates.slice(0, PREMIERS_GROUPES)} />
        )}
        {duplicates.length > PREMIERS_GROUPES ? (
          <details>
            <summary>{duplicates.length - PREMIERS_GROUPES} groupe(s) de plus</summary>
            <Arbre groupes={duplicates.slice(PREMIERS_GROUPES)} />
          </details>
        ) : null}
      </section>
    </>
  );
}

/** Un contenu, ses exemplaires, et ce que les lier rendrait. */
function Arbre({ groupes }: { groupes: DuplicateGroup[] }) {
  return (
    <ul className="ecurie-duplications">
      {groupes.map((groupe) => (
        <li key={groupe.sha256}>
          <p className="ecurie-dup-entete">
            <code>{groupe.sha256.slice(0, 12)}…</code>{" "}
            <span>{formatOctetsDisque(groupe.size)}</span>{" "}
            <span className="ecurie-etat-champ">
              {formatOctetsDisque(groupe.reclaimable_bytes)} récupérables
            </span>
          </p>
          <ul>
            {groupe.paths.map((chemin) => (
              <li key={chemin}>
                <code>{chemin}</code>
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ul>
  );
}

/**
 * Ce que le registre ne connaît pas, et ce qu'on n'a pas relu.
 *
 * Trois compteurs sur une ligne parce qu'ils disent la même chose sous trois
 * angles : la part du disque sur laquelle on ne peut rien décider. Un fichier
 * hors registre n'appartient à aucun variant ; un fichier sans sha256 compte
 * pour lui-même faute de savoir s'il est un doublon ; un hash annoncé mais
 * jamais relu suffit à compter, jamais à effacer.
 *
 * En JSX plutôt qu'en chaîne, et la raison est une correction : la première
 * version composait une phrase et y glissait des accents graves autour de
 * `ecurie store verify`, par réflexe de Markdown. Le navigateur les affichait
 * tels quels — un balisage de fichier texte au milieu d'une page. Aucun test ne
 * pouvait le voir, tous cherchant le texte par sous-chaîne ; une capture d'écran
 * l'a montré en une seconde.
 */
function HorsRegistre({ figures }: { figures: Figures }) {
  return (
    <p className="ecurie-etat-champ">
      Hors registre : {figures.unresolved_count} fichier(s),{" "}
      {formatOctetsDisque(figures.unresolved_bytes)}
      {figures.unverified_count ? (
        <>
          {" · "}sans sha256 : {figures.unverified_count} fichier(s),{" "}
          {formatOctetsDisque(figures.unverified_bytes)} — <code>ecurie store verify</code> tranche
        </>
      ) : null}
      {figures.announced_bytes ? (
        <>
          {" · "}hash annoncé non relu : {formatOctetsDisque(figures.announced_bytes)} — suffisant
          pour compter, jamais pour effacer
        </>
      ) : null}
    </p>
  );
}
