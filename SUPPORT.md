# Support

Écurie is maintained by **one person**. There is no SLA, no support contract,
and no guaranteed response time. Issues are read; that is the promise.

## Where to ask

Open a GitHub issue on this repository — English or French, both are read.
Before opening one, check that `ecurie env list` and `ecurie store status` run
clean, and include their output plus your machine class (chip + RAM) when
relevant.

`ecurie serve` binds to `127.0.0.1` by design and has no authentication. If you
believe you have found a security issue, email **julien.riel@gmail.com** — the
address in every commit and in the packages' metadata. Don't look for GitHub's
private vulnerability reporting: it is not enabled on this repository (checked
2026-08-29, `repos/julien-riel/ecurie/private-vulnerability-reporting` returns
`{"enabled": false}`), and enabling it is a maintainer decision that has not
been taken. Don't put an exploit in a public issue either. One maintainer means
no response-time promise here either — but the mailbox is read.

## What is supported

The **twelve catalog capabilities**, on Apple Silicon Macs (reference machine:
24 GB; admission adapts the budget to yours):

`text-to-speech`, `speech-to-text`, `speaker-diarization`, `audio-separation`,
`text-to-image`, `image-to-image`, `image-to-text`, `image-upscale`,
`image-matting`, `image-segment`, `depth-estimation`, `time-series-forecast`.

Supported means: a variant whose memory profile was measured at the bench, a
maintained runtime, and a bench workload in `registry/evals/bench/` that gets
attention when it goes red. Bugs there are taken seriously.

One known limit, stated rather than discovered: the bench today checks that an
output file exists and matches the contract's shape, not that its contents are
right — `moss-transcribe` once passed green while emitting speaker-tagged text
where the contract declares plain text. Output validation against the contract
schema lands with **J2**.

## What is experimental

Everything else — the other 29 capability contracts in the registry and their
runtimes. Experimental means: discoverable and runnable, **no maintenance
promise and no guarantee the profile is current** — and fifteen of the hundred
variants carry no measured profile at all, so admission
refuses them outright until someone benches one. A bench workload exists for
forty of the forty-one capabilities (`audio-denoise` has none); what the twelve
get is attention when theirs goes red. Issues are welcome as information; fixes
are not promised. The `face-*` capabilities will additionally be excluded from
the default MCP catalog (**v1.0**) because their contracts carry
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
