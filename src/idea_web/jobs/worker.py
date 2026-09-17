"""Framework-neutral SQLite queue, intake and analysis worker (plan cycle 4b-i)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol

from id_detector.attempts import AttemptJournal, DispatchRefused
from id_detector.compat import (
    LOCAL_OWNER_SCOPE,
    AnalysisInputs,
    hints_snapshot,
    index_identity,
    serves,
)
from id_detector.compat import (
    RunRequest as CompatibilityRequest,
)
from id_detector.contracts import ProviderAttemptEvent
from id_detector.decode import decode
from id_detector.hints.pipeline import run_hints
from id_detector.ingest import _load_cached, ingest
from id_detector.io import fsync_directory, native_path, path_is_file, read_text, sha256_file
from id_detector.jobs import JobStoreLocked, ProcessLock
from id_detector.journal import append_line
from id_detector.money import ceil_e2
from id_detector.paid_clip import PAID_CLIP_ENGINES
from id_detector.present.bundles import read_bundle_manifest
from id_detector.providers.base import AppConfig
from id_detector.recipes import RECIPES, Recipe, get_recipe
from id_detector.run_ledger import (
    AttemptEvent,
    LedgerConflict,
    RecoveredMoney,
    ReservationRecord,
    event_from_record,
    event_from_row,
    fold_run_ledger,
    new_run_id,
    parse_journal_lines,
    recovered_money,
)
from id_detector.scan import PAID_FILE_SCANNERS
from id_detector.service import (
    CHECKPOINT_PHASES,
    CheckpointPhase,
    LocalPath,
    PipelineOptions,
    PlatformUrl,
    RunRequest,
    RunResult,
    TargetRefused,
    UploadId,
    checkpoint_entry_valid,
    durable_artefact_records,
    validate_platform_url,
    validate_upload_id,
)
from id_detector.shazam_breaker import BreakerConfig, ShazamBreaker
from idea_web.breaker import SharedShazamBreaker
from idea_web.database import Database, migrations
from idea_web.progress import PageProgress, page_document

#: Hosted paid money is deferred to the hosted cycle (round 6) and must be re-reviewed before
#: it is enabled: until then a hosted worker refuses every paid engine at intake AND at
#: dispatch admission.
HOSTED_PAID_REFUSAL = "paid engines are not enabled in hosted mode yet"
#: The money-authority code version (migration 0003). New code stamps it on a job at submit
#: and keeps it on every claim it makes; a claim by older code marks the job 0 (the trigger
#: in 0003), and only a stamped job with no authority rows may settle at zero money.
MONEY_AUTHORITY = 3
_PAID_ENGINE_NAMES = frozenset(PAID_FILE_SCANNERS) | frozenset(PAID_CLIP_ENGINES)


class SchemaTooNew(RuntimeError):
    """The database was upgraded by newer ID'er code: this worker must stop claiming and exit."""


@lru_cache(maxsize=1)
def known_schema_version() -> int:
    """The newest migration this code ships (and therefore understands)."""

    available = migrations()
    return available[-1].number if available else 0


def recipe_uses_paid_engine(recipe: Recipe) -> bool:
    """True when ``recipe`` can spend money: a paid primary or secondary engine, or a paid cap."""

    return (
        recipe.max_usd_e2 > 0
        or recipe.primary_engine in _PAID_ENGINE_NAMES
        or (recipe.secondary_engine or "") in _PAID_ENGINE_NAMES
    )


HEARTBEAT_SECONDS = 10.0
DEFAULT_LEASE_SECONDS = 30.0
MAX_ATTEMPTS = 3
#: How long a job that is merely *waiting* (an open breaker, a busy source) stays unclaimable.
#: Waiting is not a failed attempt, so the attempt this claim consumed is given back; without a
#: cooldown the consumer would spin on the same row for as long as the provider stays down.
WAIT_COOLDOWN_SECONDS = 30.0
TERMINAL_STATES = frozenset(
    {
        "complete",
        "degraded",
        "partial",
        "provider_unavailable",
        "budget_exhausted",
        "source_changed",
        "quota_exceeded",
        "failed",
        "cancelled",
    }
)
ACTIVE_RUN_STATES = frozenset({"intake", "waiting", "analysis"})
_ACTIVE_SQL = "('intake','waiting','analysis')"
#: ``json_extract`` is NULL for a job whose progress has no ``attached`` key, and ``NOT (TRUE AND
#: NULL)`` is NULL, not TRUE -- a predicate written without COALESCE silently excluded every
#: breaker-waiting job from the queue forever.
#: Attachment is a durable column (migration 0002), never inferred from mutable progress JSON: a
#: malformed or rewritten progress document can no longer turn an attached job into a claimant.
_ATTACHED_SQL = "attached = 1"
#: No OTHER job drives this run under a live lease: the claim token on the run belongs to an active,
#: unexpired, non-attached job other than the one asking. Parameters: the asking job id, now.
_NO_OTHER_LIVE_DRIVER_SQL = (
    "NOT EXISTS (SELECT 1 FROM jobs d WHERE d.run_id = analysis_runs.run_id AND d.id <> ? "
    "AND d.attached = 0 AND d.claim_token IS NOT NULL "
    "AND d.claim_token = analysis_runs.claim_token "
    f"AND d.state IN {_ACTIVE_SQL} AND d.lease_until > ?)"
)


class StaleClaim(RuntimeError):
    """A write arrived from a claim that is no longer the current one (plan §4.6).

    Raised instead of silently succeeding: a worker whose lease was reclaimed must not be able to
    overwrite its replacement's checkpoints, money or attempt ledger, and must find out.
    """


#: The run-side fence (plan §4.6): the run's claim token must belong to a job that is still driving
#: it — active, holding that same token, and with an UNEXPIRED lease. It is checked inside the
#: caller's write transaction, immediately before the write it guards, so a worker that slept past
#: its lease cannot checkpoint, admit a dispatch or settle even before anybody else reclaims.
_RUN_FENCE_SQL = (
    "SELECT 1 FROM analysis_runs r JOIN jobs j "
    "ON j.run_id = r.run_id AND j.claim_token = r.claim_token "
    f"WHERE r.run_id = ? AND r.claim_token = ? AND j.state IN {_ACTIVE_SQL} AND j.lease_until > ?"
)
_ATTEMPT_ROW_COLUMNS = (
    "attempt_id, state, run_id, provider, egress_id, query_id, parent_attempt_id, "
    "unit_usd_e6, outcome"
)


def require_run_fence(connection: Any, run_id: str, claim_token: str | None, now: float) -> None:
    if claim_token is None:
        raise StaleClaim(f"no claim token for run {run_id}; refusing to write")
    if connection.execute(_RUN_FENCE_SQL, (run_id, claim_token, now)).fetchone() is None:
        raise StaleClaim(f"claim on run {run_id} is stale, inactive or past its lease")


#: The ONE claim check every run-side write of a job shares (plan §4.6): the job holds this claim
#: token, is active, and its lease has not expired; a job that drives a queue-side run must also
#: hold that run's token. Parameters: job id, claim token, now.
_JOB_CLAIM_SQL = (
    "SELECT 1 FROM jobs j WHERE j.id = ? AND j.claim_token = ? "
    f"AND j.state IN {_ACTIVE_SQL} AND j.lease_until > ? "
    "AND (j.run_id IS NULL "
    "OR NOT EXISTS (SELECT 1 FROM analysis_runs r WHERE r.run_id = j.run_id) "
    "OR EXISTS (SELECT 1 FROM analysis_runs r "
    "WHERE r.run_id = j.run_id AND r.claim_token = j.claim_token))"
)


class DispatchAdmission:
    """The ONE dispatch-admission check for every paid dispatch path, hosted and supervised local.

    Atomically requires the matching claim token, an active state, an unexpired lease AND no
    cancellation request, evaluated immediately before the ``dispatched`` event is durably written.
    With ``require_not_cancelled=False`` the same check is the claim fence for terminal settlement
    (a cancelled job must still be able to settle what it spent).
    """

    def __init__(
        self,
        database: Database,
        *,
        job_id: str,
        claim_token: str,
        clock: Callable[[], float] = time.time,
        require_not_cancelled: bool = True,
        hosted: bool = False,
    ) -> None:
        self.database = database
        self.job_id = job_id
        self.claim_token = claim_token
        self.clock = clock
        self.require_not_cancelled = require_not_cancelled
        #: A hosted worker's admission refuses every paid dispatch (hosted paid money is deferred).
        self.hosted = hosted
        #: Test seam: runs INSIDE the admission transaction, after the row is written.
        self.before_commit: Callable[[], None] | None = None

    def check(self, connection: Any) -> None:
        if self.hosted and self.require_not_cancelled:
            raise DispatchRefused(HOSTED_PAID_REFUSAL)
        sql = _JOB_CLAIM_SQL + (" AND j.cancel_requested = 0" if self.require_not_cancelled else "")
        if connection.execute(sql, (self.job_id, self.claim_token, self.clock())).fetchone():
            return
        if self.require_not_cancelled:
            raise DispatchRefused(
                f"job {self.job_id} is cancelled, reclaimed or past its lease: dispatch refused"
            )
        raise StaleClaim(f"job {self.job_id} no longer holds its claim: settlement refused")

    def admit_in(self, connection: Any, record: ProviderAttemptEvent, journal_path: Path) -> None:
        """The claim check AND the authoritative dispatch row, in the caller's ONE transaction.

        Nothing may send a request unless this row committed. The primary key makes a second
        dispatch of one attempt impossible: its insert is a no-op, and a no-op is a refusal.
        """

        from id_detector.io import canonical_json_bytes

        self.check(connection)
        owner = connection.execute("SELECT run_id FROM jobs WHERE id=?", (self.job_id,)).fetchone()
        if owner is None or owner["run_id"] != record.run_id:
            raise DispatchRefused(
                f"attempt {record.attempt_id} is for run {record.run_id}, "
                f"not job {self.job_id}'s run"
            )
        if record.provider != "shazam" and stored_reservation(connection, record.run_id) is None:
            raise DispatchRefused(
                f"run {record.run_id} has no durable reservation: paid dispatch refused"
            )
        inserted = connection.execute(
            "INSERT INTO run_dispatches(run_id, attempt_id, job_id, provider, event, "
            "journal_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, attempt_id) DO NOTHING",
            (
                record.run_id,
                record.attempt_id,
                self.job_id,
                record.provider,
                canonical_json_bytes(record).decode("utf-8"),
                str(journal_path),
                self.clock(),
            ),
        ).rowcount
        if inserted != 1:
            raise DispatchRefused(f"attempt {record.attempt_id} was already dispatched")
        if self.before_commit is not None:
            self.before_commit()

    def admit(self, record: ProviderAttemptEvent, journal_path: Path) -> None:
        with self.database.write() as connection:
            self.admit_in(connection, record, journal_path)

    def reserve(self, record: ReservationRecord) -> ReservationRecord:
        """The run's ONE reservation, written under the claim check before its first dispatch.

        One transaction: the claim (token, active state, unexpired lease) is checked and the unique
        ``run_reservations`` row inserted together, so a stale worker past its lease writes nothing.
        An existing row always wins: a replacement worker under a different price or cap resumes
        against the reservation the run made, never a recomputed one.
        """

        if self.hosted:
            raise DispatchRefused(HOSTED_PAID_REFUSAL)
        with self.database.write() as connection:
            if not connection.execute(
                _JOB_CLAIM_SQL, (self.job_id, self.claim_token, self.clock())
            ).fetchone():
                raise StaleClaim(
                    f"job {self.job_id} no longer holds its claim: reservation refused"
                )
            owner = connection.execute(
                "SELECT run_id FROM jobs WHERE id=?", (self.job_id,)
            ).fetchone()
            if owner is None or (owner["run_id"] is not None and owner["run_id"] != record.run_id):
                raise DispatchRefused(f"run {record.run_id} is not driven by job {self.job_id}")
            stored = stored_reservation(connection, record.run_id)
            if stored is not None:
                return stored
            connection.execute(
                "INSERT INTO run_reservations(run_id, job_id, reservation, usd_e6_reserved, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    record.run_id,
                    self.job_id,
                    json.dumps(record.document(), sort_keys=True, separators=(",", ":")),
                    record.usd_e6_reserved,
                    self.clock(),
                ),
            )
        return record

    def stored_reservation(self, run_id: str) -> ReservationRecord | None:
        with self.database.read() as connection:
            return stored_reservation(connection, run_id)

    def dispatched_events(self, run_id: str, provider: str | None = None) -> list[AttemptEvent]:
        with self.database.read() as connection:
            return dispatch_events(connection, run_id, provider)


