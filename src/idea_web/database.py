"""SQLite WAL setup and reversible numbered migrations for the hosted worker."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

_MIGRATION_NAME = re.compile(r"(?P<number>[0-9]{4})_(?P<name>[a-z0-9_]+)\.up\.sql")
#: How long a migrator waits for another process to finish migrating before giving up.
MIGRATION_LOCK_TIMEOUT_SECONDS = 120.0
#: A LOCAL work root's queue database is ``<work root>/.idea/app.db``; its supervised worker holds
#: ``<work root>/.idea/worker-supervisor.lock`` for as long as it may claim and pay.
LOCAL_DATABASE_DIR = ".idea"
LOCAL_DATABASE_NAME = "app.db"
SUPERVISOR_LOCK_NAME = "worker-supervisor.lock"


class MigrationRefused(RuntimeError):
    """A local database may not change schema while any ID'er could still be running on it."""


def _local_identity(path: Path) -> str | None:
    """The normalised identity of a LOCAL work root's queue database, or ``None`` (hosted).

    Windows lookups ignore case and accept ``\\\\?\\`` aliases, so the resolved path is stripped of
    that prefix and ``os.path.normcase``d, and the ``.idea``/``app.db`` names are compared
    case-folded: every alias of one database gets one identity (and so one supervisor lock).
    """

    text = str(Path(path).resolve())
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    normal = os.path.normcase(text)
    name = os.path.basename(normal).casefold()
    folder = os.path.basename(os.path.dirname(normal)).casefold()
    if name == LOCAL_DATABASE_NAME and folder == LOCAL_DATABASE_DIR:
        return normal
    return None


def _statements(script: str) -> list[str]:
    """One migration script as complete SQL statements (trigger bodies stay whole)."""

    statements: list[str] = []
    buffer = ""
    for chunk in script.split(";"):
        buffer += chunk + ";"
        if sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    rest = buffer[:-1]
    if rest.strip():
        statements.append(rest)  # a trailing comment, or an unterminated final statement
    return statements


def _lock_exclusive(descriptor: int, *, timeout: float) -> None:
    """Block until this descriptor owns the lock file, across processes as well as threads."""

    if os.name != "nt":
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return
    import msvcrt

    deadline = time.monotonic() + timeout
    while True:
        try:
            # ``LK_LOCK`` already retries for ten seconds; the loop covers a longer migration.
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise


def _unlock(descriptor: int) -> None:
    if os.name != "nt":
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    import contextlib
    import msvcrt

    with contextlib.suppress(OSError):
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


@dataclass(frozen=True)
class Migration:
    number: int
    name: str
    up: str
    down: str


def migrations() -> tuple[Migration, ...]:
    """Return every packaged migration, refusing gaps or a missing down script."""

    root = files("idea_web.migrations")
    found: list[Migration] = []
    for item in root.iterdir():
        match = _MIGRATION_NAME.fullmatch(item.name)
        if match is None:
            continue
        down = root.joinpath(item.name.removesuffix(".up.sql") + ".down.sql")
        if not down.is_file():
            raise RuntimeError(f"migration has no down script: {item.name}")
        found.append(
            Migration(
                int(match.group("number")),
                match.group("name"),
                item.read_text(encoding="utf-8"),
                down.read_text(encoding="utf-8"),
            )
        )
    ordered = tuple(sorted(found, key=lambda item: item.number))
    if tuple(item.number for item in ordered) != tuple(range(1, len(ordered) + 1)):
        raise RuntimeError("migration numbers must be contiguous and start at 0001")
    return ordered


