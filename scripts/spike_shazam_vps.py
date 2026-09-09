"""S3 spike (plan §5 Phase S) — OWNER-RUN, LIVE: how much unofficial-Shazam traffic does one
egress IP sustain, and where does it start throttling?

    uv run python scripts/spike_shazam_vps.py --minutes 60 --ceiling 45

Sends real recognitions at a fixed admission rate (``--ceiling`` requests per minute) for
``--minutes`` minutes over synthetic three-tone clips cut from the deterministic fixture generator.
The signature is real; the audio matches nothing, which is fine — only the transport verdict
matters (200, 429, 5xx, timeout).  Appends one dated section to ``docs/spikes/shazam-vps.md`` with
per-minute counts, the first throttle minute and the sustained clean rate, so five daily runs can
set ``shazam_daily_budget_per_egress`` (plan §2.3.5 breaker rule (b)).

The report holds numbers only — no URLs, no handles.  Refuses to run under ``IDEA_TEST_MODE=1`` or
inside pytest; never run in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import statistics
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

CLIP_SECONDS = 12
HOP_SECONDS = 9
_LABEL = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def _refuse_in_test_context() -> None:
    if os.environ.get("IDEA_TEST_MODE") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
        print("refusing to run a live Shazam spike in a test context", file=sys.stderr)
        raise SystemExit(2)


def _label(value: str) -> str:
    if not _LABEL.fullmatch(value):
        raise argparse.ArgumentTypeError("label must be 1-40 characters of [A-Za-z0-9._-]")
    return value


def _cut_clips(source: Path, out_dir: Path) -> list[Path]:
    """Cut ``source`` into 12 s clips at a 9 s hop — the frozen generation-0 schedule."""

    with wave.open(str(source), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        frames = handle.readframes(handle.getnframes())
    clip_frames = CLIP_SECONDS * rate
    hop_frames = HOP_SECONDS * rate
    frame_bytes = channels * width
    total = len(frames) // frame_bytes
    clips: list[Path] = []
    for index, start in enumerate(range(0, total - clip_frames + 1, hop_frames)):
        path = out_dir / f"clip-{index:04d}.wav"
        with wave.open(str(path), "wb") as clip:
            clip.setnchannels(channels)
            clip.setsampwidth(width)
            clip.setframerate(rate)
            clip.writeframes(frames[start * frame_bytes : (start + clip_frames) * frame_bytes])
        clips.append(path)
    if not clips:
        raise SystemExit("the source audio is shorter than one clip")
    return clips


@dataclass(frozen=True)
class Attempt:
    at_s: float
    outcome: str
    latency_s: float
    retry_after: float | None


async def _run(clips: list[Path], *, minutes: int, ceiling: int, adaptive: bool) -> list[Attempt]:
    from shazamio import Shazam

    from id_detector.shazam import (
        CircuitBreaker,
        InjectedHTTPClient,
        ShazamHTTPError,
        TokenBucket,
    )

    # A fixed floor equal to the ceiling holds the rate steady so the measurement is of Shazam,
    # not of our own back-off; --adaptive measures what the production limiter would settle at.
    limiter = TokenBucket(
        rate_per_minute=ceiling,
        capacity=1,
        min_rate_per_minute=None if adaptive else ceiling,
    )
    breaker = CircuitBreaker(failure_threshold=10**9)  # never opens: every failure is data

    async def _noop() -> None:
        return None

    client = InjectedHTTPClient(on_attempt=_noop, limiter=limiter, breaker=breaker)
    shazam = Shazam(http_client=client, segment_duration_seconds=CLIP_SECONDS)
    attempts: list[Attempt] = []
    started = time.monotonic()
    index = 0
    while time.monotonic() - started < minutes * 60:
        clip = clips[index % len(clips)]
        index += 1
        sent_at = time.monotonic()
        retry_after: float | None = None
        try:
            result = await shazam.recognize(str(clip))
            matched = isinstance(result, dict) and result.get("matches") and result.get("track")
            outcome = "match" if matched else "no_match"
        except ShazamHTTPError as exc:
            outcome = "transport" if exc.status_code == 0 else f"http_{exc.status_code}"
            retry_after = exc.retry_after
        except Exception as exc:  # noqa: BLE001 - a spike records failures, it never crashes
            outcome = f"error:{type(exc).__name__}"
        attempt = Attempt(
            at_s=sent_at - started,
            outcome=outcome,
            latency_s=time.monotonic() - sent_at,
            retry_after=retry_after,
        )
        attempts.append(attempt)
        print(f"{attempt.at_s:8.1f}s  {outcome:<14} {attempt.latency_s:5.2f}s", flush=True)
    return attempts


def _ok(attempt: Attempt) -> bool:
    return attempt.outcome in {"match", "no_match"}


def _minute_rows(attempts: list[Attempt], minutes: int) -> list[dict[str, float | int]]:
    rows: list[dict[str, float | int]] = []
    for minute in range(minutes):
        bucket = [a for a in attempts if minute * 60 <= a.at_s < (minute + 1) * 60]
        latencies = [a.latency_s for a in bucket if _ok(a)]
        rows.append(
            {
                "minute": minute + 1,
                "sent": len(bucket),
                "ok": sum(_ok(a) for a in bucket),
                "http_429": sum(a.outcome == "http_429" for a in bucket),
                "http_5xx": sum(a.outcome.startswith("http_5") for a in bucket),
                "transport": sum(a.outcome == "transport" for a in bucket),
                "other": sum(
                    not _ok(a)
                    and a.outcome not in {"http_429", "transport"}
                    and not a.outcome.startswith("http_5")
                    for a in bucket
                ),
                "p50_latency_s": round(statistics.median(latencies), 2) if latencies else 0.0,
            }
        )
    return rows


def _render(attempts: list[Attempt], *, label: str, ceiling: int, minutes: int) -> str:
    rows = _minute_rows(attempts, minutes)
    total = len(attempts)
    ok = sum(_ok(a) for a in attempts)
    throttled = sum(a.outcome == "http_429" for a in attempts)
    first_throttle = next((row["minute"] for row in rows if row["http_429"]), None)
    tail = rows[-10:] if rows else []
    sustained = (sum(int(row["ok"]) for row in tail) / len(tail)) if tail else 0.0
    retry_after = [a.retry_after for a in attempts if a.retry_after]
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    lines = [
        "",
        f"## {stamp} — egress {label} — ceiling {ceiling}/min × {minutes} min",
        "",
        f"- sent {total}; ok {ok} ({(100 * ok // total) if total else 0} %); "
        f"429 {throttled}; 5xx {sum(a.outcome.startswith('http_5') for a in attempts)}; "
        f"transport {sum(a.outcome == 'transport' for a in attempts)}",
        f"- first 429 at minute: {first_throttle if first_throttle is not None else 'none'}",
        f"- sustained clean rate over the last {len(tail)} min: {sustained:.1f}/min "
        f"(≈ {round(sustained * 1440)} clean requests/day at that rate)",
        f"- Retry-After seen: {statistics.median(retry_after):.0f} s median"
        if retry_after
        else "- Retry-After seen: none",
        "",
        "| minute | sent | ok | 429 | 5xx | transport | other | p50 latency s |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['minute']} | {row['sent']} | {row['ok']} | {row['http_429']} | "
            f"{row['http_5xx']} | {row['transport']} | {row['other']} | {row['p50_latency_s']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    _refuse_in_test_context()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=int, default=60, help="how long to send (default 60)")
    parser.add_argument(
        "--ceiling", type=int, default=45, help="admission rate, requests/min (default 45)"
    )
    parser.add_argument(
        "--label", type=_label, default="unlabelled", help="egress label for the report"
    )
    parser.add_argument(
        "--source-seconds",
        type=int,
        default=600,
        help="length of the synthetic tone source the clips are cut from (default 600)",
    )
    parser.add_argument(
        "--adaptive",
        action="store_true",
        help="let the production limiter back off on 429 instead of holding the ceiling",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "docs" / "spikes" / "shazam-vps.md",
        help="Markdown report to append to",
    )
    args = parser.parse_args()
    if args.minutes <= 0 or args.ceiling <= 0 or args.source_seconds < CLIP_SECONDS:
        parser.error("--minutes and --ceiling must be positive; --source-seconds >= 12")

    from make_audio_fixtures import generate

    print(
        f"LIVE: sending up to {args.ceiling}/min to Shazam for {args.minutes} min from this "
        "egress; Ctrl-C aborts without writing the report",
        flush=True,
    )
    with tempfile.TemporaryDirectory(prefix="idea-spike-shazam-") as tmp:
        source = Path(tmp) / "tone.wav"
        generate(source, args.source_seconds)
        clips = _cut_clips(source, Path(tmp))
        print(f"{len(clips)} distinct clips ready", flush=True)
        attempts = asyncio.run(
            _run(clips, minutes=args.minutes, ceiling=args.ceiling, adaptive=args.adaptive)
        )
    report = _render(attempts, label=args.label, ceiling=args.ceiling, minutes=args.minutes)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", encoding="utf-8") as handle:
        handle.write(report)
    print(report)
    print(f"appended to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
