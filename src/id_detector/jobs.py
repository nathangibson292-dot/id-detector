"""Single-writer asynchronous SQLite job store and crash-safe submission states."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha1, sha256
from importlib.resources import files
from pathlib import Path
from typing import Any, TypeVar

from id_detector.io import native_path

T = TypeVar("T")
WriterOperation = Callable[[sqlite3.Connection], T]

DEFAULT_LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 15
DEFAULT_SHAZAM_MAX_REQUESTS = 2_000
_ACTIVE_LOCKS: set[str] = set()


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class BudgetExhausted(RuntimeError):
    pass


class JobStoreLocked(RuntimeError):
    """Raised when another process owns a media's job store."""


class ProcessLock:
    """Non-blocking cross-process lock keyed by a resolved workspace path."""

    def __init__(self, path: Path) -> None:
        resolved = str(path.resolve())
        if sys.platform == "win32" and resolved.startswith("\\\\?\\UNC\\"):
            resolved = "\\\\" + resolved[8:]
        elif sys.platform == "win32" and resolved.startswith("\\\\?\\"):
            resolved = resolved[4:]
        self.path = Path(resolved).resolve()
        self._handle: Any = None
        self._owns_windows_mutex = False

    def acquire(self) -> None:
        if self._handle is not None:
            return
        lock_key = str(self.path).casefold() if sys.platform == "win32" else str(self.path)
        if lock_key in _ACTIVE_LOCKS:
            raise JobStoreLocked(f"workspace is already active: {self.path.parent}")
        os.makedirs(native_path(self.path.parent), exist_ok=True)
        if sys.platform == "win32":
            import win32event

            mutex_name = (
                "Local\\id-detector-"
                + sha256(str(self.path).casefold().encode("utf-8")).hexdigest()
            )
            handle = win32event.CreateMutex(None, False, mutex_name)
            result = win32event.WaitForSingleObject(handle, 0)
            if result not in {win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED}:
                handle.Close()
                raise JobStoreLocked(f"workspace is already active: {self.path.parent}")
            self._handle = handle
            self._owns_windows_mutex = True
            _ACTIVE_LOCKS.add(lock_key)
            return

        import fcntl

        handle = open(native_path(self.path), "a+b")  # noqa: SIM115 - held for lock lifetime
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise JobStoreLocked(f"workspace is already active: {self.path.parent}") from exc
        self._handle = handle
        _ACTIVE_LOCKS.add(lock_key)

    def release(self) -> None:
        if self._handle is None:
            return
        if sys.platform == "win32":
            import win32event

            if self._owns_windows_mutex:
                win32event.ReleaseMutex(self._handle)
            self._handle.Close()
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
        self._handle = None
        self._owns_windows_mutex = False
        lock_key = str(self.path).casefold() if sys.platform == "win32" else str(self.path)
        _ACTIVE_LOCKS.discard(lock_key)

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


@dataclass(frozen=True)
class Job:
    id: str
    media_key: str
    query_id: str
    provider: str
    state: str
    lease_owner: str | None
    lease_expires_at: str | None
    heartbeat_at: str | None
    attempts: int
    physical_attempts: int
    next_retry_at: str | None
    submission_started_at: str | None
    submitted_at: str | None
    remote_ref: str | None
    reserved_units: int
    reserved_usd: int
    actual_units: int
    actual_usd: int
    result_path: str | None
    error: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(**dict(row))


@dataclass(frozen=True)
class ConnectorJob:
    id: str
    media_key: str
    connector: str
    target_url: str
    cursor: str | None
    page: int
    page_cap: int
    item_cap: int
    items_fetched: int
    state: str
    lease_owner: str | None
    lease_expires_at: str | None
    heartbeat_at: str | None
    attempts: int
    next_retry_at: str | None
    result_path: str | None
    truncated: int
    error: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ConnectorJob:
        return cls(**dict(row))


