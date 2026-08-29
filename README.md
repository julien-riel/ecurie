# Écurie

**The never-swap MCP server for Apple Silicon** — local eyes, ears and a voice
for your coding agent: twelve multimodal tools, memory-safe by measured profiles.

An *écurie* is a racing stable. This one keeps a stable of open-weight models —
maintained, measured, raced, and retired.

Écurie **never executes a tensor itself, and never reimplements an engine**. It
orchestrates MLX, `mlx-audio`, `diffusers`, `mlx-vlm`, C+Metal binaries and the
rest behind adapters, each in its own isolated environment. What it adds is what
none of those runtimes do: knowing what the fleet actually occupies on disk,
refusing any job that would send the machine into swap, and making an output
produced three months ago reproducible. **Engines execute. Écurie admits.**

The why lives in [ARCHITECTURE.md](ARCHITECTURE.md), the how in
[CONCEPTION.md](CONCEPTION.md), the current state in [PLAN.md](PLAN.md) — all
three still in French, [and migrating](CONTRIBUTING.md). This file covers the
first fifteen minutes. Version française : [README.fr.md](README.fr.md).

## Status — pre-1.0, pivot in progress (2026-08-29)

Écurie is mid-pivot from a personal fleet manager to the product described
above. Read this table before believing anything else on this page:

| | Today | Lands with v1.0 |
|---|---|---|
| Install | `git clone` + `uv sync` | `uv tool install ecurie` (PyPI) |
| Surfaces | CLI (16 commands), local HTTP API, personal UI | **MCP server** (`ecurie mcp`) |
| Admission | measured profiles, on the machine that measured them | + machine classes, borrowed profiles — labeled, never silent |
| Language | CLI output and internal docs in French | English surface |
| License | none committed yet — all rights reserved | Apache-2.0 |

Nothing below is aspirational except where marked **v1.0** or *planned*.

## The problem

Your agent needs to transcribe an interview, describe a screenshot, generate a
cover image, isolate a voice from a recording. Today that means either the
cloud — cost, privacy, upload latency — or one server per modality on separate
ports with **no memory coordination between them**. One 8 GB model load too
many, while your IDE, your browser and a local LLM already sit in unified
memory, and macOS swaps: 25 tok/s becomes 2, the machine is gone for thirty
seconds. And nothing lets the agent *discover* what is installed.

Écurie is the answer for one machine and one agent at a time: a single local
endpoint that serves the non-text capabilities, knows the real memory cost of
every variant because it measured it, and refuses — legibly — what would not fit.

## What the v1.0 catalog serves

Twelve capabilities, every one with a measured incumbent, exposed as MCP tools
generated from their typed contracts (`registry/capabilities/*.json`):

- **Hearing** — `speech-to-text`, `speaker-diarization`, `audio-separation`
- **Voice** — `text-to-speech`
- **Sight** — `image-to-text` (describe + OCR), `depth-estimation`,
  `image-segment`, `image-matting`
- **Making** — `text-to-image`, `image-to-image`, `image-upscale`
- **Forecasting** — `time-series-forecast`

Plus three meta-tools, always present: `ecurie_catalog` (discover all 41
contracts and installed models), `ecurie_run` (invoke any capability by its
contract — the escape hatch), `ecurie_status` (residents, Metal budget, disk
accounting — read-only).

Two deliberate exclusions. The `face-*` capabilities stay out of the default
catalog: their contracts carry `human_subject`, and that flag is enforced, not
decorative. The other 29 contracts in the registry are **experimental** —
discoverable, runnable, but with no maintenance promise and no guaranteed
profile. The catalog is small on purpose: the measured cost of an agent is its
tool inventory, not its conversation, and forty declared tools is where local
brains start to degrade.

## Admission: measured, and negotiable

The rule that everything else serves: **a profile is filled by the bench, never
by hand. An estimated profile is a wrong profile.** Every variant in the
registry carries a measured peak (`peak_unified_memory_bytes`), and the
admission check is a pure function over those measurements and the Metal budget.

