/**
 * Le formulaire d'une capacité — rendu depuis son contrat, sans une ligne écrite pour lui.
 *
 * `schema` reçoit `capability.input` **tel quel**, tel que le serveur le sert,
 * tel qu'il est écrit dans `registry/capabilities/`. Rien n'est recomposé : ni
 * depuis `capability.required`, ni depuis `capability.defaults`, que l'API
 * duplique hors du schéma par commodité. Recomposer serait refaire un travail
 * déjà fait, et le refaire est la façon la plus sûre de finir par en diverger.
 *
 * Tout ce que le front décide vit dans l'`uiSchema` engendré par
 * `compileUiSchema`. Ce composant, lui, ne connaît aucune capacité par son nom.
 *
 * La compilation est mémoïsée, et les avis remontent par un effet plutôt que
 * pendant le rendu. Ce n'est pas de l'optimisation : remonter un tableau neuf
 * pendant le rendu du parent le fait se rendre à nouveau, qui recompile, qui
 * remonte un autre tableau neuf — une boucle infinie, invisible tant qu'aucun
 * appelant ne passe `onNotices`, et découverte au premier essai contre un vrai
 * serveur.
 */

import Form from "@rjsf/core";
import type { IChangeEvent } from "@rjsf/core";
import { useEffect, useMemo } from "react";
import type { Capability, Variant } from "../api/types";
import type { SchemaEntree } from "../schema/contract";
import type { Notice } from "../notices/notices";
import { compileUiSchema } from "../schema/uiSchema";
import { JsonField } from "./widgets/JsonField";
import { WIDGETS } from "./widgets";
import { validator } from "./validator";

export interface CapabilityFormProps {
  capability: Capability;
  variant: Variant | null;
  /** Ce que le worker a annoncé au chargement, s'il est résident. */
  runtimeOptions?: Record<string, unknown>;
  resident?: boolean;
  formData: Record<string, unknown>;
  onChange: (formData: Record<string, unknown>) => void;
  /** Les dégradations rencontrées à la compilation, remontées au bandeau. */
  onNotices?: (notices: readonly Notice[]) => void;
}

const FIELDS = { json: JsonField };

export function CapabilityForm(props: CapabilityFormProps) {
  const { capability, runtimeOptions, resident, formData, onChange, onNotices } = props;
  const entree = capability.input as unknown as SchemaEntree;

  // La clé de mémoïsation porte sur le **contenu**, jamais sur l'identité des
  // objets reçus. Un appelant qui reconstruit son contrat ou son dictionnaire
  // d'options à chaque rendu — ce qu'aucune signature n'interdit — rendrait
  // sinon la mémoïsation inopérante et ramènerait la boucle. Sérialiser coûte
  // quelques microsecondes sur un schéma de quelques kilo-octets.
  const clé = JSON.stringify({
    entree,
    runtime: runtimeOptions ?? {},
    resident: resident ?? false,
  });
  const compilé = useMemo(
    () => compileUiSchema(entree, { capacite: capability.id, runtime: runtimeOptions, resident }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [capability.id, clé],
  );

  useEffect(() => {
    onNotices?.(compilé.notices);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [compilé]);

  return (
    <Form
      schema={entree as never}
      uiSchema={compilé.uiSchema}
      formData={formData}
      validator={validator}
      widgets={WIDGETS}
      fields={FIELDS}
      liveValidate
      showErrorList={false}
      // Aucun bouton dans le formulaire : *Lancer* appartient à l'écran, qui
      // sait aussi quel variant est choisi et si un job tourne déjà. Un bouton
      // de soumission RJSF ne verrait que l'entrée.
      onChange={(e: IChangeEvent) => onChange((e.formData ?? {}) as Record<string, unknown>)}
    />
  );
}
