from __future__ import annotations

import asyncio
import json
import math
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from id_detector.benchmark.controlled import (
    _ffmpeg_to_wav,
    _single_filter,
    synthesize_test_sources,
)
from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    DecoderInfo,
    GroundTruthRecord,
    PcmAsset,
    PcmRecord,
    QueryRecord,
    Transform,
    WindowQueryTarget,
    clip_cache_key,
    compose_natural_key,
    make_id,
)
from id_detector.decode import DecodeResult
from id_detector.fuse.alignment import align_selected_points, select_logical_trial_points
from id_detector.fuse.identity import build_identity_graph
from id_detector.io import native_path, path_is_file, read_text, sha256_file
from id_detector.local_fixture import (
    DECOY_ARTIST,
    recognise_fixture_windows_in_memory,
    transform_matches_fixture_decoy,
)
from id_detector.providers.base import AppConfig
from id_detector.recognise import load_provider_config, recognise_generation_zero
from id_detector.shazam import ShazamAdapter, TokenBucket, response_to_observation
from id_detector.windows import (
    DEFAULT_TRANSFORM_GRID,
    TransformGrid,
    WindowSchedule,
    generate_windows_async,
    plan_fixture_windows,
    sample_map_for_transform,
    schedule_options,
    transform_filtergraph,
    transform_slice_sample_count,
    write_transformed_wav,
)

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 16_000
FACTORS = [
    *(Transform(type="resample", rate_e4=rate, semitones=0) for rate in (9200, 9600, 10400, 10800)),
    *(Transform(type="tempo", rate_e4=rate, semitones=0) for rate in (9200, 9600, 10400, 10800)),
    *(
        Transform(
            type="pitch",
            rate_e4=round(10_000 * (2 ** (semitone / 12))),
            semitones=semitone,
        )
        for semitone in (-2, -1, 1, 2)
    ),
]

# Budgets for the raw FFmpeg output length and for where a known marker lands after the
# production undo.  ``resample`` has an exact rational map (``uncertainty_ms = 0``); ``tempo``
# and ``pitch`` go through WSOLA, whose declared ``uncertainty_ms`` is 100 ms.  Observed worst
# cases on this fixture are recorded in docs/stage-reports/stage-4b.md.
FRAME_BUDGET_SAMPLES = {"resample": 16, "tempo": 1_600, "pitch": 1_600}
MARKER_BUDGET_SAMPLES = {"resample": 16, "tempo": 1_600, "pitch": 1_600}
MARKER_START_SAMPLE = 4 * SAMPLE_RATE
MARKER_ORIGINAL_SAMPLE = MARKER_START_SAMPLE + 6 * SAMPLE_RATE
MARKER_LENGTH_SAMPLES = 480


def _label(transform: Transform) -> str:
    if transform.type == "pitch":
        return f"{transform.type}-{transform.semitones:+d}"
    return f"{transform.type}-{transform.rate_e4}"


def _shazam_query(window: object, provider_config_version: str) -> QueryRecord:
    target = WindowQueryTarget(window_id=window.id)  # type: ignore[attr-defined]
    natural = {
        "provider": "shazam",
        "capability": "clip_recognizer",
        "target": target.model_dump(mode="json"),
        "provider_config_version": provider_config_version,
        "scan_policy": "single-window",
    }
    return QueryRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id("f" * 64, "query", compose_natural_key("query", natural)),
        generation=0,
        provider="shazam",
        capability="clip_recognizer",
        target=target,
        provider_config_version=provider_config_version,
        scan_policy="single-window",
        cache_key=clip_cache_key(
            window.wav_sha256,  # type: ignore[attr-defined]
            "shazam",
            provider_config_version,
        ),
    )


def _case(transform: Transform) -> dict[str, int | str]:
    result: dict[str, int | str] = {
        "name": "insertion",
        "kind": transform.type,
        "episode_ms": 20_000,
    }
    if transform.type == "pitch":
        result["semitones"] = transform.semitones
    else:
        result["rate_e4"] = transform.rate_e4
    return result


