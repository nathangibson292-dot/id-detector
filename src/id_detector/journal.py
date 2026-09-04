"""Crash-safe append-only invocation journal helpers."""

from __future__ import annotations

import importlib.metadata
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from id_detector.contracts import GENERATED_BY, SCHEMA_VERSION, InvocationJournalEntry
from id_detector.io import (
    atomic_write_bytes,
    canonical_json_bytes,
    path_is_file,
    read_bytes,
    redact_command_argument,
)


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def tool_versions(ffmpeg_version: str | None = None) -> dict[str, str]:
    result = {
        "id-detector": importlib.metadata.version("id-detector"),
        "shazamio": importlib.metadata.version("shazamio"),
        "shazamio-core": importlib.metadata.version("shazamio-core"),
        "yt-dlp": importlib.metadata.version("yt-dlp"),
    }
    if ffmpeg_version:
        result["ffmpeg"] = ffmpeg_version
    return result


def append_invocation(path: Path, entry: InvocationJournalEntry) -> None:
    existing = read_bytes(path) if path_is_file(path) else b""
    atomic_write_bytes(path, existing + canonical_json_bytes(entry) + b"\n")


@dataclass
class InvocationTimer:
    run_id: str
    command: list[str]
    started_at: str = field(default_factory=timestamp)
    started_monotonic: float = field(default_factory=time.monotonic)
    timings: dict[str, int] = field(default_factory=dict)
    _stage_started: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.command = [redact_command_argument(argument) for argument in self.command]

    def start_stage(self, name: str) -> None:
        self._stage_started[name] = time.monotonic()

    def finish_stage(self, name: str) -> None:
        started = self._stage_started.pop(name)
        self.timings[name] = round((time.monotonic() - started) * 1000)

    def entry(
        self,
        *,
        status: str,
        exit_code: int,
        counts: dict[str, int],
        costs: dict[str, int],
        source_ids: list[str],
        ffmpeg_version: str | None,
    ) -> InvocationJournalEntry:
        return InvocationJournalEntry(
            schema_version=SCHEMA_VERSION,
            generated_by=GENERATED_BY,
            invocation_id=self.run_id,
            command=self.command,
            started_at=self.started_at,
            finished_at=timestamp(),
            status=status,
            exit_code=exit_code,
            duration_ms=round((time.monotonic() - self.started_monotonic) * 1000),
            tool_versions=tool_versions(ffmpeg_version),
            timings=self.timings,
            counts=counts,
            costs=costs,
            source_ids=source_ids,
        )
