"""Deterministic FFmpeg controlled-transform corpus generator."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import random
import re
import shutil
import tempfile
import uuid
import wave
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from id_detector.contracts import GroundTruthRecord
from id_detector.io import (
    atomic_write_bytes,
    atomic_write_json,
    native_path,
    path_is_file,
    read_bytes,
    sha256_file,
)
from id_detector.process import run_process

SAMPLE_RATE = 16_000
AUDIBLE_RULE = (
    "per-stem EBU R128 momentary loudness; 400 ms window/100 ms hop versus mix; "
    "on >= -20 dB, off <= -26 dB hysteresis; 3-frame median; minimum run 2000 ms; "
    "mix below -50 LUFS excluded; first/last on-frame +/-100 ms"
)
_AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".aiff", ".aif"}
_LOUDNESS = re.compile(r"\bt:\s*([0-9.]+).*?\bM:\s*(-?[0-9.]+)")


@dataclass(frozen=True)
class RenderedStem:
    path: Path
    source: Path
    work_title: str
    occurrence_index: int = 0


@dataclass(frozen=True)
class RenderResult:
    manifest_path: Path
    set_count: int
    boundary_count: int


def _wave_bytes(samples: np.ndarray) -> bytes:
    payload = io.BytesIO()
    with wave.open(payload, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        pcm = np.clip(samples * 32767, -32768, 32767).astype("<i2")
        output.writeframes(pcm.tobytes())
    return payload.getvalue()


def synthesize_test_sources(directory: Path, *, seed: int, count: int = 4) -> list[Path]:
    """Create deterministic, distinctive 90-second signals without copyrighted audio."""

    if not 3 <= count <= 5:
        raise ValueError("synthetic source count must be between 3 and 5")
    os.makedirs(native_path(directory), exist_ok=True)
    samples_n = 90 * SAMPLE_RATE
    time = np.arange(samples_n, dtype=np.float64) / SAMPLE_RATE
    paths: list[Path] = []
    for index in range(count):
        rng = np.random.default_rng(seed + index * 10_007)
        base_hz = 97 + index * 41
        tones = (
            0.16 * np.sin(2 * np.pi * base_hz * time)
            + 0.09 * np.sin(2 * np.pi * (base_hz * 2 + 13) * time + index)
            + 0.05 * np.sin(2 * np.pi * (base_hz * 3 + 7) * time)
        )
        raw_noise = rng.standard_normal(samples_n)
        # A short FIR creates a band-limited texture; the rhythmic gate makes sources separable.
        smooth_noise = np.convolve(raw_noise, np.ones(9) / 9, mode="same")
        beats_hz = Decimal(90 + index * 17) / Decimal(60)
        phase = np.remainder(time * float(beats_hz), 1.0)
        envelope = 0.25 + 0.75 * np.exp(-phase * (7 + index))
        signal = (tones + 0.08 * smooth_noise) * envelope
        path = directory / f"synthetic-{index + 1}.wav"
        atomic_write_bytes(path, _wave_bytes(signal))
        paths.append(path)
    return paths


def _temporary_wav(destination: Path) -> Path:
    os.makedirs(native_path(destination.parent), exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.stem}.",
        suffix=".tmp.wav",
        dir=native_path(destination.parent),
        delete=False,
    ) as handle:
        return Path(handle.name)


async def _ffmpeg_to_wav(inputs: list[Path], filtergraph: str, destination: Path) -> None:
    temporary = _temporary_wav(destination)
    try:
        args: list[str | os.PathLike[str]] = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
        ]
        for source in inputs:
            args.extend(("-i", native_path(source)))
        args.extend(
            (
                "-filter_complex",
                filtergraph,
                "-map",
                "[out]",
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
                native_path(temporary),
            )
        )
        await run_process(args, timeout=180)
        os.replace(native_path(temporary), native_path(destination))
    finally:
        if path_is_file(temporary):
            os.unlink(native_path(temporary))


async def _momentary_loudness(path: Path) -> list[tuple[int, Decimal]]:
    result = await run_process(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "verbose",
            "-nostdin",
            "-i",
            native_path(path),
            "-filter:a",
            "ebur128=framelog=verbose:peak=none",
            "-f",
            "null",
            "NUL" if os.name == "nt" else "/dev/null",
        ],
        timeout=180,
    )
    frames: list[tuple[int, Decimal]] = []
    for match in _LOUDNESS.finditer(result.stderr):
        at_ms = int((Decimal(match.group(1)) * 1000).to_integral_value())
        frames.append((at_ms, Decimal(match.group(2))))
    if not frames:
        raise RuntimeError(f"ffmpeg emitted no EBU R128 frames for {path}")
    return frames


def _median_three(values: list[Decimal]) -> list[Decimal]:
    result: list[Decimal] = []
    for index in range(len(values)):
        neighbourhood = sorted(values[max(0, index - 1) : min(len(values), index + 2)])
        result.append(neighbourhood[len(neighbourhood) // 2])
    return result


def controlled_audible_frames(
    stem_frames: list[tuple[int, Decimal]], mix_frames: list[tuple[int, Decimal]]
) -> list[int]:
    """Apply the revision-5 controlled audible rule exactly at 100 ms frames."""

    frame_count = min(len(stem_frames), len(mix_frames))
    relative: list[Decimal] = []
    mix_valid: list[bool] = []
    for index in range(frame_count):
        _, stem_lufs = stem_frames[index]
        _, mix_lufs = mix_frames[index]
        valid = mix_lufs >= Decimal("-50")
        mix_valid.append(valid)
        relative.append(stem_lufs - mix_lufs if valid else Decimal("-999"))
    smoothed = _median_three(relative)
    state = False
    audible: list[bool] = []
    for level, valid in zip(smoothed, mix_valid, strict=True):
        if not valid:
            state = False
        elif level >= Decimal("-20"):
            state = True
        elif level <= Decimal("-26"):
            state = False
        audible.append(state)

    minimum_frames = 2_000 // 100
    retained = [False] * len(audible)
    start = 0
    while start < len(audible):
        if not audible[start]:
            start += 1
            continue
        end = start
        while end < len(audible) and audible[end]:
            end += 1
        if end - start >= minimum_frames:
            retained[start:end] = [True] * (end - start)
        start = end
    return [mix_frames[index][0] for index, value in enumerate(retained) if value]


async def audible_truth(stem: Path, mix: Path, duration_ms: int) -> tuple[list[int], list[int]]:
    stem_frames, mix_frames = await asyncio.gather(
        _momentary_loudness(stem), _momentary_loudness(mix)
    )
    audible = controlled_audible_frames(stem_frames, mix_frames)
    if not audible:
        raise RuntimeError(f"controlled audible rule found no >=2000 ms run for {stem}")
    start = audible[0]
    end = audible[-1]
    return [max(0, start - 100), min(duration_ms, start + 100)], [
        max(0, end - 100),
        min(duration_ms, end + 100),
    ]


def _pad(filtergraph: str, episode_ms: int) -> str:
    episode_samples = episode_ms * SAMPLE_RATE // 1_000
    return (
        f"{filtergraph},aresample={SAMPLE_RATE},atrim=duration={episode_ms / 1000:g},"
        f"apad=whole_len={episode_samples},adelay=1000:all=1,"
        f"apad=pad_len={SAMPLE_RATE},asetpts=PTS-STARTPTS[out]"
    )


def _case_definitions() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {"name": f"length-{length}s", "kind": "length", "episode_ms": length * 1000}
        for length in (3, 5, 10, 20, 30)
    ]
    cases.extend(
        {
            "name": f"pitch-{semitones:+d}",
            "kind": "pitch",
            "episode_ms": 20_000,
            "semitones": semitones,
        }
        for semitones in (-2, -1, 1, 2)
    )
    for kind in ("tempo", "resample"):
        cases.extend(
            {
                "name": f"{kind}-{rate_e4}",
                "kind": kind,
                "episode_ms": 20_000,
                "rate_e4": rate_e4,
            }
            for rate_e4 in (9_200, 9_600, 10_400, 10_800)
        )
    cases.extend(
        [
            {"name": "eq", "kind": "eq", "episode_ms": 20_000},
            {"name": "loop", "kind": "loop", "episode_ms": 20_000, "event": "loop"},
            {"name": "cue-jump", "kind": "cue_jump", "episode_ms": 20_000, "event": "jump"},
            {
                "name": "repeated-section",
                "kind": "repeated_section",
                "episode_ms": 20_000,
                "event": "loop",
            },
            {"name": "drifting-tempo", "kind": "drift", "episode_ms": 20_000, "event": "drift"},
        ]
    )
    cases.extend(
        {
            "name": f"crossfade-{abs(db)}db" if db else "crossfade-0db",
            "kind": "crossfade",
            "episode_ms": 20_000,
            "gain_db": db,
        }
        for db in (0, -6, -12)
    )
    return cases


def _single_filter(case: dict[str, Any], source_offset_ms: int) -> str:
    duration_ms = case["episode_ms"]
    kind = case["kind"]
    offset = source_offset_ms / 1000
    duration = duration_ms / 1000
    prefix = f"[0:a]atrim=start={offset:g}"
    if kind in {"length", "eq", "pitch"}:
        prefix += f":duration={duration:g},asetpts=PTS-STARTPTS"
    if kind == "length":
        return _pad(prefix, duration_ms)
    if kind == "eq":
        return _pad(
            prefix + ",highpass=f=180,lowpass=f=4800,equalizer=f=1200:t=q:w=1:g=-9", duration_ms
        )
    if kind == "pitch":
        pitch_e8 = round(100_000_000 * 2 ** (case["semitones"] / 12))
        pitch = Decimal(pitch_e8) / Decimal(100_000_000)
        return _pad(
            prefix
            + f",asetrate={int(Decimal(SAMPLE_RATE) * pitch)},aresample={SAMPLE_RATE},"
            + f"atempo={Decimal(1) / pitch}",
            duration_ms,
        )
    if kind in {"tempo", "resample"}:
        rate = Decimal(case["rate_e4"]) / Decimal(10_000)
        input_duration = Decimal(duration_ms) * rate / Decimal(1000)
        prefix += f":duration={input_duration},asetpts=PTS-STARTPTS"
        effect = (
            f"atempo={rate}"
            if kind == "tempo"
            else f"asetrate={int(Decimal(SAMPLE_RATE) * rate)},aresample={SAMPLE_RATE}"
        )
        return _pad(prefix + "," + effect + f",atrim=duration={duration:g}", duration_ms)
    if kind == "loop":
        return _pad(
            f"[0:a]atrim=start={offset:g}:duration=5,asetpts=PTS-STARTPTS[a];"
            "[a]asplit=4[a0][a1][a2][a3];[a0][a1][a2][a3]concat=n=4:v=0:a=1",
            duration_ms,
        )
    if kind == "cue_jump":
        return _pad(
            f"[0:a]atrim=start={offset:g}:duration=10,asetpts=PTS-STARTPTS[a];"
            f"[0:a]atrim=start={offset + 35:g}:duration=10,asetpts=PTS-STARTPTS[b];"
            "[a][b]concat=n=2:v=0:a=1",
            duration_ms,
        )
    if kind == "repeated_section":
        return _pad(
            f"[0:a]atrim=start={offset:g}:duration=5,asetpts=PTS-STARTPTS[a];"
            f"[0:a]atrim=start={offset + 15:g}:duration=5,asetpts=PTS-STARTPTS[b];"
            "[a]asplit=3[a0][a1][a2];[a0][b][a1][a2]concat=n=4:v=0:a=1",
            duration_ms,
        )
    if kind == "drift":
        rates = (Decimal("0.94"), Decimal("0.98"), Decimal("1.02"), Decimal("1.06"))
        parts: list[str] = []
        labels: list[str] = []
        cursor = Decimal(str(offset))
        for index, rate in enumerate(rates):
            source_duration = Decimal(5) * rate
            parts.append(
                f"[0:a]atrim=start={cursor}:duration={source_duration},asetpts=PTS-STARTPTS,"
                f"atempo={rate}[d{index}]"
            )
            labels.append(f"[d{index}]")
            cursor += source_duration
        return _pad(";".join(parts) + ";" + "".join(labels) + "concat=n=4:v=0:a=1", duration_ms)
    raise ValueError(f"unsupported case kind: {kind}")


async def _render_single_case(
    case: dict[str, Any], source: Path, set_dir: Path, offset_ms: int
) -> tuple[Path, list[RenderedStem], int]:
    duration_ms = case["episode_ms"] + 2_000
    stem = set_dir / "stem-00.wav"
    await _ffmpeg_to_wav([source], _single_filter(case, offset_ms), stem)
    mix = set_dir / "mix.wav"
    atomic_write_bytes(mix, read_bytes(stem))
    return mix, [RenderedStem(stem, source, source.stem)], duration_ms


async def _render_crossfade_case(
    case: dict[str, Any], sources: tuple[Path, Path], set_dir: Path, offset_ms: int
) -> tuple[Path, list[RenderedStem], int]:
    total_ms = 32_000
    first = set_dir / "stem-00.wav"
    second = set_dir / "stem-01.wav"
    first_filter = (
        f"[0:a]atrim=start={offset_ms / 1000:g}:duration=20,asetpts=PTS-STARTPTS,"
        f"afade=t=out:st=10:d=10,aresample={SAMPLE_RATE},apad=whole_len={20 * SAMPLE_RATE},"
        f"adelay=1000:all=1,apad=pad_len={11 * SAMPLE_RATE}[out]"
    )
    second_filter = (
        f"[0:a]atrim=start={offset_ms / 1000:g}:duration=20,asetpts=PTS-STARTPTS,"
        f"afade=t=in:st=0:d=10,volume={case['gain_db']}dB,aresample={SAMPLE_RATE},"
        f"apad=whole_len={20 * SAMPLE_RATE},adelay=11000:all=1,"
        f"apad=pad_len={SAMPLE_RATE}[out]"
    )
    await _ffmpeg_to_wav([sources[0]], first_filter, first)
    await _ffmpeg_to_wav([sources[1]], second_filter, second)
    mix = set_dir / "mix.wav"
    await _ffmpeg_to_wav(
        [first, second], "[0:a][1:a]amix=inputs=2:normalize=0:duration=longest[out]", mix
    )
    return (
        mix,
        [
            RenderedStem(first, sources[0], sources[0].stem),
            RenderedStem(second, sources[1], sources[1].stem),
        ],
        total_ms,
    )


async def _truth_for_render(
    *,
    set_id: str,
    case: dict[str, Any],
    mix: Path,
    stems: list[RenderedStem],
    duration_ms: int,
    corpus_version: str,
) -> GroundTruthRecord:
    ranges = await asyncio.gather(*(audible_truth(stem.path, mix, duration_ms) for stem in stems))
    episodes: list[dict[str, Any]] = []
    for index, (stem, (start_range, end_range)) in enumerate(zip(stems, ranges, strict=True)):
        start = sum(start_range) // 2
        end = sum(end_range) // 2
        roles: list[dict[str, Any]]
        if len(stems) == 1:
            roles = [{"from_ms": start, "to_ms": end, "role": "dominant"}]
        elif index == 0:
            roles = [
                {"from_ms": start, "to_ms": 11_000, "role": "dominant"},
                {"from_ms": 11_000, "to_ms": end, "role": "outgoing"},
            ]
        else:
            roles = [
                {"from_ms": start, "to_ms": 21_000, "role": "incoming"},
                {"from_ms": 21_000, "to_ms": end, "role": "dominant"},
            ]
        roles = [item for item in roles if item["to_ms"] > item["from_ms"]]
        event_note = None
        if case.get("event") and index == 0:
            event_note = f"event:{case['event']}@11000"
        episodes.append(
            {
                "work": {"artist": "Synthetic Artist", "title": stem.work_title},
                "version": {
                    "qualifier": "Controlled Source",
                    "ids": {"mb_recording": f"synthetic:{sha256_file(stem.source)}"},
                },
                "version_verified": True,
                "verified_against": "source_recording",
                "start_ms_range": start_range,
                "end_ms_range": end_range,
                "audible_rule": AUDIBLE_RULE,
                "role_segments": roles,
                "overlaps_with": [1 - index] if len(stems) == 2 else [],
                "occurrence_index": stem.occurrence_index,
                "in_reference_pool": True,
                "annotator_ref": "controlled-generator",
                "second_pass_ref": None,
                "disagreement_resolution": None,
                "note": event_note,
                "draft": False,
            }
        )
    return GroundTruthRecord(
        schema_version="1.0.0",
        generated_by="id-detector/0.1.0",
        set_id=set_id,
        source={
            "url_ref": f"controlled-source-{set_id}",
            "media_key": sha256_file(mix),
            "duration_ms": duration_ms,
            "platform": "file",
            "uploader_ref": "controlled-generator",
            "event_ref": None,
            "date": None,
        },
        stratum="controlled",
        split="controlled",
        corpus_version=corpus_version,
        selection_basis="scripted synthetic transforms fixed before recognition",
        episodes=episodes,
        regions=[],
    )


def _make_staging_directory(target: Path) -> Path:
    target = target.resolve()
    os.makedirs(native_path(target.parent), exist_ok=True)
    for _ in range(10):
        staging = target.parent / f".{target.name}.{uuid.uuid4().hex}.staging"
        try:
            os.mkdir(native_path(staging))
        except FileExistsError:
            continue
        return staging
    raise RuntimeError(f"could not allocate a staging directory beside {target}")


def _remove_staging_tree(path: Path) -> None:
    resolved = path.resolve()
    if not resolved.name or resolved.parent == resolved:
        raise ValueError(f"refusing to remove unsafe staging path: {resolved}")
    if os.path.isdir(native_path(resolved)):
        shutil.rmtree(native_path(resolved))


def _publish_directory(staging: Path, target: Path) -> None:
    target = target.resolve()
    staging = staging.resolve()
    if not target.name or target.parent == target or staging.parent != target.parent:
        raise ValueError("controlled corpus publication requires a validated sibling staging path")
    backup = target.parent / f".{target.name}.{uuid.uuid4().hex}.backup"
    had_target = os.path.isdir(native_path(target))
    if had_target:
        os.replace(native_path(target), native_path(backup))
    try:
        os.replace(native_path(staging), native_path(target))
    except BaseException:
        if had_target and os.path.isdir(native_path(backup)):
            os.replace(native_path(backup), native_path(target))
        raise
    if had_target:
        _remove_staging_tree(backup)


async def render_controlled(
    sources_dir: Path,
    out_dir: Path,
    *,
    seed: int,
    audio_dir: Path | None = None,
) -> RenderResult:
    sources_dir = sources_dir.resolve()
    sources = sorted(
        (sources_dir / entry.name).resolve()
        for entry in os.scandir(native_path(sources_dir))
        if entry.is_file() and Path(entry.name).suffix.casefold() in _AUDIO_SUFFIXES
    )
    if len(sources) < 3:
        raise ValueError("controlled rendering requires at least three local audio sources")
    out_dir = out_dir.resolve()
    corpus_version = f"controlled-r5-seed-{seed}"
    audio_dir = (
        audio_dir.resolve()
        if audio_dir is not None
        else (Path.cwd() / "data" / "local" / "controlled" / corpus_version).resolve()
    )
    if out_dir == audio_dir or out_dir in audio_dir.parents or audio_dir in out_dir.parents:
        raise ValueError("controlled JSON and rendered audio directories must be separate")
    out_staging: Path | None = _make_staging_directory(out_dir)
    audio_staging: Path | None = _make_staging_directory(audio_dir)
    try:
        rng = random.Random(seed)
        cases = _case_definitions()
        manifest_sets: list[dict[str, Any]] = []
        boundary_count = 0
        for index, case in enumerate(cases):
            set_id = f"controlled-{index + 1:03d}-{case['name']}"
            set_dir = audio_staging / set_id
            os.makedirs(native_path(set_dir), exist_ok=True)
            source_index = rng.randrange(len(sources))
            offset_ms = rng.randrange(5_000, 16_001, 100)
            if case["kind"] == "crossfade":
                other_index = (source_index + 1 + rng.randrange(len(sources) - 1)) % len(sources)
                mix, stems, duration_ms = await _render_crossfade_case(
                    case, (sources[source_index], sources[other_index]), set_dir, offset_ms
                )
            else:
                mix, stems, duration_ms = await _render_single_case(
                    case, sources[source_index], set_dir, offset_ms
                )
            truth = await _truth_for_render(
                set_id=set_id,
                case=case,
                mix=mix,
                stems=stems,
                duration_ms=duration_ms,
                corpus_version=corpus_version,
            )
            truth_dir = out_staging / set_id
            os.makedirs(native_path(truth_dir), exist_ok=True)
            truth_path = truth_dir / "ground_truth.json"
            atomic_write_json(truth_path, truth)
            boundary_count += 2 * len(truth.episodes)
            manifest_sets.append(
                {
                    "set_id": set_id,
                    "kind": case["kind"],
                    "parameters": {
                        key: value
                        for key, value in case.items()
                        if key not in {"name", "kind", "event"}
                    },
                    "source_sha256": [sha256_file(stem.source) for stem in stems],
                    "stem_sha256": [sha256_file(stem.path) for stem in stems],
                    "source_offset_ms": offset_ms,
                    "mix_sha256": sha256_file(mix),
                    "ground_truth_sha256": sha256_file(truth_path),
                    "duration_ms": duration_ms,
                    "episode_count": len(truth.episodes),
                }
            )
        manifest = {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "corpus_version": corpus_version,
            "seed": seed,
            "audible_rule": AUDIBLE_RULE,
            "set_count": len(manifest_sets),
            "boundary_count": boundary_count,
            "sets": manifest_sets,
        }
        atomic_write_json(out_staging / "render_manifest.json", manifest)
        _publish_directory(audio_staging, audio_dir)
        audio_staging = None
        _publish_directory(out_staging, out_dir)
        out_staging = None
        return RenderResult(out_dir / "render_manifest.json", len(manifest_sets), boundary_count)
    finally:
        for staging in (out_staging, audio_staging):
            if staging is not None:
                with contextlib.suppress(OSError):
                    _remove_staging_tree(staging)