@pytest.fixture(scope="module")
def insertion_vectors(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[int, int, int]]:
    """Render one natively transformed mix per factor and undo it with the production path."""

    root = tmp_path_factory.mktemp("stage4b-insertions")
    sources = synthesize_test_sources(root / "sources", seed=20_260_904, count=4)

    async def render() -> dict[str, tuple[int, int, int]]:
        results: dict[str, tuple[int, int, int]] = {}
        for transform in FACTORS:
            label = _label(transform)
            mix = root / f"mix-{label}.wav"
            await _ffmpeg_to_wav([sources[0]], _single_filter(_case(transform), 5_000), mix)
            with wave.open(native_path(mix), "rb") as handle:
                pcm = root / f"mix-{label}.pcm"
                pcm.write_bytes(handle.readframes(handle.getnframes()))
            output = root / f"undo-{label}.wav"
            input_samples = transform_slice_sample_count(
                12_000,
                transform.type,
                rate_e4=transform.rate_e4,
                semitones=transform.semitones,
            )
            raw_samples = await write_transformed_wav(
                pcm,
                output,
                start_sample=16_000,
                input_samples=input_samples,
                output_samples=192_000,
                transform=transform,
            )
            with wave.open(native_path(output), "rb") as handle:
                results[label] = (handle.getnframes(), input_samples, raw_samples)
        return results

    return asyncio.run(render())


def _write_marker_pcm(path: Path, duration_ms: int = 30_000) -> None:
    """Write silence carrying one 30 ms 1 kHz burst at a known original sample."""

    total = duration_ms * SAMPLE_RATE // 1_000
    data = np.zeros(total, dtype=np.int64)
    index = np.arange(MARKER_LENGTH_SAMPLES)
    burst = 22_000 * np.sin(2 * math.pi * 1_000 * index / SAMPLE_RATE)
    data[MARKER_ORIGINAL_SAMPLE : MARKER_ORIGINAL_SAMPLE + MARKER_LENGTH_SAMPLES] = burst.astype(
        np.int64
    )
    path.write_bytes(data.astype("<i2").tobytes())


