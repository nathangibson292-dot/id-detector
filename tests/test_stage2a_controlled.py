from __future__ import annotations

import asyncio
import json
import os
import wave
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

import id_detector.benchmark.controlled as controlled_module
from id_detector.benchmark.controlled import (
    AUDIBLE_RULE,
    _ffmpeg_to_wav,
    _single_filter,
    _wave_bytes,
    controlled_audible_frames,
    render_controlled,
    synthesize_test_sources,
)
from id_detector.contracts import GroundTruthRecord
from id_detector.io import atomic_write_bytes, native_path, path_is_file, read_bytes


@pytest.fixture(scope="module")
def controlled_renders(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, Path, Path, dict, dict]:
    root = tmp_path_factory.mktemp("controlled")
    sources = root / "sources"
    synthesize_test_sources(sources, seed=20260904, count=4)
    first = root / "first"
    second = root / "second"
    first_audio = root / "first-audio"
    second_audio = root / "second-audio"
    first.mkdir()
    first_audio.mkdir()
    (first / "stale.json").write_text("stale", encoding="utf-8")
    (first_audio / "stale.wav").write_bytes(b"stale")
    asyncio.run(render_controlled(sources, first, seed=20260904, audio_dir=first_audio))
    asyncio.run(render_controlled(sources, second, seed=20260904, audio_dir=second_audio))
    assert not (first / "stale.json").exists()
    assert not (first_audio / "stale.wav").exists()
    first_manifest = json.loads((first / "render_manifest.json").read_text(encoding="utf-8"))
    second_manifest = json.loads((second / "render_manifest.json").read_text(encoding="utf-8"))
    return first, second, first_audio, second_audio, first_manifest, second_manifest


def test_controlled_audible_rule_hysteresis_minimum_run_and_silence() -> None:
    mix = [(100 * (index + 1), Decimal("-10")) for index in range(30)]
    exactly_two_seconds = [
        (at, Decimal("-30") if index < 20 else Decimal("-40")) for index, (at, _) in enumerate(mix)
    ]
    audible = controlled_audible_frames(exactly_two_seconds, mix)
    assert len(audible) == 20
    assert audible[0] == 100
    assert audible[-1] == 2_000

    too_short = [
        (at, Decimal("-30") if index < 19 else Decimal("-40")) for index, (at, _) in enumerate(mix)
    ]
    assert controlled_audible_frames(too_short, mix) == []

    silent_mix = [(at, Decimal("-60")) for at, _ in mix]
    loud_stem = [(at, Decimal("-20")) for at, _ in mix]
    assert controlled_audible_frames(loud_stem, silent_mix) == []

    # -23 dB lies inside hysteresis: it retains the prior on state until an off frame arrives.
    levels = [Decimal("-20")] * 20 + [Decimal("-23")] * 3 + [Decimal("-30")] * 7
    stem = [(at, mix_lufs + level) for (at, mix_lufs), level in zip(mix, levels, strict=True)]
    hysteresis = controlled_audible_frames(stem, mix)
    assert hysteresis[-1] >= 2_300


