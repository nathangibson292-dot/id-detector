"""Crash-safe append-only invocation journal helpers."""

from __future__ import annotations

import importlib.metadata
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from id_detector.contracts import GENERATED_BY, SCHEMA_VERSION, InvocationJournalEntry
from id_detector.io import (
    atomic_write_bytes,
    canonical_json_bytes,
    fsync_directory,
    native_path,
    path_is_file,
    read_bytes,
    redact_command_argument,
)
from id_detector.recipes import FREE_RECIPE


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
    fsync_directory(path.parent)


def append_line(path: Path, record: Any) -> None:
    """Append one canonical JSON line and fsync it before returning.

    The attempt journal (plan §2.3.3) needs every event on disk *before* the next step — most
    importantly ``dispatched`` before network I/O — so this is a true append with a flush and an
    ``fsync``, not the read-and-replace :func:`append_invocation` uses for its one line per run.
    """

    path = path.resolve()
    os.makedirs(native_path(path.parent), exist_ok=True)
    with open(native_path(path), "ab") as handle:
        handle.write(canonical_json_bytes(record) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


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
        reason: str | None = None,
        achieved: str | None = None,
        usd_e6_reserved: int = 0,
        usd_e6_spent: int = 0,
        usd_e2_reserved: int = 0,
        usd_e2_spent: int = 0,
        requested_recipe_id: str = FREE_RECIPE.recipe_id,
        algorithm_version: str = FREE_RECIPE.algorithm_version,
        pricing_version: str = "v1",
        audd_usd_e6_per_request: int = 5_000,
    ) -> InvocationJournalEntry:
        return InvocationJournalEntry(
            schema_version=SCHEMA_VERSION,
            generated_by=GENERATED_BY,
            invocation_id=self.run_id,
            bundle_id=None,
            fuse_run=None,
            command=self.command,
            started_at=self.started_at,
            finished_at=timestamp(),
            status=status,
            reason=reason,
            achieved=achieved,  # type: ignore[arg-type]
            exit_code=exit_code,
            duration_ms=round((time.monotonic() - self.started_monotonic) * 1000),
            tool_versions=tool_versions(ffmpeg_version),
            timings=self.timings,
            counts=counts,
            costs=costs,
            usd_e6_reserved=usd_e6_reserved,
            usd_e6_spent=usd_e6_spent,
            usd_e2_reserved=usd_e2_reserved,
            usd_e2_spent=usd_e2_spent,
            requested_recipe_id=requested_recipe_id,
            algorithm_version=algorithm_version,
            pricing_version=pricing_version,
            audd_usd_e6_per_request=audd_usd_e6_per_request,
            source_ids=source_ids,
        )
