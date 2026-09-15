"""Crash-safe append-only invocation journal helpers."""

from __future__ import annotations

import importlib.metadata
import json
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
    create_file_durably,
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


_MONEY_FIELDS = ("usd_e6_reserved", "usd_e6_spent", "usd_e2_reserved", "usd_e2_spent")


def _monotonic(entry: InvocationJournalEntry, prior: dict[str, Any]) -> InvocationJournalEntry:
    """``entry`` never reporting less money than an earlier settlement of the same run."""

    update: dict[str, Any] = {}
    for name in _MONEY_FIELDS:
        earlier = prior.get(name)
        if isinstance(earlier, int) and not isinstance(earlier, bool):
            update[name] = max(int(getattr(entry, name)), earlier)
    costs = dict(entry.costs)
    earlier_costs = prior.get("costs")
    if isinstance(earlier_costs, dict):
        earlier_e2 = earlier_costs.get("usd_e2")
        if isinstance(earlier_e2, int) and not isinstance(earlier_e2, bool):
            costs["usd_e2"] = max(int(costs.get("usd_e2", 0)), earlier_e2)
    update["costs"] = costs
    return entry.model_copy(update=update)


#: Public name of the monotonic settlement merge (the SQLite settlement row uses it too).
merge_monotonic = _monotonic


def append_invocation(path: Path, entry: InvocationJournalEntry) -> InvocationJournalEntry:
    """Record ``entry`` as THE terminal settlement of its run: one line per ``invocation_id``.

    A run that is cancelled (or waits, or fails) and is later resumed under the same ``run_id``
    settles again. Appending a second line would report two settlements — and anything that sums
    the journal would count the money twice — so the earlier line is replaced and the new one is
    appended last (the newest entry stays the last line every reader expects). The money on the
    written line is monotonic: never less than any earlier settlement of the same run reported.
    """

    existing = read_bytes(path) if path_is_file(path) else b""
    kept: list[bytes] = []
    merged = entry
    for line in existing.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        if isinstance(value, dict) and value.get("invocation_id") == entry.invocation_id:
            merged = _monotonic(merged, value)
            continue
        kept.append(line)
    body = b"".join(line + b"\n" for line in kept) + canonical_json_bytes(merged) + b"\n"
    atomic_write_bytes(path, body)
    fsync_directory(path.parent)
    return merged


def invocation_lines(path: Path, invocation_id: str) -> list[dict[str, Any]]:
    """EVERY settlement line ``path`` holds for ``invocation_id``, in file order."""

    if not path_is_file(path):
        return []
    found: list[dict[str, Any]] = []
    for line in read_bytes(path).splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("invocation_id") == invocation_id:
            found.append(value)
    return found


def has_invocation(path: Path, invocation_id: str) -> bool:
    """True when the journal already holds a settlement line for ``invocation_id``."""

    if not path_is_file(path):
        return False
    for line in read_bytes(path).splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("invocation_id") == invocation_id:
            return True
    return False


def append_line(path: Path, record: Any) -> None:
    """Append one canonical JSON line and fsync it before returning.

    The attempt journal (plan §2.3.3) needs every event on disk *before* the next step — most
    importantly ``dispatched`` before network I/O — so this is a true append with a flush and an
    ``fsync``, not the read-and-replace :func:`append_invocation` uses for its one line per run.
    A new journal's directory entry is made durable first (create-if-absent with write-through on
    Windows, a directory fsync on POSIX): a fsynced line in a file whose name was lost is lost.
    """

    path = path.resolve()
    if create_file_durably(path):
        fsync_directory(path.parent)
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
    analysis_key: str | None = None
    compatibility: dict[str, Any] | None = None
    keep_intermediates: bool = False
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
            analysis_key=self.analysis_key,
            compatibility=self.compatibility,
            bundle_id=None,
            fuse_run=None,
            keep_intermediates=self.keep_intermediates,
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
