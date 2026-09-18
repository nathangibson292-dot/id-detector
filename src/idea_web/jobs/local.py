"""Local-mode durable jobs: the web-side client, the worker and its supervisor (cycle 4a-ii).

``idea serve`` never runs a pipeline (plan §4.2).  The web process validates a submission and
enqueues it in the 4b-i SQLite queue at ``<work root>/.idea/app.db``.  A separate worker *process*,
which ``idea serve`` starts, restarts if it dies and stops when the server stops, claims the job
through the queue's fenced lease API and runs the owner's local analysis runner
(:func:`id_detector.webapp.runner.make_pipeline_runner`: profiles, Deep scans, acquisition, the
reference index and a pasted tracklist) inside that process.

The job's progress state is the same :class:`id_detector.webapp.jobs.Job` the progress page has
always rendered.  The worker drives it with the unchanged in-memory state machine and publishes a
snapshot through the claim-fenced ``progress`` column every half second; the web process rebuilds
the object from that snapshot, so the page, its polling, cancellation and dismissal read exactly as
they did when the job lived in the server's memory.

Nothing in this module imports an HTTP framework, and the web-side half never imports the pipeline
runner: the worker resolves its runner only inside :func:`main`, in its own process.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import importlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from id_detector.io import path_is_file, read_text, redact_text
from id_detector.jobs import JobStoreLocked, ProcessLock
from id_detector.money import ceil_e2
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE
from id_detector.run_ledger import RecoveredMoney, fold_run_ledger, recovered_money
from id_detector.service import LocalPath, PlatformUrl, RunResult, TargetRefused
from id_detector.webapp.jobs import (
    CANCELLED,
    FAILED,
    LOG_RING,
    QUEUED,
    SUCCEEDED,
    TERMINAL_STATES,
    WAITING,
    Job,
    JobManager,
    Runner,
    TargetValidationError,
    validate_target,
)
from idea_web.database import Database, MigrationRefused  # noqa: F401 - re-exported for idea serve
from idea_web.jobs.worker import (
    ACTIVE_RUN_STATES,
    DEFAULT_LEASE_SECONDS,
    HEARTBEAT_SECONDS,
    MONEY_AUTHORITY,
    DispatchAdmission,
    JobQueue,
    LegacyRecoveryLedger,
    SchemaTooNew,
    SettlementLedger,
    StaleClaim,
    dispatch_events,
    new_run_id,
    stored_reservation,
)
from idea_web.jobs.worker import Job as QueueJob

LOCAL_DATABASE = Path(".idea") / "app.db"
SUPERVISOR_LOCK = Path(".idea") / "worker-supervisor.lock"
#: What a job still in flight says when ``idea serve`` stops (or stopped without warning).
ABANDONED = "stopped when ID'er was closed"
#: How often the worker publishes a changed progress snapshot (the page polls every 2.5 s).
FLUSH_SECONDS = 0.5
#: How often the worker re-derives missing settlements (it also sweeps once at start).
SWEEP_SECONDS = 120.0
#: Stopped queue states whose run may hold spend, and the status their settlement carries.
_SWEEP_STATUS = {
    "dead_letter": "failed",
    "failed": "failed",
    "quota_exceeded": "failed",
    "cancelled": "cancelled",
    "provider_unavailable": "provider_unavailable",
    "budget_exhausted": "budget_exhausted",
    "source_changed": "source_changed",
    "complete": "complete",
    "degraded": "degraded",
    "partial": "partial",
}
#: A settlement miss is retried at every sweep this many times, then with capped backoff — for as
#: long as the worker runs, and again at every start. It is never dropped.
IMMEDIATE_RETRIES = 3
MAX_RETRY_SECONDS = 1800.0
#: The worker's exit code when newer ID'er code has upgraded the database: the supervisor stops.
EXIT_SCHEMA_TOO_NEW = 3
#: A worker told to stop gets this long to settle its job before the process exits regardless.
EXIT_GRACE_SECONDS = 30.0
_LOCAL = "local"
_SNAPSHOT_FIELDS = tuple(
    item.name
    for item in dataclasses.fields(Job)
    if item.name not in {"target", "log", "cancel_event", "dispatch_admission", "settlement_writer"}
)
_TO_QUEUE = {
    SUCCEEDED: "complete",
    FAILED: "failed",
    CANCELLED: "cancelled",
    # Local mode stops a Shazam-paused job rather than retrying it; the page shows "waiting".
    WAITING: "provider_unavailable",
}
_FROM_QUEUE = {
    "complete": SUCCEEDED,
    "degraded": SUCCEEDED,
    "partial": SUCCEEDED,
    "cancelled": CANCELLED,
    "provider_unavailable": WAITING,
}


def _row_holds_money(row: Any) -> bool:
    try:
        entry = json.loads(row["entry"])
    except (TypeError, ValueError):
        return True  # unreadable: keep it scheduled rather than call it settled
    counts = entry.get("counts") if isinstance(entry.get("counts"), dict) else {}
    return bool(
        entry.get("usd_e6_reserved") or entry.get("usd_e6_spent") or counts.get("paid_attempts")
    )


def _epoch(at: str) -> float:
    """An attempt event's ISO-8601 ``at`` as epoch seconds; NaN when unreadable."""

    from datetime import datetime

    try:
        return datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return float("nan")


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def retry_delay_seconds(misses: int) -> float:
    """How long a run waits after its ``misses``-th settlement miss before the next attempt."""

    if misses <= IMMEDIATE_RETRIES:
        return 0.0
    return min(SWEEP_SECONDS * 2 ** (misses - IMMEDIATE_RETRIES), MAX_RETRY_SECONDS)


