"""The local-index (Panako) query stage (run_local_index_recognition).

Uses an injected fake PanakoProvider that replays a recorded query CSV — no JDK, no jar, no
subprocess.  Covers: a manifest-backed match becomes a positioned local_index_query observation
carrying the uploader/title label, a second run re-runs the JVM zero times (raw output cached), and
a missing index is a clean skip.
"""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path

from id_detector.candidates import build_manifest, write_manifest
from id_detector.contracts import WindowRecord
from id_detector.local_index import run_local_index_recognition
from id_detector.process import ProcessResult
from id_detector.providers.panako import PanakoIndexPaths, parse_query_output
from id_detector.windows import WindowsResult

ROOT = Path(__file__).resolve().parent
_MATCH_CSV = (ROOT / "fixtures" / "panako" / "query-match.csv").read_text(encoding="utf-8")


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


class _IndexedResource:
    """Minimal build_manifest input (resource_id/title/uploader) matching the CSV's Match id."""

    def __init__(self) -> None:
        self.resource_id = "700123"
        self.fingerprint_count = 4200
        self.title = "untitled-edit"
        self.uploader = "Artist A"
        self.url = "https://soundcloud.com/artist-a/untitled-edit"
        self.source = "uploader"


def _build_index(index_root: Path, label: str = "default") -> None:
    paths = PanakoIndexPaths(root=index_root / label)
    paths.root.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(index_label=label, resources=[_IndexedResource()])  # type: ignore[list-item]
    write_manifest(paths.manifest_path, manifest)


class _FakeProvider:
    def __init__(self, csv: str) -> None:
        self.csv = csv
        self.calls = 0

    async def query_wav(self, wav_path: Path) -> tuple[ProcessResult, list[object]]:
        self.calls += 1
        return (
            ProcessResult(args=["panako"], returncode=0, stdout=self.csv, stderr=""),
            parse_query_output(self.csv),
        )


def _run(**kwargs: object):
    return asyncio.run(run_local_index_recognition(**kwargs))  # type: ignore[arg-type]


def test_a_local_index_hit_becomes_a_positioned_labelled_observation(tmp_path: Path) -> None:
    window = _golden_window()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    _write_clip(media_dir, window)
    index_root = tmp_path / "db"
    _build_index(index_root)
    windows = WindowsResult(records=(window,), record_path=media_dir / "w.jsonl", cached=True)
    provider = _FakeProvider(_MATCH_CSV)
    lo = window.support_ms[0]

    result = _run(
        media_key="a" * 64,
        media_dir=media_dir,
        windows=windows,
        targets=((lo, lo + 1000),),
        duration_ms=3_600_000,
        run_id="r" * 32,
        index_label="default",
        index_root=index_root,
        provider=provider,
    )

    assert provider.calls == 1
    assert result.engines_run == ("panako",)
    matches = [obs for obs in result.observations if obs.status == "match"]
    assert len(matches) == 1
    obs = matches[0]
    assert obs.provider == "panako" and obs.capability == "local_index_query"
    # The manifest label, not the bare file stem.
    assert obs.raw_label.artist == "Artist A" and obs.raw_label.title == "untitled-edit"
    # Positioned in the mix timeline: window start + Panako's in-query offset.
    assert obs.mix_span_ms[0] == window.support_ms[0] + 360
    assert obs.provider_ids["panako"] == "700123"
    assert result.observation_paths and result.observation_paths[0].exists()


def test_a_repeat_run_reads_cached_output_and_reruns_nothing(tmp_path: Path) -> None:
    window = _golden_window()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    _write_clip(media_dir, window)
    index_root = tmp_path / "db"
    _build_index(index_root)
    windows = WindowsResult(records=(window,), record_path=media_dir / "w.jsonl", cached=True)
    lo = window.support_ms[0]
    common = dict(
        media_key="a" * 64,
        media_dir=media_dir,
        windows=windows,
        targets=((lo, lo + 1000),),
        duration_ms=3_600_000,
        index_label="default",
        index_root=index_root,
    )

    first = _FakeProvider(_MATCH_CSV)
    _run(run_id="1" * 32, provider=first, **common)
    assert first.calls == 1

    second = _FakeProvider("Index;Total\n")  # different (empty) output would be visible if used
    again = _run(run_id="2" * 32, provider=second, **common)
    assert second.calls == 0  # served from the cached raw output
    assert any(obs.status == "match" for obs in again.observations)


def test_no_index_built_is_a_clean_skip(tmp_path: Path) -> None:
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
        duration_ms=3_600_000,
        run_id="r" * 32,
        index_label="default",
        index_root=tmp_path / "db",  # nothing built here
        provider=_FakeProvider(_MATCH_CSV),
    )
    assert result.observations == ()
    assert result.skipped and result.skipped[0][0] == "panako"


def test_windows_outside_the_targets_are_never_queried(tmp_path: Path) -> None:
    window = _golden_window()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    _write_clip(media_dir, window)
    index_root = tmp_path / "db"
    _build_index(index_root)
    windows = WindowsResult(records=(window,), record_path=media_dir / "w.jsonl", cached=True)
    provider = _FakeProvider(_MATCH_CSV)
    lo = window.support_ms[0]
    result = _run(
        media_key="a" * 64,
        media_dir=media_dir,
        windows=windows,
        targets=((lo + 500_000, lo + 600_000),),
        duration_ms=3_600_000,
        run_id="r" * 32,
        index_label="default",
        index_root=index_root,
        provider=provider,
    )
    assert provider.calls == 0 and result.observations == ()
