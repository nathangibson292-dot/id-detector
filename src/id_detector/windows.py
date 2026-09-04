"""Generation-0 scheduling and sample-exact WAV materialisation."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import wave
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Literal

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
from id_detector.process import run_process
from id_detector.providers.base import (
    DEFAULT_HOP_MS,
    DEFAULT_PHASE_MS,
    DEFAULT_TRANSFORM_RATES_E4,
    DEFAULT_TRANSFORM_SEMITONES,
    DEFAULT_WINDOW_MS,
    TransformPolicy,
)
from id_detector.semantics import transform_spec

WINDOW_MS = DEFAULT_WINDOW_MS
HOP_MS = DEFAULT_HOP_MS
SAMPLES_PER_MS = SAMPLE_RATE // 1000
TransformType = Literal["none", "resample", "tempo", "pitch"]


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


@dataclass(frozen=True)
class WindowSchedule:
    window_ms: int = WINDOW_MS
    hop_ms: int = HOP_MS
    phase_ms: int = DEFAULT_PHASE_MS

    def __post_init__(self) -> None:
        if not 0 < self.window_ms <= 12_000:
            raise ValueError("window_ms must be in 1..12000")
        if self.hop_ms <= 0:
            raise ValueError("hop_ms must be positive")
        if not 0 <= self.phase_ms < self.hop_ms:
            raise ValueError("phase_ms must be in 0..hop_ms-1")

    def coverage_complete(self, minimum_event_ms: int) -> bool:
        return minimum_event_ms > 0 and self.hop_ms <= self.window_ms - minimum_event_ms


@dataclass(frozen=True)
class TransformGrid:
    rates_e4: tuple[int, ...] = DEFAULT_TRANSFORM_RATES_E4
    semitones: tuple[int, ...] = DEFAULT_TRANSFORM_SEMITONES

    def __post_init__(self) -> None:
        if not self.rates_e4 or any(rate <= 0 for rate in self.rates_e4):
            raise ValueError("rates_e4 must contain positive factors")
        if not self.semitones or any(semitone == 0 for semitone in self.semitones):
            raise ValueError("semitones must contain non-zero factors")
        if len(set(self.rates_e4)) != len(self.rates_e4):
            raise ValueError("rates_e4 must not contain duplicates")
        if len(set(self.semitones)) != len(self.semitones):
            raise ValueError("semitones must not contain duplicates")

    def hypotheses(self) -> tuple[Transform, ...]:
        values = [Transform(type="none", rate_e4=10_000, semitones=0)]
        for transform_type in ("resample", "tempo"):
            values.extend(
                Transform(type=transform_type, rate_e4=rate, semitones=0) for rate in self.rates_e4
            )
        values.extend(
            Transform(
                type="pitch",
                rate_e4=transform_spec("pitch", semitones=semitone).rate_e4,
                semitones=semitone,
            )
            for semitone in self.semitones
        )
        return tuple(values)


DEFAULT_TRANSFORM_GRID = TransformGrid()


def schedule_windows(duration_ms: int, schedule: WindowSchedule) -> list[ScheduledWindow]:
    if duration_ms <= 0:
        raise ValueError("media is too short to form a one-millisecond window")
    if duration_ms <= schedule.window_ms:
        return [ScheduledWindow(0, duration_ms, "schedule")]
    last_start = duration_ms - schedule.window_ms
    starts = list(range(schedule.phase_ms, last_start + 1, schedule.hop_ms))
    scheduled = {start: "schedule" for start in starts}
    if last_start not in scheduled:
        scheduled[last_start] = "tail"
    return [
        ScheduledWindow(start, schedule.window_ms, reason)
        for start, reason in sorted(scheduled.items())
    ]


def generation_zero_schedule(duration_ms: int) -> list[ScheduledWindow]:
    return schedule_windows(duration_ms, WindowSchedule())


def schedule_options() -> tuple[WindowSchedule, ...]:
    return tuple(
        WindowSchedule(window_ms=window_ms, hop_ms=hop_ms, phase_ms=phase_ms)
        for window_ms in (6_000, 8_000, 12_000)
        for hop_ms in (5_000, 9_000, 15_000)
        for phase_ms in (0, hop_ms // 2)
    )


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


def transform_filtergraph(transform: Transform) -> str | None:
    """Return the revision-5 FFmpeg undo graph for one hypothesis."""

    if transform.type == "none":
        return None
    if transform.type in {"resample", "tempo"}:
        rate = _decimal_text(Decimal(transform.rate_e4) / Decimal(10_000))
        if transform.type == "resample":
            return f"asetrate=16000/{rate},aresample=16000"
        correction = Decimal(10_000) / Decimal(transform.rate_e4)
        factors = _atempo_factors(correction)
        if len(factors) == 1:
            return f"atempo=1/{rate}"
        return ",".join(f"atempo={_decimal_text(factor)}" for factor in factors)
    p = Decimal(str(2 ** (transform.semitones / 12)))
    pitch = _decimal_text(p)
    factors = _atempo_factors(p)
    suffix = ",".join(f"atempo={_decimal_text(factor)}" for factor in factors)
    return f"asetrate=16000/{pitch},aresample=16000,{suffix}"


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000000000001")).normalize(), "f")


def _atempo_factors(value: Decimal) -> tuple[Decimal, ...]:
    factors: list[Decimal] = []
    while value < Decimal("0.5"):
        factors.append(Decimal("0.5"))
        value /= Decimal("0.5")
    while value > Decimal("2"):
        factors.append(Decimal("2"))
        value /= Decimal("2")
    factors.append(value)
    return tuple(factors)


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


def _temporary_path(directory: Path, suffix: str) -> Path:
    os.makedirs(native_path(directory), exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=".window-", suffix=suffix, dir=native_path(directory), delete=False
    ) as handle:
        return Path(handle.name)


def _force_wav_sample_count(path: Path, expected_samples: int) -> int:
    """Pad or trim to ``expected_samples`` and return the raw pre-normalisation frame count."""

    with wave.open(native_path(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != BYTES_PER_SAMPLE:
            raise ValueError("FFmpeg transform did not produce mono s16le audio")
        if source.getframerate() != SAMPLE_RATE:
            raise ValueError("FFmpeg transform did not produce 16 kHz audio")
        raw_samples = source.getnframes()
        frames = source.readframes(raw_samples)
    expected_bytes = expected_samples * BYTES_PER_SAMPLE
    if len(frames) < expected_bytes:
        frames += bytes(expected_bytes - len(frames))
    else:
        frames = frames[:expected_bytes]
    temporary = _temporary_path(path.parent, ".normalised.wav")
    try:
        with wave.open(native_path(temporary), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(BYTES_PER_SAMPLE)
            target.setframerate(SAMPLE_RATE)
            target.writeframes(frames)
        os.replace(native_path(temporary), native_path(path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(native_path(temporary))
    return raw_samples


async def write_transformed_wav(
    pcm_path: Path,
    wav_path: Path,
    *,
    start_sample: int,
    input_samples: int,
    output_samples: int,
    transform: Transform,
) -> int:
    """Slice sample-exact input, run the exact plan graph, and publish a fixed-length WAV.

    Returns the frame count FFmpeg actually produced, *before* the fixed-length normalisation.
    Insertion vectors assert on that raw count so the tolerance they check is a property of the
    filtergraph rather than of the padding step.
    """

    filtergraph = transform_filtergraph(transform)
    if filtergraph is None:
        _write_wav_slice(pcm_path, wav_path, start_sample, output_samples)
        return output_samples
    input_path = _temporary_path(wav_path.parent, ".input.wav")
    output_path = _temporary_path(wav_path.parent, ".output.wav")
    try:
        _write_wav_slice(pcm_path, input_path, start_sample, input_samples)
        await run_process(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                native_path(input_path),
                "-filter:a",
                filtergraph,
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                "-flags:a",
                "+bitexact",
                native_path(output_path),
            ],
            timeout=180,
        )
        raw_samples = _force_wav_sample_count(output_path, output_samples)
        os.replace(native_path(output_path), native_path(wav_path))
        return raw_samples
    finally:
        for temporary in (input_path, output_path):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(native_path(temporary))


def _load_jsonl(path: Path) -> tuple[WindowRecord, ...]:
    return tuple(
        WindowRecord.model_validate_json(line)
        for line in read_text(path).splitlines()
        if line.strip()
    )


def _support_span_ms(output_ms: int, transform: Transform) -> int:
    if transform.type not in {"resample", "tempo"}:
        return output_ms
    numerator = output_ms * 10_000
    quotient, remainder = divmod(numerator, transform.rate_e4)
    return quotient + int(remainder * 2 >= transform.rate_e4)


def _window_id(
    media_key: str,
    *,
    generation: int,
    start_ms: int,
    support_end_ms: int,
    transform: Transform,
) -> str:
    values = {
        "generation": generation,
        "start_ms": start_ms,
        "support_ms": [start_ms, support_end_ms],
        "transform": transform.model_dump(mode="json"),
    }
    return make_id(media_key, "window", compose_natural_key("window", values))


def _wanted_hypotheses(
    policy: TransformPolicy, grid: TransformGrid, reason: str
) -> tuple[Transform, ...]:
    if policy == "global" or (policy == "rescan_only" and reason == "rescan"):
        return grid.hypotheses()
    return (Transform(type="none", rate_e4=10_000, semitones=0),)


def _variant_name(transform: Transform) -> str:
    if transform.type == "none":
        return "none"
    if transform.type == "pitch":
        return f"pitch-{transform.semitones:+d}"
    return f"{transform.type}-{transform.rate_e4}"


def plan_fixture_windows(
    *,
    media_key: str,
    duration_ms: int,
    schedule: WindowSchedule,
    transform_policy: TransformPolicy,
    transform_grid: TransformGrid = DEFAULT_TRANSFORM_GRID,
) -> tuple[WindowRecord, ...]:
    """Create deterministic in-memory window records for the controlled fixture benchmark."""

    total_samples = duration_ms * SAMPLES_PER_MS
    records: list[WindowRecord] = []
    for item in schedule_windows(duration_ms, schedule):
        start_sample = item.start_ms * SAMPLES_PER_MS
        none = Transform(type="none", rate_e4=10_000, semitones=0)
        none_id = _window_id(
            media_key,
            generation=0,
            start_ms=item.start_ms,
            support_end_ms=item.start_ms + item.output_ms,
            transform=none,
        )
        for transform in _wanted_hypotheses(transform_policy, transform_grid, item.reason):
            input_samples = transform_slice_sample_count(
                item.output_ms,
                transform.type,
                rate_e4=transform.rate_e4,
                semitones=transform.semitones,
            )
            if start_sample + input_samples > total_samples:
                continue
            support_end = item.start_ms + _support_span_ms(item.output_ms, transform)
            window_id = _window_id(
                media_key,
                generation=0,
                start_ms=item.start_ms,
                support_end_ms=support_end,
                transform=transform,
            )
            content_token = sha256(f"{media_key}:{window_id}".encode()).hexdigest()
            records.append(
                WindowRecord(
                    schema_version=SCHEMA_VERSION,
                    generated_by=GENERATED_BY,
                    id=window_id,
                    generation=0,
                    start_ms=item.start_ms,
                    support_ms=(item.start_ms, support_end),
                    output_ms=item.output_ms,
                    transform=transform,
                    sample_map=sample_map_for_transform(
                        transform.type,
                        rate_e4=transform.rate_e4,
                        semitones=transform.semitones,
                    ),
                    wav_path=f"local_fixture/window-token/{content_token}.wav",
                    wav_sha256=content_token,
                    logical_trial_id=none_id,
                    reason=item.reason,
                    rescan_request_id=None,
                )
            )
    return tuple(sort_records(records))


async def generate_windows_async(
    decoded: DecodeResult,
    media_dir: Path,
    *,
    schedule: WindowSchedule | None = None,
    transform_policy: TransformPolicy = "rescan_only",
    transform_grid: TransformGrid = DEFAULT_TRANSFORM_GRID,
) -> WindowsResult:
    """Materialise generation-zero windows and configured transform siblings."""

    if transform_policy not in {"off", "rescan_only", "global"}:
        raise ValueError("transform_policy must be off, rescan_only, or global")
    schedule = schedule or WindowSchedule()

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
            duration_samples = decoded.record.pcm.duration_ms * SAMPLES_PER_MS
            expected_shape = set()
            for item in schedule_windows(decoded.record.pcm.duration_ms, schedule):
                for transform in _wanted_hypotheses(transform_policy, transform_grid, item.reason):
                    input_samples = transform_slice_sample_count(
                        item.output_ms,
                        transform.type,
                        rate_e4=transform.rate_e4,
                        semitones=transform.semitones,
                    )
                    if item.start_ms * SAMPLES_PER_MS + input_samples <= duration_samples:
                        expected_shape.add(
                            (
                                item.start_ms,
                                item.output_ms,
                                item.reason,
                                transform.type,
                                transform.rate_e4,
                                transform.semitones,
                            )
                        )
            actual_shape = {
                (
                    item.start_ms,
                    item.output_ms,
                    item.reason,
                    item.transform.type,
                    item.transform.rate_e4,
                    item.transform.semitones,
                )
                for item in records
            }
            if (
                records
                and valid_wavs
                and expected_shape == actual_shape
                and verify_completion_sidecar(record_path, upstream).valid
            ):
                return WindowsResult(records, record_path, True)
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    total_bytes = path_size(decoded.pcm_path)
    if total_bytes % BYTES_PER_SAMPLE:
        raise ValueError("PCM contains a partial sample")
    total_samples = total_bytes // BYTES_PER_SAMPLE
    duration_ms = total_samples // SAMPLES_PER_MS
    scheduled = schedule_windows(duration_ms, schedule)
    records: list[WindowRecord] = []
    for item in scheduled:
        start_sample = item.start_ms * SAMPLES_PER_MS
        none = Transform(type="none", rate_e4=10_000, semitones=0)
        none_id = _window_id(
            decoded.record.media_key,
            generation=0,
            start_ms=item.start_ms,
            support_end_ms=item.start_ms + item.output_ms,
            transform=none,
        )
        for transform in _wanted_hypotheses(transform_policy, transform_grid, item.reason):
            input_samples = transform_slice_sample_count(
                item.output_ms,
                transform.type,
                rate_e4=transform.rate_e4,
                semitones=transform.semitones,
            )
            if start_sample + input_samples > total_samples:
                continue
            support_end = item.start_ms + _support_span_ms(item.output_ms, transform)
            window_id = _window_id(
                decoded.record.media_key,
                generation=0,
                start_ms=item.start_ms,
                support_end_ms=support_end,
                transform=transform,
            )
            wav_path = (
                media_dir
                / "windows"
                / "gen0"
                / f"{item.start_ms:010d}-{_variant_name(transform)}.wav"
            )
            await write_transformed_wav(
                decoded.pcm_path,
                wav_path,
                start_sample=start_sample,
                input_samples=input_samples,
                output_samples=item.output_ms * SAMPLES_PER_MS,
                transform=transform,
            )
            records.append(
                WindowRecord(
                    schema_version=SCHEMA_VERSION,
                    generated_by=GENERATED_BY,
                    id=window_id,
                    generation=0,
                    start_ms=item.start_ms,
                    support_ms=(item.start_ms, support_end),
                    output_ms=item.output_ms,
                    transform=transform,
                    sample_map=sample_map_for_transform(
                        transform.type,
                        rate_e4=transform.rate_e4,
                        semitones=transform.semitones,
                    ),
                    wav_path=wav_path.relative_to(media_dir).as_posix(),
                    wav_sha256=sha256_file(wav_path),
                    logical_trial_id=none_id,
                    reason=item.reason,
                    rescan_request_id=None,
                )
            )

    ordered = tuple(sort_records(records))
    content = b"\n".join(canonical_json_bytes(record) for record in ordered) + b"\n"
    atomic_write_bytes(record_path, content)
    write_completion_sidecar(record_path, upstream)
    return WindowsResult(ordered, record_path, False)


def generate_windows(decoded: DecodeResult, media_dir: Path) -> WindowsResult:
    """Synchronous compatibility API for the default generation-zero policy."""

    schedule = WindowSchedule()
    # Existing callers are synchronous and the default/rescan-only path performs no FFmpeg work.
    # Keep their API stable while the pipeline uses ``generate_windows_async`` for global grids.
    import asyncio

    return asyncio.run(
        generate_windows_async(
            decoded,
            media_dir,
            schedule=schedule,
            transform_policy="rescan_only",
        )
    )