def local_database(work_root: Path) -> Database:
    """The work folder's queue database, migrated to this code's schema.

    ``Database.migrate`` itself refuses with ``MigrationRefused`` (re-exported here for
    ``idea serve``) while another ID'er holds the supervisor lock or an older claim is still live,
    so no caller can bypass that exclusion.
    """

    # A restore interrupted by a crash is undone FIRST: migrating, or serving, a half-published
    # tree would build new state on top of it that a later recovery would then overwrite. The
    # restore lock is then held until migration has returned, so no restore can start while the
    # database is being created, opened or switched to WAL.
    root = Path(work_root).resolve()
    with _restore_excluded(root):
        database = Database(root / LOCAL_DATABASE)
        database.migrate()
    return database


@contextlib.contextmanager
def _restore_excluded(work_root: Path) -> Iterator[None]:
    """Refuse during a live restore, undo an interrupted one, and exclude restores meanwhile.

    Refusals surface as ``MigrationRefused``, the error ``idea serve`` already reports.
    """

    from idea_web.backup import BackupRefused, RestoreInProgress, restore_excluded

    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(restore_excluded(work_root))
        except RestoreInProgress as exc:
            raise MigrationRefused(str(exc)) from None
        except BackupRefused as exc:
            raise MigrationRefused(
                f"ID'er found an interrupted restore in this work folder that it could not safely "
                f"undo: {exc}"
            ) from None
        yield  # the restore lock is released by the stack, on success or failure alike


def _recover_interrupted_restore(work_root: Path) -> None:
    """Consume an interrupted restore's journal; the caller already holds the supervisor lock."""

    from idea_web.backup import BackupRefused, recover_interrupted

    try:
        recover_interrupted(work_root)
    except BackupRefused as exc:
        raise MigrationRefused(
            f"ID'er found an interrupted restore in this work folder that it could not safely "
            f"undo: {exc}"
        ) from None


def snapshot(job: Job) -> dict[str, Any]:
    """The JSON-safe state of one job: every field the progress page and home cards read."""

    document = {name: getattr(job, name) for name in _SNAPSHOT_FIELDS}
    document["phase_seconds"] = dict(job.phase_seconds)
    document["log"] = list(job.log)
    return document


def _target_text(target: object) -> str:
    if isinstance(target, PlatformUrl):
        return target.url
    if isinstance(target, LocalPath):
        return str(target.path)
    raise TargetRefused("a local job is a web link or a local file")


def job_view(row: QueueJob) -> Job | None:
    """Rebuild the page's job object from a queue row; ``None`` for a dismissed or foreign row."""

    document = row.progress.get(_LOCAL)
    if not isinstance(document, dict) or row.progress.get("dismissed"):
        return None
    values = {name: document[name] for name in _SNAPSHOT_FIELDS if name in document}
    values["id"] = row.id
    # The normalised column is the ONLY authority for the run id; the snapshot never is.
    values["run_id"] = row.run_id
    try:
        job = Job(target=_target_text(row.target), **values)
    except (TypeError, TargetRefused):
        return None
    job.log = deque((str(line) for line in document.get("log") or ()), maxlen=LOG_RING)
    if row.state not in ACTIVE_RUN_STATES and job.status not in TERMINAL_STATES:
        # The queue settled this job without a final snapshot from the worker (a dead letter).
        job.status = _FROM_QUEUE.get(row.state, FAILED)
        job.finished_at = job.finished_at or row.updated_at
        if job.status == FAILED:
            job.failed_phase = job.failed_phase or job.phase
            reason = row.dead_letter_reason or "the local analysis worker stopped"
            job.error = job.error or redact_text(reason)[:500]
        job.phase = "done" if job.status == SUCCEEDED else job.status
    return job


def _mark_stopped(job: Job, message: str, now: float) -> Job:
    job.status = CANCELLED
    job.phase = CANCELLED
    job.finished_at = now
    job.log.append(f"{_stamp()} {message}")
    return job


