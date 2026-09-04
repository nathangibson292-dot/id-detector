from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import wave
from array import array
from pathlib import Path

import pytest

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    DecoderInfo,
    PcmAsset,
    PcmRecord,
)
from id_detector.decode import DecodeResult, decode
from id_detector.ingest import ingest
from id_detector.io import native_path, path_is_file, read_text, sha256_file
from id_detector.process import ProcessError
from id_detector.recognise import load_provider_config, recognise_generation_zero
from id_detector.shazam import ShazamAdapter, TokenBucket
from id_detector.windows import (
    generate_windows,
    generation_zero_schedule,
    sample_map_for_transform,
    transform_slice_sample_count,
)


def _decoded(tmp_path: Path, duration_ms: int) -> tuple[DecodeResult, list[int], Path]:
    media_dir = tmp_path / ("ユニコード and spaces " + "x" * 90)
    decode_dir = media_dir / "decode"
    decode_dir.mkdir(parents=True)
    pcm_path = decode_dir / "audio.pcm"
    sample_values = [index % 32767 for index in range(duration_ms * 16)]
    values = array("h", sample_values)
    pcm_path.write_bytes(values.tobytes())
    record_path = decode_dir / "pcm.json"
    record = PcmRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        media_key="a" * 64,
        pcm=PcmAsset(
            path="decode/audio.pcm",
            sha256=sha256_file(pcm_path),
            sample_rate=16_000,
            channels=1,
            sample_format="s16le",
            duration_ms=duration_ms,
            ffprobe_duration_ms=duration_ms,
        ),
        decoder=DecoderInfo(ffmpeg_version="fixture", filtergraph="fixture"),
    )
    record_path.write_text(record.model_dump_json(), encoding="utf-8")
    return DecodeResult(record, pcm_path, record_path, False), sample_values, media_dir


@pytest.mark.parametrize(
    ("duration_ms", "expected"),
    [
        (1, [(0, 1, "schedule")]),
        (11_999, [(0, 11_999, "schedule")]),
        (12_000, [(0, 12_000, "schedule")]),
        (12_001, [(0, 12_000, "schedule"), (1, 12_000, "tail")]),
        (
            31_000,
            [
                (0, 12_000, "schedule"),
                (9_000, 12_000, "schedule"),
                (18_000, 12_000, "schedule"),
                (19_000, 12_000, "tail"),
            ],
        ),
    ],
)
def test_generation_zero_schedule_tail_and_short_rule(
    duration_ms: int, expected: list[tuple[int, int, str]]
) -> None:
    actual = generation_zero_schedule(duration_ms)
    assert [(item.start_ms, item.output_ms, item.reason) for item in actual] == expected


def test_wav_slices_use_exact_sample_indexes_and_support_maps(tmp_path: Path) -> None:
    decoded, samples, media_dir = _decoded(tmp_path, 12_001)
    result = generate_windows(decoded, media_dir)
    assert [(item.start_ms, item.support_ms, item.reason) for item in result.records] == [
        (0, (0, 12_000), "schedule"),
        (1, (1, 12_001), "tail"),
    ]
    for record in result.records:
        with wave.open(str(media_dir / record.wav_path), "rb") as handle:
            frames = array("h")
            frames.frombytes(handle.readframes(handle.getnframes()))
        start = record.start_ms * 16
        assert frames.tolist() == samples[start : start + record.output_ms * 16]
        assert record.logical_trial_id == record.id
        assert record.sample_map.a_num == record.sample_map.a_den == 1
    assert json.loads(result.record_path.read_text(encoding="utf-8").splitlines()[0])


def test_transform_slice_and_sample_map_helpers_cover_stage_zero_vectors() -> None:
    assert transform_slice_sample_count(12_000, "none") == 192_000
    assert transform_slice_sample_count(12_000, "resample", rate_e4=10_800) == 177_778
    assert transform_slice_sample_count(12_000, "tempo", rate_e4=9_200) == 208_696
    assert transform_slice_sample_count(12_000, "pitch", semitones=2) == 192_000
    assert sample_map_for_transform("resample", rate_e4=10_800).model_dump() == {
        "a_num": 10_000,
        "a_den": 10_800,
        "b_samples": 0,
        "uncertainty_ms": 0,
    }


def _write_wav(path: Path, milliseconds: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(bytes(milliseconds * 16 * 2))


def test_short_media_decodes_and_corrupt_media_is_rejected(tmp_path: Path) -> None:
    short = tmp_path / "short audio.wav"
    _write_wav(short, 250)
    ingested = asyncio.run(ingest(str(short), tmp_path / "short work"))
    decoded = asyncio.run(decode(ingested))
    windows = generate_windows(decoded, ingested.media_dir)
    assert decoded.record.pcm.duration_ms == 250
    assert windows.records[0].output_ms == 250

    corrupt = tmp_path / "corrupt media.bin"
    corrupt.write_bytes(b"this is not audio")
    with pytest.raises(ProcessError):
        asyncio.run(ingest(str(corrupt), tmp_path / "corrupt work"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows long-path acceptance test")
def test_full_stage1_path_exceeds_max_path_and_cleans_up(tmp_path: Path) -> None:
    async def scenario(work_root: Path, source: Path) -> Path:
        ingested = await ingest(str(source), work_root)
        assert len(str(ingested.media_dir.resolve())) > 260
        decoded = await decode(ingested)
        windows = generate_windows(decoded, ingested.media_dir)
        config, _ = load_provider_config(tmp_path)

        async def handler(request: object) -> object:
            return __import__("httpx").Response(200, json={"matches": []})

        result = await recognise_generation_zero(
            media_key=ingested.record.media_key,
            media_dir=ingested.media_dir,
            windows=windows,
            project_root=tmp_path,
            run_id="long-path",
            max_requests=5,
            adapter=ShazamAdapter(
                config,
                limiter=TokenBucket(rate_per_minute=1_000_000),
                transport=__import__("httpx").MockTransport(handler),
            ),
        )
        assert path_is_file(result.raw_index_path)
        raw_index = json.loads(read_text(result.raw_index_path))
        assert len(raw_index) == 1
        assert path_is_file(ingested.media_dir / raw_index[0]["path"])
        assert path_is_file(ingested.media_dir / "jobs.sqlite")
        leftovers = [
            name
            for _, directories, files in os.walk(native_path(work_root))
            for name in directories + files
            if name.startswith(".ingest-") or name.endswith(".tmp")
        ]
        assert leftovers == []
        return ingested.media_dir

    source = tmp_path / "short source.wav"
    _write_wav(source, 250)
    work_root = tmp_path
    for index in range(4):
        work_root /= f"long segment {index} " + "x" * 45
    media_dir = asyncio.run(scenario(work_root, source))
    assert os.path.exists(native_path(media_dir))
    resolved_work = work_root.resolve()
    assert resolved_work.is_relative_to(tmp_path.resolve())
    shutil.rmtree(native_path(resolved_work))
    assert not os.path.exists(native_path(resolved_work))
