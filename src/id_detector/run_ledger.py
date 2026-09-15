"""One fold of a run's paid attempts and its durable reservation (plan §2.3.2–§2.3.3).

This is the single implementation both layers use: the service seam and pipeline (a local or
hosted run of :func:`id_detector.service.run`) and the durable queue (the worker's cancellation,
dead-letter and settlement paths).  Two copies of the resume rule could disagree about whether a
request was paid for; one cannot.

It owns four facts:

* **the attempt fold** — every durable event of one ``run_id`` from any source (the JSONL journal,
  the SQLite ``provider_attempt_events`` projection, or both), merged by ``(attempt_id, event)``.  A
  duplicate event must carry the same payload; a conflicting duplicate or a self-parented attempt
  is a :class:`LedgerConflict`, never silently ignored;
* **the per-query resume rule** — what a resumed pass may do with each clip:

  ============================================  ==========================================
  newest attempt of THIS run on the query        resume action
  ============================================  ==========================================
  none                                           ``fresh`` — normal cache rules apply
  resolved ``match`` / ``no_match``              ``reuse`` — never re-sent
  resolved billable ambiguous or terminal        ``settled`` — spent/refused, never re-sent
  resolved retryable zero-cost                   ``retry`` — may be re-sent, fresh ordinal
  dispatched, never resolved                     ``ambiguous`` — SPENT, never re-sent
  prepared, never dispatched                     ``reissue`` — may be sent, fresh ordinal
  ============================================  ==========================================

  A re-sent query always gets ``1 + max(ordinal)`` of the run's attempts on it, so its attempt id
  can never collide with an earlier one and its parent is never itself;
* **the money** — spend is ``Σ unit_usd_e6`` over billable resolutions and ambiguous dispatches,
  each at the exact price its own event recorded, never units × today's price;
* **the reservation** — written once, durably, before the first dispatch, and restored verbatim on
  every later pass: never recomputed from current pricing or caps.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from id_detector.contracts import ProviderAttemptEvent
from id_detector.io import (
    atomic_write_bytes,
    canonical_json_bytes,
    ensure_directory_durable,
    fsync_directory,
    path_is_file,
    read_text,
)
from id_detector.money import (
    BILLABLE_OUTCOMES,
    TERMINAL_PROVIDER_OUTCOMES,
    ZERO_COST_OUTCOMES,
    UsdAdmitter,
    UsdReservation,
    UsdSettlement,
    ceil_e2,
)

#: Zero-cost outcomes the recipe may retry: the provider never billed them and was not refusing.
RETRYABLE_ZERO_COST_OUTCOMES = ZERO_COST_OUTCOMES - TERMINAL_PROVIDER_OUTCOMES
#: The only outcomes whose raw body is cached and may be served again.
REUSABLE_OUTCOMES = frozenset({"match", "no_match"})
#: SQLite rows carry no ordinal; it is recovered by matching the deterministic attempt id.
_ORDINAL_SEARCH = 1_024
_SAFE_RUN_ID = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

ResumeAction = Literal["fresh", "reuse", "settled", "retry", "ambiguous", "reissue"]


def new_run_id() -> str:
    """The one code helper that mints a service run id.

    A job-driven run is bound to its job's durable id (the queue column) before it starts and never
    mints one here; CLI and other non-job analyses do. Migration 0002 backfills a live local row in
    SQL with the same shape.
    """

    return uuid.uuid4().hex


class LedgerConflict(ValueError):
    """Two durable records disagree about one attempt; recovery refuses to guess."""


@dataclass(frozen=True)
class AttemptEvent:
    """One durable attempt event, whichever store it came from."""

    attempt_id: str
    event: str
    run_id: str
    provider: str
    query_id: str | None
    parent_attempt_id: str | None
    unit_usd_e6: int
    outcome: str | None
    ordinal: int | None = None
    #: The egress that sent the attempt, when the source records one (the SQLite projection).
    egress_id: str | None = None

    def conflicts(self, other: AttemptEvent) -> bool:
        """True when two copies of one ``(attempt_id, event)`` disagree on any immutable field.

        The one comparison both stores use. ``at`` may differ between copies; ``ordinal`` and
        ``egress_id`` are compared whenever both copies carry them (the JSONL has no egress, and a
        SQLite row's ordinal is recovered from its attempt id).
        """

        if self.payload() != other.payload():
            return True
        if self.ordinal is not None and other.ordinal is not None and self.ordinal != other.ordinal:
            return True
        return (
            self.egress_id is not None
            and other.egress_id is not None
            and self.egress_id != other.egress_id
        )

    def payload(self) -> tuple[object, ...]:
        """What two copies of the same event must agree on (``at`` and egress may differ)."""

        return (
            self.run_id,
            self.provider,
            self.query_id,
            self.parent_attempt_id,
            self.unit_usd_e6,
            self.outcome,
        )


def event_from_record(record: ProviderAttemptEvent) -> AttemptEvent:
    from id_detector.attempts import attempt_id_for

    if attempt_id_for(record.run_id, record.query_id, record.ordinal) != record.attempt_id:
        # The attempt id IS (run, query, ordinal): a line that disagrees was altered or forged.
        raise LedgerConflict(
            f"journal event for attempt {record.attempt_id} does not match its ordinal"
        )
    return AttemptEvent(
        attempt_id=record.attempt_id,
        event=record.event,
        run_id=record.run_id,
        provider=record.provider,
        query_id=record.query_id,
        parent_attempt_id=record.parent_attempt_id,
        unit_usd_e6=record.unit_usd_e6,
        outcome=record.outcome,
        ordinal=record.ordinal,
    )


def _ordinal_for(run_id: str, query_id: str | None, attempt_id: str) -> int | None:
    from id_detector.attempts import attempt_id_for

    if query_id is None:
        return None
    for ordinal in range(_ORDINAL_SEARCH):
        if attempt_id_for(run_id, query_id, ordinal) == attempt_id:
            return ordinal
    return None


def event_from_row(row: Mapping[str, Any]) -> AttemptEvent:
    """A ``provider_attempt_events`` row as an attempt event (validated, ordinal recovered)."""

    state = str(row["state"])
    if state not in {"prepared", "dispatched", "resolved"}:
        raise LedgerConflict(f"unknown attempt state in the ledger: {state}")
    outcome = row["outcome"]
    if (outcome is None) == (state == "resolved"):
        raise LedgerConflict("an attempt row's outcome does not match its state")
    if outcome is not None and outcome not in BILLABLE_OUTCOMES | ZERO_COST_OUTCOMES:
        raise LedgerConflict(f"unknown provider outcome in the ledger: {outcome}")
    unit = int(row["unit_usd_e6"])
    if unit < 0:
        raise LedgerConflict("an attempt row has a negative unit price")
    run_id = str(row["run_id"])
    query_id = row["query_id"]
    return AttemptEvent(
        attempt_id=str(row["attempt_id"]),
        event=state,
        run_id=run_id,
        provider=str(row["provider"]),
        query_id=str(query_id) if query_id is not None else None,
        parent_attempt_id=row["parent_attempt_id"],
        unit_usd_e6=unit,
        outcome=str(outcome) if outcome is not None else None,
        ordinal=_ordinal_for(run_id, query_id, str(row["attempt_id"])),
        egress_id=(str(row["egress_id"]) if "egress_id" in row.keys() else None),  # noqa: SIM118
    )


def parse_journal_lines(text: str) -> list[ProviderAttemptEvent]:
    """Every readable event in a JSONL journal; a torn line (a crash mid-append) is skipped."""

    events: list[ProviderAttemptEvent] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            events.append(ProviderAttemptEvent.model_validate(json.loads(line)))
        except ValueError:  # json.JSONDecodeError and pydantic's ValidationError
            continue
    return events


def events_from_jsonl(path: Path) -> list[AttemptEvent]:
    if not path_is_file(path):
        return []
    try:
        text = read_text(path)
    except OSError:
        return []
    return [event_from_record(record) for record in parse_journal_lines(text)]


@dataclass(frozen=True)
class RunAttempt:
    """One attempt of the run, folded from all of its events."""

    attempt_id: str
    query_id: str | None
    provider: str
    ordinal: int | None
    parent_attempt_id: str | None
    unit_usd_e6: int
    dispatched: bool
    outcome: str | None

    @property
    def billed(self) -> bool:
        """Money the provider may have taken: a billable resolution or an ambiguous dispatch."""

        if self.outcome is not None:
            return self.outcome in BILLABLE_OUTCOMES
        return self.dispatched


@dataclass(frozen=True)
class QueryResume:
    action: ResumeAction
    next_ordinal: int
    parent_attempt_id: str | None
    latest: RunAttempt | None


@dataclass(frozen=True)
class RunLedger:
    run_id: str
    attempts: tuple[RunAttempt, ...] = ()

    @property
    def any(self) -> bool:
        return bool(self.attempts)

    @property
    def spent_usd_e6(self) -> int:
        return sum(attempt.unit_usd_e6 for attempt in self.attempts if attempt.billed)

    @property
    def billed_units(self) -> int:
        return sum(attempt.billed for attempt in self.attempts)

    @property
    def dispatched_attempts(self) -> int:
        return sum(attempt.dispatched for attempt in self.attempts)

    def for_query(self, query_id: str) -> tuple[RunAttempt, ...]:
        found = [attempt for attempt in self.attempts if attempt.query_id == query_id]
        # File order is attempt order within one source; the ordinal is authoritative across two.
        ranked = sorted(
            enumerate(found),
            key=lambda item: (item[1].ordinal if item[1].ordinal is not None else -1, item[0]),
        )
        return tuple(attempt for _position, attempt in ranked)

    def resume(self, query_id: str) -> QueryResume:
        attempts = self.for_query(query_id)
        if not attempts:
            return QueryResume("fresh", 0, None, None)
        ordinals = [attempt.ordinal for attempt in attempts if attempt.ordinal is not None]
        next_ordinal = (max(ordinals) + 1) if ordinals else len(attempts)
        latest = attempts[-1]
        if latest.outcome in REUSABLE_OUTCOMES:
            action: ResumeAction = "reuse"
        elif latest.outcome in BILLABLE_OUTCOMES or latest.outcome in TERMINAL_PROVIDER_OUTCOMES:
            action = "settled"
        elif latest.outcome in RETRYABLE_ZERO_COST_OUTCOMES:
            action = "retry"
        elif latest.dispatched:
            action = "ambiguous"
        else:
            action = "reissue"
        return QueryResume(action, next_ordinal, latest.attempt_id, latest)

    @property
    def resolved_query_ids(self) -> frozenset[str]:
        """Queries a resumed pass must not send again because their answer is settled."""

        return frozenset(
            attempt.query_id
            for attempt in self.attempts
            if attempt.query_id is not None
            and self.resume(attempt.query_id).action in {"reuse", "settled"}
        )

    @property
    def ambiguous_query_ids(self) -> frozenset[str]:
        return frozenset(
            attempt.query_id
            for attempt in self.attempts
            if attempt.query_id is not None and self.resume(attempt.query_id).action == "ambiguous"
        )


def fold_run_ledger(
    run_id: str, *sources: Iterable[AttemptEvent], provider: str | None = "audd"
) -> RunLedger:
    """Merge every source's events for ``run_id`` into one verified ledger."""

    seen: dict[tuple[str, str], AttemptEvent] = {}
    order: list[str] = []
    folded: dict[str, dict[str, Any]] = {}
    for source in sources:
        for event in source:
            if event.run_id != run_id:
                continue
            if provider is not None and event.provider != provider:
                continue
            if event.parent_attempt_id is not None and event.parent_attempt_id == event.attempt_id:
                raise LedgerConflict(f"attempt {event.attempt_id} names itself as its parent")
            key = (event.attempt_id, event.event)
            previous = seen.get(key)
            if previous is not None:
                if previous.conflicts(event):
                    raise LedgerConflict(
                        f"conflicting duplicate {event.event} for attempt {event.attempt_id}"
                    )
                continue
            seen[key] = event
            state = folded.get(event.attempt_id)
            if state is None:
                state = {
                    "query_id": event.query_id,
                    "provider": event.provider,
                    "ordinal": event.ordinal,
                    "parent": event.parent_attempt_id,
                    "unit": event.unit_usd_e6,
                    "dispatched": False,
                    "outcome": None,
                }
                folded[event.attempt_id] = state
                order.append(event.attempt_id)
            elif (state["query_id"], state["parent"], state["unit"]) != (
                event.query_id,
                event.parent_attempt_id,
                event.unit_usd_e6,
            ):
                raise LedgerConflict(f"attempt {event.attempt_id} changed identity between events")
            if state["ordinal"] is None and event.ordinal is not None:
                state["ordinal"] = event.ordinal
            if event.event != "prepared":
                state["dispatched"] = True
            if event.event == "resolved":
                state["outcome"] = event.outcome
    return RunLedger(
        run_id,
        tuple(
            RunAttempt(
                attempt_id=attempt_id,
                query_id=folded[attempt_id]["query_id"],
                provider=folded[attempt_id]["provider"],
                ordinal=folded[attempt_id]["ordinal"],
                parent_attempt_id=folded[attempt_id]["parent"],
                unit_usd_e6=folded[attempt_id]["unit"],
                dispatched=folded[attempt_id]["dispatched"],
                outcome=folded[attempt_id]["outcome"],
            )
            for attempt_id in order
        ),
    )


# --------------------------------------------------------------------------------- reservation


@dataclass(frozen=True)
class ReservationRecord:
    """The reservation a run made before its first dispatch — restored, never recomputed."""

    run_id: str
    planned: int
    unit_usd_e6: int
    usd_e6_reserved: int
    usd_e2_reserved: int
    effective_cap_e2: int

    @classmethod
    def from_reservation(cls, run_id: str, reservation: UsdReservation) -> ReservationRecord:
        return cls(
            run_id=run_id,
            planned=reservation.planned,
            unit_usd_e6=reservation.unit_usd_e6,
            usd_e6_reserved=reservation.usd_e6_reserved,
            usd_e2_reserved=reservation.usd_e2_reserved,
            effective_cap_e2=reservation.effective_cap_e2,
        )

    @classmethod
    def from_document(cls, value: object, *, run_id: str) -> ReservationRecord | None:
        if not isinstance(value, Mapping) or value.get("run_id") != run_id:
            return None
        try:
            fields = {
                name: value[name]
                for name in (
                    "planned",
                    "unit_usd_e6",
                    "usd_e6_reserved",
                    "usd_e2_reserved",
                    "effective_cap_e2",
                )
            }
        except KeyError:
            return None
        if any(isinstance(item, bool) or not isinstance(item, int) for item in fields.values()):
            return None
        if any(item < 0 for item in fields.values()) or fields["unit_usd_e6"] <= 0:
            return None
        return cls(run_id=run_id, **fields)

    def document(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "planned": self.planned,
            "unit_usd_e6": self.unit_usd_e6,
            "usd_e6_reserved": self.usd_e6_reserved,
            "usd_e2_reserved": self.usd_e2_reserved,
            "effective_cap_e2": self.effective_cap_e2,
        }

    def reservation(self) -> UsdReservation:
        return UsdReservation(
            planned=self.planned,
            unit_usd_e6=self.unit_usd_e6,
            usd_e6_reserved=self.usd_e6_reserved,
            usd_e2_reserved=self.usd_e2_reserved,
            effective_cap_e2=self.effective_cap_e2,
        )


def reservation_path(journal_path: Path, run_id: str) -> Path:
    if not run_id or any(character not in _SAFE_RUN_ID for character in run_id):
        raise ValueError("unsafe run_id")
    return Path(journal_path).parent / "reservations" / f"{run_id}.json"


def read_reservation(path: Path, *, run_id: str) -> ReservationRecord | None:
    if not path_is_file(path):
        return None
    try:
        return ReservationRecord.from_document(json.loads(read_text(path)), run_id=run_id)
    except (OSError, ValueError):
        return None


def write_reservation(path: Path, record: ReservationRecord) -> ReservationRecord:
    """Persist ``record`` once, durably; an existing record for the run always wins."""

    existing = read_reservation(path, run_id=record.run_id)
    if existing is not None:
        return existing
    ensure_directory_durable(path.parent)
    atomic_write_bytes(path, canonical_json_bytes(record.document()))
    fsync_directory(path.parent)
    return record


def project_reservation(path: Path, record: ReservationRecord) -> ReservationRecord:
    """Mirror the AUTHORITATIVE ``record`` beside the journal (a projection: it never decides)."""

    if read_reservation(path, run_id=record.run_id) == record:
        return record
    ensure_directory_durable(path.parent)
    atomic_write_bytes(path, canonical_json_bytes(record.document()))
    fsync_directory(path.parent)
    return record


# ---------------------------------------------------------------------------------------- money


@dataclass(frozen=True)
class RecoveredMoney:
    """A run's cumulative money as every durable record knows it; monotonic by construction."""

    usd_e6_reserved: int = 0
    usd_e6_spent: int = 0
    attempts: int = 0

    @property
    def any(self) -> bool:
        return bool(self.usd_e6_reserved or self.usd_e6_spent or self.attempts)

    def settlement(self) -> UsdSettlement:
        return UsdSettlement(
            usd_e6_reserved=self.usd_e6_reserved,
            usd_e6_spent=self.usd_e6_spent,
            usd_e2_reserved=ceil_e2(self.usd_e6_reserved),
            usd_e2_spent=ceil_e2(self.usd_e6_spent),
            usd_e6_released=max(0, self.usd_e6_reserved - self.usd_e6_spent),
        )

    def merged(self, settlement: UsdSettlement) -> UsdSettlement:
        """``settlement`` never reporting less than what is already durable."""

        reserved = max(settlement.usd_e6_reserved, self.usd_e6_reserved)
        spent = max(settlement.usd_e6_spent, self.usd_e6_spent)
        return UsdSettlement(
            usd_e6_reserved=reserved,
            usd_e6_spent=spent,
            usd_e2_reserved=max(settlement.usd_e2_reserved, ceil_e2(reserved)),
            usd_e2_spent=max(settlement.usd_e2_spent, ceil_e2(spent)),
            usd_e6_released=max(0, reserved - spent),
        )


def recovered_money(
    ledger: RunLedger,
    reservation: ReservationRecord | None,
    *floors: RecoveredMoney,
) -> RecoveredMoney:
    reserved = reservation.usd_e6_reserved if reservation is not None else 0
    spent = ledger.spent_usd_e6
    attempts = ledger.dispatched_attempts
    for floor in floors:
        reserved = max(reserved, floor.usd_e6_reserved)
        spent = max(spent, floor.usd_e6_spent)
        attempts = max(attempts, floor.attempts)
    return RecoveredMoney(reserved, spent, attempts)


def restore_admitter(
    reservation: UsdReservation, spent_usd_e6: int, admitter: UsdAdmitter | None = None
) -> tuple[UsdAdmitter, bool]:
    """An admitter holding the run's ORIGINAL reservation, pre-charged with its durable spend.

    ``admitter`` (a caller-injected one) is charged in place; it must hold the same unit price as
    the durable reservation, or the ledger and the admitter would price one request two ways.
    The second value is ``False`` when the reservation cannot even cover what is already spent.
    """

    if admitter is None:
        admitter = UsdAdmitter(reservation)
    elif admitter.reservation != reservation:
        # The WHOLE reservation — planned count, unit price, reserved µUSD/cents and cap. A smaller
        # injected reservation would stop a resume early; a different one would silently re-price.
        raise LedgerConflict(
            "the supplied admitter's reservation differs from the run's durable reservation"
        )
    return admitter, admitter.restore_spent(spent_usd_e6)
