"""Round 7: SQLite settles without media, provenance, retries, Shazam fence (round-6 review).

Against the round-6 code 11 of these 13 cases failed and 2 passed (the hosted analysis-resume and
waiting-retry refusals, round-6 behaviour). Every case was then shown to fail with its own fix
reverted in the round-7 code; see ``docs/reviews/followup-money-resume.md``. New round-7 names are
imported inside each test so a reverted fix fails that test alone.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from id_detector import ingest as ingest_module
from id_detector.attempts import AttemptJournal, DispatchRefused
from id_detector.compat import LOCAL_OWNER_SCOPE
from id_detector.jobs import ProcessLock
from id_detector.money import reserve_usd
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE
from id_detector.run_ledger import LedgerConflict, RecoveredMoney, new_run_id
from id_detector.service import PlatformUrl
from idea_web.database import Database
from idea_web.jobs import worker as worker_module
from idea_web.jobs.local import SUPERVISOR_LOCK, LocalJobs, LocalWorker, local_database
from idea_web.jobs.worker import DispatchAdmission, JobQueue, SettlementLedger, Worker
from tests.idea_web.test_followup_review_fixes import UNIT, _invocations
from tests.idea_web.test_followup_round2 import _env, _expire, _nothing_runs
from tests.idea_web.test_followup_round5 import _claimed_local_job
from tests.idea_web.test_followup_round6 import (
    _count,
    _entry_of,
    _killed_deep_job,
    _reservation_row,
    _settlement,
    _start,
)
from tests.idea_web.test_worker import AUDIO, MIX, _database, _insert_run, _intake, _job_row
from tests.test_followup_money_resume import _crash_mid_primary

HOSTED_REFUSAL = "paid engines are not enabled in hosted mode yet"


def _stamp(database: Database, job_id: str):
    with database.read() as connection:
        return connection.execute(
            "SELECT money_authority FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0]


def _terminal(database: Database, job_id: str, state: str = "cancelled") -> None:
    with database.write() as connection:
        connection.execute(
            "UPDATE jobs SET state=?, lease_owner=NULL, lease_until=NULL, claim_token=NULL "
            "WHERE id=?",
            (state, job_id),
        )


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _MissingMedia:
    """``ingest._load_cached`` that cannot locate any media, counting its lookups."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return None


# ------------------------------------------- item 1: paid media missing -> exact SQLite settlement


def test_paid_media_missing_settles_exactly_from_sqlite_and_projects_once_it_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, jobs, job_id, run_id, dispatched = _killed_deep_job(tmp_path, monkeypatch)
    reservation = _reservation_row(jobs.database, run_id)
    assert reservation is not None and dispatched
    _terminal(jobs.database, job_id)
    real = ingest_module._load_cached
    monkeypatch.setattr(ingest_module, "_load_cached", _MissingMedia())
    clock = _Clock()
    worker = LocalWorker(
        local_database(tmp_path), tmp_path, _nothing_runs, flush_seconds=0.05, monotonic=clock
    )
    worker.sweep_settlements()
    row = _settlement(jobs.database, run_id)
    assert row is not None, "SQLite proves the spend, yet no settlement row was written"
    assert row["path"] == ""  # nothing to project into until the media is found
    assert row["status"] == "cancelled"
    assert row["usd_e6_spent"] == len(dispatched) * UNIT  # the unresolved dispatch counts as spent
    assert row["usd_e6_reserved"] == reservation["usd_e6_reserved"]
    assert _invocations(tmp_path, run_id) == []

    monkeypatch.setattr(ingest_module, "_load_cached", real)
    clock.now += 1
    worker.sweep_settlements()
    projected = _settlement(jobs.database, run_id)
    assert projected is not None and projected["path"]
    assert projected["usd_e6_spent"] == row["usd_e6_spent"]
    assert projected["usd_e6_reserved"] == row["usd_e6_reserved"]
    assert _invocations(tmp_path, run_id) == [_entry_of(projected)]


# ------------------------------------------- item 2: durable provenance, never wall-clock ordering


