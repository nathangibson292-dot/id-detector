"""Confidence-gated targeting for the paid file-scanner: scan only the *uncertain* minutes.

The free (Shazam) pass runs first and fuses.  Wherever it is already confident — a ``likely`` or
``verified`` badge, or a hint-corroborated track — we trust it and do NOT pay to rescan it.  The
remaining spans (blank, ``possible``, ``unclear``) are the only audio worth sending to a paid engine
that bills per second.  This module turns the fused episodes into those target spans, slices the mix
down to just them, and remaps a scanned position in the sliced audio back to the original timeline.

Everything here is key-independent and unit-tested; only the live AudD/ACRCloud round-trip on a real
sliced file needs a credential to verify end to end.
"""

from __future__ import annotations

from pathlib import Path

from id_detector.contracts import EpisodeRecord
from id_detector.process import run_process

#: Badges we trust enough to skip paying to rescan.
CONFIDENT_BADGES = frozenset({"likely", "verified"})
#: Flags that mark a track as corroborated (also trusted): a text/crowd hint, or a second engine.
CONFIDENT_FLAGS = frozenset({"hint_supported", "hint_only", "engine_corroborated"})

Span = tuple[int, int]


def _is_confident(episode: EpisodeRecord) -> bool:
    if episode.badge in CONFIDENT_BADGES:
        return True
    return any(flag in CONFIDENT_FLAGS for flag in episode.flags)


def _merge(spans: list[Span]) -> list[Span]:
    """Sort and union overlapping/touching spans."""

    if not spans:
        return []
    ordered = sorted(spans)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def confident_coverage(episodes: list[EpisodeRecord] | tuple[EpisodeRecord, ...]) -> list[Span]:
    """The merged spans the free pass already covers confidently (never worth paying to rescan)."""

    spans = [
        (episode.best_start_ms, max(episode.best_start_ms, episode.best_end_ms))
        for episode in episodes
        if _is_confident(episode) and not episode.suppressed
    ]
    return _merge(spans)


def select_scan_targets(
    episodes: list[EpisodeRecord] | tuple[EpisodeRecord, ...],
    duration_ms: int,
    *,
    min_target_ms: int = 8_000,
    bridge_ms: int = 4_000,
    pad_ms: int = 3_000,
) -> tuple[Span, ...]:
    """The uncertain spans a paid engine should scan: everything not confidently covered.

    ``bridge_ms`` fuses uncertain spans separated by only a sliver of confident audio (so a brief
    confident hit inside a long unknown stretch does not fragment the scan).  ``pad_ms`` widens each
    target so a track's edges are not clipped.  ``min_target_ms`` drops targets too short to hold a
    recognisable ~12 s chunk.  Returns () when the whole mix is already confident (skip paid).
    """

    if duration_ms <= 0:
        return ()
    confident = confident_coverage(episodes)
    # Complement of the confident cover within [0, duration_ms].
    uncertain: list[Span] = []
    cursor = 0
    for start, end in confident:
        if start > cursor:
            uncertain.append((cursor, min(start, duration_ms)))
        cursor = max(cursor, end)
    if cursor < duration_ms:
        uncertain.append((cursor, duration_ms))
    # Bridge across thin confident slivers, then pad and clamp.
    bridged: list[Span] = []
    for start, end in uncertain:
        if bridged and start - bridged[-1][1] <= bridge_ms:
            bridged[-1] = (bridged[-1][0], end)
        else:
            bridged.append((start, end))
    padded = _merge(
        [(max(0, s - pad_ms), min(duration_ms, e + pad_ms)) for s, e in bridged]
    )
    return tuple((s, e) for s, e in padded if e - s >= min_target_ms)


def targets_total_ms(targets: tuple[Span, ...] | list[Span]) -> int:
    return sum(max(0, e - s) for s, e in targets)


def remap_sliced_ms(sliced_ms: int, targets: tuple[Span, ...] | list[Span]) -> int:
    """Map a position in the concatenated sliced audio back to the original mix timeline.

    The sliced audio is the targets concatenated in order, so offset ``sliced_ms`` lands inside the
    target whose cumulative length first exceeds it.  Positions past the end clamp to the last
    target's end (defensive against a provider reporting a chunk slightly beyond the audio).
    """

    if sliced_ms < 0:
        sliced_ms = 0
    cumulative = 0
    for start, end in targets:
        length = max(0, end - start)
        if sliced_ms < cumulative + length:
            return start + (sliced_ms - cumulative)
        cumulative += length
    return targets[-1][1] if targets else sliced_ms


def remap_span(span: Span, targets: tuple[Span, ...] | list[Span]) -> Span:
    """Remap a sliced-timeline span to the original timeline (anchored on its start)."""

    lo, hi = span
    new_lo = remap_sliced_ms(lo, targets)
    new_hi = remap_sliced_ms(max(lo, hi), targets)
    if new_hi < new_lo:
        new_hi = new_lo
    return (new_lo, new_hi)


def _atrim_filter(targets: tuple[Span, ...] | list[Span]) -> str:
    """An ffmpeg ``-filter_complex`` string that extracts each target and concatenates them."""

    parts = []
    for index, (start, end) in enumerate(targets):
        parts.append(
            f"[0:a]atrim=start={start / 1000:.3f}:end={end / 1000:.3f},"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )
    labels = "".join(f"[a{index}]" for index in range(len(targets)))
    parts.append(f"{labels}concat=n={len(targets)}:v=0:a=1[out]")
    return ";".join(parts)


async def slice_audio(source: Path, targets: tuple[Span, ...] | list[Span], out_path: Path) -> Path:
    """Write a WAV holding only the target spans, concatenated in order (ffmpeg)."""

    if not targets:
        raise ValueError("no targets to slice")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    await run_process(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-filter_complex",
            _atrim_filter(targets),
            "-map",
            "[out]",
            str(out_path),
        ],
        timeout=600,
    )
    return out_path