class AsyncJobStore:
    """Own the only SQLite writer connection behind one asyncio queue."""

    def __init__(self, path: Path, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        self.path = path.resolve()
        self.lease_seconds = lease_seconds
        self._process_lock = ProcessLock(self.path.with_suffix(self.path.suffix + ".lock"))
        self._queue: asyncio.Queue[
            tuple[WriterOperation[Any] | None, asyncio.Future[Any] | None]
        ] = asyncio.Queue()
        self._writer: asyncio.Task[None] | None = None

    async def start(self) -> AsyncJobStore:
        if self._writer is not None:
            return self
        os.makedirs(native_path(self.path.parent), exist_ok=True)
        self._process_lock.acquire()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._writer = asyncio.create_task(self._writer_loop(ready))
        try:
            await ready
            await self.recover_startup()
        except BaseException:
            if self._writer is not None:
                self._writer.cancel()
                await asyncio.gather(self._writer, return_exceptions=True)
                self._writer = None
            self._process_lock.release()
            raise
        return self

    async def __aenter__(self) -> AsyncJobStore:
        return await self.start()

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _writer_loop(self, ready: asyncio.Future[None]) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(native_path(self.path))
            connection.row_factory = sqlite3.Row
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
            ).fetchone()
            if exists is None:
                schema = (
                    files("id_detector.resources").joinpath("jobs.sql").read_text(encoding="utf-8")
                )
                connection.executescript(schema)
            else:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA busy_timeout = 5000")
            connection.commit()
            ready.set_result(None)
            while True:
                operation, future = await self._queue.get()
                if operation is None:
                    break
                assert future is not None
                try:
                    value = operation(connection)
                except BaseException as exc:
                    if not future.done():
                        future.set_exception(exc)
                else:
                    if not future.done():
                        future.set_result(value)
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise
        finally:
            if connection is not None:
                connection.close()

    async def _call(self, operation: WriterOperation[T]) -> T:
        if self._writer is None:
            raise RuntimeError("job store has not been started")
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        await self._queue.put((operation, future))
        return await future

    async def close(self) -> None:
        if self._writer is None:
            self._process_lock.release()
            return
        try:
            await self._queue.put((None, None))
            await self._writer
        finally:
            self._writer = None
            self._process_lock.release()

    async def recover_startup(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.lease_seconds * 2)
        cutoff_text = cutoff.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        now = utc_now()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE jobs SET state='pending', lease_owner=NULL, lease_expires_at=NULL,
                    heartbeat_at=NULL, updated_at=?
                WHERE state='leased' AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                """,
                (now, cutoff_text),
            )
            connection.execute(
                """
                UPDATE jobs SET state='outcome_unknown', lease_owner=NULL,
                    lease_expires_at=NULL, heartbeat_at=NULL, updated_at=?
                WHERE state='submission_started' AND submitted_at IS NULL
                  AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                """,
                (now, cutoff_text),
            )
            connection.execute(
                """
                UPDATE jobs SET state='pending', lease_owner=NULL, lease_expires_at=NULL,
                    heartbeat_at=NULL, updated_at=?
                WHERE state='submitted' AND remote_ref IS NOT NULL
                  AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                """,
                (now, cutoff_text),
            )
            connection.execute(
                """
                UPDATE jobs SET state='outcome_unknown', lease_owner=NULL,
                    lease_expires_at=NULL, heartbeat_at=NULL, updated_at=?
                WHERE state='submitted' AND remote_ref IS NULL
                  AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                """,
                (now, cutoff_text),
            )
            connection.execute(
                """
                UPDATE connector_jobs SET state='pending', lease_owner=NULL,
                    lease_expires_at=NULL, heartbeat_at=NULL, updated_at=?
                WHERE state='leased' AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                """,
                (now, cutoff_text),
            )
            connection.commit()

        await self._call(operation)

    async def ensure_budget(
        self, media_key: str, provider: str, *, max_requests: int, max_usd: int = 0
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT OR IGNORE INTO budgets
                    (media_key, provider, max_requests, max_usd, reserved_requests,
                     reserved_usd, used_requests, used_usd)
                VALUES (?, ?, ?, ?, 0, 0, 0, 0)
                """,
                (media_key, provider, max_requests, max_usd),
            )
            connection.commit()

        await self._call(operation)

    async def extend_budget(
        self, media_key: str, provider: str, *, requests: int, usd: int = 0
    ) -> None:
        """Add one explicitly requested execution allowance to an existing hard ceiling."""

        if requests < 0 or usd < 0:
            raise ValueError("budget extension cannot be negative")

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE budgets SET max_requests=max_requests+?, max_usd=max_usd+?
                WHERE media_key=? AND provider=?
                """,
                (requests, usd, media_key, provider),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise RuntimeError("provider budget has not been created")

        await self._call(operation)

    async def ensure_job(self, media_key: str, query_id: str, provider: str) -> Job:
        job_id = sha1(f"{media_key}job{query_id}".encode(), usedforsecurity=False).hexdigest()
        now = utc_now()

        def operation(connection: sqlite3.Connection) -> Job:
            connection.execute(
                """
                INSERT OR IGNORE INTO jobs
                    (id, media_key, query_id, provider, state, lease_owner, lease_expires_at,
                     heartbeat_at, attempts, physical_attempts, next_retry_at,
                     submission_started_at, submitted_at, remote_ref, reserved_units,
                     reserved_usd, actual_units, actual_usd, result_path, error,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', NULL, NULL, NULL, 0, 0, NULL, NULL, NULL,
                        NULL, 0, 0, 0, 0, NULL, NULL, ?, ?)
                """,
                (job_id, media_key, query_id, provider, now, now),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM jobs WHERE query_id=?", (query_id,)).fetchone()
            assert row is not None
            return Job.from_row(row)

        return await self._call(operation)

    async def lease_next(
        self,
        owner: str,
        *,
        media_key: str | None = None,
        provider: str | None = None,
        query_ids: frozenset[str] | None = None,
    ) -> Job | None:
        now = datetime.now(UTC)
        now_text = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        expires = (
            (now + timedelta(seconds=self.lease_seconds))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

        def operation(connection: sqlite3.Connection) -> Job | None:
            connection.execute("BEGIN IMMEDIATE")
            clauses = [
                "state IN ('pending', 'retryable_failure')",
                "(next_retry_at IS NULL OR next_retry_at <= ?)",
            ]
            parameters: list[str] = [now_text]
            if media_key is not None:
                clauses.append("media_key=?")
                parameters.append(media_key)
            if provider is not None:
                clauses.append("provider=?")
                parameters.append(provider)
            rows = connection.execute(
                f"SELECT id, query_id FROM jobs WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at, id",
                parameters,
            ).fetchall()
            row = next(
                (
                    candidate
                    for candidate in rows
                    if query_ids is None or candidate["query_id"] in query_ids
                ),
                None,
            )
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs SET state='leased', lease_owner=?, lease_expires_at=?,
                    heartbeat_at=?, attempts=attempts+1, updated_at=? WHERE id=?
                """,
                (owner, expires, now_text, now_text, row["id"]),
            )
            connection.commit()
            leased = connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            assert leased is not None
            return Job.from_row(leased)

        return await self._call(operation)

    async def heartbeat(self, job_id: str, owner: str) -> None:
        now = datetime.now(UTC)
        now_text = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        expires = (
            (now + timedelta(seconds=self.lease_seconds))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE jobs SET heartbeat_at=?, lease_expires_at=?, updated_at=?
                WHERE id=? AND lease_owner=? AND state IN
                    ('leased', 'submission_started', 'submitted')
                """,
                (now_text, expires, now_text, job_id, owner),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise RuntimeError("cannot heartbeat an unowned or inactive job")

        await self._call(operation)

    async def submission_started(self, job_id: str, owner: str) -> None:
        now = utc_now()

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE jobs SET state='submission_started',
                    submission_started_at=COALESCE(submission_started_at, ?), updated_at=?
                WHERE id=? AND state='leased' AND lease_owner=?
                """,
                (now, now, job_id, owner),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise RuntimeError("invalid transition to submission_started")

        await self._call(operation)

    async def reserve_billing(self, job_id: str, *, units: int, usd: int) -> None:
        """Reserve scanner units and integer cents before submission begins.

        Scanner units are provider billing units (AudD chunks or ACRCloud scan-seconds). Their
        upload/list/poll HTTP calls are still counted in ``physical_attempts``, but are not each a
        separately billable recognition request.
        """

        if units < 0 or usd < 0:
            raise ValueError("billing reservation cannot be negative")
        now = utc_now()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["state"] != "leased":
                connection.rollback()
                raise RuntimeError("billing reservation requires a leased job")
            existing_units = int(job["reserved_units"])
            existing_usd = int(job["reserved_usd"])
            if existing_units or existing_usd:
                if (existing_units, existing_usd) != (units, usd):
                    connection.rollback()
                    raise RuntimeError("existing billing reservation differs from requested amount")
                # A crash can occur after this transaction and before submission_started. The
                # recovered lease must reuse, rather than duplicate, that durable reservation.
                connection.commit()
                return
            budget = connection.execute(
                "SELECT * FROM budgets WHERE media_key=? AND provider=?",
                (job["media_key"], job["provider"]),
            ).fetchone()
            if budget is None:
                connection.rollback()
                raise RuntimeError("provider budget has not been created")
            if (
                budget["used_requests"] + budget["reserved_requests"] + units
                > budget["max_requests"]
            ):
                connection.rollback()
                raise BudgetExhausted("provider-unit ceiling exhausted")
            if budget["used_usd"] + budget["reserved_usd"] + usd > budget["max_usd"]:
                connection.rollback()
                raise BudgetExhausted("provider cost ceiling exhausted")
            connection.execute(
                """
                UPDATE budgets SET reserved_requests=reserved_requests+?,
                    reserved_usd=reserved_usd+? WHERE media_key=? AND provider=?
                """,
                (units, usd, job["media_key"], job["provider"]),
            )
            connection.execute(
                """
                UPDATE jobs SET reserved_units=reserved_units+?, reserved_usd=reserved_usd+?,
                    updated_at=? WHERE id=?
                """,
                (units, usd, now, job_id),
            )
            connection.commit()

        await self._call(operation)

    async def begin_physical_attempt(self, job_id: str, *, reserve_request: bool = True) -> int:
        """Count network I/O, optionally reserving one billable request.

        Clip recognizers use the default because each HTTP attempt is billable. File scanners
        reserve their duration-derived units up front and pass ``reserve_request=False`` for
        upload/reconciliation/poll traffic.
        """

        now = utc_now()

        def operation(connection: sqlite3.Connection) -> int:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["state"] not in {"submission_started", "submitted"}:
                connection.rollback()
                raise RuntimeError("physical attempt requires an active submission")
            budget = connection.execute(
                "SELECT * FROM budgets WHERE media_key=? AND provider=?",
                (job["media_key"], job["provider"]),
            ).fetchone()
            if budget is None:
                connection.rollback()
                raise RuntimeError("provider budget has not been created")
            if reserve_request and (
                budget["used_requests"] + budget["reserved_requests"] >= budget["max_requests"]
            ):
                connection.rollback()
                raise BudgetExhausted("request ceiling exhausted")
            if reserve_request:
                connection.execute(
                    """
                    UPDATE budgets SET reserved_requests=reserved_requests+1
                    WHERE media_key=? AND provider=?
                    """,
                    (job["media_key"], job["provider"]),
                )
            connection.execute(
                """
                UPDATE jobs SET physical_attempts=physical_attempts+1,
                    reserved_units=reserved_units+?, updated_at=? WHERE id=?
                """,
                (int(reserve_request), now, job_id),
            )
            connection.commit()
            return int(job["physical_attempts"]) + 1

        return await self._call(operation)

    async def submitted(self, job_id: str, remote_ref: str | None = None) -> None:
        now = utc_now()

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE jobs SET state='submitted', submitted_at=COALESCE(submitted_at, ?),
                    remote_ref=COALESCE(remote_ref, ?), updated_at=?
                WHERE id=? AND state='submission_started'
                """,
                (now, remote_ref, now, job_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise RuntimeError("invalid transition to submitted")

        await self._call(operation)

    async def persist_remote_ref(self, job_id: str, remote_ref: str) -> None:
        now = utc_now()

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                "UPDATE jobs SET remote_ref=?, updated_at=? WHERE id=?",
                (remote_ref, now, job_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise KeyError(job_id)

        await self._call(operation)

    async def finish(
        self,
        job_id: str,
        state: str,
        *,
        result_path: str | None,
        error: str | None = None,
        actual_units: int | None = None,
        actual_usd: int = 0,
    ) -> Job:
        if state not in {"succeeded", "no_match", "permanent_failure", "cancelled"}:
            raise ValueError(f"invalid terminal job state: {state}")
        now = utc_now()

        def operation(connection: sqlite3.Connection) -> Job:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(job_id)
            # ``physical_attempts`` is the cumulative audit total. Only this execution's
            # outstanding reservation is newly chargeable to the provider budget.
            charged_units = int(row["reserved_units"])
            settled_units = charged_units if actual_units is None else actual_units
            if settled_units < 0 or settled_units > charged_units:
                connection.rollback()
                raise ValueError("actual units must be within the reserved amount")
            if actual_usd < 0 or actual_usd > int(row["reserved_usd"]):
                connection.rollback()
                raise ValueError("actual cost must be within the reserved amount")
            job_actual_units = (
                int(row["physical_attempts"])
                if actual_units is None
                else int(row["actual_units"]) + settled_units
            )
            job_actual_usd = int(row["actual_usd"]) + actual_usd
            connection.execute(
                """
                UPDATE budgets SET
                    reserved_requests=reserved_requests-?, reserved_usd=reserved_usd-?,
                    used_requests=used_requests+?, used_usd=used_usd+?
                WHERE media_key=? AND provider=?
                """,
                (
                    row["reserved_units"],
                    row["reserved_usd"],
                    settled_units,
                    actual_usd,
                    row["media_key"],
                    row["provider"],
                ),
            )
            connection.execute(
                """
                UPDATE jobs SET state=?, lease_owner=NULL, lease_expires_at=NULL,
                    heartbeat_at=NULL, reserved_units=0, reserved_usd=0,
                    actual_units=?, actual_usd=?, result_path=?, error=?, updated_at=?
                WHERE id=?
                """,
                (state, job_actual_units, job_actual_usd, result_path, error, now, job_id),
            )
            connection.commit()
            final = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert final is not None
            return Job.from_row(final)

        return await self._call(operation)

    async def mark_outcome_unknown(self, job_id: str, *, error: str) -> Job:
        """Conservatively retain reservations when a billed outcome cannot be reconciled."""

        now = utc_now()

        def operation(connection: sqlite3.Connection) -> Job:
            cursor = connection.execute(
                """
                UPDATE jobs SET state='outcome_unknown', lease_owner=NULL,
                    lease_expires_at=NULL, heartbeat_at=NULL, error=?, updated_at=?
                WHERE id=? AND state IN ('submission_started', 'submitted')
                """,
                (error[:1000], now, job_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise RuntimeError("outcome_unknown requires an active submission")
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert row is not None
            return Job.from_row(row)

        return await self._call(operation)

    async def release_owner(self, owner: str) -> None:
        """Apply the Ctrl-C contract to every job leased by one invocation."""

        now = utc_now()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE jobs SET state='pending', lease_owner=NULL, lease_expires_at=NULL,
                    heartbeat_at=NULL, updated_at=? WHERE lease_owner=? AND state='leased'
                """,
                (now, owner),
            )
            connection.execute(
                """
                UPDATE jobs SET state='outcome_unknown', lease_owner=NULL,
                    lease_expires_at=NULL, heartbeat_at=NULL, updated_at=?
                WHERE lease_owner=? AND state='submission_started'
                """,
                (now, owner),
            )
            connection.execute(
                """
                UPDATE jobs SET state=CASE WHEN remote_ref IS NULL
                                           THEN 'outcome_unknown' ELSE 'pending' END,
                    lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL, updated_at=?
                WHERE lease_owner=? AND state='submitted'
                """,
                (now, owner),
            )
            connection.execute(
                """
                UPDATE connector_jobs SET state='pending', lease_owner=NULL,
                    lease_expires_at=NULL, heartbeat_at=NULL, updated_at=?
                WHERE lease_owner=? AND state='leased'
                """,
                (now, owner),
            )
            connection.commit()

        await self._call(operation)

    async def acknowledge_retry(self, job_id: str) -> Job:
        """Conservatively settle unknown exposure and fund one acknowledged replacement."""

        now = utc_now()

        def operation(connection: sqlite3.Connection) -> Job:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["state"] != "outcome_unknown":
                connection.rollback()
                raise ValueError("job is not in outcome_unknown")
            units = int(row["reserved_units"])
            usd = int(row["reserved_usd"])
            budget = connection.execute(
                "SELECT * FROM budgets WHERE media_key=? AND provider=?",
                (row["media_key"], row["provider"]),
            ).fetchone()
            if budget is None:
                connection.rollback()
                raise RuntimeError("provider budget has not been created")
            connection.execute(
                """
                UPDATE budgets SET
                    reserved_requests=reserved_requests-?, reserved_usd=reserved_usd-?,
                    used_requests=used_requests+?, used_usd=used_usd+?,
                    max_requests=max_requests+?, max_usd=max_usd+?
                WHERE media_key=? AND provider=?
                """,
                (units, usd, units, usd, units, usd, row["media_key"], row["provider"]),
            )
            connection.execute(
                """
                UPDATE jobs SET state='pending', lease_owner=NULL, lease_expires_at=NULL,
                    heartbeat_at=NULL, next_retry_at=NULL, reserved_units=0, reserved_usd=0,
                    actual_units=actual_units+?, actual_usd=actual_usd+?, error=NULL, updated_at=?
                WHERE id=? AND state='outcome_unknown'
                """,
                (units, usd, now, job_id),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert row is not None
            return Job.from_row(row)

        return await self._call(operation)

    async def reset_for_refresh(self, job_id: str) -> Job:
        """Bypass a terminal cache result without erasing historical attempt accounting."""

        now = utc_now()

        def operation(connection: sqlite3.Connection) -> Job:
            cursor = connection.execute(
                """
                UPDATE jobs SET state='pending', next_retry_at=NULL,
                    submission_started_at=NULL, submitted_at=NULL, remote_ref=NULL,
                    result_path=NULL, error=NULL, updated_at=?
                WHERE id=? AND state IN
                    ('succeeded', 'no_match', 'retryable_failure', 'permanent_failure')
                """,
                (now, job_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise ValueError("job cannot be refreshed in its current state")
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert row is not None
            return Job.from_row(row)

        return await self._call(operation)

    async def get_job(self, job_id: str) -> Job | None:
        def operation(connection: sqlite3.Connection) -> Job | None:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return Job.from_row(row) if row else None

        return await self._call(operation)

    async def get_job_by_query(self, query_id: str) -> Job | None:
        def operation(connection: sqlite3.Connection) -> Job | None:
            row = connection.execute("SELECT * FROM jobs WHERE query_id=?", (query_id,)).fetchone()
            return Job.from_row(row) if row else None

        return await self._call(operation)

    async def list_jobs(self) -> list[Job]:
        def operation(connection: sqlite3.Connection) -> list[Job]:
            return [
                Job.from_row(row)
                for row in connection.execute("SELECT * FROM jobs ORDER BY created_at, id")
            ]

        return await self._call(operation)

    async def budget(self, media_key: str, provider: str) -> dict[str, int] | None:
        def operation(connection: sqlite3.Connection) -> dict[str, int] | None:
            row = connection.execute(
                "SELECT * FROM budgets WHERE media_key=? AND provider=?", (media_key, provider)
            ).fetchone()
            return dict(row) if row else None

        return await self._call(operation)

    async def ensure_connector_job(
        self,
        media_key: str,
        connector: str,
        target_url: str,
        *,
        page_cap: int,
        item_cap: int,
        input_content_sha256: str | None = None,
        configuration_sha256: str | None = None,
    ) -> ConnectorJob:
        if page_cap < 1 or item_cap < 1:
            raise ValueError("connector caps must be positive")
        target_sha256 = sha256(target_url.encode("utf-8")).hexdigest()
        input_hash = input_content_sha256 or target_sha256
        config_hash = configuration_sha256 or sha256(b"{}").hexdigest()
        for name, value in (
            ("input_content_sha256", input_hash),
            ("configuration_sha256", config_hash),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{name} must be a lowercase SHA-256")
        identity = "\0".join(
            (
                "connector-cache-v2",
                media_key,
                connector,
                target_sha256,
                input_hash,
                config_hash,
                str(page_cap),
                str(item_cap),
            )
        )
        job_id = sha1(
            identity.encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()
        now = utc_now()

        def operation(connection: sqlite3.Connection) -> ConnectorJob:
            connection.execute(
                """
                INSERT OR IGNORE INTO connector_jobs
                    (id, media_key, connector, target_url, cursor, page, page_cap, item_cap,
                     items_fetched, state, lease_owner, lease_expires_at, heartbeat_at, attempts,
                     next_retry_at, result_path, truncated, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, NULL, 0, ?, ?, 0, 'pending', NULL, NULL, NULL, 0,
                        NULL, NULL, 0, NULL, ?, ?)
                """,
                (job_id, media_key, connector, target_url, page_cap, item_cap, now, now),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM connector_jobs WHERE id=?", (job_id,)
            ).fetchone()
            assert row is not None
            return ConnectorJob.from_row(row)

        return await self._call(operation)

    async def lease_connector(self, job_id: str, owner: str) -> ConnectorJob | None:
        now = datetime.now(UTC)
        now_text = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        expires = (
            (now + timedelta(seconds=self.lease_seconds))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

        def operation(connection: sqlite3.Connection) -> ConnectorJob | None:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE connector_jobs SET state='leased', lease_owner=?, lease_expires_at=?,
                    heartbeat_at=?, attempts=attempts+1, updated_at=?
                WHERE id=? AND state IN ('pending', 'retryable_failure')
                    AND (next_retry_at IS NULL OR next_retry_at <= ?)
                """,
                (owner, expires, now_text, now_text, job_id, now_text),
            )
            connection.commit()
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM connector_jobs WHERE id=?", (job_id,)
            ).fetchone()
            assert row is not None
            return ConnectorJob.from_row(row)

        return await self._call(operation)

    async def heartbeat_connector(self, job_id: str, owner: str) -> None:
        now = datetime.now(UTC)
        now_text = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        expires = (
            (now + timedelta(seconds=self.lease_seconds))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE connector_jobs SET heartbeat_at=?, lease_expires_at=?, updated_at=?
                WHERE id=? AND lease_owner=? AND state='leased'
                """,
                (now_text, expires, now_text, job_id, owner),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise RuntimeError("cannot heartbeat an unowned connector job")

        await self._call(operation)

    async def checkpoint_connector(
        self,
        job_id: str,
        owner: str,
        *,
        cursor: str | None,
        page: int,
        items_fetched: int,
        result_path: str | None,
        truncated: bool,
    ) -> ConnectorJob:
        now = utc_now()

        def operation(connection: sqlite3.Connection) -> ConnectorJob:
            row = connection.execute(
                "SELECT * FROM connector_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None or row["state"] != "leased" or row["lease_owner"] != owner:
                raise RuntimeError("connector checkpoint requires the active lease")
            if page < int(row["page"]) or items_fetched < int(row["items_fetched"]):
                raise ValueError("connector checkpoint cannot move backwards")
            if page > int(row["page_cap"]) or items_fetched > int(row["item_cap"]):
                raise ValueError("connector checkpoint exceeds its cap")
            connection.execute(
                """
                UPDATE connector_jobs SET cursor=?, page=?, items_fetched=?, result_path=?,
                    truncated=?, updated_at=? WHERE id=?
                """,
                (cursor, page, items_fetched, result_path, int(truncated), now, job_id),
            )
            connection.commit()
            final = connection.execute(
                "SELECT * FROM connector_jobs WHERE id=?", (job_id,)
            ).fetchone()
            assert final is not None
            return ConnectorJob.from_row(final)

        return await self._call(operation)

    async def finish_connector(
        self,
        job_id: str,
        owner: str,
        state: str,
        *,
        result_path: str | None,
        error: str | None = None,
        truncated: bool | None = None,
    ) -> ConnectorJob:
        if state not in {
            "succeeded",
            "no_match",
            "retryable_failure",
            "permanent_failure",
            "cancelled",
        }:
            raise ValueError(f"invalid terminal connector state: {state}")
        now = utc_now()

        def operation(connection: sqlite3.Connection) -> ConnectorJob:
            cursor = connection.execute(
                """
                UPDATE connector_jobs SET state=?, lease_owner=NULL, lease_expires_at=NULL,
                    heartbeat_at=NULL, result_path=?, error=?,
                    truncated=COALESCE(?, truncated), updated_at=?
                WHERE id=? AND state='leased' AND lease_owner=?
                """,
                (
                    state,
                    result_path,
                    error[:1000] if error else None,
                    int(truncated) if truncated is not None else None,
                    now,
                    job_id,
                    owner,
                ),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise RuntimeError("connector finish requires the active lease")
            row = connection.execute(
                "SELECT * FROM connector_jobs WHERE id=?", (job_id,)
            ).fetchone()
            assert row is not None
            return ConnectorJob.from_row(row)

        return await self._call(operation)

    async def reset_connector(self, job_id: str) -> ConnectorJob:
        now = utc_now()

        def operation(connection: sqlite3.Connection) -> ConnectorJob:
            cursor = connection.execute(
                """
                UPDATE connector_jobs SET state='pending', cursor=NULL, page=0, items_fetched=0,
                    lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                    next_retry_at=NULL, result_path=NULL, truncated=0, error=NULL, updated_at=?
                WHERE id=? AND state IN
                    ('succeeded', 'no_match', 'retryable_failure', 'permanent_failure', 'cancelled')
                """,
                (now, job_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise ValueError("connector job cannot be reset in its current state")
            row = connection.execute(
                "SELECT * FROM connector_jobs WHERE id=?", (job_id,)
            ).fetchone()
            assert row is not None
            return ConnectorJob.from_row(row)

        return await self._call(operation)

    async def list_connector_jobs(self, media_key: str | None = None) -> list[ConnectorJob]:
        def operation(connection: sqlite3.Connection) -> list[ConnectorJob]:
            if media_key is None:
                rows = connection.execute("SELECT * FROM connector_jobs ORDER BY created_at, id")
            else:
                rows = connection.execute(
                    "SELECT * FROM connector_jobs WHERE media_key=? ORDER BY created_at, id",
                    (media_key,),
                )
            return [ConnectorJob.from_row(row) for row in rows]

        return await self._call(operation)

    async def get_connector_job(self, job_id: str) -> ConnectorJob | None:
        def operation(connection: sqlite3.Connection) -> ConnectorJob | None:
            row = connection.execute(
                "SELECT * FROM connector_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return ConnectorJob.from_row(row) if row else None

        return await self._call(operation)


async def heartbeat_job(
    store: AsyncJobStore, job_id: str, owner: str, *, interval_seconds: int = HEARTBEAT_SECONDS
) -> None:
    """Keep a long-running provider lease live until the owning task cancels this coroutine."""

    while True:
        await asyncio.sleep(interval_seconds)
        await store.heartbeat(job_id, owner)