When a job doesn't fit, the refusal is not an error string — it is a decision
with its numbers and its exits. Eviction of idle residents is automatic (LRU),
so a refusal only happens when nothing is left to evict — and that is exactly
what it explains. The CLI already answers every refusal with the command that
repairs it; the MCP server (**v1.0**) makes that machine-readable:

```
refused:   measured peak 13.3 GiB exceeds 8.7 GiB free of the 17.76 GiB Metal budget
residents: qwen3-tts-1.7b @8bit-mlx (3.1 GiB, pinned) · moss-transcribe (6.0 GiB, busy)
options:   retry when the running transcription ends (frees 6.0 GiB)
           use variant @8bit-mps (peak 8.3 GiB — fits now)
           relay to your human: `ecurie unload qwen3-tts-1.7b` (pinned by them)
```

Admission is invisible when it passes, legible and negotiable when it refuses.

## Disk accounting: an instrument of knowledge

`ecurie store scan` walks the Hugging Face cache, Ollama blobs, LM Studio
models and any volume you point it at, hashes content, and reports **three
numbers no single tool sees together**: apparent occupation, real deduplicated
occupation, reclaimable space. It distinguishes an announced hash from a
verified one, never plans a deletion on an announced hash, re-verifies
(size, mtime, inode) + sha256 at the moment it acts, and quarantines instead of
`rm`. Don't expect miracle gigabytes — expect to finally know where every
gigabyte is and which variant, if any, it belongs to.

## What the repository contains, and what it doesn't

The distinction everything else follows from:

| | Where | Versioned |
|---|---|---|
| **Declared** — manifests, capability contracts, golden sets, measured profiles | `registry/` | yes |
| **Observed** — scanned artifacts, hash cache, telemetry | `~/.ecurie/state.db` | no |
| **Machine** — scanned paths, memory budget, residency policy | `~/.ecurie/config.toml` | no |
| **Weights** — the gigabytes | HF cache, Ollama, LM Studio, external volumes | no |
| **Environments** — the runtimes' contract | `runtimes/*/pyproject.toml` + `uv.lock` | yes; the `.venv` no |

In other words: **the repository describes a fleet, it does not contain one.**
That is what lets several people, on several Macs, share the same registry
without stepping on each other.

## Install today

