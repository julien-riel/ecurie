# Contributing

The contribution this project wants is **data, not code**. That is not a
brush-off — it is the one contribution that scales with a single maintainer,
and it is exactly the one the product needs.

## Measurement PRs — the front door

Écurie's admission control runs on measured memory profiles, and every profile
so far comes from one machine. A measurement from *your* machine class is the
most valuable thing you can send.

You need the runtime and the weights first. `env sync` takes names and rebuilds
all eighteen runtimes when given none — 13 GB of venvs — so name the one your
variant declares in its manifest:

```bash
uv run ecurie env sync <runtime>         # that runtime's isolated venv, from its uv.lock
uv run ecurie pull <model>@<variant>     # at the manifest's pinned revision
uv run ecurie bench <model>@<variant>    # empties the fleet, then measures
```

The bench empties the fleet before it measures — that is what makes the number
comparable. Don't run it in the middle of something.

### What the bench produces, and what goes in the PR

`ecurie bench` writes exactly one file,
`registry/measurements/<ref>/<machine>.json`, named after the hardware so it
never clobbers anyone else's relevé — and writes it only if every bench case
passed. It then **prints, without committing**, a
`profile:` block ready to paste under the variant in
`registry/models/<model>.yaml`. Whether you send one file or two depends on why
you benched:

- **The variant already carries a `profile:`** and you are measuring a second
  machine class — the first list below. Send the relevé, and nothing else. The
  manifest keeps the profile it has: the validator accepts a `measured_on` this
  clone has no relevé for, as long as the portable numbers agree with one of
  the relevés present.
- **The variant carries no `profile:` at all** — the second list below. Here
  the relevé alone changes nothing, and that is the trap: admission reads the
  manifest, not `measurements/`, so the variant stays refused and your machine
  hour buys nobody anything. Send two files — the relevé, and the printed block
  pasted into the manifest. Paste it verbatim, indentation included: one level
  too far and it lands inside `source:`, which YAML accepts without complaint
  and the validator later reports in words that never mention indentation.

**No human code review is needed to merge a measurement**; that is the point of
the format. The maintainer merges on the checks below rather than on a reading
of your diff. Take that for exactly what it is: green checks prove the schema
holds, the tests pass, the lint is clean and every capability contract still
renders a form. They do not prove the one rule this page calls non-negotiable —
that a `profile:` block is the copy of a measurement and not an estimate. Every
branch of `_check_profile` (`packages/core/src/ecurie_core/registry.py`) files
an `Issue("warning")`, never an error, and warnings fail neither job: a profile
typed by hand crosses both green and leaves one yellow line in the validate
output. That line is the whole of the human review a measurement PR gets, so
send numbers you actually measured.

Rules, and what actually checks them:

- what you did not measure, you do not commit;
- the file name and `measured_on` are derived from your hardware and your
  runtime versions — don't edit them. The validator uses `measured_on` to
  decide whether it demands exact equality with your relevé or mere agreement
  on the portable numbers;
- a manifest's `profile:` block must match at least one relevé on the
  **portable** numbers — `disk_bytes` and `peak_unified_memory_bytes`, the two
  that don't depend on which Mac loads the weights (`PORTABLE_FIELDS`, in
  `packages/core/src/ecurie_core/registry.py`). The machine-dependent numbers
  the bench prints alongside them do travel into the manifest — all 69 profiled
  manifests carry `warmup_ms`, and the schema declares it — but nothing ever
  compares them across machines, because that would only reproach a manifest
  for having been measured somewhere else.

Note that a manifest matching no relevé is a *warning* today, not an error, and
`ecurie registry validate` exits 0 with warnings — so read its output rather
than trusting its exit code.

Two lists are worth your machine's time. First, the twelve supported
capabilities re-measured on a machine class that is not a 24 GB Mac: that is
what lets **J2** ship borrowed profiles instead of asking everyone to re-bench.
Second, the fifteen variants that carry no profile at all and are therefore
refused admission outright: `sam3@bf16`, `sam3@4bit`, `trellis2@bf16`,
`deepfilternet3-mlx@fp32-mlx`, `bisenet-parsing@r34`, `edgeface@xxs`,
`headpose-6drepnet@mobilenetv2`, `mobilegaze@mobilenetv2`,
`qwen3-forced-aligner@bf16`, and the six `qwen36-27b-*@4bit` — `describe`,
`detect`, `ocr`, `outils`, `traduction`, `video`. The list is written out here
because nothing prints it: `ecurie registry validate` has no check for a
profile-less variant and names none of the fifteen (it exits 0 with two
standing `trellis2` warnings and nothing else). A first profile turns a variant
from unusable into usable.

## What every pull request runs

Two jobs, on Ubuntu, defined in `.github/workflows/ci.yml`:

- **registry, tests, lint** — `uv sync --locked`, then
  `uv run ecurie registry validate`, `uv run ruff check .`, `uv run pytest -q`
  (lint runs before the tests, whatever the job's frozen name suggests: steps
  are fail-fast and ruff costs 0.04 s against pytest's 33 s);
- **front-end** — `npm ci`, `npm run typecheck`, `npm test`, in `apps/ui`.

The second job is not front-end hygiene. The vitest suite reads
`registry/capabilities/*.json` off the disk at test time, so a pull request that
touches a capability contract and no Python at all can redden it — which is the
point, since that suite is what proves a contract still renders a form. A
measurement PR touches neither contracts nor code, and sees both jobs green.

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
with rewrites already in flight. The capability contracts' `title` and
`description` are French today; the twelve served by the MCP server go English
with **J1**, because they are read by a model and their token cost is counted.
The other twenty-nine follow when they are touched. French issues and commit
messages remain perfectly fine.

## Developing

```bash
uv run pytest                 # the suite; the seven `real` tests excluded by default
uv run ruff check .           # lint, 100-column lines — the whole tree, as CI runs it
cd apps/ui && npm ci && npm test  # front-end suite
```

Lint the whole tree, not `packages/`: CI runs `ruff check .`, and the eighteen
runtimes under `runtimes/` are the largest body of code here. A healthy clone
gives 1,296 passed, 19 skipped, 5 deselected and 497 passed, 8 skipped
(2026-08-29, 24 GB reference machine); the skips are all *numpy absent* or
*Pillow absent* and are the same on the runner.

Two guards to know: the OpenAPI schema is frozen by `tools/openapi_dump.py` and
compared by a test — regenerate when you touch a route; UI fixtures are frozen
by `tools/ui_fixtures.py` over the real registry — `npm run fixtures`
regenerates after registry edits.

## Licensing

This repository is Apache-2.0 ([LICENSE](LICENSE)), and so is what you send: by
**article 5** of that licence, a contribution intentionally submitted for
inclusion in the work lands under its terms, with no additional terms and no
separate agreement. Relevés and manifests included — they are the contribution
this page asks for first. There is no CLA and no DCO to sign: neither would add
anything article 5 does not already say.
