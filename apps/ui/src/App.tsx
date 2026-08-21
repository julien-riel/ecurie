/**
 * Le banc de rendu du socle — pas un écran.
 *
 * L'architecture pose un **plafond ferme de quatre écrans** : Atelier (4.4),
 * Parc (4.5), Confrontation (5.4), Bibliothèque (5.5). Ce fichier n'en est
 * aucun, et n'a pas vocation à devenir le cinquième : il montre que les deux
 * tables du socle fonctionnent sur le vrai parc — une capacité, un variant, un
 * formulaire rendu depuis le contrat, le coût que le serveur annonce pour la
 * saisie en cours. Il sera remplacé par `src/ecrans/Atelier.tsx`.
 *
 * Ce qu'il ne fait délibérément pas : **aucun bouton pour lancer**. `POST /jobs`
 * et le flux SSE n'existent pas côté serveur et attendent le déménagement du
 * superviseur dans le processus de l'API (tâche 4.6). Le coût d'admission n'est
 * demandé qu'au changement de variant et sur clic explicite — le recalcul à
 * chaque frappe est nommément la tâche 4.7, avec le profil paramétré qui la
 * justifie.
 */

import { useMemo, useState } from "react";
import * as api from "./api/endpoints";
import { optionsFor } from "./api/residents";
import type { Capability, Variant } from "./api/types";
import { useResource } from "./api/useResource";
import { phraseErreur } from "./api/errors";
import { CapabilityForm } from "./form/CapabilityForm";
import { formatBytes, phraseLourdeur } from "./format/bytes";
import type { Notice } from "./notices/notices";
import { NoticeBanner } from "./notices/NoticeBanner";
import { fromIssues, uniques } from "./notices/notices";
import { OutputPanel } from "./output/OutputPanel";
import { initialValues } from "./schema/defaults";
import { etatCapacite, phraseEtat } from "./schema/etat";
import { toContractInput } from "./schema/projection";

/**
 * Une sortie fictive, bâtie sur les chemins pointés du contrat.
 *
 * Aucun job ne tourne au 4.3 : le banc montre quels visualiseurs la capacité
 * mobiliserait. Les clés viennent d'`output_media_types`, qui porte des chemins
 * **pointés** — `tracks.vocals` —, et l'objet est reconstruit en profondeur.
 * Prendre les seules clés de premier niveau de `output.properties` afficherait
 * `tracks` comme un fichier unique, alors que c'est un objet qui contient les
 * chemins : la seule capacité du parc à sorties imbriquées, `audio-separation`,
 * ne montrerait aucun lecteur.
 */
function sortieDExemple(mediaTypes: Record<string, string>): Record<string, unknown> {
  const sortie: Record<string, unknown> = {};
  for (const chemin of Object.keys(mediaTypes)) {
    const parties = chemin.split(".");
    let noeud = sortie;
    for (const partie of parties.slice(0, -1)) {
      noeud[partie] ??= {};
      noeud = noeud[partie] as Record<string, unknown>;
    }
    noeud[parties[parties.length - 1]!] = `<${chemin}>`;
  }
  return sortie;
}

