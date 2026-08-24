/**
 * Choisir une capacité parmi trente-deux, sans dérouler une liste de trente-deux.
 *
 * Le `<select>` du 4.4 tenait tant que le parc était court : dix-sept contrats,
 * groupés par état, et l'on trouvait. À trente-deux, il ne dit plus rien de ce
 * qu'on choisit — ni ce que la capacité prend, ni ce qu'elle rend, ni si des
 * modèles la servent. On lisait une liste de titres pour en reconnaître un.
 *
 * Ce panneau répond à trois questions que la liste posait sans y répondre :
 *
 * **Que fait cette capacité ?** La glyphe la montre avant qu'on lise son nom —
 * une forme, une flèche, une forme, qui est exactement ce qu'un contrat déclare.
 * La description la dit en une phrase, celle du contrat, jamais réécrite ici.
 *
 * **Y a-t-il de quoi la faire tourner ?** Les modèles et les variants sont
 * comptés sur la carte, et l'état est marqué. Une capacité sans variant
 * exécutable **reste affichée** : elle dit ce que le parc pourrait faire et ne
 * fait pas encore, ce qui est la moitié de ce qu'un registre sert à savoir.
 *
 * **Où est celle que je cherche ?** Par catégorie, parce qu'on cherche « quelque
 * chose sur le son » avant de chercher un titre ; par filtre d'entrée et de
 * sortie, parce qu'on sait souvent ce qu'on a sous la main et ce qu'on veut en
 * tirer ; par recherche, parce qu'on connaît parfois déjà le nom.
 *
 * Les filtres se déduisent des contrats (`schema/modalites.ts`), donc une
 * capacité qui entre au registre demain est filtrable le jour même.
 */

import { useEffect, useId, useMemo, useRef, useState } from "react";

import type { Capability, Model } from "../api/types";
import { etatCapacite, phraseEtat } from "../schema/etat";
import {
  entreePrincipale,
  LIBELLE_MODALITE,
  modalitesEntree,
  modalitesSortie,
  ORDRE_ENTREE,
  ORDRE_SORTIE,
  sortiePrincipale,
  type Modalite,
} from "../schema/modalites";
import { formeEntree, formeSortie, sections } from "./catalogue";
import { GlypheFlux } from "./Glyphe";

export interface Comptes {
  modeles: number;
  variants: number;
  prets: number;
}

/**
 * Ce que le parc tient pour une capacité.
 *
 * `models` du contrat suffirait à compter les modèles, mais pas les variants :
 * un modèle en porte de un à sept, et c'est le variant qu'on lance. Le total
 * vient donc de `/registry/models`, et le nombre de prêts de `ready_variants`,
 * qui est calculé par le serveur d'après le disque de cette machine.
 */
export function comptesDe(capability: Capability, models: readonly Model[]): Comptes {
  const siens = models.filter((m) => m.capability === capability.id);
  return {
    modeles: siens.length > 0 ? siens.length : capability.models.length,
    variants: siens.reduce((total, m) => total + m.variants.length, 0),
    prets: capability.ready_variants.length,
  };
}

function phraseComptes({ modeles, variants, prets }: Comptes): string {
  const m = `${modeles} modèle${modeles > 1 ? "s" : ""}`;
  if (variants === 0) return m;
  const v = `${variants} variant${variants > 1 ? "s" : ""}`;
  // « 3 variants, 3 prêts » serait exact et bavard : quand tout est prêt, le
  // dire deux fois n'apprend rien.
  if (prets === variants) return `${m} · ${v}`;
  return `${m} · ${v}, ${prets} prêt${prets > 1 ? "s" : ""}`;
}

function correspond(capability: Capability, recherche: string): boolean {
  const q = recherche.trim().toLowerCase();
  if (!q) return true;
  const foin = `${capability.title} ${capability.id} ${capability.description ?? ""}`;
  return foin.toLowerCase().includes(q);
}

interface FiltresActifs {
  entree: Set<Modalite>;
  sortie: Set<Modalite>;
}

function passeLesFiltres(capability: Capability, filtres: FiltresActifs): boolean {
  // Une capacité passe si elle sait recevoir **l'une** des modalités demandées,
  // et non toutes : cocher « image » et « texte » cherche ce qui accepte l'une
  // ou l'autre, ce qui est ce qu'on veut dire en cochant deux cases.
  if (filtres.entree.size > 0) {
    const siennes = modalitesEntree(capability);
    if (![...filtres.entree].some((m) => siennes.has(m))) return false;
  }
  if (filtres.sortie.size > 0) {
    const siennes = modalitesSortie(capability);
    if (![...filtres.sortie].some((m) => siennes.has(m))) return false;
  }
  return true;
}

