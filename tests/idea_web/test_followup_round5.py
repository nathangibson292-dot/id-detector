"""Round 5: SQLite is the single authority for paid dispatch and settlement (round-4 review).

Every test here was run against the code before this pass and failed there; see
``docs/reviews/followup-money-resume.md``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import pytest

from id_detector import attempts as attempts_module
from id_detector import ingest as ingest_module
from id_detector.attempts import AttemptJournal, DispatchRefused
from id_detector.compat import LOCAL_OWNER_SCOPE
from id_detector.io import native_path
from id_detector.recipes import DEEP_RECIPE
from id_detector.webapp.jobs import Job, JobContext, JobManager
from idea_web.database import Database
from idea_web.jobs.local import LocalJobs, LocalWorker, local_database, snapshot
from idea_web.jobs.worker import DispatchAdmission, JobQueue
from tests.idea_web.local_runner_fakes import fake_pipeline_runner
from tests.idea_web.test_followup_review_fixes import UNIT, _config, _invocations
from tests.idea_web.test_followup_round2 import (
    _child_worker_dies,
    _dispatched_queries,
    _env,
    _nothing_runs,
)
from tests.idea_web.test_worker import AUDIO, MIX
from tests.test_followup_money_resume import _crash_mid_primary

# ------------------------------------------------ one transaction: fence check + dispatch row


def _claimed_local_job(root: Path):
    jobs = LocalJobs(root)
    job_id = jobs.submit(MIX)
    claimed = jobs.queue.claim("worker-a", lease_seconds=600)
    assert claimed is not None and claimed.id == job_id and claimed.run_id
    return jobs, claimed


def _journal(root: Path, jobs: LocalJobs, claimed) -> AttemptJournal:
    journal = AttemptJournal(
        root / "attempts.jsonl", run_id=claimed.run_id, provider="audd", unit_usd_e6=UNIT
    )
    journal.admission = DispatchAdmission(
        jobs.database, job_id=claimed.id, claim_token=claimed.token
    )
    # Round 7: a paid dispatch requires the run's durable reservation in its admission
    # transaction, exactly as the pipeline records it before its first dispatch.
    from id_detector.money import reserve_usd

    journal.record_reservation(
        reserve_usd(planned=7, unit_usd_e6=UNIT, recipe_max_usd_e2=900, configured_max_usd_e2=None)
    )
    return journal


def _race(jobs: LocalJobs, claimed, how: str) -> None:
    """Commit a cancellation or a lease reclaim from a SECOND database handle."""

    other = Database(jobs.database.path)
    if how == "cancel":
        assert JobQueue(other, local_mode=True).request_cancel(claimed.id)
        return
    with other.write() as connection:
        connection.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (claimed.id,))
    assert JobQueue(other, local_mode=True).claim("worker-b", lease_seconds=600) is not None


def _dispatched_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(
        json.loads(line)["event"] == "dispatched"
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


@pytest.mark.parametrize("how", ["cancel", "reclaim"])
def test_admission_is_one_transaction_so_a_racing_cancel_or_reclaim_never_lets_a_request_leave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    query = "c" * 64
    window = "d" * 40

    # (a) Committed from another connection BEFORE admission: refused, nothing durable, no request.
    root = tmp_path / "before"
    jobs, claimed = _claimed_local_job(root)
    journal = _journal(root, jobs, claimed)
    attempt = journal.prepare(query_id=query, window_id=window, ordinal=0, parent_attempt_id=None)
    _race(jobs, claimed, how)
    sent = False
    with pytest.raises(DispatchRefused):
        journal.dispatched(attempt)
        sent = True
    assert not sent and _dispatched_lines(journal.path) == 0

    # (b) Committed the instant admission returns, before the JSONL line: the dispatch that
    # authorised the request must ALREADY be durable, or recovery would believe nothing was sent.
    root = tmp_path / "racing"
    jobs, claimed = _claimed_local_job(root)
    journal = _journal(root, jobs, claimed)
    attempt = journal.prepare(query_id=query, window_id=window, ordinal=0, parent_attempt_id=None)
    real_append = attempts_module.append_line
    observed: dict[str, bool] = {}

    def racing_append(path: Path, record) -> None:
        if record.event == "dispatched" and "durable" not in observed:
            _race(jobs, claimed, how)
            reader = AttemptJournal(path, run_id=claimed.run_id, provider="audd", unit_usd_e6=UNIT)
            reader.admission = DispatchAdmission(
                Database(jobs.database.path), job_id=claimed.id, claim_token=claimed.token
            )
            observed["durable"] = any(item.dispatched for item in reader.run_ledger().attempts)
        real_append(path, record)

    monkeypatch.setattr(attempts_module, "append_line", racing_append)
    journal.dispatched(attempt)
    monkeypatch.undo()
    assert observed == {"durable": True}, "a request was authorised before its dispatch was durable"

    # (c) Attempted WHILE admission holds the transaction: the racer cannot commit until the
    # dispatch row has; once it has, every later dispatch is refused -- no further request.
    root = tmp_path / "holding"
    jobs, claimed = _claimed_local_job(root)
    journal = _journal(root, jobs, claimed)
    attempt = journal.prepare(query_id=query, window_id=window, ordinal=0, parent_attempt_id=None)
    outcome: dict[str, str] = {}

    def contender() -> None:
        try:
            JobQueue(
                Database(jobs.database.path, busy_timeout_ms=300), local_mode=True
            ).request_cancel(claimed.id)
            outcome["cancel"] = "committed"
        except sqlite3.OperationalError as exc:
            outcome["cancel"] = f"blocked: {exc}"

    def inside_the_admission_transaction() -> None:
        racer = threading.Thread(target=contender)
        racer.start()
        racer.join(10)

    journal.admission.before_commit = inside_the_admission_transaction
    journal.dispatched(attempt)
    assert outcome.get("cancel", "").startswith("blocked"), outcome
    assert JobQueue(Database(jobs.database.path), local_mode=True).request_cancel(claimed.id)
    later = journal.prepare(query_id=query, window_id=window, ordinal=1, parent_attempt_id=None)
    with pytest.raises(DispatchRefused):
        journal.dispatched(later)
    assert _dispatched_lines(journal.path) == 1


# ------------------------------------------------------------ settlement rows: exactly once


def _dead_lettered_paid_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(str(AUDIO), "max_accuracy")
    _child_worker_dies(tmp_path, config, kill_on=3)
    return jobs, job_id


def _settlement_rows(database: Database, run_id: str) -> int:
    with database.read() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM run_settlements WHERE run_id=?", (run_id,)
        ).fetchone()[0]


def test_two_concurrent_settlement_writers_produce_exactly_one_row_and_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, job_id = _dead_lettered_paid_job(tmp_path, monkeypatch)
    with jobs.database.write() as connection:
        connection.execute(
            "UPDATE jobs SET state='dead_letter', lease_until=NULL, claim_token=NULL WHERE id=?",
            (job_id,),
        )
    run_id, dispatched = _dispatched_queries(tmp_path)
    worker = LocalWorker(local_database(tmp_path), tmp_path, _nothing_runs, flush_seconds=0.05)
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def run(call) -> None:
        try:
            start.wait(10)
            call()
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    writers = [
        threading.Thread(target=run, args=(worker.sweep_settlements,)),
        threading.Thread(
            target=run, args=(lambda: worker._settle_abandoned(job_id, run_id, str(AUDIO)),)
        ),
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(120)
    assert not errors, errors
    assert _settlement_rows(jobs.database, run_id) == 1
    (entry,) = _invocations(tmp_path, run_id)
    assert entry["status"] == "failed"
    assert entry["usd_e6_spent"] == len(dispatched) * UNIT


def _start(root: Path) -> LocalWorker:
    worker = LocalWorker(local_database(root), root, _nothing_runs, flush_seconds=0.05)
    worker.stopped.set()  # startup work only
    worker.run_forever(poll_seconds=0.01)
    return worker


def test_a_sweep_restores_a_complete_runs_missing_settlement_line_from_its_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(str(AUDIO), "max_accuracy")
    runner = fake_pipeline_runner(tmp_path, config)
    LocalWorker(local_database(tmp_path), tmp_path, runner, flush_seconds=0.05).run_once()
    assert jobs.get(job_id).status == "succeeded", jobs.get(job_id).error
    run_id = jobs.queue.get(job_id).run_id
    (entry,) = _invocations(tmp_path, run_id)
    assert entry["usd_e6_spent"] > 0
    # The projection is lost (a crash after the settlement row committed): only the row remains.
    for root, _dirs, files in os.walk(native_path(tmp_path)):
        if "invocations.jsonl" in files:
            path = os.path.join(root, "invocations.jsonl")
            with open(path, encoding="utf-8") as handle:
                kept = [line for line in handle if run_id not in line]
            with open(path, "w", encoding="utf-8") as handle:
                handle.writelines(kept)
    assert _invocations(tmp_path, run_id) == []

    _start(tmp_path)
    assert _invocations(tmp_path, run_id) == [entry]


def test_a_recovery_miss_is_retried_by_the_next_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, job_id = _dead_lettered_paid_job(tmp_path, monkeypatch)
    with jobs.database.write() as connection:
        connection.execute(
            "UPDATE jobs SET state='cancelled', lease_until=NULL, claim_token=NULL WHERE id=?",
            (job_id,),
        )
    run_id, dispatched = _dispatched_queries(tmp_path)
    worker = LocalWorker(local_database(tmp_path), tmp_path, _nothing_runs, flush_seconds=0.05)
    real = ingest_module._load_cached
    monkeypatch.setattr(ingest_module, "_load_cached", lambda *args, **kwargs: None)
    worker.sweep_settlements()  # the media index cannot be read this time: a miss, not "nothing"
    assert _invocations(tmp_path, run_id) == []
    monkeypatch.setattr(ingest_module, "_load_cached", real)
    worker.sweep_settlements()
    (entry,) = _invocations(tmp_path, run_id)
    assert entry["status"] == "cancelled"
    assert entry["usd_e6_spent"] == len(dispatched) * UNIT


# ------------------------------------------------------ item 1: pre-upgrade terminal recovery


def test_pre_upgrade_terminal_jobs_without_a_run_id_are_settled_once_from_their_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(tmp_path, monkeypatch)
    started = time.time() - 1
    run_id = "pre-upgrade-run"
    dispatched = _crash_mid_primary(tmp_path, run_id)  # a real paid run killed mid-primary
    assert dispatched
    finished = time.time() + 1

    database = Database(tmp_path / ".idea" / "app.db")
    assert database.migrate(1) == 1
    job_id = uuid.uuid4().hex
    legacy = snapshot(
        Job(
            id=job_id,
            target=str(AUDIO),
            display=str(AUDIO),
            profile="max_accuracy",
            acquire=False,
            build_index=False,
            created_at=started,
        )
    )
    legacy.pop("run_id", None)
    legacy.update({"status": "cancelled", "started_at": started, "finished_at": finished})
    with database.write() as connection:
        connection.execute(
            "INSERT INTO jobs(id, run_id, target, recipe_id, state, cancel_requested, "
            "tenant_scope, progress, created_at, updated_at) "
            "VALUES (?, NULL, ?, ?, 'cancelled', 1, ?, ?, ?, ?)",
            (
                job_id,
                json.dumps({"kind": "local", "path": str(AUDIO)}),
                DEEP_RECIPE.recipe_id,
                LOCAL_OWNER_SCOPE,
                json.dumps({"local": legacy}),
                started,
                finished,
            ),
        )
    assert _invocations(tmp_path, run_id) == []

    _start(tmp_path)  # the upgraded worker's startup
    (entry,) = _invocations(tmp_path, run_id)
    assert entry["status"] == "cancelled"
    assert entry["usd_e6_spent"] == len(dispatched) * UNIT
    assert _settlement_rows(local_database(tmp_path), run_id) == 1
    _start(tmp_path)  # exactly once
    assert _invocations(tmp_path, run_id) == [entry]
    assert _settlement_rows(local_database(tmp_path), run_id) == 1


# ------------------------------------------------------------ item 4: job paths fail closed


def test_a_job_driven_run_without_a_durable_run_id_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from id_detector import pipeline

    config = _env(tmp_path, monkeypatch)
    runner = fake_pipeline_runner(tmp_path, config)
    job = Job(
        id=uuid.uuid4().hex,
        target=str(AUDIO),
        display=str(AUDIO),
        profile="max_accuracy",
        acquire=False,
        build_index=False,
    )
    assert job.run_id is None
    with pytest.raises(RuntimeError, match="run id"):
        runner(JobContext(JobManager(tmp_path, runner), job))
    assert not (tmp_path / ".checkpoints").exists(), "an analysis started under a fallback run id"

    with pytest.raises(ValueError, match="run id"):
        asyncio.run(
            pipeline.run_analysis(
                str(AUDIO),
                work_root=tmp_path / "pipeline",
                print_raw=False,
                refresh=False,
                max_requests=10,
                tracklist=None,
                no_hints=True,
                supplied_run_id=None,
                settlement_writer=lambda path, entry: None,
            )
        )
    assert not (tmp_path / "pipeline").exists()


def test_the_config_helper_is_available() -> None:
    assert callable(_config)
