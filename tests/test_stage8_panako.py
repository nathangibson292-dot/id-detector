"""Stage 8 — Panako local_index_query provider.

Default tests are network-free and deterministic: they parse recorded Panako output fixtures,
check observation normalisation / anchor conversion vectors, and verify JDK discovery order with
injected fakes.  The one end-to-end test that actually runs java + Panako is marked ``slow`` (so
it is deselected by default) and skips cleanly when the jar or a JDK is absent.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from hashlib import sha1
from pathlib import Path

import pytest

from id_detector.candidates import Candidate, index_candidates
from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    QueryRecord,
    WindowQueryTarget,
    compose_natural_key,
    local_index_cache_key,
    make_id,
)
from id_detector.fuse.scanners import (
    ANCHOR_CONVERSIONS,
    SCANNER_CAPABILITIES,
    engine_independence,
    validate_scanner_observations,
)
from id_detector.providers.base import ProviderUnavailable
from id_detector.providers.panako import (
    PANAKO_ANCHOR_METHOD,
    PANAKO_JAR_SHA256,
    PANAKO_LMDB_MAPSIZE,
    PanakoIndexPaths,
    PanakoProvider,
    PanakoRuntime,
    QueryWindow,
    normalise_matches,
    parse_query_output,
    parse_store_output,
    render_config_properties,
    resolve_java,
    scanner_logical_trial_id,
    sparse_extend,
)
from id_detector.providers.panako_setup import jar_path, verify_jar

FIXTURES = Path(__file__).parent / "fixtures" / "panako"


def _query_record() -> QueryRecord:
    target = WindowQueryTarget(window_id=sha1(b"window").hexdigest())
    natural = {
        "provider": "panako",
        "capability": "local_index_query",
        "target": target.model_dump(mode="json"),
        "provider_config_version": "panako-v2.json",
        "scan_policy": "reference_pool",
    }
    return QueryRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id("m" * 64, "query", compose_natural_key("query", natural)),
        generation=0,
        provider="panako",
        capability="local_index_query",
        target=target,
        provider_config_version="panako-v2.json",
        scan_policy="reference_pool",
        cache_key=local_index_cache_key("w" * 64, "idx", "ver"),
    )


# --------------------------------------------------------------------------------------------------
# Output parsing
# --------------------------------------------------------------------------------------------------
def test_parse_store_output_extracts_every_resource() -> None:
    resources = parse_store_output((FIXTURES / "store-output.txt").read_text(encoding="utf-8"))
    assert [(r.resource_id, r.fingerprint_count) for r in resources] == [
        ("700123", 2920),
        ("700124", 2333),
    ]


def test_parse_query_match_columns() -> None:
    (match,) = parse_query_output((FIXTURES / "query-match.csv").read_text(encoding="utf-8"))
    assert match.matched
    assert match.ref_id == "700123"
    assert match.query_start_s == pytest.approx(0.360)
    assert match.ref_start_s == pytest.approx(20.360)
    assert match.score == 516
    assert match.time_factor == pytest.approx(1.0)
    assert match.frequency_factor == pytest.approx(1.0)


def test_parse_query_no_match_row_is_not_a_match() -> None:
    (match,) = parse_query_output((FIXTURES / "query-no-match.csv").read_text(encoding="utf-8"))
    assert not match.matched
    assert match.ref_id is None
    assert match.score == -1


# --------------------------------------------------------------------------------------------------
# Observation normalisation + anchor conversion vector
# --------------------------------------------------------------------------------------------------
def test_normalise_match_anchor_and_span_vector() -> None:
    matches = parse_query_output((FIXTURES / "query-match.csv").read_text(encoding="utf-8"))
    window = QueryWindow(
        window_id=sha1(b"window").hexdigest(), start_ms=20_000, wav_sha256="w" * 64, chunk_index=2
    )
    (obs,) = normalise_matches(
        matches,
        query=_query_record(),
        media_key="m" * 64,
        window=window,
        duration_ms=90_000,
        raw_response_ref="recognise/raw/panako.txt",
    )
    assert obs.capability == "local_index_query"
    assert obs.provider == "panako"
    assert obs.status == "match"
    assert obs.transform is None  # scanner-path rule
    # mix = window start (20000) + Panako in-query offset (360) ; ends at 20000 + 14496
    assert obs.mix_span_ms == (20_360, 34_496)
    assert obs.support_ms == (20_360, 34_496)
    assert obs.anchor is not None
    assert obs.anchor.mix_anchor_ms == 20_360
    assert obs.anchor.ref_anchor_ms == 20_360  # reference offset 20.360 s
    assert obs.anchor.method == PANAKO_ANCHOR_METHOD
    assert obs.anchor.reliable is True
    assert obs.score_raw == 516
    assert obs.logical_trial_id == scanner_logical_trial_id("panako", 2)
    assert obs.native["time_factor_e4"] == 10_000
    assert obs.raw_label.title == "artist-a - untitled-edit"
    # The observation must satisfy the contract (no floats, closed schema).
    obs.model_validate(obs.model_dump(mode="json"))


def test_normalise_scaled_factor_conversion_to_e4() -> None:
    matches = parse_query_output((FIXTURES / "query-scaled.csv").read_text(encoding="utf-8"))
    window = QueryWindow(
        window_id=sha1(b"w").hexdigest(), start_ms=48_000, wav_sha256="w" * 64, chunk_index=5
    )
    (obs,) = normalise_matches(
        matches,
        query=_query_record(),
        media_key="m" * 64,
        window=window,
        duration_ms=180_000,
        raw_response_ref="recognise/raw/panako.txt",
    )
    assert obs.mix_span_ms == (49_200, 61_680)  # 48000 + 1200 .. 48000 + 13680
    assert obs.anchor is not None and obs.anchor.ref_anchor_ms == 61_200
    assert obs.native["time_factor_e4"] == 10_410  # 1.041 -> 10410
    assert obs.native["frequency_factor_e4"] == 9_720  # 0.972 -> 9720
    assert obs.score_raw == 342


def test_normalise_no_match_emits_a_single_no_match_observation() -> None:
    matches = parse_query_output((FIXTURES / "query-no-match.csv").read_text(encoding="utf-8"))
    window = QueryWindow(
        window_id=sha1(b"w").hexdigest(), start_ms=9_000, wav_sha256="w" * 64, chunk_index=1
    )
    (obs,) = normalise_matches(
        matches,
        query=_query_record(),
        media_key="m" * 64,
        window=window,
        duration_ms=90_000,
        raw_response_ref="recognise/raw/panako.txt",
    )
    assert obs.status == "no_match"
    assert obs.anchor is None
    assert obs.score_raw is None
    assert obs.transform is None
    obs.model_validate(obs.model_dump(mode="json"))


# --------------------------------------------------------------------------------------------------
# Fusion wiring (scanner path)
# --------------------------------------------------------------------------------------------------
def test_panako_is_a_scanner_capability_with_a_registered_anchor() -> None:
    assert "local_index_query" in SCANNER_CAPABILITIES
    assert PANAKO_ANCHOR_METHOD in ANCHOR_CONVERSIONS


def test_panako_observations_pass_scanner_validation_and_count_as_independent() -> None:
    matches = parse_query_output((FIXTURES / "query-match.csv").read_text(encoding="utf-8"))
    window = QueryWindow(
        window_id=sha1(b"w").hexdigest(), start_ms=20_000, wav_sha256="w" * 64, chunk_index=0
    )
    observations = normalise_matches(
        matches,
        query=_query_record(),
        media_key="m" * 64,
        window=window,
        duration_ms=90_000,
        raw_response_ref="recognise/raw/panako.txt",
    )
    validate_scanner_observations(observations)  # must not raise
    independence = engine_independence(observations)
    # Panako is self-hosted/free: an independent source, never on the commercial dependence prior.
    assert independence.providers == ("panako",)
    assert independence.discounted == ()


# --------------------------------------------------------------------------------------------------
# JDK discovery order
# --------------------------------------------------------------------------------------------------
def test_jdk_discovery_prefers_java_home(tmp_path: Path) -> None:
    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True)
    java = home / "bin" / ("java.exe" if sys.platform == "win32" else "java")
    java.write_text("", encoding="utf-8")
    install = tmp_path / "install" / "bin" / ("java.exe" if sys.platform == "win32" else "java")
    install.parent.mkdir(parents=True)
    install.write_text("", encoding="utf-8")
    resolution = resolve_java(
        env={"JAVA_HOME": str(home)},
        install_globs=(str(install),),
        which=lambda _: "on-path-java",
    )
    assert resolution is not None
    assert resolution.source == "JAVA_HOME"
    assert resolution.path == java


def test_jdk_discovery_falls_back_to_install_then_path(tmp_path: Path) -> None:
    older = tmp_path / "jdk-11.0.20" / "bin" / "java.exe"
    newer = tmp_path / "jdk-21.0.12" / "bin" / "java.exe"
    for candidate in (older, newer):
        candidate.parent.mkdir(parents=True)
        candidate.write_text("", encoding="utf-8")
    install = resolve_java(
        env={}, install_globs=(str(older), str(newer)), which=lambda _: "on-path"
    )
    assert install is not None and install.source == "install"
    assert install.path == newer  # newest parsed version wins

    on_path = resolve_java(env={}, install_globs=(), which=lambda name: "resolved-java")
    assert on_path is not None and on_path.source == "PATH"

    assert resolve_java(env={}, install_globs=(), which=lambda _: None) is None


# --------------------------------------------------------------------------------------------------
# Config + runtime gating
# --------------------------------------------------------------------------------------------------
def test_config_properties_uses_platform_decoder() -> None:
    windows = render_config_properties(windows=True)
    assert "DECODER_PIPE_ENVIRONMENT=cmd.exe" in windows
    assert "DECODER_PIPE_ENVIRONMENT_ARG=/C" in windows
    posix = render_config_properties(windows=False)
    assert "DECODER_PIPE_ENVIRONMENT=/bin/bash" in posix
    assert "OLAF_STORAGE=LMDB" in windows and "OLAF_SAMPLE_RATE=16000" in windows


def test_runtime_resolve_reports_missing_jar_as_unavailable(tmp_path: Path) -> None:
    with pytest.raises(ProviderUnavailable):
        PanakoRuntime.resolve(jar=tmp_path / "missing.jar", env={})


# --------------------------------------------------------------------------------------------------
# Windows LMDB sparse preallocation
# --------------------------------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="sparse preallocation is a Windows path")
def test_sparse_extend_sets_size_without_consuming_disk(tmp_path: Path) -> None:
    data = tmp_path / "data.mdb"
    data.write_bytes(b"\x00" * 8192)  # stand-in for LMDB's initial meta pages
    sparse_extend(data, size=4 * 1024 * 1024)  # 4 MiB, kept small for the test
    assert data.stat().st_size == 4 * 1024 * 1024


def test_sparse_extend_is_a_noop_off_windows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("id_detector.providers.panako.os.name", "posix")
    data = tmp_path / "data.mdb"
    data.write_bytes(b"\x00" * 100)
    sparse_extend(data, size=PANAKO_LMDB_MAPSIZE)  # must not raise or grow the file on POSIX
    assert data.stat().st_size == 100


# --------------------------------------------------------------------------------------------------
# "Audio deleted after fingerprinting" guarantee (network-free, no java)
# --------------------------------------------------------------------------------------------------
class _FakeStored:
    def __init__(self, resource_id: str) -> None:
        self.resource_id = resource_id
        self.fingerprint_count = 1234


class _FakeProvider:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.seen_audio_present: list[bool] = []

    async def store(self, audio_paths):
        # The audio must still exist while Panako fingerprints it.
        self.seen_audio_present.append(all(Path(p).is_file() for p in audio_paths))
        if self.fail:
            raise RuntimeError("panako failed")
        return [_FakeStored("700900")]


def _fake_downloader(audio_bytes: bytes = b"RIFFfake"):
    async def download(candidate: Candidate, dest: Path) -> Path:
        target = dest / "candidate.wav"
        target.write_bytes(audio_bytes)
        return target

    return download


def test_audio_is_deleted_after_fingerprinting(tmp_path: Path) -> None:
    provider = _FakeProvider()
    candidates = [Candidate(url="u/a", title="A", uploader="U", source="explicit_url")]
    resources = asyncio.run(
        index_candidates(
            provider,  # type: ignore[arg-type]
            candidates,
            download_dir=tmp_path / "dl",
            downloader=_fake_downloader(),
        )
    )
    assert provider.seen_audio_present == [True]
    assert [r.resource_id for r in resources] == ["700900"]
    # Only fingerprints are kept: no decoded/downloaded audio remains under the download dir.
    assert not list((tmp_path / "dl").rglob("*.wav"))


def test_audio_is_deleted_even_when_fingerprinting_fails(tmp_path: Path) -> None:
    provider = _FakeProvider(fail=True)
    candidates = [Candidate(url="u/a", title="A", uploader="U", source="explicit_url")]
    with pytest.raises(RuntimeError):
        asyncio.run(
            index_candidates(
                provider,  # type: ignore[arg-type]
                candidates,
                download_dir=tmp_path / "dl",
                downloader=_fake_downloader(),
            )
        )
    assert not list((tmp_path / "dl").rglob("*.wav"))


# --------------------------------------------------------------------------------------------------
# End-to-end: actually run java + Panako on synthetic references (slow; skips without jar/JDK)
# --------------------------------------------------------------------------------------------------
def _synthetic_sources(directory: Path) -> list[Path]:
    from id_detector.benchmark.controlled import synthesize_test_sources

    return synthesize_test_sources(directory, seed=8, count=3)


@pytest.mark.slow
def test_panako_index_and_query_end_to_end(tmp_path: Path) -> None:
    tool_dir = Path("data/local/panako")
    jar = jar_path(tool_dir)
    ok, _ = verify_jar(jar)
    if not ok:
        pytest.skip("pinned Panako jar not present; run `id-detector panako-setup`")
    if resolve_java() is None:
        pytest.skip("no JDK resolvable")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")

    from id_detector.process import run_process

    async def _run() -> None:
        runtime = PanakoRuntime.resolve(jar=jar)
        index = PanakoIndexPaths(root=tmp_path / "index")
        provider = PanakoProvider(runtime=runtime, paths=index)
        sources = _synthetic_sources(tmp_path / "srcs")
        stored = await provider.store(sources)
        assert len(stored) == len(sources)
        # Extract a 15 s window from the first source and query for it.
        query_wav = tmp_path / "query.wav"
        await run_process(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(sources[0].resolve()),
                "-ss",
                "20",
                "-t",
                "15",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-sample_fmt",
                "s16",
                str(query_wav.resolve()),
            ],
            timeout=120,
        )
        _result, matches = await provider.query_wav(query_wav)
        hit = [m for m in matches if m.matched]
        assert hit, "Panako should recognise a window taken from an indexed reference"
        assert hit[0].ref_id == stored[0].resource_id

    asyncio.run(_run())


def test_pinned_jar_sha256_matches_when_present() -> None:
    jar = jar_path(Path("data/local/panako"))
    if not jar.is_file():
        pytest.skip("pinned Panako jar not present")
    ok, detail = verify_jar(jar)
    assert ok, detail
    assert PANAKO_JAR_SHA256 in detail
