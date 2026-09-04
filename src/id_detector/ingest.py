"""Source ingestion through yt-dlp with byte-identity caching."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    OriginalAsset,
    SourceMetadata,
    SourceRecord,
    derive_media_key_from_path,
    derive_source_key,
)
from id_detector.io import (
    atomic_write_json,
    native_path,
    path_is_file,
    read_text,
    sha256_file,
    url_has_credentials,
    verify_completion_sidecar,
    write_completion_sidecar,
)
from id_detector.process import run_process


@dataclass(frozen=True)
class IngestResult:
    record: SourceRecord
    media_dir: Path
    source_path: Path
    original_path: Path
    cached: bool


def canonicalize_url(url: str, info: dict[str, Any] | None = None) -> tuple[str, str, str | None]:
    """Return a stable public URL, platform name, and platform identifier."""

    if url_has_credentials(url):
        raise ValueError("credential-bearing URLs are not accepted")
    info = info or {}
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").casefold()
    platform_id = str(info["id"]) if info.get("id") is not None else None
    candidate = str(info.get("webpage_url") or info.get("original_url") or url)
    if url_has_credentials(candidate):
        raise ValueError("extractor returned a credential-bearing public URL")
    parts = urlsplit(candidate)
    host = (parts.hostname or "").casefold()

    if "youtube" in extractor or host in {
        "youtube.com",
        "www.youtube.com",
        "youtu.be",
        "m.youtube.com",
    }:
        platform = "youtube"
        if not platform_id:
            platform_id = (
                parts.path.removeprefix("/")
                if host == "youtu.be"
                else parse_qs(parts.query).get("v", [None])[0]
            )
        canonical = (
            f"https://www.youtube.com/watch?{urlencode({'v': platform_id})}"
            if platform_id
            else candidate
        )
    elif "soundcloud" in extractor or host.endswith("soundcloud.com"):
        platform = "soundcloud"
        canonical = urlunsplit(("https", "soundcloud.com", parts.path.rstrip("/"), "", ""))
    elif "mixcloud" in extractor or host.endswith("mixcloud.com"):
        platform = "mixcloud"
        canonical = urlunsplit(("https", "www.mixcloud.com", parts.path.rstrip("/") + "/", "", ""))
    else:
        platform = "other"
        canonical = urlunsplit(
            (parts.scheme.casefold(), parts.netloc.casefold(), parts.path, parts.query, "")
        )
    return canonical, platform, platform_id


def _chapter_records(chapters: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(chapters, list):
        return result
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        item: dict[str, Any] = {"title": str(chapter.get("title") or "")}
        for source_name, target_name in (
            ("start_time", "start_time_ms"),
            ("end_time", "end_time_ms"),
        ):
            value = chapter.get(source_name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                item[target_name] = round(value * 1000)
        result.append(item)
    return result


async def _probe_original(path: Path) -> tuple[str, str, int | None]:
    result = await run_process(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "format=format_name,bit_rate:stream=codec_name,bit_rate",
            "-of",
            "json",
            native_path(path),
        ],
        timeout=60,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    if not streams:
        raise ValueError("downloaded media contains no audio stream")
    stream = streams[0]
    format_info = payload.get("format") or {}
    bitrate_value = stream.get("bit_rate") or format_info.get("bit_rate")
    bitrate = int(bitrate_value) if bitrate_value not in {None, "N/A"} else None
    return (
        str(format_info.get("format_name") or path.suffix.lstrip(".")),
        str(stream.get("codec_name") or "unknown"),
        bitrate,
    )


def _load_cached(work_root: Path, input_url: str) -> IngestResult | None:
    local_path = Path(input_url)
    candidate_uri = local_path.resolve().as_uri() if local_path.is_file() else None
    candidate_canonical = candidate_uri or canonicalize_url(input_url, None)[0]
    import glob

    pattern = native_path(work_root / "*" / "*" / "ingest" / "source.json")
    for source_name in sorted(glob.iglob(pattern)):
        source_path = Path(source_name)
        try:
            record = SourceRecord.model_validate_json(read_text(source_path))
            if input_url not in {record.input_url, record.canonical_url} and (
                candidate_canonical != record.canonical_url
            ):
                continue
            media_dir = source_path.parents[1]
            original_path = media_dir / record.original.path
            verification = verify_completion_sidecar(
                source_path, {record.original.path: original_path}
            )
            if verification.valid and sha256_file(original_path) == record.media_key:
                return IngestResult(record, media_dir, source_path, original_path, True)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None


async def ingest(input_url: str, work_root: Path) -> IngestResult:
    """Download one best-audio source and materialise its immutable source record."""

    if url_has_credentials(input_url):
        raise ValueError("credential-bearing URLs are not accepted")
    work_root = work_root.resolve()
    os.makedirs(native_path(work_root), exist_ok=True)
    cached = _load_cached(work_root, input_url)
    if cached is not None:
        return cached

    local = Path(input_url).expanduser()
    info: dict[str, Any]
    format_id: str | None
    temporary_root = Path(tempfile.mkdtemp(prefix=".ingest-", dir=native_path(work_root)))
    temporary_media: Path | None = None
    try:
        if local.is_file():
            local = local.resolve()
            temporary_media = temporary_root / local.name
            shutil.copyfile(native_path(local), native_path(temporary_media))
            canonical_url = local.as_uri()
            platform, platform_id = "file", None
            info = {}
            format_id = None
        else:
            output_template = str(temporary_root / "asset.%(ext)s")
            await run_process(
                [
                    sys.executable,
                    "-m",
                    "yt_dlp",
                    "-f",
                    "ba",
                    "--write-info-json",
                    "--no-write-comments",
                    "--no-playlist",
                    "--no-progress",
                    "--newline",
                    "-o",
                    output_template,
                    input_url,
                ],
                timeout=7200,
            )
            info_paths = list(temporary_root.glob("*.info.json"))
            if len(info_paths) != 1:
                raise ValueError(f"yt-dlp produced {len(info_paths)} info JSON files; expected one")
            info = json.loads(info_paths[0].read_text(encoding="utf-8"))
            candidates = [
                item
                for item in temporary_root.iterdir()
                if item.is_file()
                and not item.name.endswith(".info.json")
                and not item.name.endswith((".part", ".ytdl"))
            ]
            if len(candidates) != 1:
                raise ValueError(f"yt-dlp produced {len(candidates)} media files; expected one")
            temporary_media = candidates[0]
            canonical_url, platform, platform_id = canonicalize_url(input_url, info)
            format_id = str(info["format_id"]) if info.get("format_id") is not None else None

        media_key = derive_media_key_from_path(temporary_media)
        source_key = derive_source_key(canonical_url)
        media_dir = work_root / source_key / media_key
        ingest_dir = media_dir / "ingest"
        os.makedirs(native_path(ingest_dir), exist_ok=True)
        suffix = temporary_media.suffix.casefold() or ".bin"
        original_path = ingest_dir / f"original{suffix}"
        if path_is_file(original_path):
            if sha256_file(original_path) != media_key:
                raise ValueError("cached original path contains different bytes")
            os.unlink(native_path(temporary_media))
        else:
            os.replace(native_path(temporary_media), native_path(original_path))
        temporary_media = None

        container, codec, bitrate = await _probe_original(original_path)
        relative_original = original_path.relative_to(media_dir).as_posix()
        record = SourceRecord(
            schema_version=SCHEMA_VERSION,
            generated_by=GENERATED_BY,
            source_key=source_key,
            media_key=media_key,
            input_url=input_url,
            canonical_url=canonical_url,
            platform=platform,
            platform_id=platform_id,
            uploader_id=str(info["uploader_id"]) if info.get("uploader_id") is not None else None,
            uploader_name=str(info["uploader"]) if info.get("uploader") is not None else None,
            title=str(info["title"])
            if info.get("title") is not None
            else local.stem
            if local.is_file()
            else None,
            upload_date=str(info["upload_date"]) if info.get("upload_date") is not None else None,
            original=OriginalAsset(
                path=relative_original,
                sha256=media_key,
                container=container,
                codec=codec,
                bitrate=bitrate,
                ytdlp_format_id=format_id,
            ),
            metadata=SourceMetadata(
                description=str(info["description"])
                if info.get("description") is not None
                else None,
                chapters=_chapter_records(info.get("chapters")),
                comment_count=int(info["comment_count"])
                if info.get("comment_count") is not None
                else None,
            ),
            config_snapshot={
                "format": "ba",
                "write_info_json": True,
                "write_comments": False,
                "playlist": False,
                # Carried through so the Stage 7 page can honour an embedding-disabled set.
                # SoundCloud info.json exposes ``embeddable_by`` ∈ {"all","me","none"}.
                "embeddable_by": str(info["embeddable_by"])
                if info.get("embeddable_by") is not None
                else None,
            },
        )
        source_path = ingest_dir / "source.json"
        atomic_write_json(source_path, record)
        write_completion_sidecar(source_path, {relative_original: original_path})
        return IngestResult(record, media_dir, source_path, original_path, False)
    finally:
        if temporary_media is not None:
            with suppress(FileNotFoundError):
                os.unlink(native_path(temporary_media))
        shutil.rmtree(native_path(temporary_root), ignore_errors=True)
