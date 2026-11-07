"""Turn a written script into a finished narration, through the MCP server.

    uv run python tools/narrate.py script.md --out build/ --variant kokoro-82m@bf16 \
        --voice af_heart --lexicon lexicon.json

This is a client of `ecurie mcp`, not a part of it: the server stays a control
plane that runs one job at a time, and everything specific to narration lives here.

Each segment's audio is stored under a hash of what produced it, so a second run
only pays for what changed. Editing a paragraph resynthesizes that paragraph;
`--regenerate 7` retakes segment 7 with another seed, and the retake is remembered.

The lexicon maps one written word to what should be said, so `{"JSON": "Jason"}`
is heard as "Jason" and subtitled as "JSON". Subtitles therefore need a timing for
what was written while the aligner times what was said, which is what `Token` tracks.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RACINE = Path(__file__).resolve().parents[1]

MAX_SEGMENT_CHARS = 400
PAUSE_SEGMENT = 0.25
PAUSE_PARAGRAPH = 0.6
PAUSE_SECTION = 1.2
SAMPLE_RATE = 48_000
TRUE_PEAK_CEILING = -1.5
# Measuring a level is not exact to the hundredth, and a short file reads a little low.
TARGET_TOLERANCE_LU = 0.5
CUE_LINE_CHARS = 42
CUE_MAX_SECONDS = 6.0
CUE_MIN_SECONDS = 1.0
# Below this share of the segment's duration, the aligner has collapsed the words
# onto an instant: the failure the `audio-align` contract warns about.
MIN_ALIGNED_SHARE = 0.5


class NarrationError(Exception):
    """A failure that names what to do, printed without a traceback."""


@dataclass(frozen=True)
class Segment:
    text: str
    pause_after: float


@dataclass(frozen=True)
class Token:
    """One written word, and how many spoken words it became."""

    display: str
    spoken_words: int


@dataclass(frozen=True)
class Timed:
    display: str
    start: float
    end: float


# --- script ---------------------------------------------------------------------


def _strip_markdown(line: str) -> str:
    line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)
    line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)
    line = re.sub(r"`([^`]*)`", r"\1", line)
    line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)
    line = re.sub(r"(?<!\w)([*_])(.+?)\1(?!\w)", r"\2", line)
    return line.strip()


def _chunk_sentences(paragraph: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
        if current and len(current) + 1 + len(sentence) > limit:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def parse_script(source: str, limit: int = MAX_SEGMENT_CHARS) -> list[Segment]:
    """Split a Markdown or plain-text script into segments, with the pause after each.

    Headings structure the narration and are not spoken: they lengthen the pause
    before the section they open.
    """
    paragraphs: list[tuple[str, bool]] = []
    lines: list[str] = []
    section_next = False

    def flush() -> None:
        nonlocal section_next
        if lines:
            paragraphs.append((" ".join(lines), section_next))
            lines.clear()
            section_next = False

    for raw in source.splitlines():
        if not raw.strip():
            flush()
        elif raw.lstrip().startswith("#"):
            flush()
            section_next = True
        else:
            cleaned = _strip_markdown(raw)
            if cleaned:
                lines.append(cleaned)
    flush()

    segments: list[Segment] = []
    for index, (paragraph, _) in enumerate(paragraphs):
        chunks = _chunk_sentences(paragraph, limit)
        following_section = index + 1 < len(paragraphs) and paragraphs[index + 1][1]
        for position, chunk in enumerate(chunks):
            if position + 1 < len(chunks):
                pause = PAUSE_SEGMENT
            elif index + 1 == len(paragraphs):
                pause = 0.0
            else:
                pause = PAUSE_SECTION if following_section else PAUSE_PARAGRAPH
            segments.append(Segment(chunk, pause))
    return segments


# --- lexicon --------------------------------------------------------------------


def load_lexicon(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    lexicon = json.loads(path.read_text(encoding="utf-8"))
    for written in lexicon:
        if len(written.split()) != 1:
            raise NarrationError(f"lexicon keys must be one word, got {written!r}")
    return lexicon


def apply_lexicon(text: str, lexicon: dict[str, str]) -> tuple[str, list[Token]]:
    """What to say for `text`, and how each written word maps onto the spoken ones."""
    spoken: list[str] = []
    tokens: list[Token] = []
    for word in text.split():
        match = re.fullmatch(r"(\W*)(.*?)(\W*)", word)
        lead, core, trail = match.groups() if match else ("", word, "")
        replacement = lexicon.get(core)
        said = f"{lead}{replacement}{trail}" if replacement else word
        spoken.append(said)
        tokens.append(Token(word, len(said.split())))
    return " ".join(spoken), tokens


# --- subtitles ------------------------------------------------------------------


def timed_tokens(
    tokens: list[Token], units: list[dict[str, Any]] | None, duration: float, offset: float
) -> list[Timed]:
    """Place written words on the timeline, from aligned units or in proportion."""
    timed: list[Timed] = []
    total = sum(t.spoken_words for t in tokens)
    position = 0
    for token in tokens:
        last = position + token.spoken_words
        if units is not None:
            start = float(units[position]["start"])
            end = float(units[last - 1]["end"])
        else:
            start = duration * position / total
            end = duration * last / total
        timed.append(Timed(token.display, offset + start, offset + end))
        position = last
    return timed


def alignment_is_usable(
    tokens: list[Token], units: list[dict[str, Any]] | None, span: float, duration: float
) -> bool:
    """Reject an alignment that does not match the words, or collapsed onto an instant."""
    if not units or len(units) != sum(t.spoken_words for t in tokens):
        return False
    return duration <= 0 or span / duration >= MIN_ALIGNED_SHARE


def _fits_two_lines(text: str) -> bool:
    return all(len(line) <= CUE_LINE_CHARS for line in _wrap(text).split("\n"))


def build_cues(words: list[Timed]) -> list[Timed]:
    """Group words into cues that read in one go, closing on the end of a sentence."""
    cues: list[Timed] = []
    current: list[Timed] = []

    def close(count: int | None = None) -> None:
        """Emit the first `count` words as a cue, keeping the rest for the next one."""
        taken = current[:count] if count else list(current)
        if taken:
            text = " ".join(w.display for w in taken)
            cues.append(Timed(text, taken[0].start, taken[-1].end))
            del current[: len(taken)]

    for word in words:
        if current:
            candidate = " ".join(w.display for w in [*current, word])
            too_long = word.end - current[0].start > CUE_MAX_SECONDS
            if too_long or not _fits_two_lines(candidate):
                # A clause boundary reads better than wherever the room ran out.
                clause = [i for i, w in enumerate(current) if w.display.endswith((",", ";", ":"))]
                close(clause[-1] + 1 if clause else None)
        current.append(word)
        if word.display.rstrip("\"')").endswith((".", "!", "?")):
            close()
    close()
    return _merge_orphans(cues)


def _merge_orphans(cues: list[Timed]) -> list[Timed]:
    """Fold a cue too brief to read into the previous one, when they are contiguous."""
    merged: list[Timed] = []
    for cue in cues:
        if merged and cue.end - cue.start < CUE_MIN_SECONDS:
            previous = merged[-1]
            text = f"{previous.display} {cue.display}"
            contiguous = not previous.display.endswith((".", "!", "?"))
            if contiguous and _fits_two_lines(text) and cue.end - previous.start <= CUE_MAX_SECONDS:
                merged[-1] = Timed(text, previous.start, cue.end)
                continue
        merged.append(cue)
    return merged


def _wrap(text: str) -> str:
    if len(text) <= CUE_LINE_CHARS:
        return text
    words = text.split()
    best = min(
        range(1, len(words)),
        key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))),
    )
    return " ".join(words[:best]) + "\n" + " ".join(words[best:])


def _timestamp(seconds: float, separator: str) -> str:
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{millis:03}"


def to_srt(cues: list[Timed]) -> str:
    return "\n".join(
        f"{i}\n{_timestamp(c.start, ',')} --> {_timestamp(c.end, ',')}\n{_wrap(c.display)}\n"
        for i, c in enumerate(cues, 1)
    )


def to_vtt(cues: list[Timed]) -> str:
    body = "\n".join(
        f"{_timestamp(c.start, '.')} --> {_timestamp(c.end, '.')}\n{_wrap(c.display)}\n"
        for c in cues
    )
    return f"WEBVTT\n\n{body}"


# --- state ----------------------------------------------------------------------


def segment_key(spoken: str, variant: str, voice: str | None, speed: float, seed: int) -> str:
    material = json.dumps([spoken, variant, voice, speed, seed], ensure_ascii=False)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def text_key(spoken: str) -> str:
    return hashlib.sha256(spoken.encode()).hexdigest()[:16]


# --- audio ----------------------------------------------------------------------


def _ffmpeg(*args: str) -> str:
    done = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", *args], capture_output=True, text=True
    )
    if done.returncode != 0:
        raise NarrationError(f"ffmpeg failed:\n{done.stderr[-1500:]}")
    return done.stderr


def duration_of(path: Path) -> float:
    done = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        ],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return float(done.stdout.strip())


def _loudness(path: Path, target: float) -> dict[str, Any]:
    """Measure a file. `loudnorm` only analyzes here: its output goes nowhere."""
    log = _ffmpeg(
        "-i", str(path),
        "-af", f"loudnorm=I={target}:TP={TRUE_PEAK_CEILING}:LRA=11:print_format=json",
        "-f", "null", "-",
    )  # fmt: skip
    return json.loads(log[log.rindex("{") : log.rindex("}") + 1])


def linear_gain(loudness: float, true_peak: float, target: float) -> float:
    """The one gain, in dB, that reaches the target without passing the peak ceiling.

    `loudnorm` must not apply it: asked for `linear=true`, it silently falls back to
    its dynamic mode when the ceiling leaves too little room, and voice is heard to
    jump. A narration then reaches its target with a gain varying from 1.3x to 5x
    within the file. Here the voice comes out quieter than asked, and the manifest
    says so.
    """
    return min(target - loudness, TRUE_PEAK_CEILING - true_peak)


def assemble(parts: list[tuple[Path, float]], out: Path, target: float) -> dict[str, Any]:
    """Join the segments with their pauses, apply a single gain, export."""
    inputs: list[str] = []
    filters: list[str] = []
    for index, (path, pause) in enumerate(parts):
        inputs += ["-i", str(path)]
        filters.append(
            f"[{index}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=mono,"
            f"apad=pad_dur={pause}[a{index}]"
        )
    labels = "".join(f"[a{i}]" for i in range(len(parts)))
    graph = ";".join([*filters, f"{labels}concat=n={len(parts)}:v=0:a=1[out]"])
    raw = out / "narration.raw.wav"
    _ffmpeg("-y", *inputs, "-filter_complex", graph, "-map", "[out]", str(raw))

    source = _loudness(raw, target)
    gain = linear_gain(float(source["input_i"]), float(source["input_tp"]), target)
    wav = out / "narration.wav"
    _ffmpeg(
        "-y", "-i", str(raw),
        "-af", f"volume={gain:.3f}dB",
        "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(wav),
    )  # fmt: skip
    _ffmpeg("-y", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "192k", str(out / "narration.mp3"))
    raw.unlink()
    final = _loudness(wav, target)
    return {
        "mode": "linear",
        "target_lufs": target,
        "measured_lufs": float(final["input_i"]),
        "true_peak_db": float(final["input_tp"]),
        "gain_db": round(gain, 3),
        "source_lufs": float(source["input_i"]),
        "source_true_peak_db": float(source["input_tp"]),
    }


# --- MCP ------------------------------------------------------------------------


async def _run_job(client: Any, capability: str, variant: str | None, data: dict) -> dict[str, Any]:
    arguments: dict[str, Any] = {"capability": capability, "input": data}
    if variant:
        arguments["variant"] = variant
    result = await client.call_tool("ecurie_run", arguments)
    envelope = result.structured_content or {}
    if result.is_error or not envelope.get("ok"):
        raise NarrationError(f"{capability} failed: {json.dumps(envelope, ensure_ascii=False)}")
    return envelope


def _produced(envelope: dict[str, Any], key: str) -> Path:
    path = envelope.get("files", {}).get(key)
    if not path:
        raise NarrationError(f"no file for output {key!r}: {envelope.get('files')}")
    return Path(path)


async def narrate(options: argparse.Namespace) -> dict[str, Any]:
    from mcp import StdioServerParameters
    from mcp.client import Client

    out: Path = options.out
    audio_dir = out / "segments"
    audio_dir.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"retakes": {}}

    source = options.script.read_text(encoding="utf-8")
    segments = parse_script(source)
    if not segments:
        raise NarrationError(f"nothing to say in {options.script}")
    lexicon = load_lexicon(options.lexicon)

    spoken_tokens = [apply_lexicon(s.text, lexicon) for s in segments]
    for number in options.regenerate:
        if not 1 <= number <= len(segments):
            raise NarrationError(f"--regenerate {number}: the script has {len(segments)} segments")
        key = text_key(spoken_tokens[number - 1][0])
        state["retakes"][key] = state["retakes"].get(key, 0) + 1

    server = StdioServerParameters(
        command=sys.executable, args=["-m", "ecurie_mcp"], cwd=str(RACINE)
    )
    rows: list[dict[str, Any]] = []
    synthesized = reused = 0
    warnings: list[str] = []
    async with Client(server) as client:
        for number, (spoken, _) in enumerate(spoken_tokens, 1):
            retake = state["retakes"].get(text_key(spoken), 0)
            seed = options.seed + retake
            key = segment_key(spoken, options.variant, options.voice, options.speed, seed)
            wav = audio_dir / f"{key}.wav"
            if wav.exists():
                reused += 1
            else:
                data: dict[str, Any] = {"text": spoken, "speed": options.speed, "seed": seed}
                if options.voice:
                    data["voice"] = options.voice
                envelope = await _run_job(client, "text-to-speech", options.variant, data)
                shutil.copyfile(_produced(envelope, "audio"), wav)
                synthesized += 1
                warnings += [f"segment {number}: {w}" for w in envelope.get("warnings") or []]
            rows.append({"segment": number, "key": key, "seed": seed, "retake": retake})
            print(f"  speech {number}/{len(segments)}", file=sys.stderr, flush=True)

        words: list[Timed] = []
        offset = 0.0
        for row, (spoken, tokens), segment in zip(rows, spoken_tokens, segments, strict=True):
            wav = audio_dir / f"{row['key']}.wav"
            duration = duration_of(wav)
            aligned = audio_dir / f"{row['key']}.align.json"
            if aligned.exists():
                cached = json.loads(aligned.read_text())
                units, span = cached["units"], cached["span"]
            else:
                envelope = await _run_job(
                    client, "audio-align", None,
                    {"audio": str(wav.resolve()), "text": spoken, "language": options.language},
                )  # fmt: skip
                units = json.loads(_produced(envelope, "words").read_text(encoding="utf-8"))
                span = float(envelope["output"]["span_seconds"])
                aligned.write_text(json.dumps({"units": units, "span": span}))
            usable = alignment_is_usable(tokens, units, span, duration)
            if not usable:
                warnings.append(
                    f"segment {row['segment']}: alignment unusable "
                    f"({len(units or [])} units, span {span:.2f}s of {duration:.2f}s), "
                    "subtitles placed in proportion"
                )
            words += timed_tokens(tokens, units if usable else None, duration, offset)
            row.update(
                text=segment.text,
                spoken=spoken,
                start=round(offset, 3),
                duration=round(duration, 3),
                pause_after=segment.pause_after,
                aligned=usable,
            )
            offset += duration + segment.pause_after

    state_path.write_text(json.dumps(state, indent=2) + "\n")

    parts = [(audio_dir / f"{r['key']}.wav", r["pause_after"]) for r in rows]
    loudness = assemble(parts, out, options.lufs)
    if loudness["measured_lufs"] < options.lufs - TARGET_TOLERANCE_LU:
        warnings.append(
            f"loudness {loudness['measured_lufs']} LUFS, below the {options.lufs} LUFS target: "
            f"the {TRUE_PEAK_CEILING} dBTP true-peak ceiling caps the gain at "
            f"{loudness['gain_db']} dB, and no limiter is applied"
        )
    cues = build_cues(words)
    (out / "narration.srt").write_text(to_srt(cues), encoding="utf-8")
    (out / "narration.vtt").write_text(to_vtt(cues), encoding="utf-8")

    manifest = {
        "script": str(options.script),
        "script_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "variant": options.variant,
        "voice": options.voice,
        "speed": options.speed,
        "seed": options.seed,
        "lexicon": lexicon,
        "duration_seconds": round(duration_of(out / "narration.wav"), 3),
        "segments_synthesized": synthesized,
        "segments_reused": reused,
        "loudness": loudness,
        "warnings": warnings,
        "segments": rows,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Turn a script into a narration through MCP.")
    parser.add_argument("script", type=Path, help="Markdown or plain-text script")
    parser.add_argument(
        "--out", type=Path, required=True, help="project directory, kept for reruns"
    )  # noqa: E501
    parser.add_argument("--variant", required=True, help="text-to-speech model@variant")
    parser.add_argument("--voice", help="voice announced by the variant")
    parser.add_argument("--lexicon", type=Path, help='JSON {"written word": "what to say"}')
    parser.add_argument("--language", default="english", help="aligner language name")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--lufs", type=float, default=-16.0, help="target integrated loudness")
    parser.add_argument(
        "--regenerate", type=int, action="append", default=[], metavar="N",
        help="retake segment N with another seed (repeatable)",
    )  # fmt: skip
    return parser


def _leaves(group: BaseExceptionGroup) -> list[BaseException]:
    found: list[BaseException] = []
    for error in group.exceptions:
        found += _leaves(error) if isinstance(error, BaseExceptionGroup) else [error]
    return found


def main() -> int:
    options = _parser().parse_args()
    manifest = None
    # The MCP client runs in a task group, which wraps what is raised inside it.
    try:
        manifest = asyncio.run(narrate(options))
    except* NarrationError as group:
        for error in _leaves(group):
            print(f"error: {error}", file=sys.stderr)
    if manifest is None:
        return 1
    print(
        f"{options.out / 'narration.wav'}: {manifest['duration_seconds']}s, "
        f"{manifest['segments_synthesized']} synthesized, "
        f"{manifest['segments_reused']} reused, "
        f"{manifest['loudness']['measured_lufs']} LUFS"
    )
    for warning in manifest["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
