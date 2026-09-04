"""One-pass deterministic decode to 16 kHz mono signed little-endian PCM."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    DecoderInfo,
    PcmAsset,
    PcmRecord,
)
from id_detector.ingest import IngestResult
from id_detector.io import (
    atomic_write_json,
    native_path,
    path_is_file,
    path_size,
    read_text,
    sha256_file,
    verify_completion_sidecar,
    write_completion_sidecar,
)
from id_detector.process import run_process

SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2
FILTERGRAPH = "aresample=16000,aformat=sample_fmts=s16:channel_layouts=mono"


@dataclass(frozen=True)
class DecodeResult:
    record: PcmRecord
    pcm_path: Path
    record_path: Path
    cached: bool


async def _ffprobe_pcm_duration_ms(path: Path) -> int:
    result = await run_process(
        [
            "ffprobe",
            "-v",
            "error",
            "-f",
            "s16le",
            "-sample_rate",
            str(SAMPLE_RATE),
            "-ch_layout",
            "mono",
            "-i",
            native_path(path),
            "-show_entries",
            "format=duration",
            "-of",
            "json",
        ],
        timeout=60,
    )
    value = (json.loads(result.stdout).get("format") or {}).get("duration")
    if value is None:
        raise ValueError("ffprobe did not report decoded PCM duration")
    return round(float(value) * 1000)


async def decode(ingested: IngestResult) -> DecodeResult:
    media_dir = ingested.media_dir
    decode_dir = media_dir / "decode"
    pcm_path = decode_dir / "audio.pcm"
    record_path = decode_dir / "pcm.json"
    upstream = {
        "ingest/source.json": ingested.source_path,
        ingested.record.original.path: ingested.original_path,
    }
    if path_is_file(record_path) and path_is_file(pcm_path):
        try:
            record = PcmRecord.model_validate_json(read_text(record_path))
            verification = verify_completion_sidecar(record_path, upstream)
            if (
                verification.valid
                and record.media_key == ingested.record.media_key
                and sha256_file(pcm_path) == record.pcm.sha256
            ):
                return DecodeResult(record, pcm_path, record_path, True)
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    os.makedirs(native_path(decode_dir), exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=".audio.pcm.", suffix=".tmp", dir=native_path(decode_dir), delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        # Input appears before all output options and there is deliberately no -ss/-sseof: every
        # sample is decoded from the beginning, including corrupt-tail detection.
        await run_process(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                native_path(ingested.original_path),
                "-map",
                "0:a:0",
                "-vn",
                "-af",
                FILTERGRAPH,
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                "-f",
                "s16le",
                "-y",
                native_path(temporary),
            ],
            timeout=7200,
        )
        byte_count = path_size(temporary)
        if byte_count == 0 or byte_count % BYTES_PER_SAMPLE:
            raise ValueError("decoded PCM is empty or contains a partial sample")
        duration_ms = (byte_count // BYTES_PER_SAMPLE) * 1000 // SAMPLE_RATE
        ffprobe_duration_ms = await _ffprobe_pcm_duration_ms(temporary)
        if abs(ffprobe_duration_ms - duration_ms) > 500:
            raise ValueError(
                "decoded PCM duration differs from ffprobe by more than 500 ms: "
                f"{duration_ms} vs {ffprobe_duration_ms}"
            )
        os.replace(native_path(temporary), native_path(pcm_path))
    finally:
        with suppress(FileNotFoundError):
            os.unlink(native_path(temporary))

    version = await run_process(["ffmpeg", "-version"], timeout=30)
    ffmpeg_version = next(
        (line.strip() for line in version.stdout.splitlines() if line.strip()), "unknown"
    )
    relative_pcm = pcm_path.relative_to(media_dir).as_posix()
    record = PcmRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        media_key=ingested.record.media_key,
        pcm=PcmAsset(
            path=relative_pcm,
            sha256=sha256_file(pcm_path),
            sample_rate=SAMPLE_RATE,
            channels=1,
            sample_format="s16le",
            duration_ms=duration_ms,
            ffprobe_duration_ms=ffprobe_duration_ms,
        ),
        decoder=DecoderInfo(ffmpeg_version=ffmpeg_version, filtergraph=FILTERGRAPH),
    )
    atomic_write_json(record_path, record)
    write_completion_sidecar(record_path, upstream)
    return DecodeResult(record, pcm_path, record_path, False)


def load_decode(media_dir: Path) -> DecodeResult:
    """Load a materialised decode for ``show`` and tests."""

    record_path = media_dir / "decode" / "pcm.json"
    record = PcmRecord.model_validate_json(read_text(record_path))
    return DecodeResult(record, media_dir / record.pcm.path, record_path, True)
