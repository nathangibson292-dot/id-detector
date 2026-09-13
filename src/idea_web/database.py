"""SQLite WAL setup and reversible numbered migrations for the hosted worker."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

_MIGRATION_NAME = re.compile(r"(?P<number>[0-9]{4})_(?P<name>[a-z0-9_]+)\.up\.sql")
#: How long a migrator waits for another process to finish migrating before giving up.
MIGRATION_LOCK_TIMEOUT_SECONDS = 120.0


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
        """Move to ``target`` (latest by default), applying every up or down script in order."""

        available = migrations()
        latest = available[-1].number if available else 0
        desired = latest if target is None else target
        if desired < 0 or desired > latest:
            raise ValueError(f"migration target must be between 0 and {latest}")
        with self._migration_lock(), self._writer:
            connection = self.connect()
            try:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "number INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
                )
                applied = {
                    int(row["number"]): str(row["name"])
                    for row in connection.execute(
                        "SELECT number, name FROM schema_migrations ORDER BY number"
                    )
                }
                for migration in available:
                    if migration.number <= desired and migration.number not in applied:
                        connection.executescript(
                            "BEGIN IMMEDIATE;\n"
                            + migration.up
                            + "\nINSERT INTO schema_migrations(number, name, applied_at) VALUES ("
                            + f"{migration.number}, '{migration.name}', "
                            + "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\nCOMMIT;"
                        )
                for migration in reversed(available):
                    if migration.number > desired and migration.number in applied:
                        connection.executescript(
                            "BEGIN IMMEDIATE;\n"
                            + migration.down
                            + "\nDELETE FROM schema_migrations WHERE number = "
                            + f"{migration.number};\n"
                            + "COMMIT;"
                        )
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
        return desired

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