function bascule(ensemble: Set<Modalite>, modalite: Modalite): Set<Modalite> {
  const suivant = new Set(ensemble);
  if (suivant.has(modalite)) suivant.delete(modalite);
  else suivant.add(modalite);
  return suivant;
}

// --- le déclencheur ------------------------------------------------------------

export interface SelecteurCapaciteProps {
  capacites: readonly Capability[];
  models: readonly Model[];
  valeur: string | null;
  onChoisir: (id: string) => void;
}

export function SelecteurCapacite({
  capacites,
  models,
  valeur,
  onChoisir,
}: SelecteurCapaciteProps) {
  const [ouvert, setOuvert] = useState(false);
  const déclencheur = useRef<HTMLButtonElement>(null);
  const choisie = capacites.find((c) => c.id === valeur) ?? null;
  const étiquette = useId();
  const bouton = useId();

  function fermer() {
    setOuvert(false);
    // Le focus revient d'où il est parti : sans cela, fermer le panneau au
    // clavier laisse le curseur au début du document et il faut retraverser
    // l'écran pour revenir au choix qu'on vient de faire.
    déclencheur.current?.focus();
  }

  return (
    <div className="ecurie-selecteur">
      <span className="ecurie-etiquette" id={étiquette}>
        Capacité
      </span>
      {/*
        Les deux identifiants se composent plutôt que de s'écraser : `Capacité`
        vient de la plaque au-dessus, le reste du contenu du bouton. Nommer le
        bouton par la seule plaque, comme le faisait la première version, le
        faisait s'annoncer « Capacité » et taisait ce qui était choisi.
      */}
      <button
        type="button"
        id={bouton}
        ref={déclencheur}
        className="ecurie-declencheur"
        aria-haspopup="dialog"
        aria-expanded={ouvert}
        aria-labelledby={`${étiquette} ${bouton}`}
        onClick={() => setOuvert(true)}
      >
        {choisie ? (
          <>
            <span className="ecurie-declencheur-glyphe">
              <GlypheFlux
                entree={formeEntree(choisie.id, entreePrincipale(choisie))}
                sortie={formeSortie(choisie.id, sortiePrincipale(choisie))}
              />
            </span>
            <span className="ecurie-declencheur-texte">
              <strong>{choisie.title}</strong>
              <small>{phraseComptes(comptesDe(choisie, models))}</small>
            </span>
          </>
        ) : (
          <span className="ecurie-declencheur-texte">
            <strong>Choisir une capacité</strong>
            <small>{capacites.length} au registre</small>
          </span>
        )}
        <svg
          className="ecurie-chevron"
          width="14"
          height="14"
          viewBox="0 0 14 14"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M3.5 5.5L7 9l3.5-3.5" />
        </svg>
      </button>

      {ouvert ? (
        <PanneauCapacites
          capacites={capacites}
          models={models}
          valeur={valeur}
          onChoisir={(id) => {
            onChoisir(id);
            fermer();
          }}
          onFermer={fermer}
        />
      ) : null}
    </div>
  );
}

// --- le panneau ----------------------------------------------------------------