class Database:
    """Connection factory with the process-local half of the one-writer discipline.

    Every write also starts ``BEGIN IMMEDIATE``. SQLite therefore serialises writers across
    processes; this lock prevents avoidable contention between the heartbeat and worker threads.
    """

    def __init__(self, path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.path = Path(path).resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self._writer = threading.RLock()
        self._migrations = threading.RLock()
        #: Test seam: runs inside the migration's write transaction, just before the claim check.
        self.before_claim_check: Callable[[sqlite3.Connection], None] | None = None

    @property
    def migration_lock_path(self) -> Path:
        return self.path.parent / (self.path.name + ".migrate.lock")

    @contextmanager
    def _migration_lock(self) -> Iterator[None]:
        """Serialise migrators across processes.

        Reading ``schema_migrations`` and applying the next script has to be ONE critical section:
        two processes that each read "0001 is not applied" would both run it, and the loser would
        crash mid-schema (or, worse, half-apply a later migration). SQLite's own write lock cannot
        cover this because ``executescript`` commits between statements.
        """

        path = self.migration_lock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._migrations:
            descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
            try:
                _lock_exclusive(descriptor, timeout=MIGRATION_LOCK_TIMEOUT_SECONDS)
                try:
                    yield
                finally:
                    _unlock(descriptor)
            finally:
                os.close(descriptor)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._writer:
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def migrate(self, target: int | None = None) -> int:
        """Move to ``target`` (latest by default), applying every up or down script in order.

        Atomic: every pending script runs statement by statement inside ONE ``BEGIN IMMEDIATE``
        transaction that commits once. For a local work root the supervisor lock is taken before
        any DDL and the claim check runs inside that write transaction, so no claim can commit
        between the check and the schema change.
        """

        available = migrations()
        latest = available[-1].number if available else 0
        desired = latest if target is None else target
        if desired < 0 or desired > latest:
            raise ValueError(f"migration target must be between 0 and {latest}")
        with self._migration_lock(), self._writer:
            connection = self.connect()
            supervisor = None
            try:
                if not self._pending(self._applied(connection), available, desired):
                    return desired
                supervisor = self._acquire_supervisor()  # before any DDL
                connection.execute("BEGIN IMMEDIATE")  # the ONE write transaction
                if self.before_claim_check is not None:
                    self.before_claim_check(connection)
                self._refuse_live_unproven_claims(connection)
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "number INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
                )
                applied = self._applied(connection)
                for migration in available:
                    if migration.number <= desired and migration.number not in applied:
                        for statement in _statements(migration.up):
                            connection.execute(statement)
                        connection.execute(
                            "INSERT INTO schema_migrations(number, name, applied_at) "
                            "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                            (migration.number, migration.name),
                        )
                for migration in reversed(available):
                    if migration.number > desired and migration.number in applied:
                        for statement in _statements(migration.down):
                            connection.execute(statement)
                        connection.execute(
                            "DELETE FROM schema_migrations WHERE number = ?", (migration.number,)
                        )
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
                if supervisor is not None:
                    supervisor.release()
        return desired

    @staticmethod
    def _applied(connection: sqlite3.Connection) -> dict[int, str]:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if exists is None:
            return {}
        return {
            int(row["number"]): str(row["name"])
            for row in connection.execute("SELECT number, name FROM schema_migrations")
        }

    @staticmethod
    def _pending(applied: dict[int, str], available: tuple[Migration, ...], desired: int) -> bool:
        return any(
            (migration.number <= desired) != (migration.number in applied)
            for migration in available
        )

    @property
    def is_local_work_root(self) -> bool:
        return _local_identity(self.path) is not None

    @property
    def supervisor_lock_path(self) -> Path | None:
        """``<work root>/.idea/worker-supervisor.lock``, from the same normalised identity."""

        identity = _local_identity(self.path)
        if identity is None:
            return None
        return Path(os.path.dirname(identity)) / SUPERVISOR_LOCK_NAME

    def _acquire_supervisor(self) -> Any:
        """For a local work root, the supervisor lock every ``idea serve`` holds; else ``None``."""

        lock_path = self.supervisor_lock_path
        if lock_path is None:
            return None
        from id_detector.jobs import JobStoreLocked, ProcessLock

        lock = ProcessLock(lock_path)
        try:
            lock.acquire()
        except JobStoreLocked:
            raise MigrationRefused(
                "ID'er needs to upgrade this work folder's database, but another ID'er is still "
                "running analyses here: stop the running ID'er first, then start this one again"
            ) from None
        return lock

    def _refuse_live_unproven_claims(self, connection: sqlite3.Connection) -> None:
        """Inside the write transaction: no unexpired claim made without an authority token."""

        if not self.is_local_work_root:
            return
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        if "claim_token" not in columns:
            return
        unproven = "authority_token IS NOT claim_token" if "authority_token" in columns else "1"
        live = connection.execute(
            "SELECT 1 FROM jobs WHERE claim_token IS NOT NULL AND lease_until > ? "
            f"AND {unproven} LIMIT 1",
            (time.time(),),
        ).fetchone()
        if live is not None:
            raise MigrationRefused(
                "ID'er needs to upgrade this work folder's database, but an analysis started by "
                "an older ID'er may still be running here: stop the running ID'er first, then "
                "start this one again"
            )

    def version(self) -> int:
        with self.read() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if exists is None:
                return 0
            row = connection.execute(
                "SELECT COALESCE(MAX(number), 0) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"])