def test_an_old_worker_claim_marks_the_job_unproven_so_it_never_gets_a_zero_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(str(AUDIO), "max_accuracy")
    run_id = jobs.queue.get(job_id).run_id
    assert _stamp(jobs.database, job_id) == 3
    # A still-running worker of OLDER code claims it: it knows neither provenance column.
    with jobs.database.write() as connection:
        connection.execute(
            "UPDATE jobs SET claim_token='old-code-claim', lease_owner='old-worker', "
            "lease_until=?, heartbeat_at=?, attempt=attempt+1 WHERE id=?",
            (time.time() + 600, time.time(), job_id),
        )
    assert _stamp(jobs.database, job_id) == 0
    # ...pays without authority rows, dies, and the job ends up stopped with its media missing.
    _terminal(jobs.database, job_id)
    _start(tmp_path)
    assert _settlement(jobs.database, run_id) is None, "an unproven job got a zero settlement"
    # A later claim by current code cannot restore the proof either.
    with jobs.database.write() as connection:
        connection.execute("UPDATE jobs SET state='intake' WHERE id=?", (job_id,))
    assert jobs.queue.claim("new-worker", lease_seconds=600) is not None
    assert _stamp(jobs.database, job_id) == 0


def test_clock_skew_on_an_unstamped_job_never_yields_a_zero_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(str(AUDIO))
    run_id = jobs.queue.get(job_id).run_id
    with jobs.database.write() as connection:
        # A pre-authority job whose timestamp a clock correction put far in the future.
        connection.execute(
            "UPDATE jobs SET money_authority=NULL, created_at=? WHERE id=?",
            (time.time() + 10 * 365 * 86_400, job_id),
        )
    assert jobs.cancel(job_id)
    _start(tmp_path)
    assert _settlement(jobs.database, run_id) is None


def test_serve_refuses_to_migrate_while_another_supervised_worker_holds_the_lock(
    tmp_path: Path,
) -> None:
    from idea_web.jobs.local import MigrationRefused

    database = Database(tmp_path / ".idea" / "app.db")
    assert database.migrate(2) == 2
    lock = ProcessLock(tmp_path / SUPERVISOR_LOCK)
    lock.acquire()
    try:
        with pytest.raises(MigrationRefused, match="stop the running ID'er first"):
            local_database(tmp_path)
        assert database.version() == 2
    finally:
        lock.release()
    assert local_database(tmp_path).version() == 3


def test_a_worker_exits_cleanly_when_the_schema_is_newer_than_its_code(tmp_path: Path) -> None:
    from idea_web.jobs.local import EXIT_SCHEMA_TOO_NEW  # noqa: F401 - the supervisor's stop code

    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(str(AUDIO))
    with jobs.database.write() as connection:
        connection.execute(
            "INSERT INTO schema_migrations(number, name, applied_at) VALUES (99, 'future', 'x')"
        )
    worker = LocalWorker(local_database(tmp_path), tmp_path, _nothing_runs, flush_seconds=0.05)
    thread = threading.Thread(target=worker.run_forever, kwargs={"poll_seconds": 0.01}, daemon=True)
    thread.start()
    thread.join(20)
    try:
        assert not thread.is_alive(), "the worker kept running against a newer schema"
        assert worker.schema_too_new
        row = jobs.queue.get(job_id)
        assert row.state == "intake" and row.attempt == 0  # never claimed
    finally:
        worker.stopped.set()
        thread.join(5)


# ------------------------------------------- item 3: a miss stays scheduled until it succeeds


def test_a_settlement_is_recovered_after_more_than_three_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(tmp_path, monkeypatch)
    run_id = "legacy-long-outage"
    dispatched = _crash_mid_primary(tmp_path, run_id)  # JSONL-only spend: no SQLite proof
    assert dispatched
    database = local_database(tmp_path)
    job_id = uuid.uuid4().hex
    now = time.time()
    with database.write() as connection:
        connection.execute(
            "INSERT INTO jobs(id, run_id, target, recipe_id, state, tenant_scope, progress, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'cancelled', ?, '{}', ?, ?)",
            (
                job_id,
                run_id,
                json.dumps({"kind": "local", "path": str(AUDIO)}),
                DEEP_RECIPE.recipe_id,
                LOCAL_OWNER_SCOPE,
                now - 60,
                now - 30,
            ),
        )
    real = ingest_module._load_cached
    missing = _MissingMedia()
    monkeypatch.setattr(ingest_module, "_load_cached", missing)
    clock = _Clock()
    worker = LocalWorker(database, tmp_path, _nothing_runs, flush_seconds=0.05, monotonic=clock)
    for _ in range(6):
        worker.sweep_settlements()
        clock.now += 3_600  # past any backoff
    assert _settlement(database, run_id) is None
    # Backoff: straight after a late miss the run is not retried yet...
    worker.sweep_settlements()
    looked = missing.calls
    worker.sweep_settlements()
    assert missing.calls == looked
    # ...but it is still scheduled: once the media is back, the next due sweep settles it.
    monkeypatch.setattr(ingest_module, "_load_cached", real)
    clock.now += 3_600
    worker.sweep_settlements()
    row = _settlement(database, run_id)
    assert row is not None, "a settlement miss was dropped after three misses"
    assert row["usd_e6_spent"] == len(dispatched) * UNIT
    (line,) = _invocations(tmp_path, run_id)
    assert line == _entry_of(row)