def _marker_onset(samples: np.ndarray) -> int:
    magnitude = np.abs(samples.astype(np.int64))
    peak = int(magnitude.max())
    assert peak > 0, "the undone window contains no marker at all"
    return int(np.nonzero(magnitude >= peak // 4)[0][0])


@pytest.fixture(scope="module")
def marker_vectors(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[int, int]]:
    """Return ``(expected, observed)`` output sample of the known marker for every factor."""

    root = tmp_path_factory.mktemp("stage4b-markers")
    pcm = root / "marker.pcm"
    _write_marker_pcm(pcm)

    async def render() -> dict[str, tuple[int, int]]:
        results: dict[str, tuple[int, int]] = {}
        for transform in FACTORS:
            label = _label(transform)
            output = root / f"undo-{label}.wav"
            input_samples = transform_slice_sample_count(
                12_000,
                transform.type,
                rate_e4=transform.rate_e4,
                semitones=transform.semitones,
            )
            await write_transformed_wav(
                pcm,
                output,
                start_sample=MARKER_START_SAMPLE,
                input_samples=input_samples,
                output_samples=192_000,
                transform=transform,
            )
            with wave.open(native_path(output), "rb") as handle:
                samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
            sample_map = sample_map_for_transform(
                transform.type,
                rate_e4=transform.rate_e4,
                semitones=transform.semitones,
            )
            # sample_map is output k -> original (a_num/a_den)*k + b; invert it for the marker.
            offset = MARKER_ORIGINAL_SAMPLE - MARKER_START_SAMPLE - sample_map.b_samples
            expected = round(Fraction(offset * sample_map.a_den, sample_map.a_num))
            results[label] = (expected, _marker_onset(samples))
        return results

    return asyncio.run(render())


def test_config_defaults_and_policy_switches(tmp_path: Path) -> None:
    defaults = AppConfig.load(tmp_path / "missing.toml")
    assert defaults.transforms_policy == "rescan_only"
    assert defaults.transform_rates_e4 == (9_200, 9_600, 10_400, 10_800)
    assert defaults.transform_semitones == (-2, -1, 1, 2)
    # rev 5.2: generation 0 keeps 12 s / 9 s; the denser 12 s / 5 s schedule is the rescan policy.
    assert (defaults.window_ms, defaults.hop_ms, defaults.phase_ms) == (12_000, 9_000, 0)
    assert (
        defaults.rescan_window_ms,
        defaults.rescan_hop_ms,
        defaults.rescan_phase_ms,
    ) == (12_000, 5_000, 0)

    config = tmp_path / "config.toml"
    config.write_text(
        '[transforms]\npolicy="global"\nrate_e4=[9200]\nsemitones=[-1,1]\n'
        "[schedule]\nwindow_ms=8000\nhop_ms=5000\nphase_ms=2500\n"
        "[rescan]\nwindow_ms=8000\nhop_ms=4000\nphase_ms=2000\n",
        encoding="utf-8",
    )
    loaded = AppConfig.load(config)
    assert loaded.transforms_policy == "global"
    assert loaded.transform_rates_e4 == (9_200,)
    assert loaded.transform_semitones == (-1, 1)
    assert (loaded.window_ms, loaded.hop_ms, loaded.phase_ms) == (8_000, 5_000, 2_500)
    assert (loaded.rescan_window_ms, loaded.rescan_hop_ms, loaded.rescan_phase_ms) == (
        8_000,
        4_000,
        2_000,
    )

    config.write_text("[rescan]\nphase_ms=9000\nhop_ms=5000\n", encoding="utf-8")
    with pytest.raises(ValueError, match="rescan.phase_ms must be smaller"):
        AppConfig.load(config)

    config.write_text('[transforms]\npolicy="sometimes"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="off, rescan_only, or global"):
        AppConfig.load(config)


def test_example_config_matches_the_recorded_stage_4b_defaults() -> None:
    example = AppConfig.load(ROOT / "id-detector.example.toml")
    assert (example.window_ms, example.hop_ms, example.phase_ms) == (12_000, 9_000, 0)
    assert (example.rescan_window_ms, example.rescan_hop_ms, example.rescan_phase_ms) == (
        12_000,
        5_000,
        0,
    )
    assert example.transforms_policy == "rescan_only"


def test_all_schedule_options_classify_both_latency_measurements() -> None:
    options = schedule_options()
    assert len(options) == 18
    assert {(item.window_ms, item.hop_ms, item.phase_ms) for item in options} == {
        (window, hop, phase)
        for window in (6_000, 8_000, 12_000)
        for hop in (5_000, 9_000, 15_000)
        for phase in (0, hop // 2)
    }
    assert {(item.window_ms, item.hop_ms) for item in options if item.coverage_complete(3_000)} == {
        (8_000, 5_000),
        (12_000, 5_000),
        (12_000, 9_000),
    }
    assert {(item.window_ms, item.hop_ms) for item in options if item.coverage_complete(6_000)} == {
        (12_000, 5_000)
    }


def test_windows_pipeline_materialises_global_siblings_with_none_trial_ids(tmp_path: Path) -> None:
    media_dir = tmp_path / "global pipeline"
    pcm_path = media_dir / "decode" / "audio.pcm"
    pcm_path.parent.mkdir(parents=True)
    pcm_path.write_bytes(bytes(14_000 * 16 * 2))
    record_path = pcm_path.with_name("pcm.json")
    pcm_record = PcmRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        media_key="d" * 64,
        pcm=PcmAsset(
            path="decode/audio.pcm",
            sha256=sha256_file(pcm_path),
            sample_rate=16_000,
            channels=1,
            sample_format="s16le",
            duration_ms=14_000,
            ffprobe_duration_ms=14_000,
        ),
        decoder=DecoderInfo(ffmpeg_version="fixture", filtergraph="fixture"),
    )
    record_path.write_text(pcm_record.model_dump_json(), encoding="utf-8")
    decoded = DecodeResult(pcm_record, pcm_path, record_path, False)
    result = asyncio.run(
        generate_windows_async(
            decoded,
            media_dir,
            schedule=WindowSchedule(window_ms=12_000, hop_ms=13_000, phase_ms=0),
            transform_policy="global",
            transform_grid=TransformGrid(rates_e4=(10_800,), semitones=(1,)),
        )
    )
    by_start: dict[int, list] = {}
    for window in result.records:
        by_start.setdefault(window.start_ms, []).append(window)
        assert path_is_file(media_dir / window.wav_path)
        with wave.open(native_path(media_dir / window.wav_path), "rb") as handle:
            assert handle.getnframes() == 192_000
    assert set(by_start) == {0, 2_000}
    for siblings in by_start.values():
        none = next(item for item in siblings if item.transform.type == "none")
        assert {item.transform.type for item in siblings} == {
            "none",
            "resample",
            "tempo",
            "pitch",
        }
        assert all(item.logical_trial_id == none.id for item in siblings)


@pytest.mark.parametrize("transform", FACTORS, ids=lambda item: f"{item.type}-{item.rate_e4}")
def test_known_insertions_apply_every_undo_factor_with_exact_duration_and_maps(
    insertion_vectors: dict[str, tuple[int, int, int]], transform: Transform
) -> None:
    label = _label(transform)
    output_samples, input_samples, raw_samples = insertion_vectors[label]
    assert abs(output_samples - 192_000) <= 1
    # The published file is normalised to a fixed length, so the meaningful check is on the raw
    # FFmpeg output: assert the filtergraph itself lands inside an explicit per-family budget.
    assert abs(raw_samples - 192_000) <= FRAME_BUDGET_SAMPLES[transform.type], (
        f"{label}: raw FFmpeg output was {raw_samples} frames"
    )
    sample_map = sample_map_for_transform(
        transform.type,
        rate_e4=transform.rate_e4,
        semitones=transform.semitones,
    )
    first = Fraction(sample_map.b_samples)
    last = Fraction(sample_map.a_num * (output_samples - 1), sample_map.a_den)
    assert first == 0
    assert abs(last - (input_samples - 1)) <= 1
    assert sample_map.uncertainty_ms == (0 if transform.type == "resample" else 100)
    planned = plan_fixture_windows(
        media_key="f" * 64,
        duration_ms=30_000,
        schedule=WindowSchedule(window_ms=12_000, hop_ms=15_000, phase_ms=0),
        transform_policy="global",
    )
    record = next(item for item in planned if item.start_ms == 0 and item.transform == transform)
    expected_support = (
        12_000
        if transform.type == "pitch"
        else (12_000 * 10_000 + transform.rate_e4 // 2) // transform.rate_e4
    )
    assert record.support_ms == (0, expected_support)

    if transform.type == "resample":
        rate = transform.rate_e4 / 10_000
        assert transform_filtergraph(transform) == f"asetrate=16000/{rate:g},aresample=16000"
    elif transform.type == "tempo":
        rate = transform.rate_e4 / 10_000
        assert transform_filtergraph(transform) == f"atempo=1/{rate:g}"
    else:
        graph = transform_filtergraph(transform)
        assert graph is not None
        assert graph.startswith("asetrate=16000/")
        assert ",aresample=16000,atempo=" in graph


@pytest.mark.parametrize("transform", FACTORS, ids=lambda item: f"{item.type}-{item.rate_e4}")
def test_known_marker_lands_at_the_inverse_sample_map_after_the_production_undo(
    marker_vectors: dict[str, tuple[int, int]], transform: Transform
) -> None:
    """Content check: the map is verified against audio, not against its own arithmetic."""

    label = _label(transform)
    expected, observed = marker_vectors[label]
    assert expected > 0
    assert abs(observed - expected) <= MARKER_BUDGET_SAMPLES[transform.type], (
        f"{label}: marker landed at output sample {observed}, expected {expected}"
    )


@pytest.mark.parametrize("transform", FACTORS, ids=lambda item: f"{item.type}-{item.rate_e4}")
def test_transform_sibling_anchors_apply_the_measured_adapter_bias(transform: Transform) -> None:
    """Anchor bias vector: every factor's sibling anchors on its own support start, bias applied."""

    config, _ = load_provider_config(ROOT)
    assert config.measured
    assert config.adapter_bias_ms is not None
    assert config.adapter_bias_uncertainty_ms is not None
    planned = plan_fixture_windows(
        media_key="f" * 64,
        duration_ms=60_000,
        schedule=WindowSchedule(window_ms=12_000, hop_ms=15_000, phase_ms=0),
        transform_policy="global",
    )
    window = next(
        item for item in planned if item.start_ms == 15_000 and item.transform == transform
    )
    response = json.loads(read_text(ROOT / "tests" / "fixtures" / "shazam" / "response-match.json"))
    query = _shazam_query(window, config.version)
    observation = response_to_observation(
        response, query, window, config, "recognise/raw/vector.json", "f" * 64
    )
    assert observation.anchor is not None
    # response-match.json offsets are 45.25 s and 45.40 s: one cluster, median 45_325 ms.
    assert observation.anchor.mix_anchor_ms == window.support_ms[0] == 15_000
    assert observation.anchor.bias_applied_ms == config.adapter_bias_ms
    assert observation.anchor.ref_anchor_ms == 45_325 - config.adapter_bias_ms
    assert observation.anchor.uncertainty_ms >= config.adapter_bias_uncertainty_ms
    assert observation.anchor.reliable is True


@pytest.mark.parametrize(
    ("truth_name", "expected_rate_e4"),
    [
        *(
            (f"controlled-{index:03d}-pitch-{semitone:+d}", 10_000)
            for index, semitone in zip(range(6, 10), (-2, -1, 1, 2), strict=True)
        ),
        *(
            (f"controlled-{index:03d}-tempo-{rate}", rate)
            for index, rate in zip(range(10, 14), (9200, 9600, 10400, 10800), strict=True)
        ),
        *(
            (f"controlled-{index:03d}-resample-{rate}", rate)
            for index, rate in zip(range(14, 18), (9200, 9600, 10400, 10800), strict=True)
        ),
    ],
)
def test_local_fixture_anchor_slope_matches_temporal_hypothesis_within_two_percent(
    truth_name: str, expected_rate_e4: int
) -> None:
    truth = GroundTruthRecord.model_validate_json(
        read_text(
            ROOT / "data" / "corpus" / "controlled-synth-1" / truth_name / "ground_truth.json"
        )
    )
    windows = plan_fixture_windows(
        media_key=truth.source.media_key,
        duration_ms=truth.source.duration_ms,
        schedule=WindowSchedule(window_ms=6_000, hop_ms=3_000, phase_ms=0),
        transform_policy="global",
        transform_grid=DEFAULT_TRANSFORM_GRID,
    )
    observations = recognise_fixture_windows_in_memory(
        media_key=truth.source.media_key,
        truth=truth,
        windows=windows,
        source_offset_ms=5_000,
        rate_tolerance_e4=300,
    )
    identity = build_identity_graph(truth.source.media_key, observations)
    # Exclude the catalogue's decoy release: only the true recording's slope is under test.
    decoy_candidates = {
        identity.observation_candidates[item.id]
        for item in observations
        if item.raw_label.artist == DECOY_ARTIST and item.id in identity.observation_candidates
    }
    selection = select_logical_trial_points(observations, identity.observation_candidates)
    alignments = align_selected_points(selection)
    segments = [
        segment
        for candidate, occurrences in alignments.items()
        if candidate not in decoy_candidates
        for occurrence in occurrences
        for segment in occurrence.segments
        if segment.contract().n_obs >= 3
    ]
    assert segments
    assert abs(segments[0].rate_e4 - expected_rate_e4) <= expected_rate_e4 * 2 // 100


def test_policy_window_counts_and_committed_benchmark_contract() -> None:
    media_key = "a" * 64
    schedule = WindowSchedule(window_ms=12_000, hop_ms=5_000, phase_ms=0)
    off = plan_fixture_windows(
        media_key=media_key,
        duration_ms=30_000,
        schedule=schedule,
        transform_policy="off",
    )
    rescan_only = plan_fixture_windows(
        media_key=media_key,
        duration_ms=30_000,
        schedule=schedule,
        transform_policy="rescan_only",
    )
    global_windows = plan_fixture_windows(
        media_key=media_key,
        duration_ms=30_000,
        schedule=schedule,
        transform_policy="global",
        transform_grid=TransformGrid(),
    )
    assert len(off) == len(rescan_only)
    assert len(global_windows) > len(off)
    assert {item.logical_trial_id for item in global_windows} == {item.id for item in off}

    # The decision record is committed, so a missing file is a failure, not a skip.
    report_path = ROOT / "data" / "corpus" / "controlled-synth-1" / "transforms-schedule.json"
    assert report_path.is_file(), "the committed Stage 4b decision record is missing"
    payload = json.loads(read_text(report_path))
    assert len(payload["schedules"]) == 18
    assert payload["selected_defaults"] == {
        "decision": payload["selected_defaults"]["decision"],
        "grid": {
            "rate_e4": [9_200, 9_600, 10_400, 10_800],
            "semitones": [-2, -1, 1, 2],
            "types": ["resample", "tempo"],
        },
        # rev 5.2: generation 0 stays 12/9/0; 12/5/0 is recorded as the rescan policy.
        "generation_zero_schedule": {"hop_ms": 9_000, "phase_ms": 0, "window_ms": 12_000},
        "rescan_policy": {"hop_ms": 5_000, "phase_ms": 0, "window_ms": 12_000},
        "best_schedule_any_window_length": payload["selected_defaults"][
            "best_schedule_any_window_length"
        ],
        "transforms_policy": "rescan_only",
    }
    assert all(
        "hypothesis_false_match_rate_e4" in row[policy]
        for row in payload["schedules"]
        for policy in ("off", "global")
    )
    example = AppConfig.load(ROOT / "id-detector.example.toml")
    assert (example.window_ms, example.hop_ms, example.phase_ms) == (
        payload["selected_defaults"]["generation_zero_schedule"]["window_ms"],
        payload["selected_defaults"]["generation_zero_schedule"]["hop_ms"],
        payload["selected_defaults"]["generation_zero_schedule"]["phase_ms"],
    )
    assert (example.rescan_window_ms, example.rescan_hop_ms, example.rescan_phase_ms) == (
        payload["selected_defaults"]["rescan_policy"]["window_ms"],
        payload["selected_defaults"]["rescan_policy"]["hop_ms"],
        payload["selected_defaults"]["rescan_policy"]["phase_ms"],
    )


def test_only_the_active_l_min_gates_schedule_selection() -> None:
    """rev 5.2: superseded configs are loaded from disk and reported, never used as a gate."""

    payload = json.loads(
        read_text(ROOT / "data" / "corpus" / "controlled-synth-1" / "transforms-schedule.json")
    )
    measurements = payload["provider_measurements"]
    active_l = measurements["active_l_ms_used_as_gate"]
    assert measurements["active_config"] == "shazam-v3.json"
    assert active_l == 3_000
    superseded = {item["config"] for item in measurements["superseded_configs"]}
    assert superseded == {"shazam-v1.json", "shazam-v2.json"}
    assert 6_000 in measurements["coverage_reported_for_l_ms"]
    for row in payload["schedules"]:
        schedule = WindowSchedule(
            window_ms=row["window_ms"], hop_ms=row["hop_ms"], phase_ms=row["phase_ms"]
        )
        assert row["coverage"]["complete_at_active_l"] == schedule.coverage_complete(active_l)
        gating = [item for item in row["coverage"]["reported"] if item["gates_selection"]]
        assert [item["l_ms"] for item in gating] == [active_l]
    # 12/9 is complete at the active 3 s measurement and eliminated only by the superseded 6 s
    # one; it must remain in the eligible pool.
    twelve_nine = next(
        row for row in payload["schedules"] if (row["window_ms"], row["hop_ms"]) == (12_000, 9_000)
    )
    assert twelve_nine["coverage"]["complete_at_active_l"] is True


def test_false_match_metric_can_be_non_zero_and_off_policy_stays_clean() -> None:
    """The decoy identity makes the false-match predicate satisfiable (review P1)."""

    # A 1.08x set undone with the 0.96 resample hypothesis leaves a 1.125x residual: the decoy.
    set_id = "controlled-017-resample-10800"
    assert transform_matches_fixture_decoy(
        set_id, Transform(type="resample", rate_e4=9_600, semitones=0)
    )
    # No untransformed query can reach the decoy, on this set or on any other.
    for name in (set_id, "controlled-004-length-20s", "controlled-016-resample-10400"):
        assert not transform_matches_fixture_decoy(
            name, Transform(type="none", rate_e4=10_000, semitones=0)
        )
    truth = GroundTruthRecord.model_validate_json(
        read_text(ROOT / "data" / "corpus" / "controlled-synth-1" / set_id / "ground_truth.json")
    )
    schedule = WindowSchedule(window_ms=12_000, hop_ms=9_000, phase_ms=0)
    for policy, expect_decoy in (("off", False), ("global", True)):
        windows = plan_fixture_windows(
            media_key=truth.source.media_key,
            duration_ms=truth.source.duration_ms,
            schedule=schedule,
            transform_policy=policy,  # type: ignore[arg-type]
        )
        observations = recognise_fixture_windows_in_memory(
            media_key=truth.source.media_key,
            truth=truth,
            windows=windows,
            source_offset_ms=0,
            rate_tolerance_e4=300,
        )
        decoys = [
            item
            for item in observations
            if item.status == "match" and item.raw_label.artist == DECOY_ARTIST
        ]
        assert bool(decoys) is expect_decoy
        assert all(
            item.transform is not None and item.transform.type == "resample" for item in decoys
        )


def test_sibling_observations_have_unique_ids_and_one_vote_per_distinct_window(
    tmp_path: Path,
) -> None:
    """Byte-identical sibling WAVs share a query; their observations must not share an id."""

    media_dir = tmp_path / "silence"
    pcm_path = media_dir / "decode" / "audio.pcm"
    pcm_path.parent.mkdir(parents=True)
    pcm_path.write_bytes(bytes(20_000 * 16 * 2))
    record_path = pcm_path.with_name("pcm.json")
    pcm_record = PcmRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        media_key="a" * 64,
        pcm=PcmAsset(
            path="decode/audio.pcm",
            sha256=sha256_file(pcm_path),
            sample_rate=16_000,
            channels=1,
            sample_format="s16le",
            duration_ms=20_000,
            ffprobe_duration_ms=20_000,
        ),
        decoder=DecoderInfo(ffmpeg_version="fixture", filtergraph="fixture"),
    )
    record_path.write_text(pcm_record.model_dump_json(), encoding="utf-8")
    decoded = DecodeResult(pcm_record, pcm_path, record_path, False)

    async def scenario() -> tuple[int, int, int, list[str]]:
        import httpx

        windows = await generate_windows_async(
            decoded,
            media_dir,
            schedule=WindowSchedule(window_ms=12_000, hop_ms=5_000, phase_ms=0),
            transform_policy="global",
        )
        config, _ = load_provider_config(ROOT)

        async def handler(request: object) -> object:
            return httpx.Response(
                200,
                json=json.loads(
                    read_text(ROOT / "tests" / "fixtures" / "shazam" / "response-match.json")
                ),
            )

        result = await recognise_generation_zero(
            media_key="a" * 64,
            media_dir=media_dir,
            windows=windows,
            project_root=ROOT,
            run_id="sibling-uniqueness",
            max_requests=10,
            adapter=ShazamAdapter(
                config,
                limiter=TokenBucket(rate_per_minute=1_000_000),
                transport=httpx.MockTransport(handler),
            ),
        )
        stored = [line for line in read_text(result.observations_path).splitlines() if line.strip()]
        return (
            len(windows.records),
            len({record.wav_sha256 for record in windows.records}),
            len(result.observations),
            [json.loads(line)["id"] for line in stored],
        )

    window_count, distinct_wavs, observation_count, stored_ids = asyncio.run(scenario())
    assert distinct_wavs == 1, "digital silence must collapse every sibling onto one cache key"
    assert observation_count == window_count
    assert len(set(stored_ids)) == len(stored_ids) == window_count


def test_majority_vote_counts_each_window_once(tmp_path: Path) -> None:
    """The per-trial majority vote must be over distinct windows, never duplicated records."""

    truth = GroundTruthRecord.model_validate_json(
        read_text(
            ROOT
            / "data"
            / "corpus"
            / "controlled-synth-1"
            / "controlled-004-length-20s"
            / "ground_truth.json"
        )
    )
    windows = plan_fixture_windows(
        media_key=truth.source.media_key,
        duration_ms=truth.source.duration_ms,
        schedule=WindowSchedule(window_ms=12_000, hop_ms=9_000, phase_ms=0),
        transform_policy="global",
    )
    observations = recognise_fixture_windows_in_memory(
        media_key=truth.source.media_key,
        truth=truth,
        windows=windows,
        source_offset_ms=0,
        rate_tolerance_e4=300,
    )
    assert len({item.id for item in observations}) == len(observations)
    window_by_id = {window.id: window for window in windows}
    by_trial: dict[tuple[str, str], list[tuple[int, int, str]]] = {}
    for item in observations:
        if item.status != "match":
            continue
        source = str(item.native.get("simultaneous_source", "primary"))
        assert item.transform is not None
        by_trial.setdefault((item.logical_trial_id, source), []).append(
            (item.support_ms[0], item.support_ms[1], item.transform.model_dump_json())
        )
    assert by_trial
    for key, variants in by_trial.items():
        assert len(set(variants)) == len(variants), f"{key} votes twice for one window"
        assert window_by_id[key[0]].transform.type == "none"