class LocalJobs:
    """The web process's view of the durable queue: enqueue, read, cancel, dismiss — nothing else.

    ``idea serve`` uses it in local mode. A hosted server uses the same adapter over its own
    (supervised) database with ``local_mode=False``: the queue then refuses a local file target,
    and every submission carries the signed-in account (``user_id``) into ``jobs.user_id``.
    """

    def __init__(
        self,
        work_root: Path,
        *,
        database: Database | None = None,
        clock: Callable[[], float] = time.time,
        local_mode: bool = True,
    ) -> None:
        self.work_root = Path(work_root)
        self.database = database or local_database(self.work_root)
        self.queue = JobQueue(self.database, local_mode=local_mode, clock=clock)
        self.clock = clock
        #: The home page lists jobs from this server session (and anything still in flight), as
        #: it did when jobs lived in memory; earlier finished jobs stay reachable by their URL.
        self.since = clock()

    def submit(
        self,
        target: str,
        profile: str | None = None,
        *,
        acquire: bool = False,
        build_index: bool = False,
        known_tracklist: str | None = None,
        user_id: str | None = None,
    ) -> str:
        validated = validate_target(target)
        try:
            is_file = Path(validated).is_file()
        except (OSError, ValueError):
            is_file = False
        job_id = uuid.uuid4().hex
        job = Job(
            id=job_id,
            target=validated,
            display=redact_text(validated),
            profile=profile,
            acquire=acquire,
            build_index=build_index,
            known_tracklist=known_tracklist,
            created_at=self.clock(),
            # ONE durable service run for this job, reused by every worker process that runs it.
            run_id=new_run_id(),
        )
        try:
            queue_target = LocalPath(Path(validated)) if is_file else PlatformUrl(validated)
            self.queue.enqueue(
                queue_target,
                DEEP_RECIPE if profile == "max_accuracy" else FREE_RECIPE,
                job_id=job_id,
                # Normalised queue state, not only the snapshot: the queue itself can then settle
                # this run's shared ledger when it quarantines or dead-letters the row.
                run_id=job.run_id,
                progress={_LOCAL: snapshot(job)},
                user_id=user_id,
            )
        except TargetRefused as exc:
            raise TargetValidationError(str(exc)) from None
        return job_id

    def get(self, job_id: str) -> Job | None:
        try:
            return job_view(self.queue.get(job_id))
        except (KeyError, ValueError, TargetRefused):
            return None

    def recent(self, limit: int = 25) -> list[Job]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE COALESCE(json_extract(progress, '$.dismissed'), 0) = 0 "
                "AND (created_at >= ? OR (state IN ('intake','waiting','analysis') "
                "AND cancel_requested = 0)) ORDER BY created_at DESC, id DESC LIMIT ?",
                (self.since, limit),
            ).fetchall()
        jobs = [self.get(row["id"]) for row in rows]
        return [job for job in jobs if job is not None]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status in TERMINAL_STATES:
            return False
        if job.status == QUEUED:
            stopped = _mark_stopped(job, "cancelled before it started", self.clock())
            if self.queue.cancel_unclaimed(job_id, {_LOCAL: snapshot(stopped)}):
                return True
        # A worker holds it: the worker sees the request within half a second and stops the run
        # at its next progress tick, exactly as the in-memory cancel flag did.
        return self.queue.request_cancel(job_id)

    def dismiss(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status not in TERMINAL_STATES:
            return False
        return self.queue.dismiss(job_id)

    def cancel_all(self, message: str = ABANDONED) -> int:
        """Stop every job still in flight — when the server stops, or starts after it died."""

        with self.database.read() as connection:
            ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM jobs WHERE state IN ('intake','waiting','analysis')"
                ).fetchall()
            ]
        stopped = 0
        for job_id in ids:
            job = self.get(job_id)
            if job is not None and job.status == QUEUED:
                document = snapshot(_mark_stopped(job, message, self.clock()))
                if self.queue.cancel_unclaimed(job_id, {_LOCAL: document}):
                    stopped += 1
                    continue
            if self.queue.request_cancel(job_id):
                stopped += 1
        return stopped


class _ProgressPublisher:
    """Publishes the running job's snapshot through the claim fence and relays cancellation."""

    def __init__(
        self,
        queue: JobQueue,
        job_id: str,
        claim_token: str,
        job: Job,
        lock: Any,
        *,
        interval: float,
        heartbeat_seconds: float,
        lease_seconds: float,
    ) -> None:
        self.queue = queue
        self.job_id = job_id
        self.claim_token = claim_token
        self.job = job
        self.lock = lock
        self.interval = interval
        self.heartbeat_seconds = heartbeat_seconds
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"idea-local-progress-{job_id}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        last: str | None = None
        last_write = 0.0
        while not self._stop.wait(self.interval):
            try:
                # True when the owner cancelled, and also when this claim was lost or its lease
                # expired: either way the run must stop, and it stops at its next progress tick.
                if self.queue.cancel_requested(self.job_id, self.claim_token):
                    self.job.cancel_event.set()
                with self.lock:
                    self.job.progress_percent()
                    document = snapshot(self.job)
                encoded = json.dumps(document, sort_keys=True, default=str)
                now = time.monotonic()
                if encoded != last or now - last_write >= self.heartbeat_seconds:
                    if not self.queue.update_progress(
                        self.job_id,
                        self.claim_token,
                        {_LOCAL: document},
                        lease_seconds=self.lease_seconds,
                    ):
                        self.job.cancel_event.set()
                    last, last_write = encoded, now
            except Exception:  # noqa: BLE001 - a busy database must not end the job's heartbeat
                continue


