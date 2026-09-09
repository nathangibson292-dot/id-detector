"""Durable per-request attempt journal for the paid clip engine (plan §2.3.3).

Every AudD clip request is three append-only events in ``recognise/attempts.jsonl``:
``prepared`` (the attempt exists and knows its unit price), ``dispatched`` (written and fsynced
*before* the request enters network I/O, so a crash can never hide a request the provider may have
billed) and ``resolved`` (the frozen money outcome).  A retry is a new attempt whose
``parent_attempt_id`` is the one it retries.

On a later run the ledger classifies what an earlier run left behind: ``prepared`` without
``dispatched`` was never sent and is simply re-issued; ``dispatched`` without ``resolved`` is
ambiguous — it may have been billed, so it counts as spent and is re-run as a fresh attempt that
names it as its parent.  The journal is per media, shared by every run over that media.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from id_detector.contracts import (
    ATTEMPT_EVENT_SEQ,
    GENERATED_BY,
    SCHEMA_VERSION,
    ProviderAttemptEvent,
    ProviderOutcome,
)
from id_detector.io import path_is_file, read_text
from id_detector.journal import append_line, timestamp

ATTEMPTS_FILENAME = "attempts.jsonl"


def attempts_path(media_dir: Path) -> Path:
    return media_dir / "recognise" / ATTEMPTS_FILENAME


def attempt_id_for(run_id: str, query_id: str, ordinal: int) -> str:
    """Deterministic attempt identity: the ``ordinal``-th attempt on a query within a run."""

    return sha256(f"{run_id}\n{query_id}\n{ordinal}".encode()).hexdigest()


class AttemptJournal:
    """Writer for one run's attempts; every event is on disk before the method returns."""

    def __init__(self, path: Path, *, run_id: str, provider: str, unit_usd_e6: int) -> None:
        self.path = path
        self.run_id = run_id
        self.provider = provider
        self.unit_usd_e6 = unit_usd_e6
        self._open: dict[str, tuple[str, str, int, str | None]] = {}

    def prepare(
        self,
        *,
        query_id: str,
        window_id: str,
        ordinal: int,
        parent_attempt_id: str | None,
    ) -> str:
        attempt_id = attempt_id_for(self.run_id, query_id, ordinal)
        self._open[attempt_id] = (query_id, window_id, ordinal, parent_attempt_id)
        self._write("prepared", attempt_id, None)
        return attempt_id

    def dispatched(self, attempt_id: str) -> None:
        self._write("dispatched", attempt_id, None)

    def resolved(self, attempt_id: str, outcome: ProviderOutcome) -> None:
        self._write("resolved", attempt_id, outcome)

    def _write(self, event: str, attempt_id: str, outcome: ProviderOutcome | None) -> None:
        query_id, window_id, ordinal, parent_attempt_id = self._open[attempt_id]
        record = ProviderAttemptEvent(
            schema_version=SCHEMA_VERSION,
            generated_by=GENERATED_BY,
            event=event,  # type: ignore[arg-type]
            seq=ATTEMPT_EVENT_SEQ[event],
            at=timestamp(),
            attempt_id=attempt_id,
            query_id=query_id,
            run_id=self.run_id,
            provider=self.provider,
            window_id=window_id,
            ordinal=ordinal,
            parent_attempt_id=parent_attempt_id,
            unit_usd_e6=self.unit_usd_e6,
            outcome=outcome,
        )
        append_line(self.path, record)


@dataclass(frozen=True)
class AttemptState:
    """One attempt folded from its events."""

    attempt_id: str
    query_id: str
    window_id: str
    run_id: str
    ordinal: int
    parent_attempt_id: str | None
    unit_usd_e6: int
    dispatched: bool
    outcome: str | None

    @property
    def state(self) -> str:
        if self.outcome is not None:
            return "resolved"
        return "dispatched" if self.dispatched else "prepared"

    @property
    def classification(self) -> str:
        """Plan §2.3.3 resume rule: ``reissue`` | ``ambiguous`` | ``resolved``."""

        if self.outcome is not None:
            return "resolved"
        return "ambiguous" if self.dispatched else "reissue"


@dataclass(frozen=True)
class AttemptLedger:
    """Every attempt in file order plus the count of lines that could not be read."""

    attempts: tuple[AttemptState, ...] = ()
    skipped_lines: int = 0

    def latest(self, query_id: str) -> AttemptState | None:
        for attempt in reversed(self.attempts):
            if attempt.query_id == query_id:
                return attempt
        return None

    def dangling(self, query_id: str) -> AttemptState | None:
        """The query's newest attempt when an earlier run left it unresolved."""

        latest = self.latest(query_id)
        return latest if latest is not None and latest.outcome is None else None

    def with_classification(self, name: str) -> tuple[AttemptState, ...]:
        return tuple(attempt for attempt in self.attempts if attempt.classification == name)


def load_attempt_ledger(path: Path) -> AttemptLedger:
    """Fold the journal into attempts; a torn trailing line (a crash mid-write) is skipped."""

    if not path_is_file(path):
        return AttemptLedger()
    folded: dict[str, AttemptState] = {}
    skipped = 0
    for line in read_text(path).splitlines():
        if not line.strip():
            continue
        try:
            event = ProviderAttemptEvent.model_validate(json.loads(line))
        except ValueError:  # json.JSONDecodeError and pydantic's ValidationError
            skipped += 1
            continue
        current = folded.get(event.attempt_id)
        folded[event.attempt_id] = AttemptState(
            attempt_id=event.attempt_id,
            query_id=event.query_id,
            window_id=event.window_id,
            run_id=event.run_id,
            ordinal=event.ordinal,
            parent_attempt_id=event.parent_attempt_id,
            unit_usd_e6=event.unit_usd_e6,
            dispatched=(current is not None and current.dispatched) or event.event != "prepared",
            outcome=event.outcome if event.event == "resolved" else (current and current.outcome),
        )
    return AttemptLedger(tuple(folded.values()), skipped)