def test_default_render_covers_all_cases_and_valid_truth(
    controlled_renders: tuple[Path, Path, Path, Path, dict, dict],
) -> None:
    first, _, first_audio, _, manifest, _ = controlled_renders
    assert manifest["set_count"] == 25
    assert manifest["boundary_count"] >= 20
    assert manifest["audible_rule"] == AUDIBLE_RULE
    kinds = [item["kind"] for item in manifest["sets"]]
    assert {
        "pitch",
        "tempo",
        "resample",
        "eq",
        "loop",
        "cue_jump",
        "repeated_section",
        "drift",
        "crossfade",
    } <= set(kinds)
    assert {
        item["parameters"]["rate_e4"] for item in manifest["sets"] if item["kind"] == "tempo"
    } == {9_200, 9_600, 10_400, 10_800}
    assert {
        item["parameters"]["rate_e4"] for item in manifest["sets"] if item["kind"] == "resample"
    } == {9_200, 9_600, 10_400, 10_800}
    assert {
        item["parameters"]["semitones"] for item in manifest["sets"] if item["kind"] == "pitch"
    } == {-2, -1, 1, 2}
    assert {
        item["parameters"]["gain_db"] for item in manifest["sets"] if item["kind"] == "crossfade"
    } == {0, -6, -12}
    assert {
        item["parameters"]["episode_ms"] for item in manifest["sets"] if item["kind"] == "length"
    } == {3_000, 5_000, 10_000, 20_000, 30_000}

    for item in manifest["sets"]:
        set_dir = first / item["set_id"]
        truth = GroundTruthRecord.model_validate_json(
            (set_dir / "ground_truth.json").read_text(encoding="utf-8")
        )
        assert truth.stratum == "controlled"
        assert all(episode.audible_rule == AUDIBLE_RULE for episode in truth.episodes)
        with wave.open(native_path(first_audio / item["set_id"] / "mix.wav"), "rb") as audio:
            actual_duration_ms = audio.getnframes() * 1_000 // audio.getframerate()
        assert actual_duration_ms == item["duration_ms"]
        assert all(episode.end_ms_range[1] <= actual_duration_ms for episode in truth.episodes)


def test_render_is_byte_deterministic_for_same_seed(
    controlled_renders: tuple[Path, Path, Path, Path, dict, dict],
) -> None:
    first, second, first_audio, second_audio, first_manifest, second_manifest = controlled_renders
    assert first_manifest == second_manifest
    for item in first_manifest["sets"]:
        relative = Path(item["set_id"])
        assert (first / relative / "ground_truth.json").read_bytes() == (
            second / relative / "ground_truth.json"
        ).read_bytes()
        assert read_bytes(first_audio / relative / "mix.wav") == read_bytes(
            second_audio / relative / "mix.wav"
        )


def test_committed_controlled_fixture_is_json_only_and_matches_fresh_render(
    controlled_renders: tuple[Path, Path, Path, Path, dict, dict],
) -> None:
    first, _, _, _, fresh_manifest, _ = controlled_renders
    repository = Path(__file__).resolve().parents[1]
    fixture_root = repository / "data" / "fixtures" / "controlled" / "stage-2a"
    assert not [
        path
        for path in fixture_root.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".wav", ".mp3", ".flac", ".ogg"}
    ]
    committed = json.loads((fixture_root / "render_manifest.json").read_text(encoding="utf-8"))
    assert committed == fresh_manifest
    for item in committed["sets"]:
        relative = Path(item["set_id"]) / "ground_truth.json"
        assert (fixture_root / relative).read_bytes() == (first / relative).read_bytes()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-path regression")
def test_controlled_ffmpeg_writes_an_extended_length_path(tmp_path: Path) -> None:
    source_dir = tmp_path / "sources"
    source = synthesize_test_sources(source_dir, seed=7, count=3)[0]
    destination = tmp_path
    while len(str(destination.resolve())) <= 280:
        destination /= "controlled-long-path-segment-0123456789"
    output = destination / "result.wav"
    asyncio.run(
        _ffmpeg_to_wav(
            [source],
            "[0:a]atrim=duration=1,asetpts=PTS-STARTPTS[out]",
            output,
        )
    )
    assert path_is_file(output)


def test_failed_render_keeps_previously_published_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = tmp_path / "sources"
    synthesize_test_sources(sources, seed=17, count=3)
    artifacts = tmp_path / "artifacts"
    audio = tmp_path / "audio"
    artifacts.mkdir()
    audio.mkdir()
    (artifacts / "old-manifest.json").write_text("old", encoding="utf-8")
    (audio / "old-mix.wav").write_bytes(b"old")

    async def fail_truth(**_: object) -> GroundTruthRecord:
        raise RuntimeError("injected render failure")

    monkeypatch.setattr(controlled_module, "_truth_for_render", fail_truth)
    with pytest.raises(RuntimeError, match="injected render failure"):
        asyncio.run(render_controlled(sources, artifacts, seed=17, audio_dir=audio))
    assert (artifacts / "old-manifest.json").read_text(encoding="utf-8") == "old"
    assert (audio / "old-mix.wav").read_bytes() == b"old"
    assert not list(tmp_path.glob("*.staging"))


