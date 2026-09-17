"""Round 6: local money authority closed, hosted paid dispatch scoped out (round-5 review).

Thirteen of these sixteen cases were run against the round-5 code and failed there. Three are
regression coverage that already passed: the hosted free Shazam run, the empty downgrade and the
"missing line" half of the reprojection test. See ``docs/reviews/followup-money-resume.md``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from id_detector.attempts import AttemptJournal, DispatchRefused
from id_detector.compat import LOCAL_OWNER_SCOPE
from id_detector.ingest import _load_cached
from id_detector.io import canonical_json_bytes, native_path
from id_detector.journal import InvocationTimer
from id_detector.money import reserve_usd
from id_detector.providers.base import AppConfig
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE
from id_detector.service import PipelineOptions, UploadId
from idea_web.database import Database
from idea_web.jobs.local import LocalJobs, LocalWorker, local_database
from idea_web.jobs.worker import JobQueue, StaleClaim, Worker
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff
from tests.idea_web.local_runner_fakes import fake_pipeline_runner
from tests.idea_web.test_followup_queue_money import _claimed_run
from tests.idea_web.test_followup_review_fixes import UNIT, _invocations
from tests.idea_web.test_followup_round2 import (
    _child_worker_dies,
    _dispatched_queries,
    _env,
    _expire,
    _nothing_runs,
)
from tests.idea_web.test_worker import AUDIO, ROOT, SCRIPT, _database, _job_row
from tests.test_followup_money_resume import _crash_mid_primary

HOSTED_REFUSAL = "paid engines are not enabled in hosted mode yet"


def _count(database: Database, table: str, run_id: str | None = None) -> int:
    with database.read() as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            return 0
        if run_id is None:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (run_id,)
        ).fetchone()[0]


def _paid_dispatches(database: Database, run_id: str) -> int:
    """Paid dispatch rows only (round 7 fences supervised-local Shazam identities there too)."""

    with database.read() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM run_dispatches WHERE run_id=? AND provider='audd'", (run_id,)
        ).fetchone()[0]


def _settlement(database: Database, run_id: str) -> dict | None:
    with database.read() as connection:
        row = connection.execute(
            "SELECT status, journal_path, entry FROM run_settlements WHERE run_id=?", (run_id,)
        ).fetchone()
    if row is None:
        return None
    return {"status": row["status"], "path": row["journal_path"], **json.loads(row["entry"])}


def _entry_of(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in {"path"}} | {
        "status": row["status"]
    }


def _start(root: Path) -> LocalWorker:
    worker = LocalWorker(local_database(root), root, _nothing_runs, flush_seconds=0.05)
    worker.stopped.set()  # startup work only (the settlement sweep)
    worker.run_forever(poll_seconds=0.01)
    return worker


# ------------------------------------------------ A: hosted paid dispatch fails closed


def _hosted_upload(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    uploads = work / ".uploads"
    uploads.mkdir(parents=True)
    shutil.copyfile(AUDIO, uploads / "upload123")
    return work


def test_a_hosted_paid_request_is_refused_at_intake_before_any_reservation_or_dispatch(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    work = _hosted_upload(tmp_path)
    queue = JobQueue(database)
    job_id = queue.enqueue(UploadId("upload123"), DEEP_RECIPE)
    audd = FakeAudD(SCRIPT)
    options = PipelineOptions(
        project_root=ROOT,
        no_hints=True,
        max_requests=100,
        max_generations=0,
        enabled_engines=("audd",),
        paid_scan_adapters={"audd": audd},
        shazam_http_client=FakeShazamHTTP(SCRIPT),
        paid_sleep=no_backoff,
        app_config=AppConfig(),
    )
    result = Worker(database, work, options=options).run_once()
    assert result is not None and result.id == job_id
    row = _job_row(database, job_id)
    assert row["state"] == "dead_letter"
    assert HOSTED_REFUSAL in (row["dead_letter_reason"] or "")
    assert audd.calls == 0
    for table in (
        "analysis_runs",
        "provider_attempt_events",
        "run_dispatches",
        "run_reservations",
        "run_settlements",
    ):
        assert _count(database, table) == 0, table


def test_hosted_dispatch_admission_itself_refuses_a_paid_engine(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run_id = "hosted-paid-admission"
    _queue, job = _claimed_run(database, tmp_path, run_id)
    journal = Worker(database, tmp_path / "work")._journal(run_id, job.token, job_id=job.id)
    reservation = reserve_usd(
        planned=7, unit_usd_e6=UNIT, recipe_max_usd_e2=900, configured_max_usd_e2=None
    )
    with pytest.raises(DispatchRefused, match=HOSTED_REFUSAL):
        journal.record_reservation(reservation)
    with pytest.raises(DispatchRefused, match=HOSTED_REFUSAL):
        journal.prepare(query_id="a" * 64, window_id="b" * 40, ordinal=0, parent_attempt_id=None)
    assert journal.dispatch_admission is not None
    with database.write() as connection, pytest.raises(DispatchRefused, match=HOSTED_REFUSAL):
        journal.dispatch_admission.check(connection)
    for table in ("provider_attempt_events", "run_dispatches", "run_reservations"):
        assert _count(database, table) == 0, table
    with database.read() as connection:
        stored = connection.execute(
            "SELECT json_extract(checkpoints, '$._reservation') AS r, usd_e6_reserved "
            "FROM analysis_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert stored["r"] is None and stored["usd_e6_reserved"] == 0
    assert not journal.path.exists()


def test_a_hosted_free_shazam_run_still_completes(tmp_path: Path) -> None:
    database = _database(tmp_path)
    work = _hosted_upload(tmp_path)
    job_id = JobQueue(database).enqueue(UploadId("upload123"), FREE_RECIPE)
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
    with database.read() as connection:
        shazam = connection.execute(
            "SELECT COUNT(*) FROM provider_attempt_events WHERE provider='shazam'"
        ).fetchone()[0]
    assert shazam > 0


# ------------------------------------------------ B: the local reservation lives in SQLite


def _reservation_row(database: Database, run_id: str) -> dict | None:
    with database.read() as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_reservations'"
        ).fetchone()
        if exists is None:
            return None
        row = connection.execute(
            "SELECT reservation FROM run_reservations WHERE run_id=?", (run_id,)
        ).fetchone()
    return json.loads(row["reservation"]) if row is not None else None


def _local_journal(root: Path, database: Database, claimed) -> AttemptJournal:
    from idea_web.jobs.worker import DispatchAdmission

    journal = AttemptJournal(
        root / "media" / "attempts.jsonl", run_id=claimed.run_id, provider="audd", unit_usd_e6=UNIT
    )
    journal.admission = DispatchAdmission(database, job_id=claimed.id, claim_token=claimed.token)
    return journal


def _sidecars(root: Path) -> list[str]:
    return [
        os.path.join(folder, name)
        for folder, _dirs, files in os.walk(native_path(root))
        if os.path.basename(folder) == "reservations"
        for name in files
    ]


def test_a_stale_worker_writes_no_reservation_and_a_price_change_never_alters_it(
    tmp_path: Path,
) -> None:
    jobs = LocalJobs(tmp_path)
    original = reserve_usd(
        planned=7, unit_usd_e6=UNIT, recipe_max_usd_e2=900, configured_max_usd_e2=None
    )
    repriced = reserve_usd(
        planned=7, unit_usd_e6=2 * UNIT, recipe_max_usd_e2=900, configured_max_usd_e2=None
    )

    # (1) Worker A reserves while it holds the claim; its lease lapses and B reclaims the job.
    job_id = jobs.submit(str(AUDIO), "max_accuracy")
    a = jobs.queue.claim("worker-a", lease_seconds=600)
    assert a is not None and a.id == job_id
    journal_a = _local_journal(tmp_path, jobs.database, a)
    journal_a.record_reservation(original)
    stored = _reservation_row(jobs.database, a.run_id)
    assert stored is not None and stored["usd_e6_reserved"] == original.usd_e6_reserved
    _expire(jobs, job_id)
    b = jobs.queue.claim("worker-b", lease_seconds=600)
    assert b is not None and b.id == job_id and b.run_id == a.run_id
    # The stale worker past its lease writes nothing more -- not in SQLite, not beside the journal.
    with pytest.raises((StaleClaim, DispatchRefused)):
        journal_a.record_reservation(repriced)
    assert _reservation_row(jobs.database, a.run_id) == stored
    # The replacement, priced differently, resumes against the ORIGINAL reservation.
    journal_b = _local_journal(tmp_path, jobs.database, b)
    assert journal_b.durable_reservation().usd_e6_reserved == original.usd_e6_reserved
    assert journal_b.record_reservation(repriced).usd_e6_reserved == original.usd_e6_reserved
    assert _reservation_row(jobs.database, a.run_id) == stored
    # A forged or stale sidecar is a projection only: SQLite wins.
    for sidecar in _sidecars(tmp_path):
        with open(sidecar, "w", encoding="utf-8") as handle:
            json.dump({**stored, "usd_e6_reserved": 1, "unit_usd_e6": 1}, handle)
    assert journal_b.durable_reservation().usd_e6_reserved == original.usd_e6_reserved

    # (2) A worker whose lease lapsed BEFORE it ever reserved writes no row and no sidecar.
    other_root = tmp_path / "second"
    jobs2 = LocalJobs(other_root)
    job2 = jobs2.submit(str(AUDIO), "max_accuracy")
    stale = jobs2.queue.claim("worker-a", lease_seconds=600)
    assert stale is not None and stale.id == job2
    _expire(jobs2, job2)
    assert jobs2.queue.claim("worker-b", lease_seconds=600) is not None
    with pytest.raises((StaleClaim, DispatchRefused)):
        _local_journal(other_root, jobs2.database, stale).record_reservation(original)
    assert _reservation_row(jobs2.database, stale.run_id) is None
    assert _sidecars(other_root) == []


def _killed_deep_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(str(AUDIO), "max_accuracy")
    _child_worker_dies(tmp_path, config, kill_on=3)
    run_id, dispatched = _dispatched_queries(tmp_path)
    _expire(jobs, job_id)
    return config, jobs, job_id, run_id, dispatched


def test_a_resumed_local_run_restores_its_sqlite_reservation_not_the_sidecar_or_todays_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, jobs, job_id, run_id, dispatched = _killed_deep_job(tmp_path, monkeypatch)
    stored = _reservation_row(jobs.database, run_id)
    assert stored is not None
    for sidecar in _sidecars(tmp_path):
        os.remove(sidecar)  # the projection is gone; SQLite alone must carry the reservation
    runner = fake_pipeline_runner(tmp_path, config, unit_usd_e6=2 * UNIT)
    LocalWorker(local_database(tmp_path), tmp_path, runner, flush_seconds=0.05).run_once()
    assert jobs.get(job_id).status == "succeeded", jobs.get(job_id).error
    assert _reservation_row(jobs.database, run_id) == stored
    (entry,) = _invocations(tmp_path, run_id)
    assert entry["usd_e6_reserved"] == stored["usd_e6_reserved"]
    assert len(dispatched) > 0


def test_dispatch_rows_without_a_reservation_row_fail_closed_and_never_recompute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, jobs, job_id, run_id, dispatched = _killed_deep_job(tmp_path, monkeypatch)
    assert _paid_dispatches(jobs.database, run_id) == len(dispatched)
    with jobs.database.write() as connection:
        connection.execute("DELETE FROM run_reservations WHERE run_id=?", (run_id,))
    for sidecar in _sidecars(tmp_path):
        os.remove(sidecar)
    audd = FakeAudD(SCRIPT)
    runner = fake_pipeline_runner(tmp_path, config, audd=audd)
    LocalWorker(local_database(tmp_path), tmp_path, runner, flush_seconds=0.05).run_once()
    assert audd.calls == 0, "a paid request left without a durable reservation"
    assert _paid_dispatches(jobs.database, run_id) == len(dispatched)
    assert _reservation_row(jobs.database, run_id) is None
    row = _settlement(jobs.database, run_id)
    assert row is not None and row["status"] == "failed"
    assert row["reason"] == "reservation_missing"
    assert row["usd_e6_spent"] == len(dispatched) * UNIT


# ------------------------------------------------ C: legacy adoption reconciles every line


def test_legacy_lower_cancelled_then_higher_complete_lines_adopt_one_maximal_row_and_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(tmp_path, monkeypatch)
    run_id = "legacy-two-lines"
    dispatched = _crash_mid_primary(tmp_path, run_id)
    assert dispatched
    cached = _load_cached(tmp_path.resolve(), str(AUDIO))
    assert cached is not None
    path = Path(cached.media_dir) / "invocations.jsonl"
    fold_spent = len(dispatched) * UNIT

    def line(status: str, reserved: int, spent: int) -> bytes:
        entry = InvocationTimer(run_id, ["analyse", str(AUDIO)]).entry(
            status=status,
            exit_code=0 if status == "complete" else 130,
            counts={"paid_attempts": len(dispatched)},
            costs={"usd_e2": -(-spent // 10_000)},
            source_ids=[],
            ffmpeg_version=None,
            usd_e6_reserved=reserved,
            usd_e6_spent=spent,
            usd_e2_reserved=-(-reserved // 10_000),
            usd_e2_spent=-(-spent // 10_000),
        )
        return canonical_json_bytes(entry) + b"\n"

    # The pre-fix cancel/resume journal: a lower `cancelled` line, then a higher `complete` one --
    # except that the cancelled line carries the larger reservation (maxima are per field).
    high_reserved = 90_000_000
    high_spent = fold_spent + 7 * UNIT
    with open(native_path(path), "ab") as handle:
        handle.write(line("cancelled", high_reserved, UNIT))
        handle.write(line("complete", 10, high_spent))

    database = local_database(tmp_path)
    job_id = uuid.uuid4().hex
    now = time.time()
    with database.write() as connection:
        connection.execute(
            "INSERT INTO jobs(id, run_id, target, recipe_id, state, tenant_scope, progress, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'complete', ?, '{}', ?, ?)",
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

    _start(tmp_path)
    row = _settlement(database, run_id)
    assert row is not None
    assert row["status"] == "complete"
    assert row["usd_e6_reserved"] == high_reserved
    assert row["usd_e6_spent"] == high_spent
    assert row["usd_e2_reserved"] == -(-high_reserved // 10_000)
    assert row["usd_e2_spent"] == -(-high_spent // 10_000)
    (projected,) = _invocations(tmp_path, run_id)
    assert _entry_of(row) == projected
    assert _count(database, "run_settlements", run_id) == 1
    _start(tmp_path)  # idempotent
    assert _invocations(tmp_path, run_id) == [projected]


# ------------------------------------------------ D: a populated downgrade fails closed


def _insert_authority_row(database: Database, table: str) -> None:
    with database.write() as connection:
        if table == "run_dispatches":
            connection.execute(
                "INSERT INTO run_dispatches(run_id, attempt_id, job_id, provider, event, "
                "journal_path, created_at) VALUES ('r', 'a', 'j', 'audd', '{}', 'p', 1)"
            )
        elif table == "run_settlements":
            connection.execute(
                "INSERT INTO run_settlements(run_id, job_id, status, journal_path, entry, "
                "created_at, updated_at) VALUES ('r', 'j', 'failed', 'p', '{}', 1, 1)"
            )
        else:
            connection.execute(
                "INSERT INTO run_reservations(run_id, job_id, reservation, usd_e6_reserved, "
                "created_at) VALUES ('r', 'j', '{}', 5, 1)"
            )


@pytest.mark.parametrize("table", ["run_dispatches", "run_settlements", "run_reservations"])
def test_a_populated_0003_downgrade_is_refused_and_the_rows_survive(
    tmp_path: Path, table: str
) -> None:
    database = _database(tmp_path)
    _insert_authority_row(database, table)
    with pytest.raises(sqlite3.DatabaseError, match="money authority"):
        database.migrate(2)
    assert database.version() == 5
    assert _count(database, table) == 1


def test_an_empty_0003_downgrade_still_works(tmp_path: Path) -> None:
    database = _database(tmp_path)
    assert database.migrate(2) == 2
    with database.read() as connection:
        names = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert not names & {"run_dispatches", "run_settlements", "run_reservations"}
    assert database.migrate() == 5


# ------------------------------------------------ E: every local job-owned terminal run has a row


@pytest.mark.parametrize("how", ["claimed", "unclaimed"])
def test_a_zero_money_local_cancellation_gets_a_settlement_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(str(AUDIO))
    run_id = jobs.queue.get(job_id).run_id
    if how == "claimed":
        assert jobs.queue.request_cancel(job_id)
        LocalWorker(local_database(tmp_path), tmp_path, _nothing_runs).run_once()
    else:
        assert jobs.cancel(job_id)
        _start(tmp_path)
    assert jobs.get(job_id).status == "cancelled"
    row = _settlement(jobs.database, run_id)
    assert row is not None, "a job-owned terminal run has no settlement row"
    assert row["status"] == "cancelled"
    assert row["usd_e6_spent"] == 0 and row["usd_e6_reserved"] == 0


def test_a_zero_money_compatible_return_gets_a_settlement_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    runner = fake_pipeline_runner(tmp_path, config)
    first = jobs.submit(str(AUDIO))
    LocalWorker(local_database(tmp_path), tmp_path, runner, flush_seconds=0.05).run_once()
    assert jobs.get(first).status == "succeeded", jobs.get(first).error
    second = jobs.submit(str(AUDIO))
    LocalWorker(local_database(tmp_path), tmp_path, runner, flush_seconds=0.05).run_once()
    assert jobs.get(second).status == "succeeded", jobs.get(second).error
    # Served from the compatible result: the second run never reached recognition.
    assert not any("recognis" in line for line in jobs.get(second).log), list(jobs.get(second).log)
    run_id = jobs.queue.get(second).run_id
    row = _settlement(jobs.database, run_id)
    assert row is not None, "a compatible return has no settlement row"
    assert row["usd_e6_spent"] == 0
    (projected,) = _invocations(tmp_path, run_id)
    assert _entry_of(row) == projected


@pytest.mark.parametrize("damage", ["missing", "differs"])
def test_the_sweep_reprojects_a_line_that_is_missing_or_differs_from_its_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    config = _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(str(AUDIO))
    runner = fake_pipeline_runner(tmp_path, config)
    LocalWorker(local_database(tmp_path), tmp_path, runner, flush_seconds=0.05).run_once()
    assert jobs.get(job_id).status == "succeeded", jobs.get(job_id).error
    run_id = jobs.queue.get(job_id).run_id
    row = _settlement(jobs.database, run_id)
    assert row is not None
    path = Path(row["path"])
    kept: list[str] = []
    for text in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(text)
        if value["invocation_id"] != run_id:
            kept.append(text)
        elif damage == "differs":
            kept.append(json.dumps({**value, "status": "cancelled", "reason": "tampered"}))
            kept.append(json.dumps({**value, "status": "failed"}))  # and a duplicate
    path.write_text("".join(item + "\n" for item in kept), encoding="utf-8")

    _start(tmp_path)
    assert _invocations(tmp_path, run_id) == [_entry_of(row)]
    assert _settlement(jobs.database, run_id) == row
