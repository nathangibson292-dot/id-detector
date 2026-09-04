"""Generation-0 scheduling and sample-exact WAV materialisation."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    SampleMap,
    Transform,
    WindowRecord,
    compose_natural_key,
    make_id,
    sort_records,
)
from id_detector.decode import BYTES_PER_SAMPLE, SAMPLE_RATE, DecodeResult
from id_detector.io import (
    atomic_write_bytes,
    canonical_json_bytes,
    native_path,
    path_is_file,
    path_size,
    read_text,
    sha256_file,
    verify_completion_sidecar,
    write_completion_sidecar,
)
from id_detector.semantics import transform_spec

WINDOW_MS = 12_000
HOP_MS = 9_000
SAMPLES_PER_MS = SAMPLE_RATE // 1000


@dataclass(frozen=True)
class ScheduledWindow:
    start_ms: int
    output_ms: int
    reason: str


@dataclass(frozen=True)
class WindowsResult:
    records: tuple[WindowRecord, ...]
    record_path: Path
    cached: bool


def generation_zero_schedule(duration_ms: int) -> list[ScheduledWindow]:
    if duration_ms <= 0:
        raise ValueError("media is too short to form a one-millisecond window")
    if duration_ms <= WINDOW_MS:
        return [ScheduledWindow(0, duration_ms, "schedule")]
    starts = list(range(0, duration_ms - WINDOW_MS + 1, HOP_MS))
    tail_start = duration_ms - WINDOW_MS
    result = [ScheduledWindow(start, WINDOW_MS, "schedule") for start in starts]
    if starts[-1] != tail_start:
        result.append(ScheduledWindow(tail_start, WINDOW_MS, "tail"))
    return result


def transform_slice_sample_count(
    output_ms: int,
    transform_type: str,
    *,
    rate_e4: int = 10_000,
    semitones: int = 0,
    sample_rate: int = SAMPLE_RATE,
) -> int:
    """Return the original-input sample count required for a transformed output span."""

    spec = transform_spec(transform_type, rate_e4=rate_e4, semitones=semitones)
    numerator = output_ms * sample_rate * spec.a_num
    denominator = 1000 * spec.a_den
    quotient, remainder = divmod(numerator, denominator)
    return quotient + int(remainder * 2 >= denominator)


def sample_map_for_transform(
    transform_type: str, *, rate_e4: int = 10_000, semitones: int = 0
) -> SampleMap:
    spec = transform_spec(transform_type, rate_e4=rate_e4, semitones=semitones)
    return SampleMap(
        a_num=spec.a_num,
        a_den=spec.a_den,
        b_samples=spec.b_samples,
        uncertainty_ms=spec.uncertainty_ms,
    )


def _write_wav_slice(pcm_path: Path, wav_path: Path, start_sample: int, samples: int) -> None:
    os.makedirs(native_path(wav_path.parent), exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{wav_path.name}.",
        suffix=".tmp",
        dir=native_path(wav_path.parent),
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        with open(native_path(pcm_path), "rb") as source:
            source.seek(start_sample * BYTES_PER_SAMPLE)
            content = source.read(samples * BYTES_PER_SAMPLE)
        if len(content) != samples * BYTES_PER_SAMPLE:
            raise ValueError("PCM ended before the scheduled window's final sample")
        with (
            open(native_path(temporary), "wb") as raw_target,
            wave.open(raw_target, "wb") as target,
        ):
            target.setnchannels(1)
            target.setsampwidth(BYTES_PER_SAMPLE)
            target.setframerate(SAMPLE_RATE)
            target.writeframes(content)
        os.replace(native_path(temporary), native_path(wav_path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(native_path(temporary))


def _load_jsonl(path: Path) -> tuple[WindowRecord, ...]:
    return tuple(
        WindowRecord.model_validate_json(line)
        for line in read_text(path).splitlines()
        if line.strip()
    )


def generate_windows(decoded: DecodeResult, media_dir: Path) -> WindowsResult:
    """Build only generation 0's ``none`` hypotheses, as assigned to Stage 1."""

    record_path = media_dir / "windows" / "windows.gen0.jsonl"
    upstream = {
        "decode/pcm.json": decoded.record_path,
        decoded.record.pcm.path: decoded.pcm_path,
    }
    if path_is_file(record_path):
        try:
            records = _load_jsonl(record_path)
            valid_wavs = all(
                path_is_file(media_dir / record.wav_path)
                and sha256_file(media_dir / record.wav_path) == record.wav_sha256
                for record in records
            )
            if records and valid_wavs and verify_completion_sidecar(record_path, upstream).valid:
                return WindowsResult(records, record_path, True)
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    total_bytes = path_size(decoded.pcm_path)
    if total_bytes % BYTES_PER_SAMPLE:
        raise ValueError("PCM contains a partial sample")
    total_samples = total_bytes // BYTES_PER_SAMPLE
    duration_ms = total_samples // SAMPLES_PER_MS
    schedule = generation_zero_schedule(duration_ms)
    records: list[WindowRecord] = []
    for item in schedule:
        start_sample = item.start_ms * SAMPLES_PER_MS
        sample_count = transform_slice_sample_count(item.output_ms, "none")
        wav_path = media_dir / "windows" / "gen0" / f"{item.start_ms:010d}-none.wav"
        _write_wav_slice(decoded.pcm_path, wav_path, start_sample, sample_count)
        support_end = item.start_ms + item.output_ms
        values = {
            "generation": 0,
            "start_ms": item.start_ms,
            "support_ms": [item.start_ms, support_end],
            "transform": {"type": "none", "rate_e4": 10_000, "semitones": 0},
        }
        window_id = make_id(
            decoded.record.media_key, "window", compose_natural_key("window", values)
        )
        record = WindowRecord(
            schema_version=SCHEMA_VERSION,
            generated_by=GENERATED_BY,
            id=window_id,
            generation=0,
            start_ms=item.start_ms,
            support_ms=(item.start_ms, support_end),
            output_ms=item.output_ms,
            transform=Transform(type="none", rate_e4=10_000, semitones=0),
            sample_map=sample_map_for_transform("none"),
            wav_path=wav_path.relative_to(media_dir).as_posix(),
            wav_sha256=sha256_file(wav_path),
            logical_trial_id=window_id,
            reason=item.reason,
            rescan_request_id=None,
        )
        records.append(record)

    ordered = tuple(sort_records(records))
    content = b"\n".join(canonical_json_bytes(record) for record in ordered) + b"\n"
    atomic_write_bytes(record_path, content)
    write_completion_sidecar(record_path, upstream)
    return WindowsResult(ordered, record_path, False)
