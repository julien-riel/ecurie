/**
 * L'Atelier — capacité, variant, formulaire, coût. Le premier des quatre écrans.
 *
 * Il remplace le banc de rendu du 4.3, qui prouvait que les deux tables
 * d'aiguillage fonctionnent sans prétendre être un écran. Ce qui change tient en
 * quatre points, et aucun n'est cosmétique :
 *
 * 1. **Le bandeau de ressources est permanent** et se rafraîchit tout seul. La
 *    mémoire bouge sans qu'on touche à l'écran — un `ecurie run` dans un
 *    terminal charge un modèle, un worker meurt —, et c'est la seule lecture du
 *    front qui sonde.
 * 2. **Les choix sont ordonnés par ce qui marche.** Sept capacités sur dix-sept
 *    n'ont aucun modèle ; les mêler aux dix exécutables oblige à lire dix-sept
 *    lignes pour en trouver une. Le variant est préselectionné sur le titulaire.
 * 3. **Ce qui empêche de lancer est écrit en clair**, sous le sélecteur, avec la
 *    commande qui répare. Un `<option>` qui porterait trois blockers en suffixe
 *    est illisible ; c'est ce que faisait le banc.
 * 4. **La projection de l'entrée est repliée.** Voir partir le JSON est un outil
 *    de mise au point, pas le sujet de l'écran.
 *
 * Ce qu'il ne fait pas, et pourquoi il ne le fait pas : **il n'y a pas de bouton
 * *Lancer*.** `POST /jobs` et le flux SSE attendent le déménagement du
 * superviseur dans le processus de l'API (tâche 4.6), et l'ordre est délibéré —
 * écrire la soumission avant reviendrait à faire vivre l'occupation des
 * résidents dans un fichier verrouillé pendant toute la durée d'un job, puis à
 * le défaire. Un bouton grisé, lui, serait un mensonge : l'écran dit ce qui
 * manque plutôt que de le suggérer.
 *
 * Le chiffrage reste donc sur clic. Le bandeau chiffre le **variant** en
 * continu, `POST /runtime/admission` chiffre l'**entrée** : c'est le second qui
 * fait parler `peak_scaling`, et le fusionner dans une frappe de clavier est
 * nommément la tâche 4.7.
 *
 * Le premier essai contre un vrai serveur a montré ce que cette division coûte,
 * et rien ne le laissait deviner : pour douze variants sur treize, le pic ne
 * dépend pas de l'entrée, si bien que les deux réponses sont **mot pour mot la
 * même phrase**, affichée deux fois dans le même écran. D'où l'intitulé « Pour
 * l'entrée saisie » : le bandeau dit l'état du parc, le chiffrage répond à une
 * question qu'on vient de poser. Ce que le second ajoute vraiment tient dans ce
 * que le premier ne peut pas savoir — les reproches du contrat sur la saisie, et
 * le pic réel d'un variant à pente.
 */

import { useEffect, useMemo, useState } from "react";
import * as api from "../api/endpoints";
import { phraseErreur } from "../api/errors";
import { optionsFor } from "../api/residents";
import type { Admission, Capability, Variant } from "../api/types";
import { useResource } from "../api/useResource";
import { PERIODE_MS, useSondage } from "../api/useSondage";
import { CapabilityForm } from "../form/CapabilityForm";
import { formatBytes, phraseLourdeur } from "../format/bytes";
import { jourMesure } from "../format/dates";
import type { Notice } from "../notices/notices";
import { NoticeBanner } from "../notices/NoticeBanner";
import { fromIssues, uniques } from "../notices/notices";
import { SortiesPromises } from "../output/SortiesPromises";
import { BandeauRessources } from "../ressources/BandeauRessources";
import { parametreDuPic, phrasesAdmission, severiteAdmission } from "../ressources/ressources";
import { initialValues } from "../schema/defaults";
import { etatCapacite, phraseEtat } from "../schema/etat";
import { toContractInput } from "../schema/projection";
import { groupesDeCapacites, groupesDeVariants, variantParDefaut } from "./choix";

export interface AtelierProps {
  /** Cadence du sondage du bandeau ; les tests la raccourcissent. */
  periodeBandeau?: number;
}

/** Le chiffrage d'une entrée : ce que le serveur a répondu, ou pourquoi il n'a pas répondu. */
interface Chiffrage {
  admission: Admission | null;
  /** Reproches du contrat et blockers du variant — deux listes, un seul affichage. */
  reproches: readonly string[];
  erreur: string | null;
}

