"""The operator's view of a running service (cycle 4b-ii).

4b-i deliberately left "the operator view of long-waiting jobs" to this cycle: a job that waits on
an open breaker consumes no attempts, so nothing in the queue ever complains about it, and without
a view of it a service can sit idle for a day and look healthy. This module answers, from the
durable state alone and without running anything:

* what the queue holds, by state, and what has been waiting too long;
* what has dead-lettered, and why;
* what each egress has asked of each provider today, and what the ledger says that cost;
* what the shared §2.3.5 breaker is doing right now, and the one operator action it takes
  (re-enable, after rule (c) has latched it off — owner decision D8's manual half);
* how much room the work root has left (risk R7).

**Reading is read-only at the SQLite boundary, not merely by intention.** `Database.connect()`
creates the file when it is missing and runs ``PRAGMA journal_mode = WAL`` — both writes — so a
typoed path would create a database and a non-WAL database would be modified by being looked at.
Every read here opens the file through SQLite's ``mode=ro`` URI instead, and a database that is not
there is reported as nothing rather than brought into existence. The single exception is
:func:`reenable_breaker`, which writes because an operator asked it to.

It is also **not** the money authority: spend here is the attempt ledger's own view, for an
operator's eyes, while `run_settlements` remains the single authority every settlement is written
and read through.

There is no HTTP route here. Hosted accounts, sessions and the `/admin` pages arrive in 4c-i and 6b;
an unauthenticated operations endpoint would be a hole, so this cycle ships the data and the action
behind the same process boundary the worker already runs in (``python -m idea_web.ops``).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from idea_web.breaker import SHAZAM, SharedShazamBreaker
from idea_web.database import Database
from idea_web.jobs.worker import ACTIVE_RUN_STATES, TERMINAL_STATES

#: A job parked longer than this is worth an operator's attention: §4.6 waits are deliberately
#: free (they consume no attempt), so nothing else will ever raise its hand.
LONG_WAIT_SECONDS = 900.0
#: How many long-waiting and dead-lettered jobs the snapshot names individually.
SAMPLE_LIMIT = 20


def _identity(*paths: Path) -> tuple[tuple[int, int] | None, ...]:
    found: list[tuple[int, int] | None] = []
    for path in paths:
        try:
            status = path.stat()
        except FileNotFoundError:
            found.append(None)
            continue
        found.append((status.st_size, status.st_mtime_ns))
    return tuple(found)


def _copy_into(path: Path, memory: sqlite3.Connection, *, attempts: int = 5) -> bool:
    """Copy the live database into ``memory`` without creating a single file beside it.

    Two situations, told apart by the shared-memory index:

    * **A WAL connection is open** (``-wal`` and ``-shm`` both exist): read through SQLite's own
      locking with ``mode=ro`` and ``Connection.backup()``. That sees every committed transaction —
      including ones still only in the WAL — consistently, and the sidecars it uses already exist.
    * **Nothing has it open** (no ``-shm``): the files are at rest. They are copied into a scratch
      directory outside the work root and read there. If their identity moves during the copy,
      somebody started writing, and the next pass takes the first route.

    ``immutable=1`` was the wrong tool: SQLite documents it as a promise that the file never
    changes, and a queue being written breaks that promise — giving silently wrong answers.
    """

    import tempfile

    wal = Path(str(path) + "-wal")
    shm = Path(str(path) + "-shm")
    for _ in range(attempts):
        if wal.exists() and shm.exists():
            try:
                source = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0)
            except sqlite3.DatabaseError:
                return False
            try:
                source.backup(memory)
                return True
            except sqlite3.DatabaseError:
                return False
            finally:
                source.close()
        before = _identity(path, wal, shm)
        with tempfile.TemporaryDirectory(prefix="idea-ops-", ignore_cleanup_errors=True) as scratch:
            copy = Path(scratch) / path.name
            try:
                shutil.copyfile(path, copy)
                if before[1] is not None:
                    shutil.copyfile(wal, Path(str(copy) + "-wal"))
            except FileNotFoundError:
                continue
            if _identity(path, wal, shm) != before:
                continue
            source = sqlite3.connect(copy)
            try:
                source.backup(memory)
            except sqlite3.DatabaseError:
                return False
            finally:
                source.close()
            return True
    return False


#: While :func:`operations_snapshot` runs, every section reads the ONE copy it took, so the
#: document describes a single moment and the database is copied once rather than per section.
_PINNED: ContextVar[tuple[Database, sqlite3.Connection | None] | None] = ContextVar(
    "idea_ops_pinned", default=None
)


@contextmanager
def read_only(database: Database) -> Iterator[sqlite3.Connection | None]:
    """A private, in-memory copy of the queue database, or ``None`` when there is none.

    Nothing is created beside the database — no file, no ``-wal``, no ``-shm`` — and no journal
    mode is touched, yet the view stays correct while the service is writing.
    """

    pinned = _PINNED.get()
    if pinned is not None and pinned[0] is database:
        yield pinned[1]
        return
    path = Path(database.path)
    if not path.is_file():
        yield None
        return
    memory = sqlite3.connect(":memory:")
    try:
        if not _copy_into(path, memory):
            yield None
            return
        memory.row_factory = sqlite3.Row
        yield memory
    finally:
        memory.close()


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).date().isoformat()


def queue_depth(database: Database) -> dict[str, int]:
    """How many jobs sit in each state, including the states with none."""

    counts = {state: 0 for state in sorted(ACTIVE_RUN_STATES | TERMINAL_STATES)}
    with read_only(database) as connection:
        if connection is None:
            return counts
        for row in connection.execute("SELECT state, COUNT(*) AS held FROM jobs GROUP BY state"):
            counts[str(row["state"])] = int(row["held"])
    return counts


def waiting_jobs(
    database: Database, *, now: float | None = None, threshold: float = LONG_WAIT_SECONDS
) -> list[dict[str, Any]]:
    """Jobs parked in ``waiting`` (or held in intake) for longer than ``threshold`` seconds."""

    moment = time.time() if now is None else now
    found: list[dict[str, Any]] = []
    with read_only(database) as connection:
        if connection is None:
            return found
        rows = connection.execute(
            "SELECT id, run_id, state, progress, updated_at FROM jobs "
            "WHERE state IN ('waiting', 'intake') ORDER BY updated_at"
        ).fetchall()
    for row in rows:
        waited = moment - float(row["updated_at"])
        if waited < threshold:
            continue
        try:
            document = json.loads(row["progress"]) or {}
        except (TypeError, ValueError):
            document = {}
        found.append(
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "state": row["state"],
                "waited_seconds": round(waited, 3),
                "reason": document.get("reason") if isinstance(document, dict) else None,
            }
        )
    return found[:SAMPLE_LIMIT]


def dead_letters(database: Database) -> list[dict[str, Any]]:
    """Jobs that exhausted their attempts, with the reason they stopped."""

    with read_only(database) as connection:
        if connection is None:
            return []
        rows = connection.execute(
            "SELECT id, run_id, dead_letter_reason, updated_at FROM jobs "
            "WHERE state='dead_letter' ORDER BY updated_at DESC"
        ).fetchall()
    return [
        {
            "id": row["id"],
            "run_id": row["run_id"],
            "reason": row["dead_letter_reason"],
            "at": row["updated_at"],
        }
        for row in rows[:SAMPLE_LIMIT]
    ]


def provider_usage(database: Database, *, now: float | None = None) -> list[dict[str, Any]]:
    """Per provider and egress, today's attempt ledger: asked, sent, resolved, failed, and cost.

    ``usd_e6`` counts only §2.3.2's **billable** outcomes. A 429, an auth error and a quota error
    cost nothing, and a display that billed an operator for them would simply be false.
    """

    from id_detector.money import BILLABLE_OUTCOMES
    from id_detector.shazam_breaker import FAILURES

    today = _day(time.time() if now is None else now)
    with read_only(database) as connection:
        if connection is None:
            return []
        rows = connection.execute(
            "SELECT provider, egress_id, state, outcome, unit_usd_e6 FROM provider_attempt_events "
            "WHERE substr(at, 1, 10) = ?",
            (today,),
        ).fetchall()
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["provider"]), str(row["egress_id"]))
        entry = totals.setdefault(
            key,
            {
                "provider": key[0],
                "egress_id": key[1],
                "day": today,
                "prepared": 0,
                "dispatched": 0,
                "resolved": 0,
                "failures": 0,
                "usd_e6": 0,
            },
        )
        state = str(row["state"])
        if state == "prepared":
            entry["prepared"] += 1
        elif state == "dispatched":
            # What actually left this egress — the number rule (b) budgets against.
            entry["dispatched"] += 1
        elif state == "resolved":
            entry["resolved"] += 1
            entry["failures"] += row["outcome"] in FAILURES
            if row["outcome"] in BILLABLE_OUTCOMES:
                entry["usd_e6"] += int(row["unit_usd_e6"])
    return [totals[key] for key in sorted(totals)]


def breaker_states(
    database: Database, *, config: Any = None, now: float | None = None
) -> list[dict[str, Any]]:
    """Every provider/egress the breaker has state for, plus what it refuses right now.

    Read-only twice over: the connection cannot write, and the judgement is
    :meth:`SharedShazamBreaker.view_with`, which persists nothing. It judges with the worker's
    *configured* policy rather than library defaults — a display using different thresholds would
    report a state the service is not actually in.
    """

    moment = datetime.fromtimestamp(time.time() if now is None else now, UTC)
    states: list[dict[str, Any]] = []
    with read_only(database) as connection:
        if connection is None:
            return states
        known = {
            (str(row["provider"]), str(row["egress_id"]))
            for row in connection.execute("SELECT provider, egress_id FROM provider_breaker_state")
        }
        seen = {
            (str(row["provider"]), str(row["egress_id"]))
            for row in connection.execute(
                "SELECT DISTINCT provider, egress_id FROM provider_attempt_events"
            )
        }
        for provider, egress in sorted(known | seen | {(SHAZAM, "default")}):
            if provider != SHAZAM:
                continue
            breaker = SharedShazamBreaker(
                database, provider=provider, egress_id=egress, config=config
            )
            refusing, judged = breaker.view_with(connection, moment)
            states.append(
                {
                    "provider": provider,
                    "egress_id": egress,
                    "refusing": refusing,
                    "day": judged.day,
                    "opens_today": judged.opens,
                    "open_until": judged.open_until,
                    "latched": judged.latched,
                    "reenable_generation": judged.reenable_generation,
                    "reenabled_at": judged.reenabled_at,
                }
            )
    return states


def reenable_breaker(
    database: Database,
    *,
    provider: str = SHAZAM,
    egress_id: str = "default",
    config: Any = None,
) -> dict[str, Any]:
    """The operator action of §2.3.5 rule (c): turn a latched provider back on.

    The one thing in this module that writes, and it only ever writes because an operator asked.
    """

    SharedShazamBreaker(database, provider=provider, egress_id=egress_id, config=config).reenable()
    return next(
        state
        for state in breaker_states(database, config=config)
        if state["provider"] == provider and state["egress_id"] == egress_id
    )


def disk(work_root: Path | None) -> dict[str, Any] | None:
    """Free space where the artefacts live (risk R7). O(1): never a walk of the work root."""

    if work_root is None:
        return None
    root = Path(work_root)
    try:
        usage = shutil.disk_usage(root if root.exists() else root.anchor or ".")
    except OSError:
        return None
    return {
        "path": str(root),
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "free_ratio_e4": int(usage.free * 10_000 / usage.total) if usage.total else 0,
    }


def operations_snapshot(
    database: Database,
    *,
    work_root: Path | None = None,
    config: Any = None,
    now: float | None = None,
    threshold: float = LONG_WAIT_SECONDS,
) -> dict[str, Any]:
    """Everything above, in one JSON-safe document, and not one write among them."""

    moment = time.time() if now is None else now
    with read_only(database) as connection:
        token = _PINNED.set((database, connection))
        try:
            return {
                "at": moment,
                "day": _day(moment),
                "queue": queue_depth(database),
                "waiting": waiting_jobs(database, now=moment, threshold=threshold),
                "dead_letters": dead_letters(database),
                "providers": provider_usage(database, now=moment),
                "breakers": breaker_states(database, config=config, now=moment),
                "disk": disk(work_root),
            }
        finally:
            _PINNED.reset(token)


def main(argv: list[str] | None = None) -> int:
    """``python -m idea_web.ops <app.db> [show|reenable] [--egress ID] [--work-root DIR]``."""

    import argparse

    parser = argparse.ArgumentParser(prog="idea_web.ops", description=__doc__)
    parser.add_argument("database", type=Path, help="path to the queue database")
    parser.add_argument("action", nargs="?", default="show", choices=("show", "reenable"))
    parser.add_argument("--provider", default=SHAZAM)
    parser.add_argument("--egress", default="default")
    parser.add_argument("--work-root", type=Path, default=None)
    args = parser.parse_args(argv)

    from id_detector.providers.base import AppConfig

    database = Database(args.database)
    # The same policy the worker builds its breaker from, so the display and the service agree.
    config = getattr(AppConfig(), "shazam_breaker", None)
    if args.action == "reenable":
        payload: Any = reenable_breaker(
            database, provider=args.provider, egress_id=args.egress, config=config
        )
    else:
        payload = operations_snapshot(database, work_root=args.work_root, config=config)
    json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
