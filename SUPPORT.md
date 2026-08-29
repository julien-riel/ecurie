# Support

Écurie is maintained by **one person**. There is no SLA, no support contract,
and no guaranteed response time. Issues are read; that is the promise.

## Where to ask

Open a GitHub issue on this repository — English or French, both are read.
Before opening one, check that `ecurie env list` and `ecurie store status` run
clean, and include their output plus your machine class (chip + RAM) when
relevant.

`ecurie serve` binds to `127.0.0.1` by design and has no authentication. If you
believe you have found a security issue, say so in the issue title and do not
post an exploit; details can be exchanged privately from there.

## What is supported

The **twelve catalog capabilities**, on Apple Silicon Macs (reference machine:
24 GB; admission adapts the budget to yours):

`text-to-speech`, `speech-to-text`, `speaker-diarization`, `audio-separation`,
`text-to-image`, `image-to-image`, `image-to-text`, `image-upscale`,
`image-matting`, `image-segment`, `depth-estimation`, `time-series-forecast`.

Supported means: a measured incumbent, a maintained runtime, and bench coverage.
Bugs there are taken seriously.

## What is experimental

Everything else — the other 29 capability contracts in the registry and their
runtimes. Experimental means: discoverable and runnable, **no maintenance
promise, no guaranteed profile, no bench coverage**. Issues are welcome as
information; fixes are not promised. The `face-*` capabilities are additionally
excluded from the default MCP catalog because their contracts carry
`human_subject`.

Linux, CUDA, multi-user serving and enterprise deployment are out of scope —
not backlog, out of scope.

## The published exit criterion

This project pre-commits its own fallback rather than fading out silently: if
v1.0 attracts no external signal within three months of launch (no external
issue, no active fork, no third-party measurement PR), Écurie officially
returns to being a published personal tool — maintained for its author, with no
product promise. The full criterion lives in [ARCHITECTURE.md](ARCHITECTURE.md).
An honest downgrade beats an abandoned repository.
