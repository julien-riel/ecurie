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

import { screen, waitFor } from "@testing-library/react";
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

/** Choisit une capacité et attend que ses variants soient arrivés. */
export async function choisir(capability: string): Promise<HTMLSelectElement> {
  await userEvent.selectOptions(await screen.findByLabelText("Capacité"), capability);
  const variants = (await screen.findByLabelText("Variant")) as HTMLSelectElement;
  await waitFor(() => expect(variants.options.length).toBeGreaterThan(1));
  return variants;
}
