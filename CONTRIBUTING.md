# Contributing

The contribution this project wants is **data, not code**. That is not a
brush-off — it is the one contribution that scales with a single maintainer,
and it is exactly the one the product needs.

## Measurement PRs — the front door

Écurie's admission control runs on measured memory profiles, and every profile
so far comes from one machine. A measurement from *your* machine class is the
most valuable thing you can send.

```bash
uv run ecurie bench <model>@<variant>
```

writes `registry/measurements/<ref>/<machine>.json` — one file, named after the
hardware, never clobbering anyone else's relevé. Open a PR containing that
file and nothing else. CI validates the schema and the invariants; a green
check is the whole review. **No human code review is needed to merge a
measurement.**

Rules, enforced by `ecurie registry validate`:

- what you did not measure, you do not commit;
- machine-dependent numbers (`warmup_ms`, latencies, throughput,
  `peak_scaling` slopes) stay in your relevé, never in the manifest;
- a manifest's `profile:` block must match at least one relevé on the portable
  numbers (`disk_bytes`, `peak_unified_memory_bytes`).

## Issues

Welcome, in English or French. Bug reports against the twelve supported
capabilities carry the most weight (see [SUPPORT.md](SUPPORT.md) for the
supported/experimental line). Include your machine class and the relevant
command output.

## What is maintainer-only in v1

**New runtimes and new capability contracts.** Each runtime is a Python
ecosystem to keep alive and each contract is an editorial commitment; both are
exactly the maintenance surface that kills solo projects. Propose them in an
issue first — a PR adding a runtime or a contract will be closed with a
pointer here, however good it is. This is the anti-burnout rule, stated up
front rather than discovered in a stale review queue.

Code PRs for bugs in the supported path are read, but expect slowness and
expect "no" to anything that widens scope. When in doubt, open the issue first.

## Language policy

**The product speaks English; the engine room still speaks French — and is
migrating.** User-facing surfaces (README, CLI output, MCP tool descriptions,
errors) are English-first. Identifiers, the design documents
(ARCHITECTURE/CONCEPTION/PLAN) and most of the registry are still French; the
migration is progressive — any file that gets substantially rewritten is
migrated in the same pass. Don't send translation-only PRs; they will collide
with rewrites already in flight. French issues and commit messages remain
perfectly fine.

## Developing

```bash
uv run pytest                 # the suite; `real` tests excluded by default
uv run ruff check packages/   # lint, 100-column lines
cd apps/ui && npm test        # front-end suite
```

Two guards to know: the OpenAPI schema is frozen by `tools/openapi_dump.py` and
compared by a test — regenerate when you touch a route; UI fixtures are frozen
by `tools/ui_fixtures.py` over the real registry — `npm run fixtures`
regenerates after registry edits.
