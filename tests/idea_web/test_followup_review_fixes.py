"""Second-model review of the money/resume follow-up: supervised local worker and queue ownership.

Each test was run against the pre-review-fix implementation first and failed there (see
``docs/reviews/followup-money-resume.md``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from id_detector.io import native_path
from id_detector.money import ceil_e2
from id_detector.providers.base import AppConfig
from id_detector.recipes import FREE_RECIPE
from id_detector.service import LocalPath, PipelineOptions, PlatformUrl, RunResult
from idea_web.jobs.local import LocalJobs, LocalWorker, local_database
from idea_web.jobs.worker import JobQueue, Worker
from tests.fakes.providers import FakeAudD, FakeShazamHTTP
from tests.idea_web.local_runner_fakes import fake_pipeline_runner
from tests.idea_web.test_worker import AUDIO, MIX, ROOT, SCRIPT, _database, _intake, _run_row

FAKES = "tests.idea_web.local_runner_fakes"
UNIT = AppConfig().audd_usd_e6_per_request


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "idea.toml"
    path.write_text("[hints]\nenabled = false\n", encoding="utf-8")
    return path


def _attempt_events(work: Path) -> list[dict]:
    events: list[dict] = []
    for root, _dirs, files in os.walk(native_path(work)):
        if "attempts.jsonl" in files:
            with open(os.path.join(root, "attempts.jsonl"), encoding="utf-8") as handle:
                events.extend(json.loads(line) for line in handle if line.strip())
    return events


def _invocations(work: Path, run_id: str) -> list[dict]:
    found: list[dict] = []
    for root, _dirs, files in os.walk(native_path(work)):
        if "invocations.jsonl" in files:
            with open(os.path.join(root, "invocations.jsonl"), encoding="utf-8") as handle:
                for line in handle:
                    if line.strip() and json.loads(line)["invocation_id"] == run_id:
                        found.append(json.loads(line))
    return found


def _kill_local_worker_mid_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Submit a Deep job and run the REAL worker process until it dies after a dispatch."""

    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    monkeypatch.setenv("IDEA_FOLLOWUP_SCRIPT", str(SCRIPT))
    monkeypatch.delenv("IDEA_ENGINE_SHAZAM", raising=False)
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(str(AUDIO), "max_accuracy")
    config = _config(tmp_path)
    env = dict(os.environ, IDEA_FOLLOWUP_KILL_ON="3")
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
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert finished.returncode == 9, finished.stderr[-3000:]
    events = _attempt_events(tmp_path)
    dispatched = {e["attempt_id"] for e in events if e["event"] == "dispatched"}
    resolved = {e["attempt_id"] for e in events if e["event"] == "resolved"}
    assert dispatched - resolved, "the worker died with an ambiguous attempt on disk"
    # The dead worker's lease lapses (as it would after 30 s) so a restarted worker can reclaim.
    with jobs.database.write() as connection:
        connection.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job_id,))
    return jobs, job_id, config, events


def _first_pass(events: list[dict]) -> tuple[str, set[str], set[str]]:
    (run_id,) = {e["run_id"] for e in events}
    attempts = {e["attempt_id"] for e in events}
    queries = {e["query_id"] for e in events if e["event"] == "dispatched"}
    return run_id, attempts, queries


def _resume_in_process(tmp_path: Path, config: Path, *, unit_usd_e6=None) -> FakeAudD:
    audd = FakeAudD(SCRIPT)
    runner = fake_pipeline_runner(tmp_path, config, audd=audd, unit_usd_e6=unit_usd_e6)
    LocalWorker(local_database(tmp_path), tmp_path, runner, flush_seconds=0.05).run_once()
    return audd


def _assert_resumed_without_rebilling(tmp_path: Path, first: list[dict]) -> tuple[str, int]:
    run_id, first_attempts, first_queries = _first_pass(first)
    events = _attempt_events(tmp_path)
    assert {e["run_id"] for e in events} == {run_id}, "the restart ran a different service run"
    resent = {
        e["query_id"]
        for e in events
        if e["event"] == "dispatched" and e["attempt_id"] not in first_attempts
    }
    assert not resent & first_queries, "a clip the dead worker already dispatched was sent again"
    clips = {e["query_id"] for e in events}
    assert resent | first_queries == clips
    return run_id, len(clips)


# ---------------------------------------------------------------------------------------- P0-1


def test_a_supervised_local_job_killed_mid_sweep_resumes_its_own_run_without_rebilling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, job_id, config, first = _kill_local_worker_mid_sweep(tmp_path, monkeypatch)
    _resume_in_process(tmp_path, config)
    assert jobs.get(job_id).status == "succeeded", jobs.get(job_id).error
    run_id, clips = _assert_resumed_without_rebilling(tmp_path, first)
    (entry,) = _invocations(tmp_path, run_id)
    assert entry["usd_e6_spent"] == clips * UNIT
    assert entry["usd_e6_reserved"] == (clips * UNIT * 105 + 99) // 100


def test_a_price_change_between_local_crash_and_restart_keeps_the_original_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, job_id, config, first = _kill_local_worker_mid_sweep(tmp_path, monkeypatch)
    _resume_in_process(tmp_path, config, unit_usd_e6=4_000)
    assert jobs.get(job_id).status == "succeeded", jobs.get(job_id).error
    run_id, clips = _assert_resumed_without_rebilling(tmp_path, first)
    (entry,) = _invocations(tmp_path, run_id)
    assert entry["usd_e6_spent"] == clips * UNIT
    assert entry["usd_e6_reserved"] == (clips * UNIT * 105 + 99) // 100