def stored_reservation(connection: Any, run_id: str) -> ReservationRecord | None:
    """``run_id``'s authoritative reservation row; an unreadable row fails closed (raises)."""

    row = connection.execute(
        "SELECT reservation FROM run_reservations WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["reservation"])
    except (TypeError, ValueError):
        value = None
    record = ReservationRecord.from_document(value, run_id=run_id)
    if record is None:
        raise LedgerConflict(f"run {run_id} has an unreadable reservation row")
    return record


def dispatch_events(
    connection: Any, run_id: str, provider: str | None = None, *, paid_only: bool = False
) -> list[AttemptEvent]:
    """``run_id``'s authoritative dispatch rows as attempt events (their JSONL may be missing).

    ``provider`` narrows them to one provider; ``paid_only`` leaves out the free Shazam identities
    (which are fenced here too, but never carry money).
    """

    sql = "SELECT event FROM run_dispatches WHERE run_id=?"
    params: list[Any] = [run_id]
    if provider is not None:
        sql += " AND provider=?"
        params.append(provider)
    if paid_only:
        sql += " AND provider <> 'shazam'"
    rows = connection.execute(sql + " ORDER BY created_at, attempt_id", params).fetchall()
    return [
        event_from_record(ProviderAttemptEvent.model_validate_json(row["event"])) for row in rows
    ]


class SettlementLedger:
    """``run_settlements``: the ONE authority for a run's terminal settlement (plan §2.3.2).

    The claim holder's write (``fence``) upserts the row under the claim check in one transaction,
    with monotonic money; ``only_if_missing`` writers (the derived sweep, the post-commit fast path)
    insert only when no row exists, so concurrent writers produce exactly one row. After commit the
    row is projected into ``invocations.jsonl``; :meth:`reproject` restores a lost projection.
    """

    def __init__(
        self,
        database: Database,
        *,
        fence: Callable[[Any], None] | None = None,
        job_id: str | None = None,
        clock: Callable[[], float] = time.time,
        only_if_missing: bool = False,
    ) -> None:
        self.database = database
        self.fence = fence
        self.job_id = job_id
        self.clock = clock
        self.only_if_missing = only_if_missing

    def __call__(self, path: Path | None, entry: Any) -> bool:
        return self.settle(path, entry)

    def settle(self, path: Path | None, entry: Any) -> bool:
        """Upsert (``only_if_missing``: insert) the run's row; project it when ``path`` is known.

        ``path`` is ``None`` for a run whose media cannot be located right now. Such a row may
        hold paid spend, conservatively folded from SQLite alone (every unresolved dispatch counts
        as spent); it has no journal yet, and :meth:`attach` projects it once the media is found.
        """

        from id_detector.io import canonical_json_bytes
        from id_detector.journal import merge_monotonic

        run_id = entry.invocation_id
        with self.database.write() as connection:
            if self.fence is not None:
                self.fence(connection)  # stale, reclaimed or expired -> raises; nothing written
            if self.job_id is not None:
                self._check_owner(connection, run_id)
            row = connection.execute(
                "SELECT entry FROM run_settlements WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is not None and self.only_if_missing:
                return False
            merged = entry if row is None else merge_monotonic(entry, json.loads(row["entry"]))
            now = self.clock()
            connection.execute(
                "INSERT INTO run_settlements(run_id, job_id, status, journal_path, entry, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "job_id=COALESCE(excluded.job_id, run_settlements.job_id), "
                "status=excluded.status, journal_path=excluded.journal_path, "
                "entry=excluded.entry, updated_at=excluded.updated_at",
                (
                    run_id,
                    self.job_id,
                    merged.status,
                    str(Path(path)) if path is not None else "",
                    canonical_json_bytes(merged).decode("utf-8"),
                    now,
                    now,
                ),
            )
        if path is not None:
            _project_settlement(Path(path), merged)  # after commit
        return True

    def _check_owner(self, connection: Any, run_id: str) -> None:
        """In the settlement transaction: the job exists and ``jobs.run_id`` IS this run."""

        owner = connection.execute("SELECT run_id FROM jobs WHERE id=?", (self.job_id,)).fetchone()
        if owner is None:
            raise LedgerConflict(f"settlement of run {run_id} names a missing job {self.job_id}")
        if owner["run_id"] != run_id:
            raise LedgerConflict(
                f"settlement of run {run_id} does not belong to job {self.job_id} "
                f"(its run is {owner['run_id']})"
            )

    def row(self, run_id: str) -> dict[str, Any] | None:
        with self.database.read() as connection:
            found = connection.execute(
                "SELECT journal_path, entry, status FROM run_settlements WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return dict(found) if found is not None else None

    def adopt(
        self, run_id: str, path: Path, lines: list[Mapping[str, Any]], money: RecoveredMoney
    ) -> bool:
        """Reconcile settlement lines written before SQLite held settlements into ONE row.

        Every line for the run is read (the pre-fix cancel/resume journal could leave several) and
        folded with the run's durable money (attempts, dispatch rows, reservation): each money
        field is the maximum any of them reports, the newest line names the outcome. That canonical
        row is inserted once, and the projection is rewritten to exactly one line for the run.
        """

        from id_detector.contracts import InvocationJournalEntry
        from id_detector.journal import merge_monotonic

        entries: list[Any] = []
        for line in lines:
            with suppress(ValueError):
                entries.append(InvocationJournalEntry.model_validate(dict(line)))
        if not entries:
            raise ValueError(f"run {run_id} has no readable settlement line to adopt")
        canonical = entries[-1]
        for line in lines:
            canonical = merge_monotonic(canonical, dict(line))
        settlement = money.settlement()
        canonical = merge_monotonic(
            canonical,
            {
                "usd_e6_reserved": settlement.usd_e6_reserved,
                "usd_e6_spent": settlement.usd_e6_spent,
                "usd_e2_reserved": settlement.usd_e2_reserved,
                "usd_e2_spent": settlement.usd_e2_spent,
                "costs": {"usd_e2": settlement.usd_e2_spent},
            },
        )
        attempts = money.attempts
        for line in lines:
            counts = line.get("counts")
            value = counts.get("paid_attempts") if isinstance(counts, Mapping) else None
            if isinstance(value, int) and not isinstance(value, bool):
                attempts = max(attempts, value)
        if attempts:
            canonical = canonical.model_copy(
                update={"counts": {**canonical.counts, "paid_attempts": attempts}}
            )
        inserted = self.settle(path, canonical)
        self.reproject(run_id)  # whoever inserted the row, the file ends with exactly one line
        return inserted

    def attach(self, run_id: str, path: Path, money: RecoveredMoney) -> bool:
        """Give a row settled while its media was missing its journal, once the media is found.

        The row keeps its outcome; its money is raised to the full fold (never lowered: the
        SQLite-only settlement already counted every unresolved dispatch as spent), and the journal
        is then made to hold exactly that one line.
        """

        from id_detector.contracts import InvocationJournalEntry
        from id_detector.io import canonical_json_bytes
        from id_detector.journal import merge_monotonic

        with self.database.write() as connection:
            row = connection.execute(
                "SELECT entry, journal_path FROM run_settlements WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False
            if not row["journal_path"]:
                entry = merge_monotonic(
                    InvocationJournalEntry.model_validate_json(row["entry"]), _money_prior(money)
                )
                attempts = max(int(entry.counts.get("paid_attempts", 0) or 0), money.attempts)
                if attempts:
                    entry = entry.model_copy(
                        update={"counts": {**entry.counts, "paid_attempts": attempts}}
                    )
                connection.execute(
                    "UPDATE run_settlements SET journal_path=?, entry=?, updated_at=? "
                    "WHERE run_id=? AND journal_path=''",
                    (
                        str(Path(path)),
                        canonical_json_bytes(entry).decode("utf-8"),
                        self.clock(),
                        run_id,
                    ),
                )
        return self.reproject(run_id)

    def reproject(self, run_id: str) -> bool:
        """Make the journal hold EXACTLY the row's settlement: restored when missing or different.

        A line reporting MORE money than the row first raises the row (money is monotonic and the
        row must never under-report); otherwise the row wins. Duplicate lines collapse to one.
        """

        from id_detector.contracts import InvocationJournalEntry
        from id_detector.io import canonical_json_bytes
        from id_detector.journal import invocation_lines, merge_monotonic

        row = self.row(run_id)
        if row is None or not row["journal_path"]:
            return False
        path = Path(row["journal_path"])
        lines = invocation_lines(path, run_id)
        stored = InvocationJournalEntry.model_validate_json(row["entry"])
        if len(lines) == 1 and _same_settlement(lines[0], stored):
            return False
        with self.database.write() as connection:
            current = connection.execute(
                "SELECT entry FROM run_settlements WHERE run_id=?", (run_id,)
            ).fetchone()
            canonical = InvocationJournalEntry.model_validate_json(current["entry"])
            raised = canonical
            for line in lines:
                raised = merge_monotonic(raised, line)
            if raised != canonical:
                connection.execute(
                    "UPDATE run_settlements SET entry=?, updated_at=? WHERE run_id=?",
                    (canonical_json_bytes(raised).decode("utf-8"), self.clock(), run_id),
                )
        _project_settlement(path, raised)  # replaces every line for the run with this one
        return True


class LegacyRecoveryLedger(SettlementLedger):
    """The ONE explicit path for a pre-upgrade job that has no ``jobs.run_id``.

    Its association is validated in the settlement transaction instead of run ownership: the job
    exists, still has no run id and is stopped; no job owns the run; and no settlement of the run
    already names a different job.
    """

    def _check_owner(self, connection: Any, run_id: str) -> None:
        legacy = connection.execute(
            f"SELECT 1 FROM jobs WHERE id=? AND run_id IS NULL AND state NOT IN {_ACTIVE_SQL}",
            (self.job_id,),
        ).fetchone()
        if legacy is None:
            raise LedgerConflict(
                f"job {self.job_id} is not a stopped pre-upgrade job without a run id"
            )
        if connection.execute("SELECT 1 FROM jobs WHERE run_id=?", (run_id,)).fetchone():
            raise LedgerConflict(f"run {run_id} belongs to another job")
        settled = connection.execute(
            "SELECT job_id FROM run_settlements WHERE run_id=?", (run_id,)
        ).fetchone()
        if settled is not None and settled["job_id"] not in (None, self.job_id):
            raise LedgerConflict(f"run {run_id} is already settled for job {settled['job_id']}")


def _money_prior(money: RecoveredMoney) -> dict[str, Any]:
    settlement = money.settlement()
    return {
        "usd_e6_reserved": settlement.usd_e6_reserved,
        "usd_e6_spent": settlement.usd_e6_spent,
        "usd_e2_reserved": settlement.usd_e2_reserved,
        "usd_e2_spent": settlement.usd_e2_spent,
        "costs": {"usd_e2": settlement.usd_e2_spent},
    }


def _same_settlement(line: Mapping[str, Any], stored: Any) -> bool:
    from id_detector.contracts import InvocationJournalEntry

    try:
        return InvocationJournalEntry.model_validate(dict(line)) == stored
    except ValueError:
        return False


def _project_settlement(path: Path, entry: Any) -> None:
    from id_detector import journal as journal_module

    journal_module.append_invocation(path, entry)


def _run_attempt_events(connection: Any, run_id: str, provider: str = "audd") -> list[AttemptEvent]:
    rows = connection.execute(
        f"SELECT {_ATTEMPT_ROW_COLUMNS} FROM provider_attempt_events "
        "WHERE run_id = ? AND provider = ? ORDER BY event_id",
        (run_id, provider),
    ).fetchall()
    return [event_from_row(row) for row in rows]


def fold_run_money(connection: Any, run_id: str) -> RecoveredMoney:
    """Fold every durable record of ``run_id`` into its cumulative money and raise the row to it.

    The shared :mod:`id_detector.run_ledger` fold over the SQLite attempt events, the durable
    reservation, the primary checkpoint and the row itself: a cancellation, dead letter or
    settlement of a run that died before ``primary`` never reports less than it really spent.
    """

    run = connection.execute(
        "SELECT checkpoints, usd_e6_reserved, usd_e6_spent, attempts FROM analysis_runs "
        "WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if run is None:
        return RecoveredMoney()
    try:
        document = json.loads(run["checkpoints"])
    except (TypeError, json.JSONDecodeError):
        document = {}
    if not isinstance(document, dict):
        document = {}
    primary = document.get("primary")
    primary_state = primary.get("state") if isinstance(primary, dict) else None
    if not isinstance(primary_state, dict):
        primary_state = {}
    money = recovered_money(
        fold_run_ledger(run_id, _run_attempt_events(connection, run_id)),
        ReservationRecord.from_document(document.get("_reservation"), run_id=run_id),
        RecoveredMoney(int(run["usd_e6_reserved"]), int(run["usd_e6_spent"]), int(run["attempts"])),
        RecoveredMoney(
            int(primary_state.get("usd_e6_reserved", 0) or 0),
            int(primary_state.get("usd_e6_spent", 0) or 0),
            int(primary_state.get("attempts", 0) or 0),
        ),
    )
    connection.execute(
        "UPDATE analysis_runs SET usd_e6_reserved=MAX(usd_e6_reserved, ?), "
        "usd_e6_spent=MAX(usd_e6_spent, ?), attempts=MAX(attempts, ?) WHERE run_id=?",
        (money.usd_e6_reserved, money.usd_e6_spent, money.attempts, run_id),
    )
    return money


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def fsync_artefact(path: Path) -> None:
    """Flush one artefact's bytes (and, on POSIX, its directory entry) to the device.

    §3.4's publication invariant is that artefact files are written *and fsynced* before any row
    points at them. Existence is not durability: the decoder, for instance, renames an ffmpeg
    temporary into place without flushing it, so a power cut can leave a committed checkpoint
    naming a PCM file whose bytes were never on the disk. The checkpoint boundary therefore
    flushes what it is about to reference rather than trusting every producer to have done it.
    """

    descriptor = os.open(native_path(Path(path)), os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(Path(path).parent)


def require_durable(artefacts: tuple[Path, ...] | list[Path]) -> tuple[Path, ...]:
    """Return the artefacts, refusing any that is missing or cannot be made durable."""

    return tuple(Path(record["path"]) for record in durable_records(artefacts))


def durable_records(artefacts: tuple[Path, ...] | list[Path]) -> list[dict[str, Any]]:
    """The service's shared checkpoint boundary (flush, then size + SHA-256 per artefact)."""

    return durable_artefact_records(artefacts, flush=lambda path: fsync_artefact(path))


def _target_document(
    target: PlatformUrl | UploadId | LocalPath, *, local_mode: bool
) -> dict[str, str]:
    if isinstance(target, PlatformUrl):
        return {"kind": "platform", "url": validate_platform_url(target.url)}
    if isinstance(target, UploadId):
        return {"kind": "upload", "upload_id": validate_upload_id(target.upload_id)}
    if isinstance(target, LocalPath):
        if not local_mode:
            raise TargetRefused("LocalPath is accepted only in local mode")
        return {"kind": "local", "path": str(target.path)}
    raise TypeError("target must be PlatformUrl, UploadId, or LocalPath")


def _read_target(document: str, *, local_mode: bool) -> PlatformUrl | UploadId | LocalPath:
    """Parse and validate an untrusted queue value every time it leaves SQLite."""

    try:
        value = json.loads(document)
    except (TypeError, json.JSONDecodeError) as exc:
        raise TargetRefused("job target is not valid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        raise TargetRefused("job target has no valid kind")
    kind = value["kind"]
    if kind == "platform" and set(value) == {"kind", "url"}:
        return PlatformUrl(validate_platform_url(value.get("url")))
    if kind == "upload" and set(value) == {"kind", "upload_id"}:
        return UploadId(validate_upload_id(value.get("upload_id")))
    if kind == "local" and set(value) == {"kind", "path"}:
        if not local_mode:
            raise TargetRefused("LocalPath is accepted only in local mode")
        if not isinstance(value.get("path"), str) or not value["path"]:
            raise TargetRefused("local path is empty")
        return LocalPath(Path(value["path"]))
    raise TargetRefused("job target shape is invalid")


@dataclass(frozen=True)
class Job:
    id: str
    run_id: str | None
    target: PlatformUrl | UploadId | LocalPath
    recipe: Recipe
    state: str
    lease_owner: str | None
    lease_until: float | None
    heartbeat_at: float | None
    claim_token: str | None
    attempt: int
    max_attempts: int
    dead_letter_reason: str | None
    cancel_requested: bool
    progress: dict[str, Any]
    log_path: Path | None
    result_bundle_id: str | None
    tenant_scope: str
    created_at: float
    updated_at: float

    @property
    def token(self) -> str:
        if not self.claim_token:
            raise StaleClaim(f"job {self.id} is not claimed")
        return self.claim_token


@dataclass(frozen=True)
class PreparedIntake:
    """Durable media identity assembled before intake's one deciding transaction."""

    inputs: AnalysisInputs
    media_dir: Path
    duration_ms: int
    checkpoints: Mapping[str, Mapping[str, object]]

    @property
    def analysis_key(self) -> str:
        return self.inputs.analysis_key


class ReservationSeam(Protocol):
    """4d-i implements this inside the intake transaction; 4b-i does not mint credits."""

    def reserve_for_new_run(self, connection: Any, job: Job, intake: PreparedIntake) -> None: ...


class SQLiteCheckpointStore:
    """The service checkpoint protocol backed by ``analysis_runs.checkpoints``.

    Every write is fenced by the claim token of the job that owns the run, and no phase is
    committed until the artefacts it names have been flushed to the device.
    """

    mode: Literal["local", "hosted"]

    def __init__(
        self,
        database: Database,
        work_root: Path,
        *,
        mode: Literal["local", "hosted"] = "hosted",
        options: PipelineOptions | None = None,
        upload_root: Path | None = None,
        claim_token: str | None = None,
        before_commit: Callable[[str, CheckpointPhase], None] | None = None,
        after_commit: Callable[[str, CheckpointPhase], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.work_root = Path(work_root).resolve()
        self.mode = mode
        self.options = options or PipelineOptions()
        self.upload_root = (upload_root or self.work_root / ".uploads").resolve()
        self.claim_token = claim_token
        self.before_commit = before_commit
        self.after_commit = after_commit
        self.clock = clock

    def _document(self, connection: Any, run_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT checkpoints FROM analysis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown analysis run: {run_id}")
        try:
            document = json.loads(row["checkpoints"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid checkpoints for run {run_id}") from exc
        if not isinstance(document, dict):
            raise ValueError(f"invalid checkpoints for run {run_id}")
        return document

    def completed_phases(self, run_id: str) -> frozenset[CheckpointPhase]:
        with self.database.read() as connection:
            document = self._document(connection, run_id)
        # A phase counts only while its artefacts are still there with their recorded content; a
        # checkpoint whose artefact is absent (a rename a power cut lost) is not complete.
        return frozenset(
            phase
            for phase in CHECKPOINT_PHASES
            if phase in document and checkpoint_entry_valid(document[phase], phase=phase)
        )

    def write(
        self,
        run_id: str,
        phase: CheckpointPhase,
        *,
        artefacts: tuple[Path, ...],
        state: dict[str, Any] | None = None,
    ) -> None:
        if phase not in CHECKPOINT_PHASES:
            raise ValueError(f"unknown checkpoint phase: {phase}")
        if self.claim_token is None:
            raise StaleClaim("checkpoint store has no claim token; refusing to write")
        records = durable_records(artefacts)
        if self.before_commit is not None:
            self.before_commit(run_id, phase)
        with self.database.write() as connection:
            require_run_fence(connection, run_id, self.claim_token, self.clock())
            document = self._document(connection, run_id)
            document[phase] = {
                "artefacts": [record["path"] for record in records],
                "artefact_records": records,
                "state": state or {},
            }
            if phase == "primary":
                phase_state = state or {}
                # Money and attempts are monotonic within a run (§2.3.2): a later write may report
                # more spend, never less, so a settlement can never erase recovered spend.
                changed = connection.execute(
                    "UPDATE analysis_runs SET checkpoints=?, "
                    "usd_e6_reserved=MAX(usd_e6_reserved, ?), "
                    "usd_e6_spent=MAX(usd_e6_spent, ?), attempts=MAX(attempts, ?) "
                    "WHERE run_id=? AND claim_token=?",
                    (
                        _json(document),
                        int(phase_state.get("usd_e6_reserved", 0)),
                        int(phase_state.get("usd_e6_spent", 0)),
                        int(phase_state.get("attempts", 0)),
                        run_id,
                        self.claim_token,
                    ),
                ).rowcount
            else:
                changed = connection.execute(
                    "UPDATE analysis_runs SET checkpoints = ? WHERE run_id = ? AND claim_token = ?",
                    (_json(document), run_id, self.claim_token),
                ).rowcount
            if changed != 1:
                raise StaleClaim(f"checkpoint claim is no longer current: {run_id}/{phase}")
        if self.after_commit is not None:
            self.after_commit(run_id, phase)

    def settlement_writer(self, run_id: str) -> SettlementLedger:
        """The run's terminal settlement: its SQLite row under the run fence, in one transaction."""

        return SettlementLedger(
            self.database,
            fence=lambda connection: require_run_fence(
                connection, run_id, self.claim_token, self.clock()
            ),
            clock=self.clock,
        )

    def state(self, run_id: str, phase: CheckpointPhase) -> dict[str, Any]:
        with self.database.read() as connection:
            value = self._document(connection, run_id).get(phase, {})
        state = value.get("state", {}) if isinstance(value, dict) else {}
        return state if isinstance(state, dict) else {}

    def resolve_upload(self, upload_id: str) -> Path:
        candidate = (self.upload_root / validate_upload_id(upload_id)).resolve()
        if not candidate.is_relative_to(self.upload_root) or not candidate.is_file():
            raise TargetRefused(f"unknown upload id: {upload_id}")
        return candidate


def _http_status(outcome: str | None) -> int | None:
    return {
        "auth_error": 401,
        "quota_error": 402,
        "http_429": 429,
        "http_503": 503,
        "http_5xx": 500,
    }.get(outcome)


#: The ledger's own sequence numbers. The durable JSONL numbers the same three events from zero
#: (``contracts.ATTEMPT_EVENT_SEQ``), so a projection must MAP the event kind rather than copy the
#: file's number -- copying it wrote rows the ``seq BETWEEN 1 AND 3`` check rejected.
_EVENT_SEQ = {"prepared": 1, "dispatched": 2, "resolved": 3}


class _EventProjection:
    """Insert immutable ``provider_attempt_events`` rows, fenced by the run's claim token."""

    def __init__(
        self,
        database: Database,
        *,
        run_id: str,
        claim_token: str | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.run_id = run_id
        self.claim_token = claim_token
        self.clock = clock

    def fence(self, connection: Any) -> None:
        if self.claim_token is None:
            raise StaleClaim("attempt journal has no claim token; refusing to write")
        require_run_fence(connection, self.run_id, self.claim_token, self.clock())

    def insert(
        self,
        connection: Any,
        *,
        attempt_id: str,
        provider: str,
        egress_id: str,
        query_id: str | None,
        parent_attempt_id: str | None,
        state: str,
        outcome: str | None,
        unit_usd_e6: int,
        at: str,
        verify_egress: bool = True,
        ordinal: int | None = None,
    ) -> None:
        # ``ON CONFLICT DO NOTHING``, not ``INSERT OR IGNORE``: replaying an event this run already
        # projected is expected, but a row that violates a CHECK must raise instead of vanishing.
        inserted = connection.execute(
            "INSERT INTO provider_attempt_events("
            "attempt_id, seq, run_id, provider, egress_id, query_id, parent_attempt_id, "
            "state, outcome, http_status, unit_usd_e6, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(attempt_id, seq) DO NOTHING",
            (
                attempt_id,
                _EVENT_SEQ[state],
                self.run_id,
                provider,
                egress_id,
                query_id,
                parent_attempt_id,
                state,
                outcome,
                _http_status(outcome),
                unit_usd_e6,
                at,
            ),
        ).rowcount
        if inserted == 1:
            return
        # Replaying an event this run already projected is expected. A DIFFERENT payload under the
        # same ``(attempt_id, seq)`` is two stories about one request: refused, never ignored.
        existing = connection.execute(
            f"SELECT {_ATTEMPT_ROW_COLUMNS} FROM provider_attempt_events "
            "WHERE attempt_id=? AND seq=?",
            (attempt_id, _EVENT_SEQ[state]),
        ).fetchone()
        incoming = AttemptEvent(
            attempt_id=attempt_id,
            event=state,
            run_id=self.run_id,
            provider=provider,
            query_id=query_id,
            parent_attempt_id=parent_attempt_id,
            unit_usd_e6=unit_usd_e6,
            outcome=outcome,
            ordinal=ordinal,
            # A backfill replays a JSONL line, which records no egress: the projected row's own
            # attribution stands. A live duplicate claiming another egress is a conflict.
            egress_id=egress_id if verify_egress else None,
        )
        if existing is None or event_from_row(existing).conflicts(incoming):
            raise LedgerConflict(f"conflicting duplicate {state} event for attempt {attempt_id}")


class SQLiteAttemptJournal(AttemptJournal):
    """Attempt journal that also projects each immutable event into SQLite.

    SQLite is the authority: each event's row (and, for a dispatch, its ``run_dispatches`` row)
    commits under the claim fence first, and the JSONL line is its projection, written after that
    commit. :meth:`backfill` projects a JSONL-only event (an older writer's) into SQLite.
    """

    def __init__(
        self,
        path: Path,
        *,
        database: Database,
        run_id: str,
        provider: str,
        unit_usd_e6: int,
        egress_id: str,
        claim_token: str | None = None,
        clock: Callable[[], float] = time.time,
        job_id: str | None = None,
        hosted: bool = True,
    ) -> None:
        super().__init__(path, run_id=run_id, provider=provider, unit_usd_e6=unit_usd_e6)
        self.database = database
        #: Fail closed: a hosted journal refuses every paid event and reservation (only the free
        #: Shazam provider may journal). The queue's local mode passes ``hosted=False``.
        self.hosted = hosted
        self.egress_id = egress_id
        self.claim_token = claim_token
        self.job_id = job_id
        self.projection = _EventProjection(
            database, run_id=run_id, claim_token=claim_token, clock=clock
        )
        #: Paid dispatches are admitted by the ONE queue check, inside the event's transaction.
        self.dispatch_admission = (
            DispatchAdmission(
                database, job_id=job_id, claim_token=claim_token, clock=clock, hosted=hosted
            )
            if job_id is not None and claim_token is not None and provider == "audd"
            else None
        )

    def backfill(self, *, batch_size: int = 64) -> int:
        """Project any durable JSONL event this run is missing in SQLite.

        A crash between the journal append and the SQLite insert would otherwise leave the hosted
        ledger (the breaker denominator and 4b-ii's operations views) permanently short of an
        attempt the provider really saw.
        """

        if not path_is_file(self.path):
            return 0
        # Read and parsed OUTSIDE any write transaction, then projected in short fenced batches:
        # an unbounded read holding SQLite's writer lock could starve the heartbeat past the lease.
        events = [
            event
            for event in parse_journal_lines(read_text(self.path))
            if event.run_id == self.run_id
        ]
        projected = 0
        batch = max(1, batch_size)
        for start in range(0, len(events), batch):
            with self.database.write() as connection:
                self.projection.fence(connection)
                for event in events[start : start + batch]:
                    self.projection.insert(
                        connection,
                        attempt_id=event.attempt_id,
                        provider=event.provider,
                        egress_id=self.egress_id,
                        query_id=event.query_id,
                        parent_attempt_id=event.parent_attempt_id,
                        state=event.event,
                        outcome=event.outcome,
                        unit_usd_e6=event.unit_usd_e6,
                        at=event.at,
                        verify_egress=False,
                        ordinal=event.ordinal,
                    )
                    projected += 1
        return projected

    def durable_events(self) -> list[Any]:
        """The JSONL AND the validated SQLite projection, folded together.

        A new journal whose directory entry a power cut lost leaves its events in SQLite only; the
        run's paid state still recovers from them rather than restoring zero and re-sending.
        """

        with self.database.read() as connection:
            projected = _run_attempt_events(connection, self.run_id, self.provider)
        return [*super().durable_events(), *projected]

    def _stored_reservation(self, connection: Any) -> ReservationRecord | None:
        row = connection.execute(
            "SELECT CASE WHEN json_valid(checkpoints) "
            "THEN json_extract(checkpoints, '$._reservation') END AS reservation "
            "FROM analysis_runs WHERE run_id=?",
            (self.run_id,),
        ).fetchone()
        if row is None or row["reservation"] is None:
            return None
        try:
            value = json.loads(row["reservation"])
        except (TypeError, json.JSONDecodeError):
            return None
        return ReservationRecord.from_document(value, run_id=self.run_id)

    def durable_reservation(self) -> Any:
        record = super().durable_reservation()
        if record is not None:
            return record
        with self.database.read() as connection:
            return self._stored_reservation(connection)

    def record_reservation(self, reservation: Any) -> Any:
        """Durable in SQLite (fenced, write-once) and beside the journal, before any dispatch."""

        if self.hosted:
            raise DispatchRefused(HOSTED_PAID_REFUSAL)
        record = ReservationRecord.from_reservation(self.run_id, reservation)
        with self.database.write() as connection:
            self.projection.fence(connection)
            existing = self._stored_reservation(connection)
            if existing is not None:
                record = existing
            else:
                connection.execute(
                    "UPDATE analysis_runs SET checkpoints=json_set("
                    "CASE WHEN json_valid(checkpoints) THEN checkpoints ELSE '{}' END, "
                    "'$._reservation', json(?)), usd_e6_reserved=MAX(usd_e6_reserved, ?) "
                    "WHERE run_id=? AND claim_token=?",
                    (
                        json.dumps(record.document()),
                        record.usd_e6_reserved,
                        self.run_id,
                        self.claim_token,
                    ),
                )
        if self.dispatch_admission is not None:
            # The queue's local mode: the same unique, claim-fenced ``run_reservations`` row every
            # paid dispatch requires inside its admission transaction.
            record = self.dispatch_admission.reserve(record)
        super().record_reservation(record.reservation())
        return record

    def for_provider(self, provider: str) -> SQLiteAttemptJournal:
        """The same run's journal for another provider, projected into the same SQLite ledger."""

        return SQLiteAttemptJournal(
            self.path.parent / f"{provider}-{self.path.name}",
            database=self.database,
            run_id=self.run_id,
            provider=provider,
            unit_usd_e6=0,
            egress_id=self.egress_id,
            claim_token=self.claim_token,
            hosted=self.hosted,
            clock=self.projection.clock,
        )

    def events_for(self, provider: str) -> list[Any]:
        with self.database.read() as connection:
            return _run_attempt_events(connection, self.run_id, provider)

    def _persist(self, record: ProviderAttemptEvent) -> None:
        """ONE immutable event object: projected (and verified) and appended in one transaction.

        Fenced first: a worker that lost its lease must not journal — and therefore must not
        dispatch — another paid request. The projection is inserted before the append, so a
        conflicting duplicate is refused before anything reaches the JSONL; a crash after the
        append and before the commit leaves the event in the JSONL, which :meth:`backfill` repairs.
        """

        if self.hosted and self.provider != "shazam":
            # Before any transaction, row or line: hosted paid money is deferred (round 6).
            raise DispatchRefused(HOSTED_PAID_REFUSAL)
        with self.database.write() as connection:
            self.projection.fence(connection)
            if record.event == "dispatched" and self.dispatch_admission is not None:
                # Claim check + authoritative dispatch row, in THIS transaction.
                self.dispatch_admission.admit_in(connection, record, self.path)
            self.projection.insert(
                connection,
                attempt_id=record.attempt_id,
                provider=record.provider,
                egress_id=self.egress_id,
                query_id=record.query_id,
                parent_attempt_id=record.parent_attempt_id,
                state=record.event,
                outcome=record.outcome,
                unit_usd_e6=record.unit_usd_e6,
                at=record.at,
                ordinal=record.ordinal,
            )
        # The JSONL is a projection, written only after the authoritative rows committed.
        append_line(self.path, record)


class LedgerShazamBreaker(ShazamBreaker):
    """One run's view of the worker's ONE process Shazam breaker (§2.3.5).

    Every policy decision -- samples, daily budget, cooldown, latch -- belongs to ``inner``, the
    worker's single process breaker, so its state outlives each job. The breaker no longer writes
    attempt rows: a Shazam request has ONE identity, the recognise journal's own event (the clip
    query id, a deterministic attempt id), which the hosted journal projects into
    ``provider_attempt_events``. Two writers would give one request two identities.
    """

    def __init__(
        self,
        config: BreakerConfig | None = None,
        *,
        database: Database | None = None,
        run_id: str | None = None,
        egress_id: str | None = None,
        claim_token: str | None = None,
        clock: Callable[[], Any] | None = None,
        inner: ShazamBreaker | None = None,
        lease_clock: Callable[[], float] = time.time,
    ) -> None:
        # Deliberately no ShazamBreaker.__init__: this object holds no policy state of its own.
        del database, run_id, egress_id, claim_token, lease_clock
        self.inner = inner if inner is not None else ShazamBreaker(config, clock=clock)

    @property
    def config(self) -> BreakerConfig:  # type: ignore[override]
        return self.inner.config

    def configure(self, config: BreakerConfig) -> None:
        self.inner.configure(config)

    def reenable(self) -> None:
        self.inner.reenable()

    def reason(self) -> str | None:
        return self.inner.reason()

    def dispatch(self, *, running_free: bool) -> Any:
        return self.inner.dispatch(running_free=running_free)

    def release_dispatch(self, dispatch_day: Any) -> None:
        self.inner.release_dispatch(dispatch_day)

    def sent(self) -> None:
        self.inner.sent()

    def resolved(self, outcome: str) -> None:
        self.inner.resolved(outcome)


class JobQueue:
    """All job transitions, each fenced and committed in one immediate transaction."""

    def __init__(
        self,
        database: Database,
        *,
        local_mode: bool = False,
        clock: Callable[[], float] = time.time,
        on_abandon: Callable[[str, str | None, str], None] | None = None,
    ) -> None:
        self.database = database
        self.local_mode = local_mode
        self.clock = clock
        #: Called AFTER a quarantine or dead letter commits, with ``(job id, run id, target)``: the
        #: local worker settles the run's shared ledger there (a hosted run's money is folded into
        #: its ``analysis_runs`` row inside the transaction instead).
        self.on_abandon = on_abandon
        self._abandoned: list[tuple[str, str | None, str]] = []

    def enqueue(
        self,
        target: PlatformUrl | UploadId | LocalPath,
        recipe: Recipe,
        *,
        job_id: str | None = None,
        run_id: str | None = None,
        tenant_scope: str | None = None,
        log_path: Path | None = None,
        progress: Mapping[str, object] | None = None,
    ) -> str:
        document = _target_document(target, local_mode=self.local_mode)
        scope = tenant_scope or (
            LOCAL_OWNER_SCOPE if isinstance(target, (UploadId, LocalPath)) else "public"
        )
        if scope != "public" and not scope.startswith("user:"):
            raise ValueError("invalid tenant scope")
        identifier = job_id or uuid.uuid4().hex
        now = self.clock()
        with self.database.write() as connection:
            connection.execute(
                "INSERT INTO jobs(id, run_id, target, recipe_id, state, log_path, tenant_scope, "
                "progress, created_at, updated_at, money_authority) "
                "VALUES (?, ?, ?, ?, 'intake', ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    run_id,
                    _json(document),
                    recipe.recipe_id,
                    str(Path(log_path).resolve()) if log_path is not None else None,
                    scope,
                    _json(dict(progress or {})),
                    now,
                    now,
                    MONEY_AUTHORITY,
                ),
            )
        return identifier

    def get(self, job_id: str) -> Job:
        with self.database.read() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row(row)

    def _row(self, row: Any) -> Job:
        known_recipes = (*RECIPES.values(), get_recipe("deep", primary_density=2))
        recipe = next(
            (candidate for candidate in known_recipes if candidate.recipe_id == row["recipe_id"]),
            None,
        )
        if recipe is None:
            raise ValueError(f"untrusted job has unknown recipe id: {row['recipe_id']}")
        try:
            progress = json.loads(row["progress"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("untrusted job has invalid progress JSON") from exc
        if not isinstance(progress, dict):
            raise ValueError("untrusted job has invalid progress JSON")
        return Job(
            id=row["id"],
            run_id=row["run_id"],
            target=_read_target(row["target"], local_mode=self.local_mode),
            recipe=recipe,
            state=row["state"],
            lease_owner=row["lease_owner"],
            lease_until=row["lease_until"],
            heartbeat_at=row["heartbeat_at"],
            claim_token=row["claim_token"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            dead_letter_reason=row["dead_letter_reason"],
            cancel_requested=bool(row["cancel_requested"]),
            progress=progress,
            log_path=Path(row["log_path"]) if row["log_path"] else None,
            result_bundle_id=row["result_bundle_id"],
            tenant_scope=row["tenant_scope"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _abandon_run(
        self,
        connection: Any,
        run_id: str | None,
        *,
        status: str,
        reason: str | None,
        now: float,
        job_id: str | None = None,
    ) -> None:
        """Move a run to the same terminal fate as the job that was driving it.

        A dead letter that left its run ``analysis`` is an immortal run: nobody will ever execute
        it, yet the intake transaction would keep attaching new submissions to it. Accounting
        (attempts, reservation, spend) is folded from every durable record first and only ever
        raised: a run that died before ``primary`` still carries what its events prove it spent.
        """

        if run_id is None:
            return
        live_driver = connection.execute(
            f"SELECT 1 FROM analysis_runs WHERE run_id=? AND NOT {_NO_OTHER_LIVE_DRIVER_SQL}",
            (run_id, job_id or "", now),
        ).fetchone()
        if live_driver is not None:
            # Another job drives this run under an unexpired lease: whatever became of THIS row,
            # the run is not its to fold, abandon or unfence.
            return
        # A corrupt ledger must not keep a dead row claimable; the row's own money then stands.
        with suppress(LedgerConflict):
            fold_run_money(connection, run_id)
        connection.execute(
            "UPDATE analysis_runs SET status=?, reason=COALESCE(reason, ?), "
            "finished_at=COALESCE(finished_at, ?), claim_token=NULL "
            f"WHERE run_id=? AND status IN {_ACTIVE_SQL}",
            (status, reason, now, run_id),
        )

    def _quarantine(self, connection: Any, row: Any, reason: str, now: float) -> None:
        """Retire a row this worker cannot even parse, inside the claim transaction.

        One corrupt row used to abort the claim transaction (rolling back its own attempt
        increment) and kill ``run_forever``; the queue must survive its worst row.
        """

        connection.execute(
            "UPDATE jobs SET state='dead_letter', dead_letter_reason=?, lease_owner=NULL, "
            "lease_until=NULL, heartbeat_at=NULL, claim_token=NULL, updated_at=? WHERE id=?",
            (reason, now, row["id"]),
        )
        self._remember_abandoned(row["id"], row["run_id"], row["target"])
        self._abandon_run(
            connection,
            row["run_id"],
            status="dead_letter",
            reason=reason,
            now=now,
            job_id=row["id"],
        )

    def _remember_abandoned(self, job_id: str, run_id: str | None, target: object) -> None:
        if self.on_abandon is None or run_id is None:
            return
        text = ""
        with suppress(TypeError, ValueError, AttributeError):
            value = json.loads(target) if isinstance(target, str) else None
            if isinstance(value, dict):
                text = str(value.get("url") or value.get("path") or value.get("upload_id") or "")
        if isinstance(target, PlatformUrl):
            text = target.url
        elif isinstance(target, LocalPath):
            text = str(target.path)
        self._abandoned.append((job_id, run_id, text))

    def _flush_abandoned(self) -> None:
        pending, self._abandoned = self._abandoned, []
        for job_id, run_id, target in pending:
            with suppress(Exception):  # settlement bookkeeping never breaks the queue
                assert self.on_abandon is not None
                self.on_abandon(job_id, run_id, target)

    def claim(self, worker_id: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> Job | None:
        """Fence the oldest claimable job to a fresh claim token and return it.

        A row quarantined on the way is settled through ``on_abandon`` only once the claim
        transaction has committed; a rolled-back quarantine settles nothing.
        """

        self._abandoned = []
        try:
            job = self._claim(worker_id, lease_seconds=lease_seconds)
        except BaseException:
            self._abandoned = []
            raise
        self._flush_abandoned()
        return job

    def _claim(self, worker_id: str, *, lease_seconds: float) -> Job | None:

        now = self.clock()
        with self.database.write() as connection:
            # Inside the claim transaction: a database upgraded by newer code is never claimed from.
            version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(number), 0) FROM schema_migrations"
                ).fetchone()[0]
            )
            if version > known_schema_version():
                raise SchemaTooNew(
                    f"this work folder's database is at schema {version}, newer than this ID'er "
                    f"understands ({known_schema_version()}): stop it and run the newer ID'er"
                )
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE state IN {_ACTIVE_SQL} "
                "AND attached = 0 "
                "AND (lease_until IS NULL OR lease_until <= ?) ORDER BY created_at, id",
                (now,),
            ).fetchall()
            for row in rows:
                if row["attempt"] >= row["max_attempts"]:
                    self._quarantine(connection, row, "maximum attempts exhausted", now)
                    continue
                token = uuid.uuid4().hex
                changed = connection.execute(
                    "UPDATE jobs SET lease_owner=?, lease_until=?, heartbeat_at=?, claim_token=?, "
                    # Provenance (migration 0003): this claim is made by money-authority code. A
                    # job first claimed by older code, or marked 0 by the trigger, stays unproven.
                    "authority_token=?, money_authority=CASE WHEN money_authority=? "
                    "OR (money_authority IS NULL AND attempt=0) THEN ? ELSE 0 END, "
                    "attempt=attempt+1, "
                    "updated_at=? WHERE id=? AND (lease_until IS NULL OR lease_until <= ?)",
                    (
                        worker_id,
                        now + lease_seconds,
                        now,
                        token,
                        token,
                        MONEY_AUTHORITY,
                        MONEY_AUTHORITY,
                        now,
                        row["id"],
                        now,
                    ),
                ).rowcount
                if changed != 1:
                    continue
                claimed = connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (row["id"],)
                ).fetchone()
                try:
                    job = self._row(claimed)
                except (ValueError, TargetRefused) as exc:
                    self._quarantine(
                        connection, claimed, f"unusable job row: {type(exc).__name__}: {exc}", now
                    )
                    continue
                if job.run_id is not None and not self._rotate_run(
                    connection, job.run_id, row["id"], token, now
                ):
                    # Another job holds this run under a live lease, so this row is not its
                    # driver: give the claim back untouched and leave the run to its owner.
                    connection.execute(
                        "UPDATE jobs SET lease_owner=NULL, lease_until=NULL, heartbeat_at=NULL, "
                        "claim_token=NULL, attempt=MAX(attempt - 1, 0), updated_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                    continue
                return job
        return None

    @staticmethod
    def _rotate_run(connection: Any, run_id: str, job_id: str, token: str, now: float) -> bool:
        """Hand the run's fence to this claim — only if no other job drives it under a live lease.

        Ownership rotates with the claim, so the previous claim's checkpoint, money and journal
        writes are refused from this moment on (including the cancel-before-start path). A run
        with no row yet (intake has not committed) has nothing to rotate.
        """

        if (
            connection.execute("SELECT 1 FROM analysis_runs WHERE run_id=?", (run_id,)).fetchone()
            is None
        ):
            return True
        return (
            connection.execute(
                "UPDATE analysis_runs SET claim_token=? "
                f"WHERE run_id=? AND {_NO_OTHER_LIVE_DRIVER_SQL}",
                (token, run_id, job_id, now),
            ).rowcount
            == 1
        )

    def begin_analysis(self, job_id: str, claim_token: str) -> bool:
        """Make a waiting retry visibly analysis before it enters the pipeline."""

        now = self.clock()
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT run_id FROM jobs WHERE id=? AND claim_token=? AND lease_until > ? "
                "AND state IN ('waiting','analysis')",
                (job_id, claim_token, now),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE jobs SET state='analysis', updated_at=? WHERE id=?",
                (now, job_id),
            )
            connection.execute(
                "UPDATE analysis_runs SET status='analysis' WHERE run_id=? AND claim_token=?",
                (row["run_id"], claim_token),
            )
            return True

    def reconcile_attached(self) -> int:
        """Mirror a coalesced run's terminal result without ever running its pipeline twice."""

        now = self.clock()
        changed = 0
        with self.database.write() as connection:
            rows = connection.execute(
                "SELECT j.id, j.run_id, r.status FROM jobs j "
                "JOIN analysis_runs r ON r.run_id=j.run_id "
                "WHERE j.state='waiting' AND j.cancel_requested=0 AND j.attached=1 "
                f"AND r.status NOT IN {_ACTIVE_SQL}"
            ).fetchall()
            for row in rows:
                bundle = connection.execute(
                    "SELECT bundle_id FROM result_bundles WHERE run_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (row["run_id"],),
                ).fetchone()
                connection.execute(
                    "UPDATE jobs SET state=?, result_bundle_id=?, updated_at=? WHERE id=?",
                    (
                        row["status"],
                        bundle["bundle_id"] if bundle is not None else None,
                        now,
                        row["id"],
                    ),
                )
                changed += 1
        return changed

    def heartbeat(self, job_id: str, claim_token: str, *, lease_seconds: float) -> bool:
        """Renew a lease that has NOT yet expired; an expired lease is the replacement's."""

        now = self.clock()
        with self.database.write() as connection:
            return (
                connection.execute(
                    "UPDATE jobs SET heartbeat_at=?, lease_until=?, updated_at=? "
                    "WHERE id=? AND claim_token=? AND lease_until > ? "
                    f"AND state IN {_ACTIVE_SQL}",
                    (now, now + lease_seconds, now, job_id, claim_token, now),
                ).rowcount
                == 1
            )

    def cancel_unclaimed(self, job_id: str, progress: Mapping[str, object]) -> bool:
        """Settle a job no worker holds as ``cancelled`` at once, in one fenced transaction.

        Only an intake job with no run and no claim qualifies: the moment a worker claims it the
        fence fails and the caller falls back to :meth:`request_cancel`, which that worker honours.
        """

        now = self.clock()
        with self.database.write() as connection:
            # "No run" is no analysis_runs row: a local job carries its durable service run id
            # from submission, long before (and without ever) creating a queue-side run.
            return (
                connection.execute(
                    "UPDATE jobs SET state='cancelled', cancel_requested=1, progress=?, "
                    "lease_owner=NULL, lease_until=NULL, heartbeat_at=NULL, updated_at=? "
                    "WHERE id=? AND state='intake' AND claim_token IS NULL AND (run_id IS NULL "
                    "OR NOT EXISTS (SELECT 1 FROM analysis_runs r WHERE r.run_id = jobs.run_id))",
                    (_json(dict(progress)), now, job_id),
                ).rowcount
                == 1
            )

    def dismiss(self, job_id: str) -> bool:
        """Hide one terminal job from the owner's activity list; running work is never touched."""

        now = self.clock()
        with self.database.write() as connection:
            return (
                connection.execute(
                    "UPDATE jobs SET progress=json_set(progress, '$.dismissed', 1), updated_at=? "
                    f"WHERE id=? AND state NOT IN {_ACTIVE_SQL}",
                    (now, job_id),
                ).rowcount
                == 1
            )

    def adopt_run_id(self, job_id: str, claim_token: str) -> str:
        """The claimed job's ONE service run id, durable in the column AND the snapshot at once.

        The normalised ``jobs.run_id`` is the only authority; a row that somehow has none gets one
        minted by :func:`new_run_id` and written to both places in a single fenced transaction.
        """

        now = self.clock()
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT run_id FROM jobs WHERE id=? AND claim_token=? AND lease_until > ? "
                f"AND state IN {_ACTIVE_SQL}",
                (job_id, claim_token, now),
            ).fetchone()
            if row is None:
                raise StaleClaim(f"job {job_id} lost its claim before its run id was adopted")
            run_id = row["run_id"] or new_run_id()
            connection.execute(
                "UPDATE jobs SET run_id=?, progress=CASE WHEN json_valid(progress) THEN "
                "CASE WHEN json_type(progress, '$.local') = 'object' "
                "THEN json_set(progress, '$.local.run_id', ?) ELSE progress END "
                "ELSE progress END WHERE id=? AND claim_token=?",
                (run_id, run_id, job_id, claim_token),
            )
            return run_id

    def settlement_candidates(self, states: frozenset[str]) -> list[tuple[str, str, str, str]]:
        """``(job id, run id, target, state)`` of settled-state jobs that carry a service run."""

        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT id, run_id, target, state FROM jobs WHERE run_id IS NOT NULL "
                f"AND state IN ({', '.join('?' for _ in states)})",
                tuple(sorted(states)),
            ).fetchall()
        found: list[tuple[str, str, str, str]] = []
        for row in rows:
            with suppress(TypeError, ValueError, AttributeError):
                value = json.loads(row["target"])
                text = str(value.get("url") or value.get("path") or "")
                if text:
                    found.append((row["id"], row["run_id"], text, row["state"]))
        return found

    def unidentified_terminal_rows(self) -> list[dict[str, Any]]:
        """Stopped local jobs with no ``run_id`` (pre-upgrade rows) and their time window."""

        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT id, target, state, progress, created_at, updated_at FROM jobs "
                f"WHERE run_id IS NULL AND state NOT IN {_ACTIVE_SQL}"
            ).fetchall()
        found: list[dict[str, Any]] = []
        for row in rows:
            with suppress(TypeError, ValueError, AttributeError):
                target = json.loads(row["target"])
                text = str(target.get("url") or target.get("path") or "")
                progress = json.loads(row["progress"]) if row["progress"] else {}
                local = progress.get("local") if isinstance(progress, dict) else None
                if not text or not isinstance(local, dict):
                    continue
                start = local.get("started_at") or local.get("created_at") or row["created_at"]
                end = local.get("finished_at") or row["updated_at"]
                found.append(
                    {
                        "job_id": row["id"],
                        "target": text,
                        "state": row["state"],
                        "start": float(start),
                        "end": float(end),
                    }
                )
        return found

    def request_cancel(self, job_id: str) -> bool:
        now = self.clock()
        with self.database.write() as connection:
            # An ATTACHED job drives nothing: cancelling it is an explicit detachment, settled at
            # once, which reconciliation can never overwrite with the driving run's result.
            detached = connection.execute(
                "UPDATE jobs SET state='cancelled', cancel_requested=1, "
                "progress=json_set(CASE WHEN json_valid(progress) THEN progress ELSE '{}' END, "
                "'$.detached', 1), lease_owner=NULL, lease_until=NULL, heartbeat_at=NULL, "
                "claim_token=NULL, updated_at=? "
                f"WHERE id=? AND state='waiting' AND claim_token IS NULL AND {_ATTACHED_SQL}",
                (now, job_id),
            ).rowcount
            if detached == 1:
                return True
            return (
                connection.execute(
                    "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=? "
                    f"AND state IN {_ACTIVE_SQL}",
                    (now, job_id),
                ).rowcount
                == 1
            )

    def cancel_requested(self, job_id: str, claim_token: str) -> bool:
        """True when this claim must stop: cancelled, gone, reclaimed, or lease expired."""

        now = self.clock()
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT cancel_requested, claim_token, lease_until FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        if row is None or row["claim_token"] != claim_token:
            return True
        if row["lease_until"] is None or row["lease_until"] <= now:
            return True
        return bool(row["cancel_requested"])

    def update_progress(
        self, job_id: str, claim_token: str, progress: Mapping[str, object], *, lease_seconds: float
    ) -> bool:
        now = self.clock()
        with self.database.write() as connection:
            return (
                connection.execute(
                    "UPDATE jobs SET progress=?, heartbeat_at=?, lease_until=?, updated_at=? "
                    "WHERE id=? AND claim_token=? AND lease_until > ? "
                    f"AND state IN {_ACTIVE_SQL}",
                    (_json(progress), now, now + lease_seconds, now, job_id, claim_token, now),
                ).rowcount
                == 1
            )

    def fail(self, job: Job, claim_token: str, reason: str, *, permanent: bool = False) -> None:
        now = self.clock()
        dead = permanent or job.attempt >= job.max_attempts
        state = "dead_letter" if dead else job.state
        dead_reason = reason if dead else None
        self._abandoned = []
        try:
            self._fail(
                job, claim_token, reason, now=now, dead=dead, state=state, dead_reason=dead_reason
            )
        except BaseException:
            self._abandoned = []
            raise
        self._flush_abandoned()

    def _fail(
        self,
        job: Job,
        claim_token: str,
        reason: str,
        *,
        now: float,
        dead: bool,
        state: str,
        dead_reason: str | None,
    ) -> None:
        with self.database.write() as connection:
            changed = connection.execute(
                "UPDATE jobs SET state=?, dead_letter_reason=?, lease_owner=NULL, "
                "lease_until=NULL, heartbeat_at=NULL, claim_token=NULL, updated_at=? "
                f"WHERE id=? AND claim_token=? AND lease_until > ? AND state IN {_ACTIVE_SQL}",
                (state, dead_reason, now, job.id, claim_token, now),
            ).rowcount
            if changed != 1:
                return
            if dead:
                self._remember_abandoned(job.id, job.run_id, job.target)
                self._abandon_run(
                    connection,
                    job.run_id,
                    status="dead_letter",
                    reason=reason,
                    now=now,
                    job_id=job.id,
                )
            else:
                # Retryable: the run stays claimable, but this claim no longer owns it.
                connection.execute(
                    "UPDATE analysis_runs SET claim_token=NULL WHERE run_id=? AND claim_token=?",
                    (job.run_id, claim_token),
                )

    def terminal(
        self,
        job_id: str,
        claim_token: str,
        result: RunResult,
        *,
        bundle_path: Path | None,
        progress: Mapping[str, object] | None = None,
    ) -> bool:
        if result.status not in TERMINAL_STATES:
            raise ValueError(f"not a terminal status: {result.status}")
        now = self.clock()
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT run_id FROM jobs WHERE id=? AND claim_token=? AND lease_until > ? "
                f"AND state IN {_ACTIVE_SQL}",
                (job_id, claim_token, now),
            ).fetchone()
            if row is None:
                return False
            if row["run_id"] is not None:
                # Durable provider events first (§2.3.2): exactly one settlement, never below them.
                fold_run_money(connection, row["run_id"])
                # MAX, not assignment: money and attempts already recovered onto this run are
                # facts. A settlement that reported less (a cancel before restart, a refusal)
                # would erase spend the owner was really charged.
                connection.execute(
                    "UPDATE analysis_runs SET achieved=?, status=?, reason=?, "
                    "attempts=MAX(attempts, ?), "
                    "usd_e6_reserved=MAX(usd_e6_reserved, ?), usd_e6_spent=MAX(usd_e6_spent, ?), "
                    "finished_at=?, claim_token=NULL WHERE run_id=? AND claim_token=?",
                    (
                        _json(result.achieved) if result.achieved is not None else None,
                        result.status,
                        result.reason,
                        result.attempts,
                        result.usd_e6_reserved,
                        result.usd_e6_spent,
                        now,
                        row["run_id"],
                        claim_token,
                    ),
                )
            bundle_id = None
            if result.bundle_id is not None and bundle_path is not None:
                manifest = read_bundle_manifest(bundle_path)
                if manifest is None or bundle_path.name != result.bundle_id:
                    raise ValueError("result bundle is not durably published")
                connection.execute(
                    "INSERT OR IGNORE INTO result_bundles(bundle_id, run_id, presentation_version, "
                    "path, manifest_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        result.bundle_id,
                        row["run_id"],
                        manifest["presentation_version"],
                        str(bundle_path.resolve()),
                        sha256_file(bundle_path / "manifest.json"),
                        now,
                    ),
                )
                bundle_id = result.bundle_id
            changed = connection.execute(
                "UPDATE jobs SET state=?, result_bundle_id=?, lease_owner=NULL, lease_until=NULL, "
                "heartbeat_at=NULL, claim_token=NULL, progress=COALESCE(?, progress), updated_at=? "
                "WHERE id=? AND claim_token=?",
                (
                    result.status,
                    bundle_id,
                    _json(dict(progress)) if progress is not None else None,
                    now,
                    job_id,
                    claim_token,
                ),
            ).rowcount
            return changed == 1

    def wait(
        self,
        job_id: str,
        claim_token: str,
        reason: str | None,
        *,
        cooldown_seconds: float = WAIT_COOLDOWN_SECONDS,
        state: str = "waiting",
    ) -> bool:
        """Park a job that has not failed: give the attempt back and hold it for a cooldown.

        ``state`` is ``waiting`` for §4.6's provider wait. A job deferred *during intake* has no
        run yet and is parked back in ``intake``: parking it in ``waiting`` would send the next
        claim down the analysis path of a job that has nothing to resume.
        """

        now = self.clock()
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT run_id FROM jobs WHERE id=? AND claim_token=? AND lease_until > ? "
                f"AND state IN {_ACTIVE_SQL}",
                (job_id, claim_token, now),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE analysis_runs SET status='waiting', reason=?, claim_token=NULL "
                "WHERE run_id=? AND claim_token=?",
                (reason, row["run_id"], claim_token),
            )
            connection.execute(
                "UPDATE jobs SET state=?, progress=?, lease_owner=NULL, "
                "lease_until=?, heartbeat_at=NULL, claim_token=NULL, "
                "attempt=MAX(attempt - 1, 0), updated_at=? WHERE id=?",
                (
                    state,
                    _json({"attached": 0, "reason": reason}),
                    now + max(0.0, cooldown_seconds),
                    now,
                    job_id,
                ),
            )
            return True

    def expire_dead_letters(self) -> int:
        """After 24 hours a dead letter has the externally visible terminal state ``failed``."""

        now = self.clock()
        with self.database.write() as connection:
            rows = connection.execute(
                "SELECT id, run_id FROM jobs WHERE state='dead_letter' AND updated_at <= ?",
                (now - 24 * 60 * 60,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE jobs SET state='failed', updated_at=? WHERE id=?", (now, row["id"])
                )
                if row["run_id"] is not None:
                    connection.execute(
                        "UPDATE analysis_runs SET status='failed', "
                        "finished_at=COALESCE(finished_at, ?), claim_token=NULL "
                        "WHERE run_id=? AND status IN ('dead_letter', 'intake', 'waiting', "
                        "'analysis')",
                        (now, row["run_id"]),
                    )
            return len(rows)


IntakeResolver = Callable[["Job", SQLiteCheckpointStore], PreparedIntake]
ServiceRunner = Callable[[RunRequest], RunResult]


def _display_target(target: object) -> str:
    """A job's target as one string, for the page document's fallback label.

    The page reads its real target from the job's own column (never from the snapshot), so this is
    only the label a job wears before its title resolves.
    """

    for attribute in ("url", "path", "upload_id"):
        value = getattr(target, attribute, None)
        if value is not None:
            return str(value)
    return str(target)


def _page_profile(recipe: Recipe) -> str:
    """The profile name the RENDERER understands, not the recipe's own name.

    `present/server.py` (frozen by §4.2) decides "this run could have cost money" by looking for
    the profile ``max_accuracy``; a Deep run labelled ``deep`` renders as a Free scan and tells the
    owner nothing was spent. The mapping therefore happens here, on the web side.
    """

    return "max_accuracy" if recipe_uses_paid_engine(recipe) else "free"


def _plain_path(path: Path) -> Path:
    r"""``resolve()`` without Windows' ``\\?\`` prefix, so ``relative_to`` keeps working."""

    text = str(Path(path).resolve())
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return Path(text)


def _result_path(work_root: Path, bundle_dir: Path | None) -> str | None:
    """A served, work-root-relative result path — the route the web layer actually answers.

    The page links ``/<result_path>``; a bare bundle id would point at the site root and 404.
    """

    if bundle_dir is None:
        return None
    try:
        relative = _plain_path(bundle_dir).relative_to(_plain_path(work_root))
    except ValueError:
        return None
    return (relative / "index.html").as_posix()


class Worker:
    """One queue consumer. It has no HTTP imports and serves no requests."""

    def __init__(
        self,
        database: Database,
        work_root: Path,
        *,
        worker_id: str | None = None,
        local_mode: bool = False,
        options: PipelineOptions | None = None,
        intake_resolver: IntakeResolver | None = None,
        service_runner: ServiceRunner | None = None,
        reservation_seam: ReservationSeam | None = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        wait_cooldown_seconds: float = WAIT_COOLDOWN_SECONDS,
        egress_id: str = "default",
        clock: Callable[[], float] = time.time,
        checkpoint_before_commit: Callable[[str, CheckpointPhase], None] | None = None,
        checkpoint_after_commit: Callable[[str, CheckpointPhase], None] | None = None,
    ) -> None:
        self.database = database
        self.work_root = Path(work_root).resolve()
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex}"
        self.local_mode = local_mode
        self.options = options or PipelineOptions()
        self.intake_resolver = intake_resolver or self._prepare_intake
        if service_runner is None:
            from id_detector.service import run

            service_runner = run
        self.service_runner = service_runner
        self.reservation_seam = reservation_seam
        self.heartbeat_seconds = heartbeat_seconds
        self.lease_seconds = lease_seconds
        self.wait_cooldown_seconds = wait_cooldown_seconds
        self.egress_id = egress_id
        self.checkpoint_before_commit = checkpoint_before_commit
        self.checkpoint_after_commit = checkpoint_after_commit
        self.queue = JobQueue(database, local_mode=local_mode, clock=clock)
        config = self.options.app_config or AppConfig()
        #: ONE breaker for this egress (§2.3.5), shared by every worker process rather than owned
        #: by this one: its denominator is the resolved Shazam attempts in `provider_attempt_events`
        #: and its daily budget the **dispatched** ones -- an attempt that was only ever prepared
        #: was never sent and spends nothing (§2.3.3) -- so two workers cannot each see a third of
        #: the failures and each conclude the rate is fine. Only the open deadline, the day's open
        #: count, the latch and the re-enable cutoff are stored (4b-ii).
        self.process_breaker: ShazamBreaker = self.options.shazam_breaker or SharedShazamBreaker(
            database,
            egress_id=egress_id,
            config=getattr(config, "shazam_breaker", None),
        )
        self._draining = threading.Event()

    def drain(self) -> None:
        """Stop claiming new work; an already-running job retains its heartbeat."""

        self._draining.set()

    def run_forever(
        self,
        *,
        poll_seconds: float = 0.5,
        stop: threading.Event | None = None,
    ) -> None:
        """Consume jobs until drained or stopped, without interrupting the current job."""

        external_stop = stop or threading.Event()
        while not self._draining.is_set() and not external_stop.is_set():
            try:
                self.queue.expire_dead_letters()
                claimed = self.run_once()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                # One job -- or one unreadable row -- must never end the consumer.
                external_stop.wait(poll_seconds)
                continue
            if claimed is None:
                external_stop.wait(poll_seconds)

    def run_once(self) -> Job | None:
        if self._draining.is_set():
            return None
        self.queue.reconcile_attached()
        job = self.queue.claim(self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return None
        token = job.token
        # The heartbeat covers the WHOLE claim, intake included: fetching, decoding and hints take
        # far longer than a lease, and an unrenewed intake is reclaimed while it is still running
        # -- the replacement then repeats the fetch and the pair can burn every attempt.
        with self._heartbeat(job.id, token):
            try:
                if job.cancel_requested:
                    self._cancel_before_start(job, token)
                    return self._current(job.id)
                if not self.local_mode and recipe_uses_paid_engine(job.recipe):
                    # Hosted paid money is deferred to the hosted cycle: refused here, before
                    # intake, any run row, reservation or dispatch -- and permanently.
                    self.queue.fail(job, token, HOSTED_PAID_REFUSAL, permanent=True)
                    return self._current(job.id)
                if (job.state == "intake" or job.run_id is None) and not recipe_uses_paid_engine(
                    job.recipe
                ):
                    # §2.3.5: while the breaker is open, a NEW free job waits instead of starting.
                    # A free primary that is already running is never interrupted (this branch is
                    # only reached before a run exists), and waiting costs the job no attempt.
                    try:
                        open_reason = self.process_breaker.reason()
                    except Exception:
                        open_reason = None  # an unreadable breaker must never stall the queue
                    if open_reason is not None:
                        self.queue.wait(
                            job.id,
                            token,
                            open_reason,
                            cooldown_seconds=self.wait_cooldown_seconds,
                            state="intake" if job.run_id is None else "waiting",
                        )
                        return self._current(job.id)
                # ONE page document spans the whole claim -- intake, a cache hit, an attachment,
                # the analysis and its terminal state -- and it resumes what the previous attempt
                # left, so a job that fails in intake is still visible with its cause (U-F33) and
                # a retry continues the bar instead of resetting it (U-F9).
                tracker = self._tracker(job)
                tracker.start()
                self._publish(job.id, token, tracker.tick("intake", 0, 1, ""))
                if job.state == "intake" or job.run_id is None:
                    # No run yet, whatever the row says: there is nothing to resume, so this claim
                    # must go through intake rather than down the analysis path.
                    intake = self.intake_resolver(job, self._store(job))
                    decision = self._commit_intake(job, intake, tracker)
                    if decision != "analysis":
                        return self._current(job.id)
                    job = self.queue.get(job.id)
                else:
                    intake = self._intake_for_run(job)
                    if not self.queue.begin_analysis(job.id, token):
                        raise StaleClaim("job lease was lost before analysis")
                    job = self.queue.get(job.id)
                self._run_analysis(job, intake, token, tracker)
            except JobStoreLocked as exc:
                # Another process holds the source or media lock: not a failure, so the attempt is
                # returned and the job is retried after a cooldown.
                self.queue.wait(
                    job.id,
                    token,
                    f"source_busy: {exc}",
                    cooldown_seconds=self.wait_cooldown_seconds,
                    state=job.state if job.state == "intake" else "waiting",
                )
            except asyncio.CancelledError:
                self._cancel_before_start(job, token)
            except StaleClaim:
                pass  # the replacement owns this job now; write nothing
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                self.queue.fail(job, token, f"{type(exc).__name__}: {exc}")
        return self._current(job.id)

    def _current(self, job_id: str) -> Job | None:
        try:
            return self.queue.get(job_id)
        except (KeyError, ValueError, TargetRefused):
            return None

    def _tracker(self, job: Job) -> PageProgress:
        """This claim's page document, RESUMING whatever the previous attempt left.

        A retry that started a fresh tracker would throw away the measured phase durations and the
        monotonic high-water mark, so the bar would snap backwards on every reclaim (U-F9).
        """

        return PageProgress(
            self.work_root,
            job.id,
            _display_target(job.target),
            profile=_page_profile(job.recipe),
            run_id=job.run_id,
            created_at=getattr(job, "created_at", None),
            resume=page_document(job.progress),
        )

    def _publish(self, job_id: str, claim_token: str, document: Mapping[str, object]) -> None:
        """Publish the page document under the claim fence; a refused write is a lost claim."""

        if not self.queue.update_progress(
            job_id, claim_token, document, lease_seconds=self.lease_seconds
        ):
            raise StaleClaim("job lease was lost while publishing progress")

    @contextmanager
    def _heartbeat(self, job_id: str, claim_token: str) -> Iterator[None]:
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self.heartbeat_seconds):
                if not self.queue.heartbeat(job_id, claim_token, lease_seconds=self.lease_seconds):
                    return

        thread = threading.Thread(target=beat, daemon=True, name=f"idea-heartbeat-{job_id}")
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=max(1.0, self.heartbeat_seconds * 2))

    def _options_for(self, job: Job) -> PipelineOptions:
        """This job's pipeline options.

        ``tenant_scope`` must be the job's: intake derives the analysis key from it, and a pipeline
        that derived a different scope would compute a different compatibility identity and defeat
        serving the result back to its owner.
        """

        return replace(self.options, tenant_scope=job.tenant_scope)

    def _store(self, job: Job) -> SQLiteCheckpointStore:
        return SQLiteCheckpointStore(
            self.database,
            self.work_root,
            mode="local" if self.local_mode else "hosted",
            options=self._options_for(job),
            claim_token=job.claim_token,
            before_commit=self.checkpoint_before_commit,
            after_commit=self.checkpoint_after_commit,
            clock=self.queue.clock,
        )

    def _prepare_intake(self, job: Job, store: SQLiteCheckpointStore) -> PreparedIntake:
        target_value, source_kind = self._target_value(job.target, store)
        options = self._options_for(job)
        # The same locks the pipeline takes, in the same order and with the same keys: intake
        # fetches and decodes into the shared media directory, so it cannot run beside another
        # process doing the same. They are released before the pipeline re-acquires them.
        source_lock = ProcessLock(
            self.work_root / ".locks" / f"{sha256(target_value.encode('utf-8')).hexdigest()}.lock"
        )
        media_lock: ProcessLock | None = None

        async def prepare() -> PreparedIntake:
            nonlocal media_lock
            retained = _load_cached(self.work_root, target_value)
            ingested = retained or await ingest(target_value, self.work_root)
            media_lock = ProcessLock(ingested.media_dir / ".media.lock")
            media_lock.acquire()
            # A retained result keeps its duration in the bundle manifest after retention has
            # pruned the PCM and the original. Decoding unconditionally here turned a servable
            # stored bundle into three failed attempts and a dead letter.
            retained_manifest = (
                read_bundle_manifest(ingested.source_path.parent) if retained else None
            )
            decoded = None
            if retained_manifest is not None:
                duration_ms = int(retained_manifest["duration_ms"])
            else:
                decoded = await decode(ingested)
                duration_ms = decoded.record.pcm.duration_ms
            hint_result = None
            if not options.no_hints:
                config = options.app_config or AppConfig()
                hint_result = await run_hints(
                    source=ingested.record,
                    duration_ms=duration_ms,
                    media_dir=ingested.media_dir,
                    source_path=ingested.source_path,
                    project_root=options.project_root,
                    manual_tracklist=None,
                    confirmed_mirrors=options.confirmed_mirrors,
                    refresh=options.refresh,
                    disabled_connectors=config.disabled_hint_connectors,
                )
            panako_id = index_identity(options.index_root, options.local_index_label)
            inputs = AnalysisInputs(
                media_key=ingested.record.media_key,
                recipe_id=job.recipe.recipe_id,
                source_kind=source_kind,  # type: ignore[arg-type]
                tenant_scope=job.tenant_scope,
                hints_snapshot_id=hints_snapshot(hint_result.hints if hint_result else ()),
                manual_tracklist_sha256="",
                panako_index_id=panako_id,
            )
            ingest_artefacts = [str(ingested.source_path.resolve())]
            if path_is_file(ingested.original_path):
                ingest_artefacts.append(str(ingested.original_path.resolve()))
            checkpoints: dict[str, Mapping[str, object]] = {
                "ingest": {"artefacts": ingest_artefacts, "state": {}}
            }
            if decoded is not None:
                checkpoints["decode"] = {
                    "artefacts": [
                        str(decoded.pcm_path.resolve()),
                        str(decoded.record_path.resolve()),
                    ],
                    "state": {},
                }
            return PreparedIntake(
                inputs=inputs,
                media_dir=ingested.media_dir.resolve(),
                duration_ms=duration_ms,
                checkpoints=checkpoints,
            )

        source_lock.acquire()
        try:
            return asyncio.run(prepare())
        finally:
            if media_lock is not None:
                media_lock.release()
            source_lock.release()

    def _target_value(
        self, target: PlatformUrl | UploadId | LocalPath, store: SQLiteCheckpointStore
    ) -> tuple[str, str]:
        if isinstance(target, PlatformUrl):
            return validate_platform_url(target.url), "platform"
        if isinstance(target, UploadId):
            return str(store.resolve_upload(validate_upload_id(target.upload_id))), "upload"
        if isinstance(target, LocalPath) and self.local_mode:
            return str(target.path), "local"
        raise TargetRefused("LocalPath is accepted only in local mode")

    def _compatible_bundle(
        self, connection: Any, intake: PreparedIntake, recipe: Recipe
    ) -> tuple[str, str, Path] | None:
        request = CompatibilityRequest(
            intake.inputs, recipe, accept_degraded=False, local=self.local_mode
        )
        rows = connection.execute(
            "SELECT b.bundle_id, b.path, r.run_id FROM result_bundles b "
            "JOIN analysis_runs r ON r.run_id=b.run_id WHERE r.media_key=? "
            "ORDER BY b.created_at DESC",
            (intake.inputs.media_key,),
        ).fetchall()
        config = self.options.app_config or AppConfig()
        for row in rows:
            path = Path(row["path"]).resolve()
            if not path.is_relative_to(self.work_root):
                continue
            manifest = read_bundle_manifest(path)
            if manifest is None:
                continue
            stored = {
                **(manifest.get("compatibility") or {}),
                "status": manifest["status"],
                "achieved": manifest.get("achieved"),
            }
            if serves(stored, request, serve_free_from_deep=config.serve_free_from_deep):
                # The path is part of the answer: compatibility selects by ``media_key``, and two
                # source keys can share one media. Rebuilding the URL under the NEW intake's media
                # directory would then hand the owner a link that 404s.
                return row["run_id"], row["bundle_id"], path
        return None

    def _commit_intake(
        self, job: Job, intake: PreparedIntake, tracker: PageProgress | None = None
    ) -> str:
        # Normally the claim's own tracker, carrying what intake already measured; a caller that
        # commits an intake on its own gets one built from the job's stored document instead, so
        # the cache-hit and attachment branches always have a page document to publish.
        tracker = self._tracker(job) if tracker is None else tracker
        now = self.queue.clock()
        token = job.token
        checkpoints = self._validated_checkpoints(intake.checkpoints)
        with self.database.write() as connection:
            owned = connection.execute(
                "SELECT * FROM jobs WHERE id=? AND claim_token=? AND state='intake' "
                "AND lease_until > ?",
                (job.id, token, self.queue.clock()),
            ).fetchone()
            if owned is None:
                raise StaleClaim("job lease was lost during intake")
            connection.execute(
                "INSERT OR IGNORE INTO media(media_key, duration_ms, first_seen) VALUES (?, ?, ?)",
                (intake.inputs.media_key, intake.duration_ms, now),
            )
            compatible = self._compatible_bundle(connection, intake, job.recipe)
            if compatible is not None:
                run_id, bundle_id, bundle_path = compatible
                # A cache hit is a finished job: it gets the same page document an analysed one
                # gets, pointing at the bundle the lookup actually SELECTED and validated.
                served = tracker.settle(
                    "complete",
                    result_path=_result_path(self.work_root, bundle_path),
                    usd_e2_spent=0,
                    spend_known=True,
                )
                connection.execute(
                    "UPDATE jobs SET run_id=?, state='complete', result_bundle_id=?, "
                    "lease_owner=NULL, lease_until=NULL, heartbeat_at=NULL, claim_token=NULL, "
                    "progress=?, updated_at=? WHERE id=? AND claim_token=?",
                    (
                        run_id,
                        bundle_id,
                        _json({**served, "phase": "intake", "served": True}),
                        now,
                        job.id,
                        token,
                    ),
                )
                return "complete"
            # Attach only to a run somebody is still driving. A run whose job dead-lettered is
            # abandoned: attaching to it would leave the new submission waiting forever.
            attached = connection.execute(
                "SELECT r.run_id FROM analysis_runs r JOIN jobs j ON j.run_id=r.run_id "
                f"WHERE r.analysis_key=? AND r.status IN {_ACTIVE_SQL} "
                f"AND j.state IN {_ACTIVE_SQL} AND j.id<>? "
                "ORDER BY r.started_at LIMIT 1",
                (intake.analysis_key, job.id),
            ).fetchone()
            if attached is not None:
                connection.execute(
                    "UPDATE jobs SET run_id=?, state='waiting', attached=1, lease_owner=NULL, "
                    "lease_until=NULL, heartbeat_at=NULL, claim_token=NULL, progress=?, "
                    "updated_at=? WHERE id=? AND claim_token=?",
                    (
                        attached["run_id"],
                        _json(
                            {
                                **tracker.tick("intake", 1, 1, ""),
                                "phase": "intake",
                                "attached": True,
                            }
                        ),
                        now,
                        job.id,
                        token,
                    ),
                )
                return "waiting"
            run_id = job.run_id or new_run_id()
            if self.reservation_seam is not None:
                self.reservation_seam.reserve_for_new_run(connection, job, intake)
            connection.execute(
                "INSERT INTO analysis_runs(run_id, analysis_key, media_key, requested_recipe_id, "
                "algorithm_version, adapter_versions, status, tenant_scope, checkpoints, "
                "started_at, analysis_inputs, non_recipe_key, claim_token) "
                "VALUES (?, ?, ?, ?, ?, ?, 'analysis', ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    intake.analysis_key,
                    intake.inputs.media_key,
                    job.recipe.recipe_id,
                    job.recipe.algorithm_version,
                    _json(dict(job.recipe.adapter_versions)),
                    job.tenant_scope,
                    _json(checkpoints),
                    now,
                    _json(vars(intake.inputs)),
                    intake.inputs.non_recipe_key,
                    token,
                ),
            )
            changed = connection.execute(
                "UPDATE jobs SET run_id=?, state='analysis', progress=?, updated_at=? "
                "WHERE id=? AND claim_token=?",
                (
                    run_id,
                    _json(
                        {
                            **tracker.tick("intake", 1, 1, ""),
                            "phase": "intake",
                            "done": 1,
                            "total": 1,
                        }
                    ),
                    now,
                    job.id,
                    token,
                ),
            ).rowcount
            if changed != 1:
                raise StaleClaim("job lease was lost while committing intake")
        return "analysis"

    @staticmethod
    def _validated_checkpoints(
        checkpoints: Mapping[str, Mapping[str, object]],
    ) -> dict[str, Mapping[str, object]]:
        result: dict[str, Mapping[str, object]] = {}
        for phase, checkpoint in checkpoints.items():
            if phase not in CHECKPOINT_PHASES:
                raise ValueError(f"unknown intake checkpoint phase: {phase}")
            artefacts = checkpoint.get("artefacts", ())
            if not isinstance(artefacts, (list, tuple)) or not all(
                isinstance(path, str) for path in artefacts
            ):
                raise ValueError(f"checkpoint artefacts are not durable: {phase}")
            # Same boundary as the service's checkpoints: contents flushed before the row that
            # names them is committed (§3.4), never merely "the file exists".
            records = durable_records([Path(path) for path in artefacts])
            state = checkpoint.get("state", {})
            if not isinstance(state, dict):
                raise ValueError(f"checkpoint state is invalid: {phase}")
            result[phase] = {
                "artefacts": [record["path"] for record in records],
                "artefact_records": records,
                "state": state,
            }
        return result

    def _intake_for_run(self, job: Job) -> PreparedIntake:
        if job.run_id is None:
            raise ValueError("analysis job has no run id")
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_runs WHERE run_id=?", (job.run_id,)
            ).fetchone()
        if row is None:
            raise ValueError("analysis job names an unknown run")
        inputs = AnalysisInputs(**json.loads(row["analysis_inputs"]))
        media_dirs = list(self.work_root.glob(f"*/{inputs.media_key}"))
        if not media_dirs:
            raise ValueError("analysis media directory is missing")
        return PreparedIntake(
            inputs=inputs,
            media_dir=media_dirs[0],
            duration_ms=self._duration(inputs.media_key),
            checkpoints=json.loads(row["checkpoints"]),
        )

    def _duration(self, media_key: str) -> int:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT duration_ms FROM media WHERE media_key=?", (media_key,)
            ).fetchone()
        if row is None:
            raise ValueError("analysis media row is missing")
        return int(row["duration_ms"])

    def _run_analysis(
        self, job: Job, intake: PreparedIntake, claim_token: str, tracker: PageProgress
    ) -> None:
        if job.run_id is None:
            raise ValueError("analysis job has no run id")

        class QueueCancelToken:
            def is_set(inner_self) -> bool:
                del inner_self
                return self.queue.cancel_requested(job.id, claim_token)

        # The claim's page document continues here -- it already carries intake, and on a retry the
        # measured time and monotonic bar of the attempt before it. Published again as analysis
        # begins so a run that fails in its very first phase is still visible with its cause.
        self._publish(job.id, claim_token, tracker.document())

        def progress(phase: str, done: int, total: int, message: str) -> None:
            document = tracker.tick(phase, done, total, message)
            updated = self.queue.update_progress(
                job.id,
                claim_token,
                {**document, "phase": phase, "done": done, "total": total, "message": message},
                lease_seconds=self.lease_seconds,
            )
            if not updated or self.queue.cancel_requested(job.id, claim_token):
                raise asyncio.CancelledError("job cancelled or lease lost")

        options = self._options_for(job)
        app_config = options.app_config or AppConfig()
        journal = self._journal(job.run_id, claim_token, app_config, job_id=job.id)
        # Repair the projection before trusting it: an event may be on disk in the durable JSONL
        # and missing from SQLite if the previous pass died between the two.
        journal.backfill()
        # Always decorated, never bypassed: a caller-supplied breaker is this worker's process
        # breaker, and this run's attempts still reach the hosted ledger.
        options = replace(
            options,
            shazam_breaker=LedgerShazamBreaker(
                None,
                database=self.database,
                run_id=job.run_id,
                egress_id=self.egress_id,
                claim_token=claim_token,
                inner=self.process_breaker,
                lease_clock=self.queue.clock,
            ),
        )
        store = SQLiteCheckpointStore(
            self.database,
            self.work_root,
            mode="local" if self.local_mode else "hosted",
            options=options,
            claim_token=claim_token,
            before_commit=self.checkpoint_before_commit,
            after_commit=self.checkpoint_after_commit,
            clock=self.queue.clock,
        )
        request = RunRequest(
            run_id=job.run_id,
            analysis_key=intake.analysis_key,
            target=job.target,
            recipe=job.recipe,
            accept_degraded=False,
            hints_snapshot_policy="frozen",
            manual_tracklist=None,
            checkpoint_store=store,
            attempt_journal=journal,
            usd_admitter=None,
            progress=progress,
            cancel_token=QueueCancelToken(),
        )
        result = self.service_runner(request)
        if result.status == "failed":
            # §4.3: a real failure comes back as a RunResult with its settled money. The queue
            # still owns retry and dead-letter, and both fold that money from the durable events.
            raise RuntimeError(result.reason or "analysis failed")
        if result.status == "waiting":
            self.queue.wait(
                job.id, claim_token, result.reason, cooldown_seconds=self.wait_cooldown_seconds
            )
            return
        bundle_path = None
        if result.bundle_id is not None:
            candidates = list(
                self.work_root.glob(
                    f"*/{intake.inputs.media_key}/present/bundles/{result.bundle_id}"
                )
            )
            if candidates:
                bundle_path = candidates[0]
        self.queue.terminal(
            job.id,
            claim_token,
            result,
            bundle_path=bundle_path,
            progress=tracker.settle(
                result.status,
                reason=result.reason,
                result_path=_result_path(self.work_root, bundle_path),
                # The exact settled figure from the authority, not this pass's own belief.
                usd_e2_spent=ceil_e2(self._settled_spend(job.run_id, result.usd_e6_spent)),
                spend_known=True,
            ),
        )

    def _journal(
        self,
        run_id: str,
        claim_token: str,
        app_config: AppConfig | None = None,
        *,
        job_id: str | None = None,
    ) -> SQLiteAttemptJournal:
        config = app_config or self.options.app_config or AppConfig()
        return SQLiteAttemptJournal(
            self.work_root / ".attempts" / f"{run_id}.jsonl",
            database=self.database,
            run_id=run_id,
            provider="audd",
            unit_usd_e6=config.audd_usd_e6_per_request,
            egress_id=self.egress_id,
            claim_token=claim_token,
            clock=self.queue.clock,
            job_id=job_id,
            hosted=not self.local_mode,
        )

    def _recovered_money(self, run_id: str | None) -> tuple[int, int, int]:
        """This run's already-recorded reservation, spend and attempts."""

        if run_id is None:
            return 0, 0, 0
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT usd_e6_reserved, usd_e6_spent, attempts FROM analysis_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return 0, 0, 0
        reserved = int(row["usd_e6_reserved"])
        spent = int(row["usd_e6_spent"])
        attempts = int(row["attempts"])
        state = SQLiteCheckpointStore(self.database, self.work_root).state(run_id, "primary")
        return (
            max(reserved, int(state.get("usd_e6_reserved", 0))),
            max(spent, int(state.get("usd_e6_spent", 0))),
            max(attempts, int(state.get("attempts", 0))),
        )

    def _settled_spend(self, run_id: str | None, reported: int) -> int:
        """This run's spend according to SQLite, which is the authority (§2.3.2).

        ``RunResult`` carries what *this pass* believes it spent. Recovery can fold durable provider
        events that a resumed or interrupted pass never saw, and `terminal()` raises the row to
        them. Reading the folded figure means the page states the cost the owner was really
        charged instead of understating it.
        """

        if run_id is None:
            return reported
        try:
            with self.database.write() as connection:
                fold_run_money(connection, run_id)
                row = connection.execute(
                    "SELECT usd_e6_spent FROM analysis_runs WHERE run_id=?", (run_id,)
                ).fetchone()
        except Exception:  # noqa: BLE001 - a money read must never fail a settled run
            return reported
        return max(reported, int(row["usd_e6_spent"]) if row is not None else 0)

    def _cancel_before_start(self, job: Job, claim_token: str) -> None:
        """Cancel without erasing what an earlier pass of this run already spent.

        A job killed after a paid primary checkpoint carries a real reservation, real spend and
        real attempts. Settling it with zeros -- because *this* pass did nothing -- would tell the
        owner their money was never charged.
        """

        if job.run_id is not None:
            # Project anything durable in the JSONL but missing from SQLite before settling; the
            # settlement then folds every event (a run that died before ``primary``). If this
            # claim is stale the fenced settlement below refuses to write at all.
            with suppress(OSError, ValueError, StaleClaim):
                self._journal(job.run_id, claim_token).backfill()
        reserved, spent, attempts = self._recovered_money(job.run_id)
        result = RunResult(
            job.run_id or "", "cancelled", None, None, None, reserved, spent, attempts
        )
        self.queue.terminal(job.id, claim_token, result, bundle_path=None)