export function Atelier({ periodeBandeau }: AtelierProps) {
  const contrats = useResource((s) => api.capabilities(s), []);
  const [capId, setCapId] = useState<string | null>(null);
  const [ref, setRef] = useState<string | null>(null);
  const [formData, setFormData] = useState<Record<string, unknown>>({});
  const [avisFormulaire, setAvisFormulaire] = useState<readonly Notice[]>([]);
  const [chiffrage, setChiffrage] = useState<Chiffrage | null>(null);
  const [projectionOuverte, setProjectionOuverte] = useState(false);

  const capacités = contrats.données?.capabilities ?? [];
  const capacité: Capability | null = capacités.find((c) => c.id === capId) ?? null;

  const modèles = useResource(
    (s) => (capacité ? api.models(capacité.id, s) : Promise.resolve(null)),
    [capacité?.id],
  );
  // Le filtre sur `capability` n'est pas une ceinture de sécurité : `useResource`
  // garde les dernières données pendant qu'il recharge — c'est ce qui évite de
  // vider l'écran à chaque changement —, si bien qu'entre le clic sur une
  // nouvelle capacité et l'arrivée de ses modèles, `modèles.données` contient
  // encore ceux de la précédente. Sans lui, la préselection choisissait une
  // référence de la bonne capacité dans la liste de la mauvaise, ne trouvait
  // rien, et posait un formulaire sans les défauts du manifeste — `voice`
  // resterait vide alors que `qwen3-tts-1.7b` déclare `serena`.
  const models = useMemo(
    () => (modèles.données?.models ?? []).filter((m) => m.capability === capacité?.id),
    [modèles.données, capacité?.id],
  );
  const variants: Variant[] = useMemo(() => models.flatMap((m) => m.variants), [models]);
  const variant = variants.find((v) => v.ref === ref) ?? null;

  // Un seul sondage de `/runtime/residents` pour tout l'écran : le bandeau en
  // tire ses chiffres, le formulaire ses `x-options-from`. Ces derniers n'ont
  // pas d'autre source que le champ `options` d'un worker chargé — tant qu'un
  // modèle n'a pas tourné une fois, on ne connaît pas ses voix —, si bien qu'une
  // lecture unique au montage les figerait à « aucune » pour toute la session,
  // même après que l'utilisateur a lancé le modèle depuis un terminal.
  const parc = useSondage(
    (s) => api.residents(ref ?? undefined, s),
    [ref],
    periodeBandeau ?? PERIODE_MS,
  );
  const résidents = parc.données?.residents ?? [];
  const runtimeOptions = optionsFor(résidents, ref);
  const estRésident = résidents.some((r) => r.ref === ref);

  function poserVariant(nouveau: string | null) {
    setRef(nouveau);
    setChiffrage(null);
    if (capacité) {
      const v = nouveau ? (variants.find((x) => x.ref === nouveau) ?? null) : null;
      setFormData(initialValues(capacité, v));
    }
  }

  function choisirCapacite(id: string) {
    setCapId(id || null);
    setRef(null);
    setFormData({});
    setChiffrage(null);
  }

  // La préselection attend les modèles : `ready_variants` suffirait à nommer le
  // variant, mais pas à poser ses défauts — `voice: serena` vient du manifeste.
  // Elle ne se déclenche qu'une fois, tant que rien n'est choisi, pour ne jamais
  // écraser une saisie en cours.
  useEffect(() => {
    if (!capacité || ref !== null) return;
    const défaut = variantParDefaut(capacité);
    if (!défaut) return;
    const v = variants.find((x) => x.ref === défaut);
    // Rien tant que le variant lui-même n'est pas là : `ready_variants` suffit à
    // le nommer, pas à poser ses défauts — `voice: serena` vient du manifeste.
    if (!v) return;
    setRef(défaut);
    setFormData(initialValues(capacité, v));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [capacité?.id, variants, ref]);

  async function chiffrerLeJob() {
    if (!capacité || !ref) return;
    try {
      const réponse = await api.admission(ref, toContractInput(formData, capacité));
      setChiffrage({
        admission: réponse.admission,
        reproches: [...réponse.input_errors, ...réponse.blockers],
        erreur: null,
      });
    } catch (cause) {
      setChiffrage({ admission: null, reproches: [], erreur: phraseErreur(cause) });
    }
  }

  const avis = uniques([
    ...fromIssues(contrats.données?.issues ?? []),
    ...fromIssues(modèles.données?.issues ?? []),
    ...avisFormulaire,
  ]);

  const entrée = capacité ? toContractInput(formData, capacité) : {};

  return (
    <section className="ecurie-atelier" aria-label="Atelier">
      <BandeauRessources
        sondage={parc}
        pour={ref}
        picParametre={parametreDuPic(variant?.profile ?? null)}
      />

      <NoticeBanner notices={avis} />

      {contrats.erreur ? <p className="text-danger">{phraseErreur(contrats.erreur)}</p> : null}

      <div className="ecurie-choix">
        <div>
          <label htmlFor="capacite">Capacité</label>
          <select id="capacite" value={capId ?? ""} onChange={(e) => choisirCapacite(e.target.value)}>
            <option value="">— choisir —</option>
            {groupesDeCapacites(capacités).map((groupe) => (
              <optgroup key={groupe.etat} label={groupe.titre}>
                {groupe.capacites.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.title}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          {capacité ? (
            <p className="ecurie-etat-champ">{phraseEtat(etatCapacite(capacité))}</p>
          ) : null}
        </div>

        {capacité ? (
          <div>
            <label htmlFor="variant">Variant</label>
            <select
              id="variant"
              value={ref ?? ""}
              onChange={(e) => poserVariant(e.target.value || null)}
            >
              <option value="">— choisir —</option>
              {groupesDeVariants(models, capacité.incumbent).map((groupe) => (
                <optgroup
                  key={groupe.modele}
                  label={groupe.titulaire ? `${groupe.modele} (titulaire)` : groupe.modele}
                >
                  {groupe.variants.map((v) => (
                    <option key={v.ref} value={v.ref}>
                      {v.id}
                      {v.ready ? "" : " — non exécutable"}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            {variant ? <FicheVariant variant={variant} /> : null}
          </div>
        ) : null}
      </div>

      {capacité ? (
        <>
          <h2>{capacité.title}</h2>
          {capacité.description ? <p>{capacité.description}</p> : null}

          <CapabilityForm
            capability={capacité}
            variant={variant}
            runtimeOptions={runtimeOptions}
            resident={estRésident}
            formData={formData}
            onChange={setFormData}
            onNotices={setAvisFormulaire}
          />

          <div className="ecurie-actions">
            <button type="button" onClick={chiffrerLeJob} disabled={!ref}>
              Chiffrer ce job
            </button>
            <p className="ecurie-etat-champ">
              Pas de bouton <em>Lancer</em> : la soumission d'un job et sa progression en
              SSE attendent le déménagement du superviseur dans le processus de l'API
              (tâche 4.6). En attendant, <code>ecurie run {ref ?? "<ref>"}</code>.
            </p>
          </div>

          {chiffrage ? (
            <>
              <h3>Pour l'entrée saisie</h3>
              <ResultatChiffrage chiffrage={chiffrage} />
            </>
          ) : null}

          <details open={projectionOuverte} onToggle={(e) => setProjectionOuverte(e.currentTarget.open)}>
            <summary>Ce qui partirait ({Object.keys(entrée).length} champ(s))</summary>
            <pre className="ecurie-json">{JSON.stringify(entrée, null, 2)}</pre>
          </details>

          <h3>Ce que ce job produirait</h3>
          <SortiesPromises capability={capacité} />
        </>
      ) : null}
    </section>
  );
}

/**
 * Ce qu'on sait du variant choisi, y compris ce qui l'empêche de tourner.
 *
 * Les blockers sont ici plutôt que dans le libellé de l'`<option>` : ils portent
 * la commande qui répare — « poids absents du cache — ecurie pull … » —, et
 * `hunyuan3d-2.1-shape-mlx@mlx-bf16` en a trois. Un sélecteur qui les concatène
 * n'est plus un sélecteur.
 */
function FicheVariant({ variant }: { variant: Variant }) {
  const profil = variant.profile;
  return (
    <div className="ecurie-fiche">
      <p className="ecurie-etat-champ">
        {formatBytes(profil?.peak_unified_memory_bytes ?? null)} · {phraseLourdeur(variant.heavy)} ·
        runtime {variant.runtime} · palier {variant.tier}
        {profil ? ` · mesuré le ${jourMesure(profil.measured_at)}` : ""}
      </p>
      {variant.caveats?.length ? (
        <ul className="ecurie-etat-champ">
          {variant.caveats.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      ) : null}
      {variant.ready ? null : (
        <ul className="text-danger">
          {variant.blockers.map((b) => (
            <li key={b}>{b}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Le coût de l'entrée saisie — ou ce qui a empêché de le calculer. */
function ResultatChiffrage({ chiffrage }: { chiffrage: Chiffrage }) {
  if (chiffrage.erreur) {
    return <p className="text-danger">{chiffrage.erreur}</p>;
  }
  const lignes = phrasesAdmission(chiffrage.admission);
  return (
    <div className="ecurie-chiffrage" data-severite={severiteAdmission(chiffrage.admission)}>
      <ul className="ecurie-cout">
        {lignes.map((ligne) => (
          <li key={ligne}>{ligne}</li>
        ))}
      </ul>
      {chiffrage.reproches.length ? (
        <ul className="text-danger">
          {chiffrage.reproches.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
