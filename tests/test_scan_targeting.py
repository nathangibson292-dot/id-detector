"""Confidence-gated paid-scan targeting (:mod:`id_detector.scan_targeting`)."""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from types import SimpleNamespace

from id_detector.scan_targeting import (
    remap_sliced_ms,
    remap_span,
    select_gap_targets,
    select_scan_targets,
    slice_audio,
    targets_total_ms,
)


def ep(badge: str, start: int, end: int, *, flags=(), suppressed=None) -> SimpleNamespace:
    return SimpleNamespace(
        badge=badge, best_start_ms=start, best_end_ms=end, flags=list(flags), suppressed=suppressed
    )


def test_targets_are_the_complement_of_confident_coverage() -> None:
    episodes = [
        ep("likely", 10_000, 25_000),  # confident -> skip
        ep("possible", 30_000, 36_000),  # uncertain -> scan (a phantom lives here)
        ep("possible", 41_000, 44_000, flags=["hint_supported"]),  # hint = confident -> skip
    ]
    targets = select_scan_targets(episodes, 60_000, pad_ms=0, bridge_ms=0, min_target_ms=1_000)
    assert targets == ((0, 10_000), (25_000, 41_000), (44_000, 60_000))
    # We pay for the uncertain minutes only, not the whole hour.
    assert targets_total_ms(targets) < 60_000


def test_gap_targets_are_the_complement_of_any_match_not_just_confident() -> None:
    # Paid-first: the paid engine already ran the whole mix. The free engine should fill only the
    # spans NOTHING matched — a merely "possible" match still counts as covered (unlike scan
    # targets, which treat "possible" as uncertain and worth a paid cross-check).
    episodes = [
        ep("likely", 10_000, 25_000),  # covered both ways
        ep("possible", 30_000, 40_000),  # a match exists -> a gap-fill SKIP, a scan-target HIT
    ]
    scan = select_scan_targets(episodes, 60_000, pad_ms=0, bridge_ms=0, min_target_ms=1_000)
    gaps = select_gap_targets(episodes, 60_000, pad_ms=0, bridge_ms=0, min_target_ms=1_000)
    # Scan (uncertain) includes the possible region; gaps exclude it.
    assert scan == ((0, 10_000), (25_000, 60_000))
    assert gaps == ((0, 10_000), (25_000, 30_000), (40_000, 60_000))


def test_a_suppressed_match_leaves_a_gap() -> None:
    # A suppressed episode is not real coverage, so its span is still a gap to fill.
    episodes = [ep("possible", 20_000, 30_000, suppressed="buried")]
    assert select_gap_targets(episodes, 60_000, pad_ms=0) == ((0, 60_000),)


def test_all_confident_means_skip_paid_entirely() -> None:
    episodes = [ep("likely", 0, 30_000), ep("verified", 30_000, 60_000)]
    assert select_scan_targets(episodes, 60_000) == ()


def test_blank_mix_scans_everything() -> None:
    assert select_scan_targets([], 60_000, pad_ms=0) == ((0, 60_000),)


def test_thin_confident_sliver_is_bridged_not_fragmented() -> None:
    # A 2s confident hit inside a long unknown stretch should not split the scan into two.
    episodes = [ep("likely", 20_000, 22_000)]
    targets = select_scan_targets(episodes, 60_000, pad_ms=0, bridge_ms=4_000, min_target_ms=1_000)
    assert targets == ((0, 60_000),)


def test_short_targets_are_dropped() -> None:
    # Confident from 0-58s leaves only a 2s tail -> too short to bother scanning.
    episodes = [ep("likely", 0, 58_000)]
    assert select_scan_targets(episodes, 60_000, pad_ms=0, min_target_ms=8_000) == ()


def test_remap_sliced_positions_back_to_the_original_timeline() -> None:
    targets = [(10_000, 20_000), (40_000, 55_000)]  # sliced length 10s + 15s = 25s
    assert remap_sliced_ms(0, targets) == 10_000  # start of first target
    assert remap_sliced_ms(9_000, targets) == 19_000  # still in first target
    assert remap_sliced_ms(10_000, targets) == 40_000  # boundary -> start of second target
    assert remap_sliced_ms(24_000, targets) == 54_000  # near end of second
    assert remap_sliced_ms(999_999, targets) == 55_000  # past end clamps to last target end
    assert remap_span((10_000, 13_000), targets) == (40_000, 43_000)


def _wav(path: Path, seconds: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 16_000 * seconds)


def _duration_s(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


def test_slice_audio_keeps_only_the_targets(tmp_path: Path) -> None:
    src = tmp_path / "mix.wav"
    _wav(src, 60)
    out = tmp_path / "sliced.wav"
    targets = [(10_000, 20_000), (40_000, 55_000)]  # 10s + 15s = 25s expected
    asyncio.run(slice_audio(src, targets, out))
    assert out.is_file()
    assert abs(_duration_s(out) - 25.0) < 0.3