function PanneauCapacites({
  capacites,
  models,
  valeur,
  onChoisir,
  onFermer,
}: SelecteurCapaciteProps & { onFermer: () => void }) {
  const [recherche, setRecherche] = useState("");
  const [filtres, setFiltres] = useState<FiltresActifs>({
    entree: new Set(),
    sortie: new Set(),
  });
  const champ = useRef<HTMLInputElement>(null);
  const titre = useId();

  useEffect(() => {
    champ.current?.focus();
  }, []);

  const retenues = useMemo(
    () => capacites.filter((c) => correspond(c, recherche) && passeLesFiltres(c, filtres)),
    [capacites, recherche, filtres],
  );
  const groupes = useMemo(() => sections(retenues, etatCapacite), [retenues]);
  const filtré = recherche.trim() !== "" || filtres.entree.size > 0 || filtres.sortie.size > 0;

  return (
    <div
      className="ecurie-voile"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) onFermer();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onFermer();
        }
      }}
    >
      <div className="ecurie-panneau" role="dialog" aria-modal="true" aria-labelledby={titre}>
        <header className="ecurie-panneau-tete">
          <div>
            <h2 id={titre}>Choisir une capacité</h2>
            <p className="ecurie-sous-titre">
              Ce que le parc sait faire, rangé par famille. Une capacité sans variant
              exécutable reste dans la liste : elle dit ce qu'un <code>ecurie pull</code>{" "}
              rendrait possible.
            </p>
          </div>
          <button type="button" className="ecurie-fermer" onClick={onFermer} aria-label="Fermer">
            <svg
              width="16"
              height="16"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              aria-hidden="true"
            >
              <path d="M4 4l8 8M12 4l-8 8" />
            </svg>
          </button>
        </header>

        <div className="ecurie-panneau-barre">
          <label className="ecurie-recherche">
            <svg
              width="15"
              height="15"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              aria-hidden="true"
            >
              <circle cx="7" cy="7" r="4.5" />
              <path d="M10.5 10.5L14 14" />
            </svg>
            <input
              ref={champ}
              type="search"
              value={recherche}
              placeholder="Chercher une capacité"
              aria-label="Chercher une capacité"
              onChange={(e) => setRecherche(e.target.value)}
            />
          </label>

          <RangeeDeFiltres
            legende="Prend"
            ordre={ORDRE_ENTREE}
            actifs={filtres.entree}
            onBasculer={(m) => setFiltres((f) => ({ ...f, entree: bascule(f.entree, m) }))}
          />
          <RangeeDeFiltres
            legende="Rend"
            ordre={ORDRE_SORTIE}
            actifs={filtres.sortie}
            onBasculer={(m) => setFiltres((f) => ({ ...f, sortie: bascule(f.sortie, m) }))}
          />
        </div>

        <div className="ecurie-panneau-corps">
          {groupes.length === 0 ? (
            <p className="ecurie-panneau-vide">
              Aucune capacité ne prend et ne rend cela. Retirer un filtre, ou chercher
              autrement.
            </p>
          ) : (
            groupes.map(({ categorie, capacites: dansLaCategorie }) => (
              <section key={categorie.id} className="ecurie-famille">
                <div className="ecurie-famille-tete">
                  <h3>{categorie.titre}</h3>
                  <p>{categorie.sous_titre}</p>
                  <span className="ecurie-compteur-famille">{dansLaCategorie.length}</span>
                </div>
                <ul className="ecurie-grille-capacites">
                  {dansLaCategorie.map((c) => (
                    <li key={c.id}>
                      <CarteCapacite
                        capability={c}
                        comptes={comptesDe(c, models)}
                        choisie={c.id === valeur}
                        onChoisir={() => onChoisir(c.id)}
                      />
                    </li>
                  ))}
                </ul>
              </section>
            ))
          )}
        </div>

        {filtré ? (
          <footer className="ecurie-panneau-pied">
            <span>
              {retenues.length} capacité{retenues.length > 1 ? "s" : ""} sur {capacites.length}
            </span>
            <button
              type="button"
              className="ecurie-lien"
              onClick={() => {
                setRecherche("");
                setFiltres({ entree: new Set(), sortie: new Set() });
                champ.current?.focus();
              }}
            >
              Tout afficher
            </button>
          </footer>
        ) : null}
      </div>
    </div>
  );
}

function RangeeDeFiltres({
  legende,
  ordre,
  actifs,
  onBasculer,
}: {
  legende: string;
  ordre: readonly Modalite[];
  actifs: Set<Modalite>;
  onBasculer: (m: Modalite) => void;
}) {
  return (
    <fieldset className="ecurie-filtres">
      <legend>{legende}</legend>
      {ordre.map((m) => (
        <button
          key={m}
          type="button"
          className="ecurie-pastille"
          aria-pressed={actifs.has(m)}
          onClick={() => onBasculer(m)}
        >
          {LIBELLE_MODALITE[m]}
        </button>
      ))}
    </fieldset>
  );
}

function CarteCapacite({
  capability,
  comptes,
  choisie,
  onChoisir,
}: {
  capability: Capability;
  comptes: Comptes;
  choisie: boolean;
  onChoisir: () => void;
}) {
  const état = etatCapacite(capability);
  return (
    <button
      type="button"
      className="ecurie-capacite"
      data-etat={état}
      aria-pressed={choisie}
      onClick={onChoisir}
    >
      <span className="ecurie-capacite-tete">
        <GlypheFlux
          entree={formeEntree(capability.id, entreePrincipale(capability))}
          sortie={formeSortie(capability.id, sortiePrincipale(capability))}
        />
        {état === "prête" ? null : (
          <span className="ecurie-etat-carte" title={phraseEtat(état)}>
            {état === "sans-modèle" ? "aucun modèle" : "rien d'exécutable"}
          </span>
        )}
      </span>
      <span className="ecurie-capacite-titre">{capability.title}</span>
      {capability.description ? (
        <span className="ecurie-capacite-description">{capability.description}</span>
      ) : null}
      <span className="ecurie-capacite-pied">{phraseComptes(comptes)}</span>
    </button>
  );
}
