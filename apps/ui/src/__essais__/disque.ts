/**
 * Le disque tel que les tests de l'écran Parc le voient — les trois réponses, sans serveur.
 *
 * Contrairement au parc des capacités (`parc.ts`), rien ici ne vient d'une
 * fixture capturée : `tools/ui_fixtures.py` fige le **registre**, qui est un
 * état déclaré et vit dans Git, alors que l'occupation disque est un état
 * observé, propre à la machine qui scanne. Capturer les trente giga-octets de
 * la machine de référence figerait des chemins personnels dans le dépôt et
 * rendrait la suite dépendante de ce qui est téléchargé ce jour-là.
 *
 * Les chiffres sont donc composés, mais pas inventés : ce sont ceux du parc
 * d'essai de `packages/api/tests/test_store_endpoint.py`, le même contenu
 * détenu par deux gestionnaires, pour que les deux suites parlent du même
 * disque.
 */

import { repond } from "../../vitest.setup";
import type {
  Figures,
  Plan,
  StorePlanResponse,
  StoreSummaryResponse,
  TieringResponse,
} from "../api/types";

export const SHA = "a".repeat(64);
export const SHA_AUTRE = "b".repeat(64);

export function figures(partiel: Partial<Figures> = {}): Figures {
  return {
    apparent_bytes: 2000,
    real_unique_bytes: 1000,
    recoverable: {
      duplication_bytes: 1000,
      hf_stale_bytes: 0,
      orphan_bytes: 0,
      unused_known: false,
      unused_bytes: 0,
      total_known_bytes: 1000,
    },
    by_manager: { hf: [1000, 1], ollama: [1000, 1] },
    duplicates: [
      {
        sha256: SHA,
        size: 1000,
        paths: ["/hub/model.safetensors", "/ollama/blobs/sha256-aaa"],
        reclaimable_bytes: 1000,
      },
    ],
    cold: [],
    cold_unavailable: [],
    unresolved_bytes: 1000,
    unresolved_count: 1,
    unverified_bytes: 0,
    unverified_count: 0,
    announced_bytes: 0,
    unused_variants: [],
    mismatched: [],
    ...partiel,
  };
}

export function resume(partiel: Partial<StoreSummaryResponse> = {}): StoreSummaryResponse {
  return {
    scanned: true,
    last_scan_at: "2026-08-22T10:00:00+00:00",
    stale: false,
    figures: figures(),
    telemetry: { conclusive: false, first_run_at: null, unused_after_days: 90 },
    hint: null,
    ...partiel,
  };
}

export function plan(partiel: Partial<Plan> = {}): Plan {
  return {
    plan_version: 1,
    plan_id: "3f2a1b",
    generated_at: "2026-08-22T10:05:00+00:00",
    scan_id: "scan-1",
    telemetry: false,
    unused_after_days: 90,
    actions: [
      {
        kind: "hardlink",
        keep: "/hub/model.safetensors",
        replace: ["/ollama/blobs/sha256-aaa"],
        sha256: SHA,
        hash_source: "verified",
        reason: "duplication",
        bytes_reclaimed: 1000,
        stats: {
          "/hub/model.safetensors": { size: 1000, mtime: 0, inode: 10, device: 1 },
          "/ollama/blobs/sha256-aaa": { size: 1000, mtime: 0, inode: 11, device: 1 },
        },
      },
    ],
    ignored: [],
    by_reason: { duplication: 1000 },
    total_bytes_reclaimed: 1000,
    ...partiel,
  };
}

export function reponsePlan(partiel: Partial<StorePlanResponse> = {}): StorePlanResponse {
  return {
    scanned: true,
    last_scan_at: "2026-08-22T10:00:00+00:00",
    stale: false,
    plan: plan(),
    labels: {
      duplication: "duplication inter-gestionnaires",
      "hf-stale-revision": "révision HF obsolète",
      "orphan-blob": "blob orphelin",
      "unused-variant": "variant jamais utilisé",
      "detached-snapshot": "instantané HF détaché",
    },
    command: "ecurie store plan",
    hint: null,
    ...partiel,
  };
}

export function tiering(partiel: Partial<TieringResponse> = {}): TieringResponse {
  return {
    scanned: true,
    last_scan_at: "2026-08-22T10:00:00+00:00",
    volumes: [],
    cold: [],
    variants: [],
    hint: null,
    ...partiel,
  };
}

/** Les trois lectures dont l'écran Parc a besoin pour s'afficher. */
export function poserLeDisque(
  partiel: {
    resume?: StoreSummaryResponse;
    plan?: StorePlanResponse;
    tiering?: TieringResponse;
  } = {},
): void {
  repond("/store/summary", { body: partiel.resume ?? resume() });
  repond("/store/plan", { body: partiel.plan ?? reponsePlan() });
  repond("/store/tiering", { body: partiel.tiering ?? tiering() });
}