@pytest.mark.parametrize("kind", ["resample", "tempo"])
@pytest.mark.parametrize("rate_e4", [9_200, 9_600, 10_400, 10_800])
def test_rendered_rate_transform_maps_known_markers_and_pitch(
    tmp_path: Path, kind: str, rate_e4: int
) -> None:
    rate = rate_e4 / 10_000
    _assert_rendered_transform_probe(
        tmp_path,
        {"kind": kind, "episode_ms": 12_000, "rate_e4": rate_e4},
        source_scale=rate,
        expected_pitch_hz=440 * (rate if kind == "resample" else 1),
    )


@pytest.mark.parametrize("semitones", [-2, -1, 1, 2])
def test_rendered_pitch_transform_maps_known_markers_and_pitch(
    tmp_path: Path, semitones: int
) -> None:
    pitch = 2 ** (semitones / 12)
    _assert_rendered_transform_probe(
        tmp_path,
        {"kind": "pitch", "episode_ms": 12_000, "semitones": semitones},
        source_scale=1,
        expected_pitch_hz=440 * pitch,
    )


def _assert_rendered_transform_probe(
    tmp_path: Path,
    case: dict,
    *,
    source_scale: float,
    expected_pitch_hz: float,
) -> None:
    sample_rate = 16_000
    offset_seconds = 1
    samples = np.arange(18 * sample_rate, dtype=np.float64)
    signal = 0.06 * np.sin(2 * np.pi * 440 * samples / sample_rate)

    def marker(at_seconds: float, duration_seconds: float, amplitude: float) -> None:
        start = round((offset_seconds + at_seconds) * sample_rate)
        end = round((offset_seconds + at_seconds + duration_seconds) * sample_rate)
        signal[start:end] += amplitude

    source_span_seconds = 12 * source_scale
    marker(0, 0.08 * source_scale, 0.35)
    marker(3 * source_scale, 0.05 * source_scale, 0.8)
    marker(source_span_seconds - 0.08 * source_scale, 0.08 * source_scale, 0.35)
    source = tmp_path / "probe-source.wav"
    atomic_write_bytes(source, _wave_bytes(signal))
    output = tmp_path / "probe-output.wav"
    asyncio.run(_ffmpeg_to_wav([source], _single_filter(case, 1_000), output))

    with wave.open(native_path(output), "rb") as audio:
        assert audio.getframerate() == sample_rate
        assert audio.getnframes() == 14 * sample_rate
        rendered = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(
            np.float64
        )
    active = rendered[sample_rate : 13 * sample_rate] / 32768
    baseline = np.sqrt(np.mean(active[5 * sample_rate : 7 * sample_rate] ** 2))
    assert np.sqrt(np.mean(active[: round(0.1 * sample_rate)] ** 2)) > baseline * 2
    assert np.sqrt(np.mean(active[-round(0.1 * sample_rate) :] ** 2)) > baseline * 2

    window = max(1, round(0.03 * sample_rate))
    envelope = np.convolve(np.abs(active), np.ones(window) / window, mode="same")
    anchor_ms = int(np.argmax(envelope) * 1_000 / sample_rate)
    assert abs(anchor_ms - 3_000) <= 100

    frequency_slice = active[5 * sample_rate : 7 * sample_rate] * np.hanning(2 * sample_rate)
    spectrum = np.abs(np.fft.rfft(frequency_slice))
    frequencies = np.fft.rfftfreq(len(frequency_slice), 1 / sample_rate)
    dominant_hz = frequencies[int(np.argmax(spectrum[1:])) + 1]
    assert abs(dominant_hz - expected_pitch_hz) <= 3