class LocalWorker:
    """One local queue consumer.  It imports no HTTP framework and serves no requests."""

    def __init__(
        self,
        database: Database,
        work_root: Path,
        runner: Runner,
        *,
        worker_id: str | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        flush_seconds: float = FLUSH_SECONDS,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = database
        self.work_root = Path(work_root)
        self.runner = runner
        self.monotonic = monotonic
        #: Set when newer code upgraded the database: the worker exits with EXIT_SCHEMA_TOO_NEW.
        self.schema_too_new = False
        self.worker_id = worker_id or f"local-worker-{uuid.uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.flush_seconds = flush_seconds
        self.clock = clock
        self.queue = JobQueue(
            database, local_mode=True, clock=clock, on_abandon=self._settle_abandoned
        )
        self.stopped = threading.Event()
        self._current_lock = threading.Lock()
        self._current: Job | None = None
        self._swept: set[str] = set()
        self._recovered_jobs: set[str] = set()
        #: run id -> (consecutive misses, monotonic time of the next attempt)
        self._retry: dict[str, tuple[int, float]] = {}

    def stop(self) -> None:
        """Claim nothing more and cancel the job in hand (it settles as ``cancelled``)."""

        self.stopped.set()
        with self._current_lock:
            current = self._current
        if current is not None:
            current.cancel_event.set()

    def sweep_settlements(self) -> int:
        """Derive every missing settlement from SQLite, the single authority (idempotent).

        For every stopped job in ANY terminal state (``complete`` included) whose run has no
        ``run_settlements`` row, it adopts an existing ``invocations.jsonl`` line, or settles what
        the run's durable ledger proves it spent. A row whose projection is missing is
        re-projected. A run whose media cannot be located is settled from SQLite alone when its
        authority rows (or its provenance stamp) prove the money; otherwise it waits. Every miss
        stays scheduled — immediately for the first few, then with capped backoff — for as long as
        the worker runs. Pre-upgrade stopped jobs with no run id are recovered from the run ids
        their attempt ledger names.
        """

        settled = self._recover_unidentified()
        now = self.monotonic()
        for job_id, run_id, target, state in self.queue.settlement_candidates(
            frozenset(_SWEEP_STATUS)
        ):
            if run_id in self._swept:
                continue
            misses, due = self._retry.get(run_id, (0, 0.0))
            if now < due:
                continue
            try:
                settled += int(self._settle_run(job_id, run_id, target, _SWEEP_STATUS[state]))
            except Exception:  # noqa: BLE001 - a miss or a failure stays scheduled, never dropped
                misses += 1
                self._retry[run_id] = (misses, now + retry_delay_seconds(misses))
                continue
            self._retry.pop(run_id, None)
            self._swept.add(run_id)
        return settled

    def _settle_run(
        self, job_id: str, run_id: str, target: str, status: str, *, legacy: bool = False
    ) -> bool:
        from id_detector.service import (
            SettlementMiss,
            interrupted_entry,
            settle_interrupted_run,
            settlement_lines,
        )

        # ``legacy``: a pre-upgrade job with no ``jobs.run_id``, recovered through the separate,
        # explicitly validated association; every other writer requires the job to own the run.
        ledger_type = LegacyRecoveryLedger if legacy else SettlementLedger
        ledger = ledger_type(self.database, job_id=job_id, clock=self.clock, only_if_missing=True)
        existing = ledger.row(run_id)
        if existing is not None and existing["journal_path"]:
            ledger.reproject(run_id)  # the file is a projection of the row: missing or different
            return False
        try:
            path, lines = settlement_lines(self.work_root, target, run_id)
        except SettlementMiss:
            if existing is not None:
                if _row_holds_money(existing):
                    raise  # settled from SQLite; its projection waits for the media
                return False
            money = self._authority_money(job_id, run_id)
            if money is None:
                raise  # no SQLite proof either way: wait for the media, never "nothing was spent"
            ledger.settle(None, interrupted_entry(run_id, target, status, money, []))
            if money.any:
                raise SettlementMiss(
                    f"run {run_id} settled from SQLite; its projection waits"
                ) from None
            return False
        authority = {
            "dispatch_events": self._dispatch_events(run_id),
            "reservation": self._stored_reservation(run_id),
        }
        if existing is not None:
            # Settled while its media was missing: now project it, raised to the full fold.
            money = settle_interrupted_run(
                self.work_root, target, run_id, status=status, write=False, **authority
            )
            ledger.attach(run_id, path, money)
            return False
        if lines:
            # Written before SQLite held settlements: every line, reconciled with the durable fold.
            money = settle_interrupted_run(
                self.work_root, target, run_id, status=status, write=False, **authority
            )
            ledger.adopt(run_id, path, lines, money)
            return False
        money = settle_interrupted_run(
            self.work_root,
            target,
            run_id,
            status=status,
            only_unsettled=True,
            settlement_writer=ledger,
            **authority,
        )
        return bool(money.any)

    def _recover_unidentified(self) -> int:
        """Settle runs of stopped pre-upgrade jobs that have no ``jobs.run_id``.

        Such a job's runs are the run ids its media's attempt ledger names inside the job's own
        time window. Each is settled at most once, through the same unique settlement row.
        """

        from id_detector.attempts import attempts_path
        from id_detector.run_ledger import parse_journal_lines
        from id_detector.service import SettlementMiss, settlement_line

        settled = 0
        for row in self.queue.unidentified_terminal_rows():
            if row["job_id"] in self._recovered_jobs:
                continue
            try:
                journal_path, _line = settlement_line(self.work_root, row["target"], "")
                ledger_path = attempts_path(journal_path.parent)
                records = (
                    parse_journal_lines(read_text(ledger_path)) if path_is_file(ledger_path) else []
                )
            except SettlementMiss:
                continue
            except Exception:  # noqa: BLE001 - retried at the next sweep
                continue
            low, high = row["start"] - 5.0, row["end"] + 5.0
            runs = sorted({record.run_id for record in records if low <= _epoch(record.at) <= high})
            complete = True
            for run_id in runs:
                try:
                    settled += int(
                        self._settle_run(
                            row["job_id"],
                            run_id,
                            row["target"],
                            _SWEEP_STATUS.get(row["state"], "failed"),
                            legacy=True,
                        )
                    )
                except Exception:  # noqa: BLE001 - a miss or failure: this job is retried
                    complete = False
            if complete:
                self._recovered_jobs.add(row["job_id"])
        return settled

    def run_forever(self, *, poll_seconds: float = 0.5) -> None:
        last_sweep = -SWEEP_SECONDS
        while True:
            if time.monotonic() - last_sweep >= SWEEP_SECONDS:
                with contextlib.suppress(Exception):
                    self.sweep_settlements()
                last_sweep = time.monotonic()
            if self.stopped.is_set():
                return
            try:
                claimed = self.run_once()
            except (KeyboardInterrupt, SystemExit):
                raise
            except SchemaTooNew as exc:
                # Newer code upgraded the database: claim nothing more and exit cleanly.
                print(str(exc), file=sys.stderr, flush=True)
                self.schema_too_new = True
                self.stopped.set()
                return
            except Exception:  # noqa: BLE001 - one bad row or a busy database never ends the loop
                claimed = None
            if claimed is None:
                self.stopped.wait(poll_seconds)

    def run_once(self) -> str | None:
        if self.stopped.is_set():
            return None
        row = self.queue.claim(self.worker_id, lease_seconds=self.lease_seconds)
        if row is None:
            return None
        token = row.token
        try:
            job = job_view(row)
            if job is None:
                self.queue.fail(row, token, "not a local ID'er job")
                return row.id
            if row.cancel_requested or self.stopped.is_set():
                message = "cancelled before it started" if job.started_at is None else ABANDONED
                self._settle(
                    row.id, token, _mark_stopped(job, message, self.clock()), interrupted=True
                )
                return row.id
            # The ONE run id, adopted from (or minted into) the normalised column and the snapshot
            # together, durably, BEFORE the run starts: a worker death from here on resumes it.
            job.run_id = self.queue.adopt_run_id(row.id, token)
            if row.attempt > 1:
                job = self._restarted(job)
            self._execute(row, token, job)
        except StaleClaim:
            pass  # another claim owns this job now; write nothing
        except Exception as exc:  # noqa: BLE001 - return the job to the queue, never crash
            self.queue.fail(row, token, f"{type(exc).__name__}: {redact_text(str(exc))[:300]}")
        return row.id

    @staticmethod
    def _restarted(job: Job) -> Job:
        restarted = Job(
            id=job.id,
            target=job.target,
            display=job.display,
            profile=job.profile,
            acquire=job.acquire,
            build_index=job.build_index,
            known_tracklist=job.known_tracklist,
            created_at=job.created_at,
            run_id=job.run_id,
        )
        restarted.log = deque(job.log, maxlen=LOG_RING)
        restarted.log.append(f"{_stamp()} restarted after the analysis worker stopped unexpectedly")
        return restarted

    def _execute(self, row: QueueJob, token: str, job: Job) -> None:
        # The job state machine is the in-memory manager's own, reused unchanged so progress,
        # cancellation, waiting and failure mean exactly what they meant before; its background
        # thread is never started — this process's run loop is the only consumer.
        machine = JobManager(self.work_root, self.runner)
        with machine.lock:
            machine._jobs[job.id] = job
        # The ONE dispatch-admission check (claim, lease, active AND not cancelled), evaluated
        # at the paid dispatch itself; the progress relay's 0.5 s cadence is not a fence.
        job.dispatch_admission = DispatchAdmission(
            self.database, job_id=row.id, claim_token=token, clock=self.clock
        )
        claim = DispatchAdmission(
            self.database,
            job_id=row.id,
            claim_token=token,
            clock=self.clock,
            require_not_cancelled=False,
        )
        # Terminal settlement: its SQLite row is written under the same claim check in one
        # transaction; invocations.jsonl is projected from that row after commit.
        job.settlement_writer = SettlementLedger(
            self.database, fence=claim.check, job_id=row.id, clock=self.clock
        )
        with self._current_lock:
            self._current = job
        if self.stopped.is_set():
            job.cancel_event.set()
        publisher = _ProgressPublisher(
            self.queue,
            row.id,
            token,
            job,
            machine.lock,
            interval=self.flush_seconds,
            heartbeat_seconds=self.heartbeat_seconds,
            lease_seconds=self.lease_seconds,
        )
        publisher.start()
        try:
            machine._execute(job.id)
        finally:
            publisher.stop()
            with self._current_lock:
                self._current = None
        self._settle(row.id, token, job, lock=machine.lock)

    def _settle_abandoned(self, job_id: str, run_id: str | None, target: str) -> None:
        """The queue quarantined or dead-lettered this job: settle its run once, as ``failed``."""

        if not run_id or not target:
            return
        from id_detector.service import settle_interrupted_run

        # A fast path only: it inserts the settlement row only when none exists. The derived sweep
        # is the guarantee, so a crash here loses nothing.
        settle_interrupted_run(
            self.work_root,
            target,
            run_id,
            status="failed",
            only_unsettled=True,
            settlement_writer=SettlementLedger(
                self.database, job_id=job_id, clock=self.clock, only_if_missing=True
            ),
            dispatch_events=self._dispatch_events(run_id),
            reservation=self._stored_reservation(run_id),
        )

    def _dispatch_events(self, run_id: str) -> list[Any]:
        with self.database.read() as connection:
            return dispatch_events(connection, run_id, paid_only=True)

    def _stored_reservation(self, run_id: str) -> Any:
        with self.database.read() as connection:
            return stored_reservation(connection, run_id)

    def _authority_money(self, job_id: str, run_id: str) -> RecoveredMoney | None:
        """What SQLite alone proves ``run_id`` spent, for a run whose media cannot be located.

        Any paid ``run_dispatches`` or ``run_reservations`` row: the conservative fold of those rows
        (an unresolved dispatch counts as spent; the reservation is the row's own). No such row:
        zero — but ONLY for a job stamped ``money_authority = 3``, i.e. every claim of it was made
        by code that writes those rows first. Anything else is ``None``: wait for the media.
        """

        with self.database.read() as connection:
            job = connection.execute(
                "SELECT money_authority FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            events = dispatch_events(connection, run_id, paid_only=True)
            reservation = stored_reservation(connection, run_id)
        if events or reservation is not None:
            return recovered_money(fold_run_ledger(run_id, events, provider=None), reservation)
        if job is not None and job["money_authority"] == MONEY_AUTHORITY:
            return RecoveredMoney()
        return None

    def _run_money(self, job: Job, *, interrupted: bool, settlement_writer: Any = None) -> Any:
        """The job's service run money from the shared ledger; settled there when interrupted."""

        # Not gated on ``started_at``: a worker can die after paying for clips and before its
        # first 0.5 s progress flush, and the ledger — not the snapshot — says what was spent.
        if not job.run_id:
            return None
        from id_detector.service import SettlementMiss, interrupted_entry, settle_interrupted_run

        try:
            return settle_interrupted_run(
                self.work_root,
                job.target,
                job.run_id,
                status="cancelled",
                write=interrupted,
                settlement_writer=settlement_writer,
                dispatch_events=self._dispatch_events(job.run_id),
                reservation=self._stored_reservation(job.run_id),
            )
        except SettlementMiss:
            if not (interrupted and settlement_writer is not None):
                return None
            with contextlib.suppress(Exception):
                money = self._authority_money(job.id, job.run_id)
                if money is not None:
                    # Media not located: SQLite proves the money (or the stamped run's zero); the
                    # sweep projects the row once the media is found.
                    settlement_writer.settle(
                        None, interrupted_entry(job.run_id, job.target, "cancelled", money, [])
                    )
                    return money
            return None  # the derived sweep retries it
        except Exception:  # noqa: BLE001 - money bookkeeping never masks the job's own outcome
            return None

    def _settle(
        self, job_id: str, token: str, job: Job, *, lock: Any = None, interrupted: bool = False
    ) -> None:
        writer = (
            SettlementLedger(
                self.database,
                fence=DispatchAdmission(
                    self.database,
                    job_id=job_id,
                    claim_token=token,
                    clock=self.clock,
                    require_not_cancelled=False,
                ).check,
                job_id=job_id,
                clock=self.clock,
            )
            if interrupted
            else None
        )
        money = self._run_money(job, interrupted=interrupted, settlement_writer=writer)
        with lock if lock is not None else contextlib.nullcontext():
            if interrupted and money is not None:
                # A job stopped before its restart settles what its dead worker really spent.
                job.usd_e2_spent = ceil_e2(money.usd_e6_spent)
                job.spend_known = True
                job.run_status = job.run_status or "cancelled"
            job.progress_percent()
            document = snapshot(job)
            status = job.status
        reason = job.error if status == FAILED else (job.message if status == WAITING else None)
        exact = money is not None and money.any
        result = RunResult(
            run_id=job.run_id or "",
            status=_TO_QUEUE.get(status, "failed"),
            reason=reason,
            achieved=None,
            bundle_id=None,
            usd_e6_reserved=money.usd_e6_reserved if exact else 0,
            usd_e6_spent=(
                money.usd_e6_spent if exact else max(0, int(job.usd_e2_spent or 0)) * 10_000
            ),
            attempts=money.attempts if exact else 0,
        )
        self.queue.terminal(job_id, token, result, bundle_path=None, progress={_LOCAL: document})


def _tie_to_this_process(process: subprocess.Popen[bytes]) -> Any:
    """Put the worker in a kill-on-close Windows job object owned by this process.

    If ``idea serve`` dies for any reason — Ctrl-C, a closed window, a crash, ``taskkill`` — the
    handle closes with it and Windows ends the worker and everything the worker started, so no
    orphaned process is left holding the virtual environment's files.
    """

    if os.name != "nt":
        return None
    try:
        import win32api
        import win32con
        import win32job

        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        handle = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid
        )
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        return job
    except Exception as exc:  # noqa: BLE001 - the pipe and parent watchdogs still stop the worker
        print(
            f"warning: could not tie the analysis worker's lifetime to ID'er: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None


class LocalWorkerSupervisor:
    """Starts the local worker process, restarts it if it dies, and stops it with the server."""

    def __init__(
        self,
        work_root: Path,
        *,
        config_path: Path,
        jobs: LocalJobs,
        python: str | None = None,
        runner_spec: str | None = None,
        stop_grace_seconds: float = 15.0,
        restart_delay_seconds: float = 1.0,
        max_restart_delay_seconds: float = 30.0,
        poll_seconds: float = 0.5,
    ) -> None:
        self.work_root = Path(work_root).resolve()
        self.config_path = Path(config_path)
        self.jobs = jobs
        self.python = python or sys.executable
        self.runner_spec = runner_spec
        self.stop_grace_seconds = stop_grace_seconds
        self.restart_delay_seconds = restart_delay_seconds
        self.max_restart_delay_seconds = max_restart_delay_seconds
        self.poll_seconds = poll_seconds
        self.restarts = 0
        self._lock: ProcessLock | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._job_object: Any = None
        self._started_at = 0.0
        self._stopping = threading.Event()
        self._guard = threading.Lock()
        self._monitor: threading.Thread | None = None

    @property
    def owner(self) -> bool:
        return self._lock is not None

    @property
    def pid(self) -> int | None:
        with self._guard:
            process = self._process
        return process.pid if process is not None and process.poll() is None else None

    def start(self) -> bool:
        """Start supervising; ``False`` when another ``idea serve`` already runs this worker."""

        lock = ProcessLock(self.work_root / SUPERVISOR_LOCK)
        try:
            lock.acquire()
        except JobStoreLocked:
            from idea_web.backup import RESTORE_IN_PROGRESS, restore_running

            if restore_running(self.work_root):
                # The holder is a restore, not another window: this server must not run at all.
                # (`idea serve` ignores a False here, so refusing is the only fail-closed answer.)
                raise MigrationRefused(RESTORE_IN_PROGRESS) from None
            self._log(
                "another ID'er window already runs analyses for this work folder; "
                "analyses started here will run there"
            )
            return False
        self._lock = lock
        self._stopping.clear()
        try:
            # An interrupted restore is undone before this server touches the tree. Opening the
            # database already tried; this pass holds the supervisor lock, so nothing can race it.
            _recover_interrupted_restore(self.work_root)
            # Anything left in flight by a server that stopped without warning is stopped, not
            # silently resumed: closing ID'er has always stopped its analyses.
            self.jobs.cancel_all(ABANDONED)
            with self._guard:
                self._spawn()
        except BaseException:
            self._release()
            raise
        self._monitor = threading.Thread(
            target=self._watch, name="idea-local-supervisor", daemon=True
        )
        self._monitor.start()
        return True

    def stop(self) -> None:
        if self._lock is None:
            return
        self._stopping.set()
        if self._monitor is not None:
            self._monitor.join(timeout=5)
        with contextlib.suppress(Exception):
            self.jobs.cancel_all(ABANDONED)
        with self._guard:
            process = self._process
        if process is not None:
            with contextlib.suppress(OSError, ValueError):
                if process.stdin is not None:
                    process.stdin.close()  # the worker's stop signal
            try:
                process.wait(timeout=self.stop_grace_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
        self._close_job_object()
        self._release()

    def _command(self) -> list[str]:
        command = [
            self.python,
            "-c",
            "from idea_web.jobs.local import main; raise SystemExit(main())",
            "--work-root",
            str(self.work_root),
            "--config",
            str(self.config_path.resolve()),
            "--parent-pid",
            str(os.getpid()),
            "--parent-pipe",
        ]
        if self.runner_spec:
            command += ["--runner", self.runner_spec]
        return command

    def _spawn(self) -> None:
        # Its own process group: Ctrl-C reaches ``idea serve``, which then stops the worker in
        # order (cancel, close the pipe, wait) instead of both dying mid-write.
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            self._command(),
            stdin=subprocess.PIPE,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
        self._job_object = _tie_to_this_process(process)
        self._process = process
        self._started_at = time.monotonic()

    def _watch(self) -> None:
        delay = self.restart_delay_seconds
        while not self._stopping.wait(self.poll_seconds):
            with self._guard:
                process = self._process
            if process is None or process.poll() is None:
                continue
            if process.returncode == EXIT_SCHEMA_TOO_NEW:
                self._log(
                    "a newer ID'er has upgraded this work folder's database, so this one has "
                    "stopped running analyses: close it and start the newer ID'er"
                )
                return
            if time.monotonic() - self._started_at > 60.0:
                delay = self.restart_delay_seconds
            self._log(
                f"the analysis worker stopped (exit code {process.returncode}); restarting it"
            )
            if self._stopping.wait(delay):
                return
            with self._guard:
                if self._stopping.is_set():
                    return
                self._close_job_object()
                self._spawn()
                self.restarts += 1
            delay = min(delay * 2, self.max_restart_delay_seconds)

    def _close_job_object(self) -> None:
        job_object, self._job_object = self._job_object, None
        if job_object is not None:
            with contextlib.suppress(Exception):
                job_object.Close()

    def _release(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            with contextlib.suppress(Exception):
                lock.release()

    @staticmethod
    def _log(message: str) -> None:
        print(message, file=sys.stderr, flush=True)


def _load_runner(work_root: Path, config: Path, spec: str | None) -> Runner:
    if spec:
        if os.environ.get("IDEA_TEST_MODE") != "1":
            raise SystemExit("--runner is available only when IDEA_TEST_MODE=1")
        module_name, _, attribute = spec.partition(":")
        factory = getattr(importlib.import_module(module_name), attribute)
        return factory(work_root, config)
    from id_detector.cli import PROJECT_ROOT
    from id_detector.webapp.runner import make_pipeline_runner

    return make_pipeline_runner(work_root, project_root=PROJECT_ROOT, config_path=config)


def _watch_parent_pid(worker: LocalWorker, parent_pid: int) -> None:
    import psutil

    try:
        created = psutil.Process(parent_pid).create_time()
    except psutil.Error:
        worker.stop()
        return
    while not worker.stopped.wait(2.0):
        try:
            alive = psutil.Process(parent_pid).create_time() == created
        except psutil.Error:
            alive = False
        if not alive:
            worker.stop()
            return


def _install_stop_triggers(
    worker: LocalWorker, *, parent_pid: int | None, parent_pipe: bool
) -> None:
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(number, lambda *_: worker.stop())
    if parent_pipe and sys.stdin is not None:

        def watch_pipe() -> None:
            with contextlib.suppress(Exception):
                while sys.stdin.buffer.read(4096):
                    pass
            worker.stop()  # the supervisor closed the pipe, or its process is gone

        threading.Thread(target=watch_pipe, name="idea-local-parent-pipe", daemon=True).start()
    if parent_pid:
        threading.Thread(
            target=_watch_parent_pid,
            args=(worker, parent_pid),
            name="idea-local-parent-pid",
            daemon=True,
        ).start()

    def exit_after_grace() -> None:
        worker.stopped.wait()
        time.sleep(EXIT_GRACE_SECONDS)
        os._exit(0)

    threading.Thread(target=exit_after_grace, name="idea-local-exit-guard", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="idea-local-worker", description="ID'er's local analysis worker (run by idea serve)."
    )
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("idea.toml"))
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--parent-pipe", action="store_true")
    parser.add_argument("--runner", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    runner = _load_runner(args.work_root, args.config, args.runner)
    worker = LocalWorker(local_database(args.work_root), args.work_root, runner)
    _install_stop_triggers(worker, parent_pid=args.parent_pid, parent_pipe=args.parent_pipe)
    worker.run_forever()
    return EXIT_SCHEMA_TOO_NEW if worker.schema_too_new else 0