# ------------------------------------------- item 4: the supervised-local Shazam fence


def test_a_stale_or_duplicate_supervised_local_shazam_dispatch_is_refused(tmp_path: Path) -> None:
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(str(AUDIO))

    def shazam_for(claimed, folder: str) -> AttemptJournal:
        parent = AttemptJournal(
            tmp_path / folder / "attempts.jsonl",
            run_id=claimed.run_id,
            provider="audd",
            unit_usd_e6=UNIT,
        )
        parent.admission = DispatchAdmission(
            jobs.database, job_id=claimed.id, claim_token=claimed.token
        )
        return parent.for_provider("shazam")

    def shazam_rows() -> int:
        with jobs.database.read() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM run_dispatches WHERE provider='shazam'"
            ).fetchone()[0]

    query, window = "e" * 64, "f" * 40
    a = jobs.queue.claim("worker-a", lease_seconds=600)
    assert a is not None
    stale = shazam_for(a, "a")
    attempt = stale.prepare(query_id=query, window_id=window, ordinal=0, parent_attempt_id=None)
    _expire(jobs, job_id)
    b = jobs.queue.claim("worker-b", lease_seconds=600)
    assert b is not None
    with pytest.raises(DispatchRefused):
        stale.dispatched(attempt)  # the reclaimed worker's request never leaves
    assert shazam_rows() == 0

    # Two writers of ONE attempt identity: the unique key lets exactly one through.
    first, second = shazam_for(b, "b1"), shazam_for(b, "b2")
    x = first.prepare(query_id=query, window_id=window, ordinal=0, parent_attempt_id=None)
    y = second.prepare(query_id=query, window_id=window, ordinal=0, parent_attempt_id=None)
    assert x == y
    first.dispatched(x)
    with pytest.raises(DispatchRefused):
        second.dispatched(y)
    assert shazam_rows() == 1

    # A cancellation committed before the dispatch refuses it too.
    z = first.prepare(query_id=query, window_id=window, ordinal=1, parent_attempt_id=x)
    assert jobs.queue.request_cancel(job_id)
    with pytest.raises(DispatchRefused):
        first.dispatched(z)
    assert shazam_rows() == 1


# ------------------------------------------- item 5: the hosted refusal matrix


def _refused_hosted(database: Database, tmp_path: Path, job_id: str) -> None:
    calls: list[str] = []

    def never_intake(*_args):
        calls.append("intake")
        raise AssertionError("a hosted paid job reached intake")

    def never_run(_request):
        calls.append("service")
        raise AssertionError("a hosted paid job reached the service")

    result = Worker(
        database, tmp_path / "work", intake_resolver=never_intake, service_runner=never_run
    ).run_once()
    assert result is not None and result.id == job_id
    row = _job_row(database, job_id)
    assert row["state"] == "dead_letter", row["dead_letter_reason"]
    assert HOSTED_REFUSAL in (row["dead_letter_reason"] or "")
    assert calls == []
    for table in ("provider_attempt_events", "run_dispatches", "run_reservations"):
        assert _count(database, table) == 0, table