export function App() {
  const contrats = useResource((s) => api.capabilities(s), []);
  const [capId, setCapId] = useState<string | null>(null);
  const [ref, setRef] = useState<string | null>(null);
  const [formData, setFormData] = useState<Record<string, unknown>>({});
  const [avisFormulaire, setAvisFormulaire] = useState<readonly Notice[]>([]);

  const capacités = contrats.données?.capabilities ?? [];
  const capacité: Capability | null = capacités.find((c) => c.id === capId) ?? null;

  const modèles = useResource(
    (s) => (capacité ? api.models(capacité.id, s) : Promise.resolve(null)),
    [capacité?.id],
  );

  const variants: Variant[] = useMemo(
    () => (modèles.données?.models ?? []).flatMap((m) => m.variants),
    [modèles.données],
  );
  const variant = variants.find((v) => v.ref === ref) ?? null;

  // Les résidents ne servent qu'à peupler les suggestions d'un `x-options-from`
  // et à savoir si le variant est chargé. Aucun sondage : c'est le bandeau de
  // ressources de la tâche 4.4 qui rafraîchira, pas ce banc.
  const résidents = useResource((s) => api.residents(undefined, s), []);
  const runtimeOptions = optionsFor(résidents.données?.residents ?? [], ref);
  const estRésident = (résidents.données?.residents ?? []).some((r) => r.ref === ref);

  const [coût, setCoût] = useState<string | null>(null);
  const [reproches, setReproches] = useState<readonly string[]>([]);

  function choisirCapacite(id: string) {
    setCapId(id);
    setRef(null);
    setFormData({});
    setCoût(null);
    setReproches([]);
  }

  function choisirVariant(nouveau: string) {
    setRef(nouveau);
    setCoût(null);
    setReproches([]);
    if (capacité) {
      const v = variants.find((x) => x.ref === nouveau) ?? null;
      setFormData(initialValues(capacité, v));
    }
  }

  async function demanderCout() {
    if (!capacité || !ref) return;
    try {
      const réponse = await api.admission(ref, toContractInput(formData, capacité));
      const a = réponse.admission;
      setCoût(
        `${formatBytes(a.peak_bytes)} — ${a.reason}` +
          (a.evict.length ? ` (déchargerait : ${a.evict.join(", ")})` : "") +
          (a.peak_note ? ` · ${a.peak_note}` : ""),
      );
      setReproches([...réponse.input_errors, ...réponse.blockers]);
    } catch (cause) {
      setCoût(phraseErreur(cause));
      setReproches([]);
    }
  }

  const avis = uniques([
    ...fromIssues(contrats.données?.issues ?? []),
    ...fromIssues(modèles.données?.issues ?? []),
    ...avisFormulaire,
  ]);

  return (
    <main className="ecurie">
      <header>
        <h1>Écurie — socle de rendu</h1>
        <p className="ecurie-etat-champ">
          Banc de la tâche 4.3 : un contrat de capacité engendre son formulaire, sans une ligne
          écrite pour lui. Les écrans arrivent au 4.4.
        </p>
        <button type="button" onClick={contrats.recharger}>
          Recharger le registre
        </button>
      </header>

      <NoticeBanner notices={avis} />

      {contrats.erreur ? <p className="text-danger">{phraseErreur(contrats.erreur)}</p> : null}

      <section>
        <label htmlFor="capacite">Capacité</label>
        <select
          id="capacite"
          value={capId ?? ""}
          onChange={(e) => choisirCapacite(e.target.value)}
        >
          <option value="">— choisir —</option>
          {capacités.map((c) => (
            <option key={c.id} value={c.id}>
              {c.title} ({phraseEtat(etatCapacite(c))})
            </option>
          ))}
        </select>
      </section>

      {capacité ? (
        <section>
          <label htmlFor="variant">Variant</label>
          <select id="variant" value={ref ?? ""} onChange={(e) => choisirVariant(e.target.value)}>
            <option value="">— choisir —</option>
            {variants.map((v) => (
              <option key={v.ref} value={v.ref}>
                {v.ref} — {v.ready ? "prêt" : v.blockers.join(" ; ")}
              </option>
            ))}
          </select>
          {variant ? (
            <p className="ecurie-etat-champ">
              {formatBytes(variant.profile?.peak_unified_memory_bytes ?? null)} ·{" "}
              {phraseLourdeur(variant.heavy)} · runtime {variant.runtime}
            </p>
          ) : null}
        </section>
      ) : null}

      {capacité ? (
        <section>
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
          <h3>Ce qui partirait</h3>
          <pre className="ecurie-json">
            {JSON.stringify(toContractInput(formData, capacité), null, 2)}
          </pre>
          <button type="button" onClick={demanderCout} disabled={!ref}>
            Chiffrer ce job
          </button>
          {coût ? <p className="ecurie-cout">{coût}</p> : null}
          {reproches.length ? (
            <ul className="text-danger">
              {reproches.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          ) : null}

          <h3>Sorties de cette capacité</h3>
          <OutputPanel
            sortie={sortieDExemple(capacité.output_media_types)}
            mediaTypes={capacité.output_media_types}
          />
        </section>
      ) : null}
    </main>
  );
}
