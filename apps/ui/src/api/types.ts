/**
 * Les types de l'API, et ce que le schéma OpenAPI ne sait pas dire.
 *
 * `openapi.gen.ts` est engendré depuis `openapi.json`, lui-même figé par
 * `tools/openapi_dump.py` : c'est la forme des réponses, et elle n'est jamais
 * écrite à la main. Ce fichier ne fait que trois choses par-dessus.
 *
 * **Il nomme.** `components["schemas"]["CapabilityOut"]` est exact et illisible ;
 * `Capability` se lit.
 *
 * **Il remet obligatoire ce que pydantic rend optionnel.** Un champ déclaré
 * `Field(default_factory=list)` sort du schéma OpenAPI sans `required`, alors que
 * le serveur l'émet toujours — `issues`, `blockers`, `output_media_types`,
 * `ready_variants`. Sans correction, chaque composant sèmerait des `?? []` pour
 * un cas qui n'arrive jamais, et le jour où l'un d'eux manquerait vraiment,
 * personne ne le verrait. `Requis` les rétablit ici, une fois.
 *
 * **Il écrit ce que pydantic transporte en dictionnaire opaque.** `figures` est
 * un `Json` côté serveur, par un choix assumé de `schemas.py` : le calcul est
 * dans `ecurie_store.figures` et un miroir pydantic finirait par diverger. Le
 * front hérite du même problème et le règle de la même façon — une interface
 * écrite à la main, qui ne prétend pas à autre chose.
 *
 * Un point qui n'est pas une omission : **le JSON Schema d'entrée d'une capacité
 * n'est pas typé.** C'est une donnée, pas un type. Le registre se recharge à
 * chaud ; un type engendré depuis `registry/capabilities/` figerait au build ce
 * que le serveur fait bouger, et « ajouter un modèle = ajouter un YAML »
 * cesserait d'être vrai.
 */

import type { components } from "./openapi.gen";

type Schemas = components["schemas"];

/** Rend obligatoires les clés `K` de `T` — celles que le serveur émet toujours. */
type Requis<T, K extends keyof T> = Omit<T, K> & { [P in K]-?: NonNullable<T[P]> };

// --- registre ---------------------------------------------------------------------

export type Issue = Schemas["IssueOut"];

export type Capability = Requis<
  Schemas["CapabilityOut"],
  "defaults" | "models" | "output_media_types" | "ready_variants" | "required" | "steps"
>;

export type Variant = Requis<Schemas["VariantOut"], "blockers">;
export type Model = Omit<Schemas["ModelOut"], "variants"> & { variants: Variant[] };
export type Profile = Schemas["ProfileOut"];
export type PeakScaling = Schemas["PeakScalingOut"];
export type Source = Schemas["SourceOut"];

export type CapabilitiesResponse = Omit<
  Requis<Schemas["CapabilitiesResponse"], "issues">,
  "capabilities"
> & { capabilities: Capability[] };

export type ModelsResponse = Omit<Requis<Schemas["ModelsResponse"], "issues">, "models"> & {
  models: Model[];
};

// --- exécution --------------------------------------------------------------------

export type Resident = Requis<Schemas["ResidentOut"], "options">;
export type StaleWorker = Schemas["StaleWorkerOut"];
export type Policy = Schemas["PolicyOut"];
export type Admission = Requis<Schemas["AdmissionOut"], "blockers" | "evict">;

export type ResidentsResponse = Omit<
  Requis<Schemas["ResidentsResponse"], "stale">,
  "residents" | "admission"
> & {
  residents: Resident[];
  admission: Admission | null;
};

export type AdmissionRequest = Schemas["AdmissionRequest"];
export type AdmissionResponse = Omit<
  Requis<Schemas["AdmissionResponse"], "blockers" | "input_errors">,
  "admission"
> & { admission: Admission };

// --- jobs ---------------------------------------------------------------------------

/**
 * L'état d'un job — celui que `POST /jobs` rend, et que chaque événement répète.
 *
 * Dix champs passent par `Requis` : pydantic les déclare avec un
 * `default_factory`, ce qui les sort du `required` d'OpenAPI, alors que le
 * serveur les émet toujours. Deux méritent d'être distingués ici parce que rien
 * dans leur nom ne le dit :
 *
 * - `output` est la réponse du worker **entière** — c'est elle qu'on aplatit,
 *   parce qu'`audio-separation` déclare cinq pistes et n'en produit que deux ou
 *   quatre selon `stems` ;
 * - `outputs` n'en retient que les sorties qui sont des fichiers, et `files` les
 *   mêmes clés vers l'URL que **le serveur** a composée. Le front n'en fabrique
 *   aucune : une sortie imbriquée est un chemin à plusieurs segments.
 */