def test_cancelling_a_killed_local_job_before_restart_settles_its_durable_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, job_id, config, first = _kill_local_worker_mid_sweep(tmp_path, monkeypatch)
    run_id, _attempts, first_queries = _first_pass(first)
    assert jobs.cancel(job_id) is True

    def must_not_run(_ctx) -> None:
        raise AssertionError("a cancelled job was restarted")

    LocalWorker(local_database(tmp_path), tmp_path, must_not_run, flush_seconds=0.05).run_once()
    job = jobs.get(job_id)
    assert job.status == "cancelled"
    spent = len(first_queries) * UNIT
    (entry,) = _invocations(tmp_path, run_id)
    assert entry["status"] == "cancelled"
    assert entry["usd_e6_spent"] == spent
    assert job.spend_known is True and job.usd_e2_spent == ceil_e2(spent)


# ---------------------------------------------------------------------------------------- P0-2


def _driver_and_attached(tmp_path: Path):
    database = _database(tmp_path)
    queue = JobQueue(database)
    intake = _intake(tmp_path)
    worker = Worker(database, tmp_path / "work")
    driver_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    driver = queue.claim("driver", lease_seconds=600)
    assert driver is not None and driver.id == driver_id
    assert worker._commit_intake(driver, intake) == "analysis"
    attached_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    attached = queue.claim("intake", lease_seconds=600)
    assert attached is not None and attached.id == attached_id
    assert worker._commit_intake(attached, intake) == "waiting"
    run_id = queue.get(driver_id).run_id
    assert queue.get(attached_id).run_id == run_id
    return database, queue, driver, attached_id, run_id


def _nothing_may_run(request) -> RunResult:
    raise AssertionError("a job that is not the run's driver started its pipeline")


@pytest.mark.parametrize("progress", ["{", "{}"], ids=["malformed", "demarked"])
def test_an_attached_job_can_never_abandon_or_take_over_a_live_drivers_run(
    tmp_path: Path, progress: str
) -> None:
    database, queue, driver, attached_id, run_id = _driver_and_attached(tmp_path)
    token_before = _run_row(database, run_id)["claim_token"]
    assert token_before == driver.token
    with database.write() as connection:
        connection.execute("UPDATE jobs SET progress=? WHERE id=?", (progress, attached_id))
    Worker(database, tmp_path / "work", service_runner=_nothing_may_run).run_once()
    run = _run_row(database, run_id)
    assert run["status"] == "analysis"
    assert run["claim_token"] == driver.token
    driver_row = queue.get(driver.id)
    assert driver_row.state == "analysis" and driver_row.claim_token == driver.token


def test_a_second_driver_row_cannot_rotate_a_run_another_job_holds_a_live_lease_on(
    tmp_path: Path,
) -> None:
    database, queue, driver, _attached_id, run_id = _driver_and_attached(tmp_path)
    intruder = queue.enqueue(PlatformUrl("https://soundcloud.com/example/other"), FREE_RECIPE)
    with database.write() as connection:
        connection.execute(
            "UPDATE jobs SET run_id=?, state='analysis' WHERE id=?", (run_id, intruder)
        )
    Worker(database, tmp_path / "work", service_runner=_nothing_may_run).run_once()
    assert _run_row(database, run_id)["claim_token"] == driver.token


# ----------------------------------------------------------------------------------- P1-3 queue


def test_hosted_shazam_ledger_rows_carry_the_clip_query_id(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database, local_mode=True)
    job_id = queue.enqueue(LocalPath(AUDIO), FREE_RECIPE, run_id="shazam-query-ids")
    options = PipelineOptions(
        project_root=ROOT,
        no_hints=True,
        max_requests=100,
        max_generations=0,
        shazam_http_client=FakeShazamHTTP(SCRIPT),
        app_config=AppConfig(),
    )
    result = Worker(database, tmp_path / "work", local_mode=True, options=options).run_once()
    assert result is not None and result.id == job_id and result.state == "complete"
    with database.read() as connection:
        rows = connection.execute(
            "SELECT query_id FROM provider_attempt_events WHERE provider='shazam'"
        ).fetchall()
    assert rows and all(row["query_id"] for row in rows)


# ------------------------------------------------------------------------------------ P1-7 queue


def test_a_duplicate_projection_attributed_to_another_egress_is_refused(tmp_path: Path) -> None:
    from idea_web.jobs.worker import SQLiteAttemptJournal
    from tests.idea_web.test_followup_queue_money import _claimed_run

    database = _database(tmp_path)
    run_id = "duplicate-egress"
    _queue, job = _claimed_run(database, tmp_path, run_id)
    journal = SQLiteAttemptJournal(
        tmp_path / "a.jsonl",
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
    with database.write() as connection, pytest.raises(ValueError):
        journal.projection.insert(
            connection,
            attempt_id=attempt,
            provider="audd",
            egress_id="egress-two",
            query_id="c" * 64,
            parent_attempt_id=None,
            state="dispatched",
            outcome=None,
            unit_usd_e6=UNIT,
            at="2026-01-01T00:00:00.000Z",
        )
