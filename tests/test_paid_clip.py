"""The two-phase paid clip-recognition stage (run_paid_clip_recognition).

Uses an injected fake adapter — no network, no credentials, no trial quota.  Covers: only
uncertain-region clips are sent, a match becomes a positioned observation, the raw response is
cached by clip-key so a second run bills zero, and an unavailable engine is skipped not raised.
"""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path

from id_detector.contracts import WindowRecord
from id_detector.paid_clip import _subsample_evenly, run_paid_clip_recognition
from id_detector.providers.base import AppConfig
from id_detector.windows import WindowsResult

ROOT = Path(__file__).resolve().parent


def _golden_window() -> WindowRecord:
    return WindowRecord.model_validate(
        json.loads((ROOT / "golden" / "window.json").read_text(encoding="utf-8"))
    )


def _write_clip(media_dir: Path, window: WindowRecord) -> None:
    path = media_dir / window.wav_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 16_000)


class _FakeAdapter:
    """Returns a fixed match and counts how many times it was actually called (billed)."""

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls = 0

    async def recognize_clip(self, path: Path, on_attempt: object) -> dict[str, object]:
        self.calls += 1
        return self.response


def _run(**kwargs: object):
    return asyncio.run(run_paid_clip_recognition(**kwargs))  # type: ignore[arg-type]


def test_windows_in_spans_subsets_gap_windows_for_the_free_engine_fill() -> None:
    from id_detector.cli import _windows_in_spans

    window = _golden_window()  # support_ms = (18000, 30000)
    windows = WindowsResult(records=(window,), record_path=Path("w"), cached=True)
    lo = window.support_ms[0]
    # A span covering the window's start keeps it; one that doesn't drops it.
    assert _windows_in_spans(windows, ((lo, lo + 1),)).records == (window,)
    assert _windows_in_spans(windows, ((lo + 100_000, lo + 200_000),)).records == ()
    # Metadata is carried through so the subset is a usable WindowsResult.
    subset = _windows_in_spans(windows, ((lo, lo + 1),))
    assert subset.record_path == windows.record_path and subset.cached == windows.cached


def test_subsample_spreads_a_budget_evenly_across_the_timeline() -> None:
    items = list(range(100))
    # Under budget: everything is kept.
    assert _subsample_evenly(items[:40], 150) == items[:40]  # type: ignore[arg-type]
    # Over budget: exactly `budget` items, spanning the whole range (first kept, last near the end).
    picked = _subsample_evenly(items, 10)  # type: ignore[arg-type]
    assert len(picked) == 10
    assert picked[0] == 0 and picked[-1] >= 90  # covers the tail, not just the first 10
    assert picked == sorted(picked) and len(set(picked)) == 10  # ordered, distinct
    assert _subsample_evenly(items, 0) == []  # type: ignore[arg-type]


def test_only_uncertain_windows_are_recognised_and_a_match_is_positioned(tmp_path: Path) -> None:
    window = _golden_window()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    _write_clip(media_dir, window)
    windows = WindowsResult(records=(window,), record_path=media_dir / "w.jsonl", cached=True)
    adapter = _FakeAdapter(
        {"status": "success", "result": {"artist": "Effy", "title": "CLUBGRLS"}}
    )
    lo = window.support_ms[0]

    result = _run(
        media_key="a" * 64,
        media_dir=media_dir,
        windows=windows,
        targets=((lo, lo + 1000),),
        run_id="r" * 32,
        app_config=AppConfig(False),
        enabled_engines=("audd",),
        cli_confirmation=False,
        adapters={"audd": adapter},
    )

    assert adapter.calls == 1
    assert len(result.observations) == 1
    obs = result.observations[0]
    assert obs.provider == "audd" and obs.status == "match"
    assert obs.raw_label.title == "CLUBGRLS"
    assert obs.support_ms == window.support_ms  # competes with the Shazam clip at this window
    assert result.engines_run == ("audd",)
    assert result.observation_paths and result.observation_paths[0].exists()


def test_a_repeat_run_is_served_from_cache_and_bills_nothing(tmp_path: Path) -> None:
    window = _golden_window()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    _write_clip(media_dir, window)
    windows = WindowsResult(records=(window,), record_path=media_dir / "w.jsonl", cached=True)
    lo = window.support_ms[0]
    common = dict(
        media_key="a" * 64,
        media_dir=media_dir,
        windows=windows,
        targets=((lo, lo + 1000),),
        app_config=AppConfig(False),
        enabled_engines=("audd",),
        cli_confirmation=False,
    )

    first = _FakeAdapter({"status": "success", "result": {"artist": "Effy", "title": "CLUBGRLS"}})
    _run(run_id="1" * 32, adapters={"audd": first}, **common)
    assert first.calls == 1  # cold: one billable request

    second = _FakeAdapter({"status": "success", "result": {"artist": "X", "title": "Y"}})
    again = _run(run_id="2" * 32, adapters={"audd": second}, **common)
    assert second.calls == 0  # warm: served from the clip-key cache, zero billing
    assert again.observations[0].raw_label.title == "CLUBGRLS"  # cached response, not the new one


def test_windows_outside_the_uncertain_targets_are_never_sent(tmp_path: Path) -> None:
    window = _golden_window()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    _write_clip(media_dir, window)
    windows = WindowsResult(records=(window,), record_path=media_dir / "w.jsonl", cached=True)
    adapter = _FakeAdapter({"status": "success", "result": {"artist": "A", "title": "B"}})
    lo = window.support_ms[0]

    result = _run(
        media_key="a" * 64,
        media_dir=media_dir,
        windows=windows,
        targets=((lo + 500_000, lo + 600_000),),  # nowhere near this window
        run_id="r" * 32,
        app_config=AppConfig(False),
        enabled_engines=("audd",),
        cli_confirmation=False,
        adapters={"audd": adapter},
    )
    assert adapter.calls == 0 and result.observations == ()


def test_engine_not_enabled_is_a_clean_skip(tmp_path: Path) -> None:
    window = _golden_window()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    windows = WindowsResult(records=(window,), record_path=media_dir / "w.jsonl", cached=True)
    lo = window.support_ms[0]
    result = _run(
        media_key="a" * 64,
        media_dir=media_dir,
        windows=windows,
        targets=((lo, lo + 1000),),
        run_id="r" * 32,
        app_config=AppConfig(False),
        enabled_engines=(),  # audd not enabled
        cli_confirmation=False,
        adapters={"audd": _FakeAdapter({})},
    )
    assert result.observations == () and result.engines_run == ()


def test_missing_credentials_is_skipped_not_raised(tmp_path: Path) -> None:
    window = _golden_window()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    _write_clip(media_dir, window)
    windows = WindowsResult(records=(window,), record_path=media_dir / "w.jsonl", cached=True)
    lo = window.support_ms[0]

    # No injected adapter and no token -> AudDCredentials.from_env raises ProviderUnavailable,
    # which the stage must swallow into a recorded skip rather than propagate.
    import os

    saved = os.environ.pop("AUDD_API_TOKEN", None)
    try:
        result = _run(
            media_key="a" * 64,
            media_dir=media_dir,
            windows=windows,
            targets=((lo, lo + 1000),),
            run_id="r" * 32,
            app_config=AppConfig(False),
            enabled_engines=("audd",),
            cli_confirmation=False,
            adapters=None,
        )
    finally:
        if saved is not None:
            os.environ["AUDD_API_TOKEN"] = saved
    assert result.observations == ()
    assert result.skipped and result.skipped[0][0] == "audd"
