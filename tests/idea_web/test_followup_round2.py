"""Round-2 second-model review of the money/resume follow-up.

Each test was run against the code before this pass and failed there, or (for the two coverage
findings) was shown to fail when the fix it pins is reverted; see
``docs/reviews/followup-money-resume.md``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from id_detector.attempts import AttemptJournal
from id_detector.io import native_path
from id_detector.providers.base import AppConfig
from id_detector.recipes import FREE_RECIPE
from id_detector.run_ledger import LedgerConflict
from id_detector.service import LocalPath, PipelineOptions
from idea_web.database import Database, Migration
from idea_web.jobs.local import LocalJobs, LocalWorker, LocalWorkerSupervisor, local_database
from idea_web.jobs.worker import JobQueue, SQLiteAttemptJournal, Worker
from tests.fakes.providers import FakeShazamHTTP, no_backoff
from tests.idea_web.test_followup_queue_money import _claimed_run
from tests.idea_web.test_followup_review_fixes import (
    FAKES,
    UNIT,
    _attempt_events,
    _config,
    _invocations,
)
from tests.idea_web.test_local_queue import _alive, _until
from tests.idea_web.test_worker import AUDIO, MIX, ROOT, SCRIPT, _database
from tests.test_followup_money_resume import _KillShazamAfterSend


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    monkeypatch.setenv("IDEA_FOLLOWUP_SCRIPT", str(SCRIPT))
    monkeypatch.delenv("IDEA_ENGINE_SHAZAM", raising=False)
    monkeypatch.delenv("IDEA_FOLLOWUP_KILL_ONCE", raising=False)
    return _config(tmp_path)


def _child_worker_dies(tmp_path: Path, config: Path, *, kill_on: int) -> None:
    """Run the REAL local worker process until it dies right after a paid dispatch."""

    finished = subprocess.run(
        [
            sys.executable,
            "-c",
            "from idea_web.jobs.local import main; raise SystemExit(main())",
            "--work-root",
            str(tmp_path),
            "--config",
            str(config),
            "--runner",
            f"{FAKES}:deep_runner",
        ],
        cwd=ROOT,
        env=dict(os.environ, IDEA_FOLLOWUP_KILL_ON=str(kill_on)),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert finished.returncode == 9, finished.stderr[-3000:]


def _expire(jobs: LocalJobs, job_id: str) -> None:
    with jobs.database.write() as connection:
        connection.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job_id,))


def _nothing_runs(_ctx) -> None:
    raise AssertionError("a job that must not run was started")


def _dispatched_queries(tmp_path: Path) -> tuple[str, set[str]]:
    events = [event for event in _attempt_events(tmp_path) if event["provider"] == "audd"]
    (run_id,) = {event["run_id"] for event in events}
    return run_id, {event["query_id"] for event in events if event["event"] == "dispatched"}


# ---------------------------------------------------------------------------------------- P0-2


@pytest.mark.parametrize(
    "override",
    [
        {"paid_scan_adapters": {"audd": object()}},
        {"shazam_http_client": object()},
        {"paid_sleep": no_backoff},
        {"configure": lambda config: config},
    ],
    ids=["paid_scan_adapters", "shazam_http_client", "paid_sleep", "configure"],
)
def test_the_production_runner_refuses_offline_overrides_outside_test_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: dict
) -> None:
    from id_detector.webapp.runner import make_pipeline_runner

    monkeypatch.delenv("IDEA_TEST_MODE", raising=False)
    config = _config(tmp_path)
    with pytest.raises(PermissionError):
        make_pipeline_runner(tmp_path, project_root=ROOT, config_path=config, **override)
    # The production construction (no override) is unaffected.
    assert callable(make_pipeline_runner(tmp_path, project_root=ROOT, config_path=config))


# ---------------------------------------------------------------------------------------- P0-1


def test_a_submitted_local_job_carries_its_run_id_in_the_queue_row(tmp_path: Path) -> None:
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(MIX)
    row = jobs.queue.get(job_id)
    assert row.run_id is not None and row.run_id == jobs.get(job_id).run_id
    # A queued job with a durable run id is still cancelled at once (it has no run row yet).
    assert jobs.cancel(job_id) is True
    assert jobs.queue.get(job_id).state == "cancelled"


def test_three_worker_deaths_dead_letter_the_job_and_settle_its_durable_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(str(AUDIO), "max_accuracy")
    for _ in range(3):
        _child_worker_dies(tmp_path, config, kill_on=1)
        _expire(jobs, job_id)
    # The fourth claim finds the attempts exhausted and quarantines the row before any runner.
    LocalWorker(local_database(tmp_path), tmp_path, _nothing_runs, flush_seconds=0.05).run_once()
    assert jobs.queue.get(job_id).state == "dead_letter"
    run_id, dispatched = _dispatched_queries(tmp_path)
    assert dispatched
    (entry,) = _invocations(tmp_path, run_id)
    assert entry["status"] == "failed"
    assert entry["usd_e6_spent"] == len(dispatched) * UNIT


def test_cancelling_after_a_death_before_the_first_progress_flush_settles_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(str(AUDIO), "max_accuracy")
    submitted = jobs.queue.get(job_id).progress
    _child_worker_dies(tmp_path, config, kill_on=3)
    # The worker died before its 0.5 s publisher ever flushed: the row still holds the snapshot
    # written at submission (no `started_at`), while the attempt ledger holds the paid dispatches.
    with jobs.database.write() as connection:
        connection.execute(
            "UPDATE jobs SET progress=?, lease_until=0 WHERE id=?", (json.dumps(submitted), job_id)
        )
    assert jobs.get(job_id).started_at is None
    assert jobs.cancel(job_id) is True
    LocalWorker(local_database(tmp_path), tmp_path, _nothing_runs, flush_seconds=0.05).run_once()
    assert jobs.get(job_id).status == "cancelled"
    run_id, dispatched = _dispatched_queries(tmp_path)
    (entry,) = _invocations(tmp_path, run_id)
    assert entry["status"] == "cancelled"
    assert entry["usd_e6_spent"] == len(dispatched) * UNIT


# ---------------------------------------------------------------------------------------- P1-6


def test_a_paid_job_survives_a_worker_kill_through_the_real_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _env(tmp_path, monkeypatch)
    marker = tmp_path / "killed-once.marker"
    monkeypatch.setenv("IDEA_FOLLOWUP_KILL_ON", "3")
    monkeypatch.setenv("IDEA_FOLLOWUP_KILL_ONCE", str(marker))
    jobs = LocalJobs(tmp_path)
    supervisor = LocalWorkerSupervisor(
        tmp_path,
        config_path=config,
        jobs=jobs,
        runner_spec=f"{FAKES}:deep_runner",
        restart_delay_seconds=0.2,
    )
    pids: list[int] = []
    assert supervisor.start() is True
    try:
        first = supervisor.pid
        assert first is not None
        pids.append(first)
        job_id = jobs.submit(str(AUDIO), "max_accuracy")
        assert _until(marker.exists, timeout=240), "the worker never reached its paid sweep"
        assert _until(lambda: supervisor.pid not in (None, first), timeout=60)
        pids.append(supervisor.pid)
        assert supervisor.restarts >= 1
        # The replacement reclaims once the dead worker's 30 s lease lapses, then finishes.
        assert _until(lambda: jobs.get(job_id).status == "succeeded", timeout=300), jobs.get(job_id)
    finally:
        supervisor.stop()
    assert _until(lambda: not any(_alive(pid) for pid in pids), timeout=30)

    events = [event for event in _attempt_events(tmp_path) if event["provider"] == "audd"]
    (run_id,) = {event["run_id"] for event in events}
    dispatches = [event["query_id"] for event in events if event["event"] == "dispatched"]
    assert len(dispatches) == len(set(dispatches)), "a clip was dispatched twice"
    clips = {event["query_id"] for event in events}
    (entry,) = _invocations(tmp_path, run_id)
    assert entry["usd_e6_spent"] == len(clips) * UNIT
    assert entry["usd_e6_reserved"] == (len(clips) * UNIT * 105 + 99) // 100


# ---------------------------------------------------------------------------------------- P1-3


def test_a_hosted_worker_recovers_shazam_ambiguity_from_sqlite_after_losing_the_job_store(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    work = tmp_path / "work"
    queue = JobQueue(database, local_mode=True)
    job_id = queue.enqueue(LocalPath(AUDIO), FREE_RECIPE, run_id="shazam-hosted-ambiguous")

    def options(shazam) -> PipelineOptions:
        return PipelineOptions(
            project_root=ROOT,
            no_hints=True,
            max_requests=100,
            max_generations=0,
            shazam_http_client=shazam,
            app_config=AppConfig(),
        )

    killer = _KillShazamAfterSend(SCRIPT, kill_on=3)
    Worker(database, work, local_mode=True, options=options(killer)).run_once()
    assert killer.killed_window is not None
    assert queue.get(job_id).state == "analysis"
    with database.read() as connection:
        states = {
            row["state"]
            for row in connection.execute(
                "SELECT state FROM provider_attempt_events WHERE provider='shazam'"
            )
        }
    assert "dispatched" in states
    # Lose every per-media record of the in-flight query: the job store AND any local journal.
    for root, _dirs, files in os.walk(native_path(work)):
        for name in files:
            if name.startswith("jobs.sqlite") or name == "shazam-attempts.jsonl":
                os.unlink(os.path.join(root, name))

    resumed = FakeShazamHTTP(SCRIPT)
    Worker(database, work, local_mode=True, options=options(resumed)).run_once()
    windows = [attempt["window"] for attempt in resumed.attempts]
    assert killer.killed_window not in windows, windows


# ---------------------------------------------------------------------------------------- P1-4


_TARGET = json.dumps({"kind": "platform", "url": MIX})


def _populate_0001(database: Database) -> None:
    rows = [
        ("in-flight", "analysis", {"phase": "recognise"}, 0, "run-a", 9e12, "token-a", None),
        (
            "breaker-waiting",
            "waiting",
            {"attached": 0, "reason": "shazam"},
            0,
            "run-b",
            None,
            None,
            None,
        ),
        (
            "attached",
            "waiting",
            {"phase": "intake", "attached": True},
            0,
            "run-a",
            None,
            None,
            None,
        ),
        ("attached-cancelled", "waiting", {"attached": True}, 1, "run-a", None, None, None),
        ("terminal", "complete", {"attached": True}, 0, "run-c", None, None, None),
        ("dead", "dead_letter", {"phase": "decode"}, 0, "run-d", None, None, "boom"),
        ("malformed", "intake", "{", 0, None, None, None, None),
    ]
    with database.write() as connection:
        for identifier, state, progress, cancel, run_id, lease, token, reason in rows:
            connection.execute(
                "INSERT INTO jobs(id, run_id, target, recipe_id, state, lease_owner, lease_until, "
                "claim_token, cancel_requested, dead_letter_reason, progress, tenant_scope, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'public', 1, 1)",
                (
                    identifier,
                    run_id,
                    _TARGET,
                    FREE_RECIPE.recipe_id,
                    state,
                    "worker" if token else None,
                    lease,
                    token,
                    cancel,
                    reason,
                    progress if isinstance(progress, str) else json.dumps(progress),
                ),
            )


def _rows(database: Database) -> dict[str, dict]:
    with database.read() as connection:
        return {row["id"]: dict(row) for row in connection.execute("SELECT * FROM jobs")}


def test_migration_0002_on_a_populated_0001_database_backfills_detaches_and_reverses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from idea_web import database as database_module

    database = Database(tmp_path / "app.db")
    assert database.migrate(1) == 1
    _populate_0001(database)
    before = _rows(database)

    # Crash safety: a 0002 that dies part-way leaves the 0001 database exactly as it was.
    real = database_module.migrations()
    broken = Migration(
        real[1].number, real[1].name, real[1].up + "\nSELECT no_such_function();\n", real[1].down
    )
    monkeypatch.setattr(database_module, "migrations", lambda: (real[0], broken))
    with pytest.raises(sqlite3.OperationalError):
        database.migrate()
    monkeypatch.undo()
    assert database.version() == 1
    assert _rows(database) == before

    assert database.migrate() == 4
    after = _rows(database)
    assert {key: row["attached"] for key, row in after.items()} == {
        "in-flight": 0,
        "breaker-waiting": 0,
        "attached": 1,
        "attached-cancelled": 1,
        "terminal": 1,
        "dead": 0,
        "malformed": 0,
    }
    # A subscriber the owner had already cancelled is terminally detached, never stranded.
    assert after["attached-cancelled"]["state"] == "cancelled"
    assert after["attached-cancelled"]["claim_token"] is None
    for key in ("in-flight", "breaker-waiting", "attached", "terminal", "dead", "malformed"):
        assert after[key]["state"] == before[key]["state"], key
    assert after["in-flight"]["claim_token"] == "token-a"

    assert database.migrate(1) == 1
    reverted = _rows(database)
    assert all("attached" not in row for row in reverted.values())
    assert set(reverted) == set(before)
    assert reverted["attached-cancelled"]["state"] == "cancelled"  # a cancellation is not undone


# ---------------------------------------------------------------------------------------- P1-5


def test_one_event_object_feeds_both_the_jsonl_and_the_sqlite_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from id_detector import attempts as attempts_module

    database = _database(tmp_path)
    run_id = "one-payload"
    _queue, job = _claimed_run(database, tmp_path, run_id)
    ticks = iter(range(10_000))

    def tick() -> str:
        return f"2026-01-01T00:00:{next(ticks) % 60:02d}.{next(ticks) % 1000:03d}Z"

    # The event object (and its `at`) is built once; both stores receive that same object.
    monkeypatch.setattr(attempts_module, "timestamp", tick)
    path = tmp_path / "one.jsonl"
    journal = SQLiteAttemptJournal(
        path,
        database=database,
        run_id=run_id,
        provider="audd",
        unit_usd_e6=UNIT,
        egress_id="egress-one",
        claim_token=job.token,
        hosted=False,  # the queue's local mode: hosted paid dispatch is refused (round 6)
    )
    attempt = journal.prepare(
        query_id="c" * 64, window_id="d" * 40, ordinal=0, parent_attempt_id=None
    )
    journal.dispatched(attempt)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    with database.read() as connection:
        rows = connection.execute(
            "SELECT state, at FROM provider_attempt_events ORDER BY event_id"
        ).fetchall()
    assert [(line["event"], line["at"]) for line in lines] == [
        (row["state"], row["at"]) for row in rows
    ]


def test_a_second_real_writer_cannot_reuse_an_attempt_under_another_egress(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    run_id = "second-writer"
    _queue, job = _claimed_run(database, tmp_path, run_id)
    path = tmp_path / "shared.jsonl"

    def writer(egress: str) -> SQLiteAttemptJournal:
        return SQLiteAttemptJournal(
            path,
            database=database,
            run_id=run_id,
            provider="audd",
            unit_usd_e6=UNIT,
            egress_id=egress,
            claim_token=job.token,
            hosted=False,  # the queue's local mode: hosted paid dispatch is refused (round 6)
        )

    first = writer("egress-one")
    first.prepare(query_id="c" * 64, window_id="d" * 40, ordinal=0, parent_attempt_id=None)
    with pytest.raises(ValueError):
        writer("egress-two").prepare(
            query_id="c" * 64, window_id="d" * 40, ordinal=0, parent_attempt_id=None
        )
    # Nothing conflicting reached either store.
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    with database.read() as connection:
        rows = connection.execute("SELECT egress_id FROM provider_attempt_events").fetchall()
    assert [row["egress_id"] for row in rows] == ["egress-one"]


def test_a_journal_line_whose_ordinal_disagrees_with_its_attempt_id_is_refused(
    tmp_path: Path,
) -> None:
    path = tmp_path / "attempts.jsonl"
    writer = AttemptJournal(path, run_id="r", provider="audd", unit_usd_e6=UNIT)
    writer.prepare(query_id="c" * 64, window_id="d" * 40, ordinal=0, parent_attempt_id=None)
    line = json.loads(path.read_text(encoding="utf-8"))
    line["ordinal"] = 1
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    with pytest.raises(LedgerConflict):
        AttemptJournal(path, run_id="r", provider="audd", unit_usd_e6=UNIT).run_ledger()
