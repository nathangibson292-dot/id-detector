"""Follow-up cycle: money and resume safety in the durable queue (retro-review 4b-i).

Every test here was written to FAIL at ``ec32f97`` before any production change.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from id_detector import contracts
from id_detector.attempts import AttemptJournal, attempt_id_for
from id_detector.io import native_path
from id_detector.providers.base import AppConfig
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE
from id_detector.service import LocalPath, PipelineOptions, PlatformUrl, RunResult, UploadId
from id_detector.service import run as service_run
from id_detector.shazam_breaker import BreakerConfig, ShazamBreaker
from idea_web.jobs.worker import (
    JobQueue,
    SQLiteAttemptJournal,
    SQLiteCheckpointStore,
    StaleClaim,
    Worker,
)
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff
from tests.idea_web.test_worker import (
    AUDIO,
    MIX,
    ROOT,
    SCRIPT,
    _complete,
    _database,
    _insert_run,
    _intake,
    _job_row,
    _resolver,
    _run_row,
)
from tests.test_followup_money_resume import _KillAfterDispatch

UNIT = AppConfig().audd_usd_e6_per_request
DEEP_CLIPS = 7
ORIGINAL_RESERVED_E6 = (DEEP_CLIPS * UNIT * 105 + 99) // 100


def _deep_options(audd, **kwargs) -> PipelineOptions:
    return PipelineOptions(
        project_root=ROOT,
        no_hints=True,
        max_requests=100,
        max_generations=0,
        enabled_engines=("audd",),
        paid_scan_adapters={"audd": audd},
        shazam_http_client=kwargs.pop("shazam", None) or FakeShazamHTTP(SCRIPT),
        paid_sleep=no_backoff,
        app_config=AppConfig(),
        **kwargs,
    )


def _audd_rows(database, run_id: str, state: str) -> int:
    with database.read() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM provider_attempt_events "
            "WHERE run_id=? AND provider='audd' AND state=?",
            (run_id, state),
        ).fetchone()[0]


def _claimed_run(database, tmp_path: Path, run_id: str, *, lease_seconds: float = 60.0):
    queue = JobQueue(database)
    job_id = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, run_id=run_id)
    _insert_run(database, _intake(tmp_path, DEEP_RECIPE), run_id=run_id, status="analysis")
    job = queue.claim("owner", lease_seconds=lease_seconds)
    assert job is not None and job.id == job_id
    return queue, job


def _crash_deep_job(tmp_path: Path, run_id: str):
    database = _database(tmp_path)
    work = tmp_path / "work"
    queue = JobQueue(database, local_mode=True)
    job_id = queue.enqueue(LocalPath(AUDIO), DEEP_RECIPE, run_id=run_id)
    killer = _KillAfterDispatch(FakeAudD(SCRIPT), kill_on=3)
    Worker(database, work, local_mode=True, options=_deep_options(killer)).run_once()
    assert queue.get(job_id).state == "analysis"  # returned to the queue, not settled
    dispatched = _audd_rows(database, run_id, "dispatched")
    assert dispatched > _audd_rows(database, run_id, "resolved")  # an ambiguous attempt exists
    return database, work, queue, job_id, dispatched


# ------------------------------------------------------------------------------ 4b-i A / P0-1


def test_a_lost_jsonl_directory_entry_recovers_paid_state_from_sqlite(tmp_path: Path) -> None:
    run_id = "jsonl-lost"
    database, work, _queue, _job_id, dispatched = _crash_deep_job(tmp_path, run_id)
    journal = work / ".attempts" / f"{run_id}.jsonl"
    assert journal.is_file()
    journal.unlink()  # the power cut lost the new file's directory entry; SQLite survived

    resumed = FakeAudD(SCRIPT)
    Worker(database, work, local_mode=True, options=_deep_options(resumed)).run_once()
    assert resumed.calls == DEEP_CLIPS - dispatched
    row = _run_row(database, run_id)
    assert row["usd_e6_spent"] == DEEP_CLIPS * UNIT


def test_a_new_attempt_journal_is_namespace_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from id_detector import journal as journal_module

    synced: list[Path] = []
    real = journal_module.fsync_directory
    monkeypatch.setattr(
        journal_module, "fsync_directory", lambda path: (synced.append(Path(path)), real(path))
    )
    path = tmp_path / "fresh" / "attempts" / "run.jsonl"
    writer = AttemptJournal(path, run_id="r", provider="audd", unit_usd_e6=UNIT)
    writer.prepare(query_id="a" * 64, window_id="b" * 40, ordinal=0, parent_attempt_id=None)
    assert path.parent.resolve() in {item.resolve() for item in synced}


# ------------------------------------------------------------------------------ 4b-i A / P0-2


def test_self_parenting_and_a_reused_attempt_id_are_refused(tmp_path: Path) -> None:
    writer = AttemptJournal(tmp_path / "a.jsonl", run_id="r", provider="audd", unit_usd_e6=UNIT)
    query = "a" * 64
    with pytest.raises(ValueError):
        writer.prepare(
            query_id=query,
            window_id="b" * 40,
            ordinal=0,
            parent_attempt_id=attempt_id_for("r", query, 0),
        )
    writer.prepare(query_id=query, window_id="b" * 40, ordinal=1, parent_attempt_id=None)
    with pytest.raises(ValueError):
        writer.prepare(query_id=query, window_id="b" * 40, ordinal=1, parent_attempt_id=None)


def test_a_conflicting_duplicate_event_is_verified_not_ignored(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run_id = "duplicate-payload"
    _queue, job = _claimed_run(database, tmp_path, run_id)
    journal = SQLiteAttemptJournal(
        tmp_path / "a.jsonl",
        database=database,
        run_id=run_id,
        provider="audd",
        unit_usd_e6=UNIT,
        egress_id="egress",
        claim_token=job.token,
        hosted=False,  # the queue's local mode: hosted paid dispatch is refused (round 6)
    )
    attempt = journal.prepare(
        query_id="c" * 64, window_id="d" * 40, ordinal=0, parent_attempt_id=None
    )
    journal.dispatched(attempt)
    journal.resolved(attempt, "match")
    with database.write() as connection, pytest.raises((ValueError, sqlite3.IntegrityError)):
        journal.projection.insert(
            connection,
            attempt_id=attempt,
            provider="audd",
            egress_id="egress",
            query_id="c" * 64,
            parent_attempt_id=None,
            state="resolved",
            outcome="no_match",
            unit_usd_e6=UNIT,
            at="2026-01-01T00:00:00.000Z",
        )


# ------------------------------------------------------------------------------ 4b-i A / P1-1


def test_malformed_waiting_progress_is_quarantined_and_the_consumer_carries_on(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    broken = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    good = queue.enqueue(PlatformUrl("https://soundcloud.com/example/two"), FREE_RECIPE)
    with database.write() as connection:
        connection.execute("UPDATE jobs SET state='waiting', progress='{' WHERE id=?", (broken,))
    worker = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(_intake(tmp_path)),
        service_runner=_complete,
    )
    result = worker.run_once()
    assert result is not None and result.id == good and result.state == "complete"
    assert _job_row(database, broken)["state"] == "dead_letter"


# ------------------------------------------------------------------------------ 4b-i A / P1-2


def test_cancelling_an_attached_job_detaches_it_and_reconciliation_keeps_it_cancelled(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    intake = _intake(tmp_path)
    _insert_run(database, intake, run_id="shared", status="analysis")
    driver = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, run_id="shared")
    attached = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, run_id="shared")
    with database.write() as connection:
        connection.execute("UPDATE jobs SET state='analysis' WHERE id=?", (driver,))
        connection.execute(
            "UPDATE jobs SET state='waiting', attached=1, progress=? WHERE id=?",
            (json.dumps({"attached": True}), attached),
        )
    assert queue.request_cancel(attached)
    with database.write() as connection:
        connection.execute("UPDATE analysis_runs SET status='complete' WHERE run_id='shared'")
        connection.execute("UPDATE jobs SET state='complete' WHERE id=?", (driver,))
    queue.reconcile_attached()
    assert _job_row(database, attached)["state"] == "cancelled"


# ------------------------------------------------------------------------------ 4b-i A / P1-3


def test_a_hosted_worker_never_writes_the_local_current_pointer_or_index(tmp_path: Path) -> None:
    database = _database(tmp_path)
    work = tmp_path / "work"
    uploads = work / ".uploads"
    uploads.mkdir(parents=True)
    shutil.copyfile(AUDIO, uploads / "upload123")
    queue = JobQueue(database)
    job_id = queue.enqueue(UploadId("upload123"), FREE_RECIPE)
    options = PipelineOptions(
        project_root=ROOT,
        no_hints=True,
        max_requests=100,
        max_generations=0,
        shazam_http_client=FakeShazamHTTP(SCRIPT),
        app_config=AppConfig(),
    )
    result = Worker(database, work, options=options).run_once()
    assert result is not None and result.id == job_id
    assert result.state in {"complete", "degraded"}, _job_row(database, job_id)[
        "dead_letter_reason"
    ]
    written = [
        os.path.join(root, name)
        for root, _dirs, files in os.walk(native_path(work))
        for name in files
        if (name == "current" and os.path.basename(root) == "present") or name == "library.json"
    ]
    assert written == []


# ------------------------------------------------------------------------------ 4b-i B / P0-1


def test_an_expired_but_unreclaimed_lease_fences_every_run_side_write(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run_id = "expired-lease"
    queue, job = _claimed_run(database, tmp_path, run_id, lease_seconds=0.3)
    token = job.token
    time.sleep(0.6)

    store = SQLiteCheckpointStore(database, tmp_path / "work", claim_token=token)
    with pytest.raises(StaleClaim):
        store.write(run_id, "ingest", artefacts=())
    path = tmp_path / "expired.jsonl"
    journal = SQLiteAttemptJournal(
        path,
        database=database,
        run_id=run_id,
        provider="audd",
        unit_usd_e6=UNIT,
        egress_id="egress",
        claim_token=token,
        hosted=False,  # the queue's local mode: hosted paid dispatch is refused (round 6)
    )
    with pytest.raises(StaleClaim):
        journal.prepare(query_id="a" * 64, window_id="b" * 40, ordinal=0, parent_attempt_id=None)
    assert not path.exists()
    result = RunResult(run_id, "complete", None, None, None, 0, 0, 0)
    assert queue.terminal(job.id, token, result, bundle_path=None) is False
    assert queue.wait(job.id, token, "provider down") is False
    queue.fail(job, token, "late failure")
    assert _job_row(database, job.id)["claim_token"] == token  # nothing written


def test_an_expired_intake_claim_cannot_commit_its_intake(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    intake_job_id = queue.enqueue(PlatformUrl("https://soundcloud.com/example/late"), FREE_RECIPE)
    late = queue.claim("owner", lease_seconds=0.3)
    assert late is not None and late.id == intake_job_id
    time.sleep(0.6)
    with pytest.raises(StaleClaim):
        Worker(database, tmp_path / "work")._commit_intake(late, _intake(tmp_path))
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0


# ------------------------------------------------------------------------------ 4b-i B / P0-2


def test_cancelling_a_job_that_died_before_primary_keeps_its_durable_spend(tmp_path: Path) -> None:
    run_id = "cancel-before-primary"
    database, work, queue, job_id, dispatched = _crash_deep_job(tmp_path, run_id)
    assert queue.request_cancel(job_id)
    Worker(database, work, local_mode=True, options=_deep_options(FakeAudD(SCRIPT))).run_once()
    row = _run_row(database, run_id)
    assert row["status"] == "cancelled"
    assert row["usd_e6_spent"] == dispatched * UNIT
    assert row["attempts"] == dispatched
    assert row["usd_e6_reserved"] == ORIGINAL_RESERVED_E6


def test_dead_lettering_a_job_that_died_before_primary_keeps_its_durable_spend(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    work = tmp_path / "work"
    run_id = "dead-before-primary"
    queue = JobQueue(database, local_mode=True)
    job_id = queue.enqueue(LocalPath(AUDIO), DEEP_RECIPE, run_id=run_id)
    calls = [0]

    def service(request):
        calls[0] += 1
        if calls[0] == 1:
            return service_run(request)
        raise RuntimeError("keeps failing before primary")

    killer = _KillAfterDispatch(FakeAudD(SCRIPT), kill_on=3)
    worker = Worker(
        database, work, local_mode=True, options=_deep_options(killer), service_runner=service
    )
    for _ in range(3):
        worker.run_once()
    assert queue.get(job_id).state == "dead_letter"
    dispatched = _audd_rows(database, run_id, "dispatched")
    row = _run_row(database, run_id)
    assert row["usd_e6_spent"] == dispatched * UNIT > 0
    assert row["usd_e6_reserved"] == ORIGINAL_RESERVED_E6


# ------------------------------------------------------------------------------ 4b-i B / P1-1


def test_a_sqlite_checkpoint_whose_artefact_is_absent_is_invalidated(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run_id = "absent-artefact"
    _queue, job = _claimed_run(database, tmp_path, run_id)
    artefact = tmp_path / "audio.pcm"
    artefact.write_bytes(b"\x00\x01" * 64)
    store = SQLiteCheckpointStore(database, tmp_path / "work", claim_token=job.token)
    store.write(run_id, "decode", artefacts=(artefact,))
    assert "decode" in store.completed_phases(run_id)
    artefact.unlink()
    assert "decode" not in store.completed_phases(run_id)


# ------------------------------------------------------------------------------ 4b-i B / P1-2


def test_journal_backfill_parses_outside_the_write_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    run_id = "backfill-batches"
    _queue, job = _claimed_run(database, tmp_path, run_id)
    path = tmp_path / "attempts.jsonl"
    orphan = AttemptJournal(path, run_id=run_id, provider="audd", unit_usd_e6=UNIT)
    attempt = orphan.prepare(
        query_id="e" * 64, window_id="f" * 40, ordinal=0, parent_attempt_id=None
    )
    orphan.dispatched(attempt)
    orphan.resolved(attempt, "no_match")

    locked: list[bool] = []
    real_validate = contracts.ProviderAttemptEvent.model_validate.__func__

    def probing(cls, value, *args, **kwargs):
        probe = sqlite3.connect(str(database.path), timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
            locked.append(False)
        except sqlite3.OperationalError:
            locked.append(True)
        finally:
            probe.close()
        return real_validate(cls, value, *args, **kwargs)

    monkeypatch.setattr(contracts.ProviderAttemptEvent, "model_validate", classmethod(probing))
    journal = SQLiteAttemptJournal(
        path,
        database=database,
        run_id=run_id,
        provider="audd",
        unit_usd_e6=UNIT,
        egress_id="egress",
        claim_token=job.token,
    )
    journal.backfill()
    assert locked and not any(locked), locked


# ------------------------------------------------------------------------------ 4b-i B / P1-3


def test_one_breaker_state_spans_sequential_jobs_in_a_worker(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    config = BreakerConfig(shazam_daily_budget_per_egress=1)
    seen: list[str | None] = []

    def service(request) -> RunResult:
        breaker = request.checkpoint_store.options.shazam_breaker
        seen.append(breaker.reason())
        breaker.dispatch(running_free=True)
        breaker.resolved("no_match")
        return _complete(request)

    queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    queue.enqueue(PlatformUrl("https://soundcloud.com/example/two"), FREE_RECIPE)
    worker = Worker(
        database,
        tmp_path / "work",
        options=PipelineOptions(
            project_root=ROOT, no_hints=True, app_config=AppConfig(shazam_breaker=config)
        ),
        intake_resolver=_resolver(_intake(tmp_path)),
        service_runner=service,
    )
    worker.run_once()
    worker.run_once()
    assert seen == [None, "shazam_breaker:b_daily_budget"]


def test_a_supplied_shared_breaker_is_the_workers_one_policy_state(tmp_path: Path) -> None:
    """A caller-supplied breaker is the worker's single process policy for every job.

    Round 4: the per-run wrapper delegates to it and writes NO attempt rows — a Shazam request has
    one identity, the recognise journal's own event.
    """

    database = _database(tmp_path)
    queue = JobQueue(database)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    supplied = ShazamBreaker()
    seen: list[bool] = []

    def service(request) -> RunResult:
        breaker = request.checkpoint_store.options.shazam_breaker
        seen.append(breaker.inner is supplied)
        breaker.dispatch(running_free=True)
        breaker.resolved("match")
        return _complete(request)

    Worker(
        database,
        tmp_path / "work",
        options=PipelineOptions(project_root=ROOT, no_hints=True, shazam_breaker=supplied),
        intake_resolver=_resolver(_intake(tmp_path)),
        service_runner=service,
    ).run_once()
    assert seen == [True]
    run_id = queue.get(job_id).run_id
    with database.read() as connection:
        rows = connection.execute(
            "SELECT COUNT(*) FROM provider_attempt_events WHERE run_id=? AND provider='shazam'",
            (run_id,),
        ).fetchone()[0]
    assert rows == 0


def test_shazam_dispatched_is_recorded_before_network_io(tmp_path: Path) -> None:
    """At every real Shazam send, THAT query's `dispatched` event is already in the ledger."""

    from id_detector.shazam_breaker import SHAZAM_QUERY_ID

    database = _database(tmp_path)
    queue = JobQueue(database, local_mode=True)
    queue.enqueue(LocalPath(AUDIO), FREE_RECIPE, run_id="shazam-order")
    at_send: list[tuple[str | None, list[str]]] = []

    class Probe(FakeShazamHTTP):
        async def request(self, method: str, url: str, *args: object, **kwargs):
            query_id = SHAZAM_QUERY_ID.get()
            with database.read() as connection:
                states = [
                    row["state"]
                    for row in connection.execute(
                        "SELECT state FROM provider_attempt_events "
                        "WHERE provider='shazam' AND query_id=? ORDER BY event_id",
                        (query_id,),
                    )
                ]
            at_send.append((query_id, states))
            return await super().request(method, url, *args, **kwargs)

    options = PipelineOptions(
        project_root=ROOT,
        no_hints=True,
        max_requests=100,
        max_generations=0,
        shazam_http_client=Probe(SCRIPT),
        app_config=AppConfig(),
    )
    Worker(database, tmp_path / "work", local_mode=True, options=options).run_once()
    assert at_send, "no Shazam request was made"
    for query_id, states in at_send:
        assert query_id and states and states[-1] == "dispatched", (query_id, states)