Until v1.0, installation is from source. Prerequisites: macOS on Apple Silicon,
[`uv`](https://docs.astral.sh/uv/). CLI output is currently in French.

```bash
git clone <repo> && cd ecurie
uv sync                    # the core: core, store, runtime, api
uv run ecurie env sync     # the runtimes' isolated venvs, from their uv.lock files
uv run ecurie store scan   # populate ~/.ecurie/state.db with what is already on disk
uv run ecurie store status # the three numbers: apparent, real, reclaimable
```

`~/.ecurie/config.toml` is **generated on first run**, auto-detecting
`~/.cache/huggingface/hub`, `~/.ollama/models` and `~/.lmstudio/models`. There
is nothing to fill in; a path absent at scan time is ignored, not an error.

A few environments require one extra step, and say so — `ecurie env list`
flags them in its « À lire » column: `runtimes/h3-metal` builds a C+Metal
binary and needs FFmpeg on the PATH; `runtimes/cad-recode` extracts
non-commercial upstream code via a versioned script; anything under
`runtimes/*/vendor/` is unversioned and rebuilt per that runtime's README.
These all serve experimental capabilities — none is needed for the quickstart.

Then download and run:

```bash
uv run ecurie pull qwen3-tts-1.7b@8bit-mlx        # at the manifest's pinned revision
uv run ecurie run qwen3-tts-1.7b@8bit-mlx -p text="hello from my Mac"
uv run ecurie ps                                   # residents, budget, next job's cost
uv run ecurie serve                                # the API, on 127.0.0.1:8765
```

The MCP server and `claude mcp add ecurie -- ecurie mcp` land with **v1.0**.

## What adapts to your Mac by itself

**The memory budget.** On Apple Silicon the number that decides a swap is not
installed memory but Metal's `recommendedMaxWorkingSetSize` — about 75 % of
unified memory. Écurie reads it from a runtime environment's `mlx`, falling
back to a rule of three, then to a prudent default. **The number's provenance
is always displayed next to it**: a budget read from Metal and a deduced budget
do not deserve the same trust.

**The heaviness threshold.** One heavy model resident at a time, the light ones
stay warm — and "heavy" means 45 % of the detected budget: 8 GiB on the 24 GB
reference machine (budget 17.76 GiB), proportionally lower on a 16 GB Mac.

**What does not adapt, because it doesn't have to**: `peak_unified_memory_bytes`
and `disk_bytes` of a measured profile. Weights and activations don't depend on
which Mac loads them. That is why a manifest measured on someone else's machine
serves you as-is — and why **v1.0** ships profile classes rather than asking
you to re-bench the fleet.

## Several people, several Macs

Collaboration is by pull request — **nothing ever mutates live state**. Profiles
are measured per machine and the filename says so
(`registry/measurements/<ref>/<machine>.json`); what you didn't measure, you
don't commit; the manifest's `profile:` block must match at least one relevé on
the portable numbers, and `ecurie registry validate` checks it. Measurement PRs
are the contribution this project actually wants — see
[CONTRIBUTING.md](CONTRIBUTING.md).

And one boundary, known and kept: `ecurie serve` listens on `127.0.0.1` and
refuses a non-local address without `--expose`. The API says where weights live
on disk, what the machine holds in memory, and launches jobs. There is no
authentication and no per-user isolation — "one Mac serving several people" is
not a supported use, it is a different project.

## Developing

```bash
uv run pytest                 # the suite; tests marked `real` are excluded
uv run pytest -m real         # integration tests against the real fleet, not in CI
uv run ruff check packages/   # lint, 100-column lines
cd apps/ui && npm test        # the front-end suite (the UI is frozen, its tests are not)
```

Two guards worth knowing before you hit them: `tools/openapi_dump.py` freezes
the API schema and a test compares it — changing a route without regenerating
breaks the suite, by design. `tools/ui_fixtures.py` freezes what the server
computes over the real registry — editing `registry/` can fail an API test;
`npm run fixtures` regenerates.

## Roadmap

Four milestones to v1.0, no firm dates — one maintainer, and it says so in
[SUPPORT.md](SUPPORT.md):

- **J0 — Publishable.** Apache-2.0 LICENSE, minimal CI, the `ecurie` name on
  PyPI, repo cleanup.
- **J1 — MCP served.** `ecurie mcp` (stdio): 12 tools + 3 meta-tools generated
  from the contracts, structured admission refusals, file outputs.
- **J2 — Trustworthy profiles.** Hardened bench (output-shape validation,
  blind-profile guard), machine classes with borrowed profiles (+15 % margin,
  labeled, never silent), opportunistic bench on pull, `ecurie init` /
  `ecurie doctor`, Ollama and LM Studio residents read into the budget.
- **J3 — Launch.** The ten-minute quickstart replayed by outside testers on a
  second machine class, publication to the official MCP registry.

Planned after v1.0: OpenAI-compatible `/v1/audio/*` and
`/v1/images/generations`. **Chat completions will be delegated, never
reimplemented.** `/v1/messages` after that. Then the Library — replay any past
job from its manifest.

The exit criterion is published, not implied: if v1.0 attracts no external
signal within three months of launch, Écurie officially returns to being a
published personal tool. The full pre-commitment is in
[ARCHITECTURE.md](ARCHITECTURE.md).

## License

Écurie will ship under **Apache-2.0** — the LICENSE file lands with milestone
J0. Until it is committed, the default "all rights reserved" applies; treat the
repository as source-visible, not yet open source.