def test_hosted_refusal_on_an_analysis_state_resume(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run_id = "hosted-resume"
    job_id = JobQueue(database).enqueue(PlatformUrl(MIX), DEEP_RECIPE, run_id=run_id)
    _insert_run(database, _intake(tmp_path, DEEP_RECIPE), run_id=run_id, status="analysis")
    with database.write() as connection:
        connection.execute("UPDATE jobs SET state='analysis', attempt=1 WHERE id=?", (job_id,))
    _refused_hosted(database, tmp_path, job_id)


def test_hosted_refusal_on_a_waiting_retry(tmp_path: Path) -> None:
    database = _database(tmp_path)
    job_id = JobQueue(database).enqueue(PlatformUrl(MIX), DEEP_RECIPE)
    with database.write() as connection:
        connection.execute("UPDATE jobs SET state='waiting', attempt=1 WHERE id=?", (job_id,))
    _refused_hosted(database, tmp_path, job_id)


def test_hosted_refusal_on_a_legacy_or_attached_row(tmp_path: Path) -> None:
    database = _database(tmp_path)
    job_id = JobQueue(database).enqueue(PlatformUrl(MIX), DEEP_RECIPE)
    with database.write() as connection:
        # A pre-provenance row with no run id, corrupted from attached into a claimable one.
        connection.execute(
            "UPDATE jobs SET run_id=NULL, attached=0, "
            "progress=json_object('detached', 0) WHERE id=?",
            (job_id,),
        )
    _refused_hosted(database, tmp_path, job_id)


def test_hosted_refusal_on_a_zero_cap_recipe_naming_a_paid_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = replace(FREE_RECIPE, name="zero-cap-paid", secondary_engine="acrcloud")
    assert recipe.max_usd_e2 == 0
    recipes = {**worker_module.RECIPES, "zero-cap-paid": recipe}
    monkeypatch.setattr(worker_module, "RECIPES", recipes)
    database = _database(tmp_path)
    job_id = JobQueue(database).enqueue(PlatformUrl(MIX), recipe)
    _refused_hosted(database, tmp_path, job_id)


# ------------------------------------------- item 6 (P2): the fences bind the run


def test_paid_dispatch_requires_the_jobs_own_run_and_its_reservation(tmp_path: Path) -> None:
    jobs, claimed = _claimed_local_job(tmp_path)

    def journal_for(run_id: str) -> AttemptJournal:
        journal = AttemptJournal(
            tmp_path / "media" / f"{run_id}.jsonl", run_id=run_id, provider="audd", unit_usd_e6=UNIT
        )
        journal.admission = DispatchAdmission(
            jobs.database, job_id=claimed.id, claim_token=claimed.token
        )
        return journal

    foreign_run = new_run_id()
    with jobs.database.write() as connection:
        # The foreign run even holds a reservation: only the run binding can refuse it.
        connection.execute(
            "INSERT INTO run_reservations(run_id, job_id, reservation, usd_e6_reserved, "
            "created_at) VALUES (?, ?, ?, ?, 0)",
            (
                foreign_run,
                "other-job",
                json.dumps(
                    {
                        "run_id": foreign_run,
                        "planned": 7,
                        "unit_usd_e6": UNIT,
                        "usd_e6_reserved": 7 * UNIT,
                        "usd_e2_reserved": 4,
                        "effective_cap_e2": 900,
                    }
                ),
                7 * UNIT,
            ),
        )
    foreign = journal_for(foreign_run)
    attempt = foreign.prepare(
        query_id="a" * 64, window_id="b" * 40, ordinal=0, parent_attempt_id=None
    )
    with pytest.raises(DispatchRefused):
        foreign.dispatched(attempt)
    own = journal_for(claimed.run_id)
    attempt = own.prepare(query_id="a" * 64, window_id="b" * 40, ordinal=0, parent_attempt_id=None)
    with pytest.raises(DispatchRefused, match="reservation"):
        own.dispatched(attempt)
    assert _count(jobs.database, "run_dispatches") == 0
    own.record_reservation(
        reserve_usd(planned=7, unit_usd_e6=UNIT, recipe_max_usd_e2=900, configured_max_usd_e2=None)
    )
    own.dispatched(attempt)
    assert _count(jobs.database, "run_dispatches") == 1


def test_a_fenced_settlement_refuses_an_entry_for_another_run(tmp_path: Path) -> None:
    from id_detector.service import interrupted_entry

    jobs, claimed = _claimed_local_job(tmp_path)
    claim = DispatchAdmission(
        jobs.database, job_id=claimed.id, claim_token=claimed.token, require_not_cancelled=False
    )
    ledger = SettlementLedger(jobs.database, fence=claim.check, job_id=claimed.id)
    entry = interrupted_entry(new_run_id(), str(AUDIO), "cancelled", RecoveredMoney(), [])
    with pytest.raises(LedgerConflict):
        ledger.settle(None, entry)
    assert _count(jobs.database, "run_settlements") == 0
