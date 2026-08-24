/**
 * Le parc tel que les tests d'écran le voient : les vraies réponses, un serveur en moins.
 *
 * Les contrats et les manifestes viennent des fixtures capturées sur le vrai
 * registre par `tools/ui_fixtures.py` — ce sont les octets que le serveur
 * envoie, et deux tests pytest les gardent à jour. Les résidents, eux, ne
 * peuvent pas en venir : un résident est un processus vivant au moment de la
 * capture, et la capture ne charge aucun modèle. Ils sont donc fabriqués ici, à
 * partir du schéma.
 *
 * Ce module existe parce que deux fichiers de test montent le même écran sur le
 * même parc — le choix et le chiffrage d'un côté, les jobs de l'autre. Le
 * dupliquer aurait laissé les deux copies diverger, et c'est le genre de dérive
 * qu'on ne voit qu'en cherchant pourquoi un test passe et pas son jumeau.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { repond } from "../../vitest.setup";
import capacités from "../api/__fixtures__/capabilities.json";
import modèles from "../api/__fixtures__/models.json";
import type { Capability, Model, Resident, ResidentsResponse } from "../api/types";

export const CAPACITES = capacités as unknown as {
  capabilities: Capability[];
  issues: unknown[];
};
export const MODELES = modèles as unknown as { models: Model[]; issues: unknown[] };

export const GIO = 1024 ** 3;

export function modelesDe(capability: string) {
  return {
    models: MODELES.models.filter((m) => m.capability === capability),
    issues: MODELES.issues,
  };
}

/** Une réponse `/runtime/residents` complète, dans la forme du schéma OpenAPI. */
export function parc(partiel: Partial<ResidentsResponse> = {}): ResidentsResponse {
  const résidents = partiel.residents ?? [];
  const occupé = résidents.reduce((n, r) => n + r.peak_bytes, 0);
  return {
    budget_bytes: 16 * GIO,
    budget_source: "metal, via le mlx de runtimes/mlx-audio",
    budget_measured: true,
    used_bytes: occupé,
    free_bytes: 16 * GIO - occupé,
    policy: { budget_bytes: 16 * GIO, max_heavy_resident: 1, heavy_threshold_bytes: 8 * GIO },
    residents: résidents,
    stale: [],
    admission: null,
    ...partiel,
  } as ResidentsResponse;
}

export function resident(
  ref: string,
  peak_bytes: number,
  partiel: Record<string, unknown> = {},
): Resident {
  return {
    ref,
    pid: 4242,
    peak_bytes,
    heavy: peak_bytes > 8 * GIO,
    runtime: "mlx-audio",
    env: "mlx-audio",
    loaded_at: "2026-08-21T10:00:00+00:00",
    last_used: 1_755_712_345.6,
    pinned: false,
    busy: false,
    busy_by: 0,
    busy_since: 0,
    warmup_ms: 2400,
    options: {},
    socket: "/tmp/a.sock",
    log: "/tmp/a.log",
    ...partiel,
  } as Resident;
}

/** Les trois lectures dont l'Atelier a besoin pour s'afficher. */
export function poserLeParc(): void {
  repond("/registry/capabilities", { body: CAPACITES });
  repond("/runtime/residents", { body: parc() });
  repond("/registry/models", (requête) => {
    const capability = new URL(requête.url).searchParams.get("capability");
    return { body: capability ? modelesDe(capability) : MODELES };
  });
}

/**
 * Le même parc, mais une capacité dont plus rien n'est exécutable.
 *
 * Le cas est **fabriqué** et non cueilli, et c'est la leçon de sa première
 * écriture : deux tests prenaient `image-to-mesh` dans les fixtures parce
 * qu'elle affichait un titulaire sans rien de téléchargé, et ils sont tombés le
 * jour où quelqu'un a fait `ecurie pull hunyuan3d`. Un test qui dépend de ce
 * qu'un poste a sur son disque ne dit pas ce qu'il prétend dire.
 */
export function poserLeParcAvecCapaciteBloquee(capability: string, blockers: string[]): void {
  const contrats = {
    ...CAPACITES,
    capabilities: CAPACITES.capabilities.map((c) =>
      c.id === capability ? { ...c, ready_variants: [] } : c,
    ),
  };
  const bloqués = (models: Model[]) =>
    models.map((m) =>
      m.capability === capability
        ? { ...m, variants: m.variants.map((v) => ({ ...v, ready: false, blockers })) }
        : m,
    );

  repond("/registry/capabilities", { body: contrats });
  repond("/runtime/residents", { body: parc() });
  repond("/registry/models", (requête) => {
    const demandée = new URL(requête.url).searchParams.get("capability");
    const liste = demandée ? modelesDe(demandée).models : MODELES.models;
    return { body: { models: bloqués(liste), issues: MODELES.issues } };
  });
}

/**
 * Choisit une capacité et attend que ses variants soient arrivés.
 *
 * Le geste a changé au 4.8 : la capacité ne se choisit plus dans un `<select>`
 * mais dans un panneau, où chaque capacité est une carte. Les tests passent donc
 * par ici plutôt que de connaître le composant — c'est tout l'intérêt d'avoir
 * centralisé ce geste avant de le remplacer.
 */
export async function ouvrirLeSelecteur(): Promise<void> {
  await userEvent.click(await screen.findByRole("button", { name: /^Capacité/ }));
  await screen.findByRole("dialog");
}

export async function choisir(capability: string): Promise<HTMLSelectElement> {
  await choisirCapacite(capability);
  const variants = (await screen.findByLabelText("Variant")) as HTMLSelectElement;
  await waitFor(() => expect(variants.options.length).toBeGreaterThan(1));
  return variants;
}

/** Le seul choix de la capacité, sans rien attendre des variants. */
export async function choisirCapacite(capability: string): Promise<void> {
  const titre = CAPACITES.capabilities.find((c) => c.id === capability)?.title;
  if (!titre) throw new Error(`capacité absente de la fixture : ${capability}`);
  const déclencheur = await screen.findByRole("button", { name: /^Capacité/ });
  await userEvent.click(déclencheur);
  const panneau = await screen.findByRole("dialog");
  const cartes = within(panneau).getAllByRole("button", { name: new RegExp(échapper(titre)) });
  // Le titre d'une capacité peut être le préfixe d'un autre ; la carte porte le
  // sien en entier, et c'est la plus courte qui est la bonne.
  const carte = cartes.sort((a, b) => a.textContent!.length - b.textContent!.length)[0]!;
  await userEvent.click(carte);
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
}

function échapper(texte: string): string {
  return texte.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