export type Job = Requis<
  Schemas["JobOut"],
  | "evicted"
  | "files"
  | "input"
  | "metrics"
  | "note"
  | "output"
  | "outputs"
  | "progress"
  | "reused"
  | "warnings"
>;

/** Le job et son manifeste — `null` tant qu'il n'est pas terminé. */
export type JobDetail = Job & Pick<Schemas["JobDetail"], "manifest">;

export type JobState = Schemas["JobOut"]["state"];

// --- parc disque ------------------------------------------------------------------

/**
 * `figures` du côté du front.
 *
 * Écrit à la main pour la raison que `schemas.py` donne côté serveur : c'est
 * `ecurie_store.figures` qui fait autorité, et un miroir pydantic de sept
 * dataclasses imbriquées finirait par diverger sans qu'aucun test ne le voie.
 * Le front hérite du même problème et le règle de la même façon.
 *
 * Trois formes surprennent, et sont notées ici plutôt que découvertes :
 *
 * - le troisième chiffre n'est **pas** un entier mais un objet, `recoverable`,
 *   qui détaille les quatre postes du plan de récupération ;
 * - `by_manager` porte un **tableau** `[octets, nombre de fichiers]`, non un
 *   objet ;
 * - deux clés sont injectées par la route après le calcul et ne figurent dans
 *   aucune dataclass : `recoverable.total_known_bytes` et `cold_unavailable`,
 *   parce que les propriétés calculées ne sortent pas d'`asdict`.
 *
 * Écrire un troisième chiffre plat ici aurait rendu `figures.recoverable_bytes`
 * indéfini au premier écran Parc, et le typage n'en aurait rien dit.
 */
export interface Recoverable {
  duplication_bytes: number;
  hf_stale_bytes: number;
  orphan_bytes: number;
  unused_known: boolean;
  unused_bytes: number;
  /** Injecté par la route : la somme des postes connus, pour ne pas la refaire. */
  total_known_bytes?: number;
}

export interface Figures {
  apparent_bytes: number;
  real_unique_bytes: number;
  recoverable: Recoverable;
  by_manager?: Record<string, [number, number]>;
  [autre: string]: unknown;
}

export type StoreSummaryResponse = Omit<Schemas["StoreSummaryResponse"], "figures"> & {
  figures: Figures | null;
};

export type Telemetry = Schemas["TelemetryOut"];

// --- index ------------------------------------------------------------------------

export interface IndexResponse {
  service: string;
  version: string;
  root: string;
  home: string;
  models: number;
  capabilities: number;
  registry_errors: number;
  registry_warnings: number;
  docs: string;
}

// --- unions ouvertes ----------------------------------------------------------------

/**
 * Ces valeurs sont fermées dans le registre et **ouvertes** ici, à dessein.
 *
 * Le `| string` n'est pas une négligence de typage : il autorise le registre à
 * gagner un palier, un runtime ou un statut que cette version de l'UI ne
 * connaît pas, sans qu'un écran cesse de s'afficher. Le prix est qu'aucun
 * aiguillage sur ces valeurs ne peut se passer d'une branche par défaut — c'est
 * exactement la garantie qu'on veut : un parc qui évolue ne casse pas l'écran.
 *
 * Les valeurs énumérées sont celles de `registry/schema/model.schema.json`, et
 * elles ne s'inventent pas : le registre ne connaît ni « rejected » ni
 * « copyleft », mais bien `deprecated` et `research-only`.
 */
export type Tier = "hot" | "cold" | "absent" | (string & {});
export type Status = "active" | "candidate" | "deprecated" | "retired" | (string & {});
export type LicenseClass =
  | "permissive"
  | "restricted"
  | "research-only"
  | "unknown"
  | (string & {});
export type SourceKind = "huggingface" | "ollama" | "url" | "local" | (string & {});
export type RuntimeName =
  | "mlx-lm"
  | "mlx-audio"
  | "mlx-vlm"
  | "diffusers-mps"
  | "torch-vision"
  | "comfy"
  | "ollama"
  | "llama-cpp"
  | "custom"
  | (string & {});
export type Severity = "error" | "warning";
