"""Durable per-request attempt journal for the paid clip engine (plan §2.3.3).

Every AudD clip request is three append-only events in ``recognise/attempts.jsonl``:
``prepared`` (the attempt exists and knows its unit price), ``dispatched`` (written and fsynced
*before* the request enters network I/O, so a crash can never hide a request the provider may have
billed) and ``resolved`` (the frozen money outcome).  A retry is a new attempt whose
``parent_attempt_id`` is the one it retries.

On a later run the ledger classifies what an earlier run left behind: ``prepared`` without
``dispatched`` was never sent and is simply re-issued; ``dispatched`` without ``resolved`` is
ambiguous — it may have been billed, so it counts as spent and is never sent again
automatically.  The journal is per media, shared by every run over that media.  A queue-driven run
first commits each dispatch to SQLite (the authority) under its job's claim fence; the JSONL line
is that row's projection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

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


class DispatchRefused(RuntimeError):
    """The job queue refused to admit a dispatch: cancelled, reclaimed or past its lease.

    Raised before the ``dispatched`` event is written, so nothing was sent and the attempt
    stays ``prepared`` (re-issued by a later pass).
    """


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
        self._known_attempts: set[str] | None = None
        #: The ONE dispatch-admission check (plan §4.6), evaluated immediately before a
        #: ``dispatched`` event is durably written. The supervised local worker attaches its
        #: queue-aware guard here; the hosted journal runs the same check in its transaction.
        self.admission: Any = None

    def _durable_attempt_ids(self) -> set[str]:
        if self._known_attempts is None:
            self._known_attempts = {event.attempt_id for event in self.durable_events()}
        return self._known_attempts

    def prepare(
        self,
        *,
        query_id: str,
        window_id: str,
        ordinal: int,
        parent_attempt_id: str | None,
    ) -> str:
        attempt_id = attempt_id_for(self.run_id, query_id, ordinal)
        if parent_attempt_id is not None and parent_attempt_id == attempt_id:
            raise ValueError(f"attempt {attempt_id} cannot name itself as its parent")
        if attempt_id in self._open or attempt_id in self._durable_attempt_ids():
            # Two requests under one id would fold into one ledger attempt and hide a charge —
            # whether this writer made the first one or another writer already made it durable.
            raise ValueError(f"attempt id already used in this run: {attempt_id}")
        self._open[attempt_id] = (query_id, window_id, ordinal, parent_attempt_id)
        self._write("prepared", attempt_id, None)
        return attempt_id

    # ------------------------------------------------------ recovery (id_detector.run_ledger)

    def durable_events(self) -> list[Any]:
        """Every durable event this journal can read back (the JSONL; a projection adds more)."""

        from id_detector.run_ledger import events_from_jsonl

        events = events_from_jsonl(self.path)
        # The dispatch authority's rows: a committed dispatch whose JSONL projection was lost is
        # still spent and still never re-sent.
        reader = getattr(self.admission, "dispatched_events", None)
        if callable(reader):
            events.extend(reader(self.run_id, self.provider))
        return events

    def for_provider(self, provider: str) -> AttemptJournal:
        """This run's journal for another provider: same run identity, its own file."""

        journal = AttemptJournal(
            self.path.parent / f"{provider}-{self.path.name}",
            run_id=self.run_id,
            provider=provider,
            unit_usd_e6=0,
        )
        # The SAME claim fence (claim, lease, not cancelled): a Shazam attempt identity is inserted
        # under it before its request leaves, and a second writer's insert of that identity refuses.
        journal.admission = self.admission
        return journal

    def events_for(self, provider: str) -> list[Any]:
        """Durable events another provider's attempts left in this journal's store (none here)."""

        del provider
        return []

    def run_ledger(self) -> Any:
        """This run's verified attempt fold — the one resume rule both layers share."""

        from id_detector.run_ledger import fold_run_ledger

        return fold_run_ledger(self.run_id, self.durable_events(), provider=self.provider)

    def _reservation_authority(self) -> Any:
        """The SQLite reservation authority of a queue-driven run, or ``None`` (the direct CLI)."""

        admission = self.admission
        if callable(getattr(admission, "reserve", None)) and callable(
            getattr(admission, "stored_reservation", None)
        ):
            return admission
        return None

    def durable_reservation(self) -> Any:
        """The run's reservation: SQLite first when an authority is attached, else the file.

        With an authority attached the JSON file beside the journal is a projection only: a stale
        worker could have written it, so it is never read back as the reservation.
        """

        from id_detector.run_ledger import read_reservation, reservation_path

        authority = self._reservation_authority()
        if authority is not None:
            return authority.stored_reservation(self.run_id)
        return read_reservation(reservation_path(self.path, self.run_id), run_id=self.run_id)

    def dispatch_without_reservation(self) -> bool:
        """True when the authority proves a dispatch but holds no reservation (fail closed)."""

        authority = self._reservation_authority()
        if authority is None or authority.stored_reservation(self.run_id) is not None:
            return False
        return any(attempt.dispatched for attempt in self.run_ledger().attempts)

    def record_reservation(self, reservation: Any) -> Any:
        """Persist the reservation before any dispatch; an existing record for the run wins.

        A queue-driven run writes it to SQLite under the claim check, in one transaction (a stale
        worker writes nothing), and only then projects it beside the journal.
        """

        from id_detector.run_ledger import (
            ReservationRecord,
            project_reservation,
            reservation_path,
            write_reservation,
        )

        record = ReservationRecord.from_reservation(self.run_id, reservation)
        path = reservation_path(self.path, self.run_id)
        authority = self._reservation_authority()
        if authority is None:
            return write_reservation(path, record)
        stored = authority.reserve(record)  # fenced; raises before anything is written
        return project_reservation(path, stored)

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
        self._persist(record)

    def _persist(self, record: ProviderAttemptEvent) -> None:
        """Write ONE event object durably (a projection writes the same object)."""

        if record.event == "dispatched" and self.admission is not None:
            # The authoritative dispatch row, committed in the SAME transaction as the claim check
            # (claim, lease, active, run token, not cancelled). Refused -> DispatchRefused, and
            # nothing is written: no row, no line, no request.
            self.admission.admit(record, self.path)
        append_line(self.path, record)  # the JSONL projection, only after that commit


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
