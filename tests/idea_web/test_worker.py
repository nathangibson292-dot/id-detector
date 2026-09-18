from __future__ import annotations

import json
import multiprocessing
import sqlite3
import subprocess
import sys
import threading
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from id_detector.attempts import AttemptJournal
from id_detector.compat import AnalysisInputs
from id_detector.compat import RunRequest as CompatibilityRequest
from id_detector.io import atomic_write_json, sha256_file
from id_detector.jobs import JobStoreLocked, ProcessLock
from id_detector.present.bundles import bundle_id
from id_detector.providers.base import AppConfig
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE
from id_detector.service import (
    CHECKPOINT_PHASES,
    LocalPath,
    PipelineOptions,
    PlatformUrl,
    RunResult,
    TargetRefused,
)
from idea_web.database import Database
from idea_web.jobs import worker as worker_module
from idea_web.jobs.worker import (
    HEARTBEAT_SECONDS,
    Job,
    JobQueue,
    LedgerShazamBreaker,
    PreparedIntake,
    SQLiteAttemptJournal,
    SQLiteCheckpointStore,
    StaleClaim,
    Worker,
)
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff

ROOT = Path(__file__).resolve().parents[2]
AUDIO = ROOT / "tests/fixtures/audio/tone-60s.wav"
SCRIPT = ROOT / "tests/fakes/scripts/gate0a-deep.json"
MIX = "https://soundcloud.com/example/mix"


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "app.db")
    database.migrate()
    return database


def _intake(tmp_path: Path, recipe=FREE_RECIPE, *, scope: str = "public") -> PreparedIntake:
    media_key = "a" * 8
    media_dir = tmp_path / "work" / "source" / media_key
    media_dir.mkdir(parents=True, exist_ok=True)
    return PreparedIntake(
        AnalysisInputs(media_key, recipe.recipe_id, "platform", scope, "hints"),
        media_dir,
        60_000,
        {},
    )


def _resolver(intake: PreparedIntake):
    def resolve(_job: Job, _store: SQLiteCheckpointStore) -> PreparedIntake:
        return intake

    return resolve


def _complete(request) -> RunResult:
    return RunResult(request.run_id, "complete", None, request.recipe.name, None, 0, 0, 0)


def _insert_run(database: Database, intake: PreparedIntake, *, run_id: str, status: str) -> None:
    now = time.time()
    with database.write() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO media(media_key, duration_ms, first_seen) VALUES (?, ?, ?)",
            (intake.inputs.media_key, intake.duration_ms, now),
        )
        connection.execute(
            "INSERT INTO analysis_runs(run_id, analysis_key, media_key, requested_recipe_id, "
            "algorithm_version, adapter_versions, status, tenant_scope, checkpoints, started_at, "
            "analysis_inputs, non_recipe_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)",
            (
                run_id,
                intake.analysis_key,
                intake.inputs.media_key,
                FREE_RECIPE.recipe_id,
                FREE_RECIPE.algorithm_version,
                json.dumps(dict(FREE_RECIPE.adapter_versions)),
                status,
                intake.inputs.tenant_scope,
                now,
                json.dumps(vars(intake.inputs)),
                intake.inputs.non_recipe_key,
            ),
        )


def _publish_bundle(
    database: Database, intake: PreparedIntake, *, run_id: str, duration_ms: int = 60_000
) -> tuple[str, Path]:
    """A retained, compatible Free bundle for ``intake`` with its own ``analysis_runs`` row."""

    _insert_run(database, intake, run_id=run_id, status="complete")
    compatibility = CompatibilityRequest(intake.inputs, FREE_RECIPE, local=False).metadata()
    identifier = bundle_id(run_id, 1)
    directory = intake.media_dir / "present" / "bundles" / identifier
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "tracklist.json", "source.json"):
        (directory / name).write_text("{}", encoding="utf-8")
    manifest = {
        "run_id": run_id,
        "presentation_version": 1,
        "status": "complete",
        "achieved": "free",
        "duration_ms": duration_ms,
        "fuse_run": f"fuse/runs/{run_id}",
        "compatibility": compatibility,
        "files": {
            name: {
                "sha256": sha256_file(directory / name),
                "size": (directory / name).stat().st_size,
            }
            for name in ("index.html", "tracklist.json", "source.json")
        },
    }
    atomic_write_json(directory / "manifest.json", manifest)
    with database.write() as connection:
        connection.execute(
            "INSERT INTO result_bundles VALUES (?, ?, ?, ?, ?, ?)",
            (
                identifier,
                run_id,
                1,
                str(directory),
                sha256_file(directory / "manifest.json"),
                time.time(),
            ),
        )
    return identifier, directory


def _claim_run(database: Database, intake: PreparedIntake, run_id: str, recipe=FREE_RECIPE) -> str:
    """A run driven by a live claim, returning its token.

    The run-side fence (follow-up cycle, 4b-i retro P0) requires the run's token to belong to an
    active job with an unexpired lease; a token written onto ``analysis_runs`` alone is refused.
    """

    queue = JobQueue(database)
    queue.enqueue(PlatformUrl(MIX), recipe, run_id=run_id)
    _insert_run(database, intake, run_id=run_id, status="analysis")
    job = queue.claim("fence-owner", lease_seconds=600)
    assert job is not None
    return job.token


def _run_row(database: Database, run_id: str) -> sqlite3.Row:
    with database.read() as connection:
        row = connection.execute("SELECT * FROM analysis_runs WHERE run_id=?", (run_id,)).fetchone()
    assert row is not None
    return row


def _job_row(database: Database, job_id: str) -> sqlite3.Row:
    with database.read() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row is not None
    return row


def test_migrations_go_up_and_down_and_enable_wal_and_foreign_keys(tmp_path: Path) -> None:
    database = _database(tmp_path)
    assert database.version() == 6
    with database.read() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "media",
        "analysis_runs",
        "result_bundles",
        "jobs",
        "provider_attempt_events",
    } <= tables

    assert database.migrate(0) == 0
    with database.read() as connection:
        # Every object, not just tables: a ``down`` that dropped nothing (or left an index or an
        # append-only trigger behind) fails here rather than passing as "reversible".
        remaining = {
            (row[0], row[1]) for row in connection.execute("SELECT type, name FROM sqlite_master")
        }
    assert remaining == {("table", "schema_migrations")}


def test_simultaneous_migrators_are_serialised(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []
    versions: list[int] = []

    def migrate() -> None:
        database = Database(path)  # a separate handle, as a second process would have
        try:
            barrier.wait(timeout=30)
            database.migrate()
            versions.append(database.version())
        except BaseException as exc:  # noqa: BLE001 - the assertion is "nobody raised"
            errors.append(exc)

    threads = [threading.Thread(target=migrate) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors
    assert versions == [6, 6, 6, 6]
    with Database(path).read() as connection:
        applied = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert applied == 6


def test_intake_transitions_to_analysis_before_the_service_runs(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    observed: list[str] = []

    def service(request) -> RunResult:
        observed.append(queue.get(job_id).state)
        assert request.analysis_key == _intake(tmp_path).analysis_key
        return _complete(request)

    result = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(_intake(tmp_path)),
        service_runner=service,
    ).run_once()
    assert observed == ["analysis"]
    assert result is not None and result.state == "complete"


def test_a_job_dead_letters_on_its_third_failed_attempt(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    job_id = queue.enqueue(PlatformUrl("https://mixcloud.com/example/mix"), FREE_RECIPE)

    def fail(_request) -> RunResult:
        raise RuntimeError("deterministic failure")

    worker = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(_intake(tmp_path)),
        service_runner=fail,
    )
    assert worker.run_once().state == "analysis"
    assert worker.run_once().state == "analysis"
    dead = worker.run_once()
    assert dead is not None and dead.state == "dead_letter"
    assert dead.attempt == 3
    assert dead.dead_letter_reason == "RuntimeError: deterministic failure"
    # The run must die with its job: an ``analysis`` run nobody will ever execute would keep
    # collecting attached submissions forever.
    run_id = _job_row(database, job_id)["run_id"]
    assert _run_row(database, run_id)["status"] == "dead_letter"
    assert _run_row(database, run_id)["claim_token"] is None


def test_a_dead_lettered_run_is_never_attached_to_and_expires_to_failed(tmp_path: Path) -> None:
    now = [1_000_000.0]
    database = _database(tmp_path)
    queue = JobQueue(database, clock=lambda: now[0])
    intake = _intake(tmp_path)
    first_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)

    def fail(_request) -> RunResult:
        raise RuntimeError("deterministic failure")

    worker = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(intake),
        service_runner=fail,
        clock=lambda: now[0],
    )
    for _ in range(3):
        worker.run_once()
    abandoned = _job_row(database, first_id)["run_id"]
    assert _job_row(database, first_id)["state"] == "dead_letter"

    second_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    worker.service_runner = _complete
    second = worker.run_once()
    assert second is not None and second.id == second_id
    assert second.state == "complete"
    assert second.run_id is not None and second.run_id != abandoned

    now[0] += 24 * 60 * 60 + 1
    assert queue.expire_dead_letters() == 1
    assert queue.get(first_id).state == "failed"
    assert _run_row(database, abandoned)["status"] == "failed"


def test_dead_letter_is_treated_as_failed_after_24_hours(tmp_path: Path) -> None:
    now = [1_000_000.0]
    database = _database(tmp_path)
    queue = JobQueue(database, clock=lambda: now[0])
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    with database.write() as connection:
        connection.execute(
            "UPDATE jobs SET state='dead_letter', dead_letter_reason='three failures' WHERE id=?",
            (job_id,),
        )
    now[0] += 24 * 60 * 60 - 1
    assert queue.expire_dead_letters() == 0
    now[0] += 1
    assert queue.expire_dead_letters() == 1
    assert queue.get(job_id).state == "failed"


def test_an_expired_lease_cannot_be_renewed_or_used_even_by_the_same_worker_id(
    tmp_path: Path,
) -> None:
    now = [100.0]
    database = _database(tmp_path)
    queue = JobQueue(database, clock=lambda: now[0])
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    first = queue.claim("worker-one", lease_seconds=5)
    assert first is not None and first.attempt == 1 and first.claim_token

    now[0] = 106.0
    # Expiry is part of ownership: renewing after it would let a paused worker take the job back
    # from the replacement that is already running it.
    assert not queue.heartbeat(job_id, first.claim_token, lease_seconds=5)
    assert queue.cancel_requested(job_id, first.claim_token)

    second = queue.claim("worker-one", lease_seconds=5)  # the SAME worker id, a new claim
    assert second is not None and second.id == job_id and second.attempt == 2
    assert second.claim_token != first.claim_token
    assert not queue.update_progress(job_id, first.claim_token, {"bad": True}, lease_seconds=5)
    assert queue.update_progress(job_id, second.claim_token, {"good": True}, lease_seconds=5)
    assert queue.get(job_id).progress == {"good": True}
    assert not queue.cancel_requested(job_id, second.claim_token)
    assert not queue.terminal(
        job_id,
        first.claim_token,
        RunResult(job_id, "cancelled", None, None, None, 0, 0, 0),
        bundle_path=None,
    )
    assert queue.get(job_id).state == "intake"


def test_a_reclaimed_claim_cannot_write_checkpoints_money_or_attempts(tmp_path: Path) -> None:
    now = [1_000.0]
    database = _database(tmp_path)
    queue = JobQueue(database, clock=lambda: now[0])
    intake = _intake(tmp_path)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    artifact = intake.media_dir / "primary.json"
    replacement: dict[str, Job] = {}

    def service(request) -> RunResult:
        atomic_write_json(artifact, {"durable": True})
        request.checkpoint_store.write(
            request.run_id,
            "primary",
            artefacts=(artifact,),
            state={"usd_e6_reserved": 40_000, "usd_e6_spent": 35_000, "attempts": 7},
        )
        # The lease expires while this pass is still executing and a replacement takes over.
        now[0] += 600
        stolen = JobQueue(database, clock=lambda: now[0]).claim("worker-two", lease_seconds=600)
        assert stolen is not None and stolen.id == job_id
        replacement["job"] = stolen
        assert JobQueue(database, clock=lambda: now[0]).begin_analysis(job_id, stolen.token)
        SQLiteCheckpointStore(
            database, tmp_path / "work", claim_token=stolen.token, clock=lambda: now[0]
        ).write(
            request.run_id,
            "primary",
            artefacts=(artifact,),
            state={"usd_e6_reserved": 40_000, "usd_e6_spent": 35_000, "attempts": 9},
        )
        # Everything the old claim tries from here must be refused, loudly.
        with pytest.raises(StaleClaim):
            request.checkpoint_store.write(request.run_id, "hints", artefacts=(artifact,))
        with pytest.raises(StaleClaim):
            request.attempt_journal.prepare(
                query_id="a" * 64, window_id="b" * 40, ordinal=1, parent_attempt_id=None
            )
        assert not request.attempt_journal.path.exists()
        assert not queue.update_progress(
            job_id, request.checkpoint_store.claim_token, {"stale": True}, lease_seconds=5
        )
        return RunResult(request.run_id, "complete", None, "free", None, 0, 0, 0)

    worker = Worker(
        database,
        tmp_path / "work",
        worker_id="worker-one",
        intake_resolver=_resolver(intake),
        service_runner=service,
        clock=lambda: now[0],
        lease_seconds=30,
        heartbeat_seconds=1_000,
    )
    worker.run_once()

    job = queue.get(job_id)
    assert job.state == "analysis"  # the old claim could not settle it
    assert job.claim_token == replacement["job"].claim_token
    run = _run_row(database, job.run_id)
    assert run["status"] == "analysis"
    assert (run["usd_e6_reserved"], run["usd_e6_spent"], run["attempts"]) == (40_000, 35_000, 9)
    store = SQLiteCheckpointStore(database, tmp_path / "work")
    assert store.completed_phases(job.run_id) == {"primary"}
    assert store.state(job.run_id, "primary")["attempts"] == 9


def test_a_checkpoint_never_commits_ahead_of_its_artefact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    intake = _intake(tmp_path)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    order: list[str] = []
    real_fsync = worker_module.fsync_artefact

    def traced(path: Path) -> None:
        order.append(f"flush:{Path(path).name}")
        real_fsync(path)

    monkeypatch.setattr(worker_module, "fsync_artefact", traced)
    artifact = intake.media_dir / "primary.json"

    def service(request) -> RunResult:
        atomic_write_json(artifact, {"durable": True})
        request.checkpoint_store.write(request.run_id, "primary", artefacts=(artifact,))
        # A checkpoint may not commit for an artefact that cannot be made durable.
        missing = intake.media_dir / "gone.json"
        with pytest.raises(ValueError, match="not durable"):
            request.checkpoint_store.write(request.run_id, "fuse1", artefacts=(missing,))
        return _complete(request)

    worker = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(intake),
        service_runner=service,
        checkpoint_after_commit=lambda _run_id, phase: order.append(f"commit:{phase}"),
    )
    assert worker.run_once().state == "complete"
    assert order == ["flush:primary.json", "commit:primary"]
    run_id = queue.get(job_id).run_id
    store = SQLiteCheckpointStore(database, tmp_path / "work")
    assert store.completed_phases(run_id) == {"primary"}

    # And the same boundary holds when the flush itself fails.
    def explode(_path: Path) -> None:
        raise OSError("device lost")

    monkeypatch.setattr(worker_module, "fsync_artefact", explode)
    assert _run_row(database, run_id)["claim_token"] is None  # settled: no writer owns it
    with database.write() as connection:
        connection.execute("UPDATE analysis_runs SET claim_token='probe' WHERE run_id=?", (run_id,))
    fenced = SQLiteCheckpointStore(database, tmp_path / "work", claim_token="probe")
    with pytest.raises(ValueError, match="could not be flushed"):
        fenced.write(run_id, "fuse1", artefacts=(artifact,))
    assert store.completed_phases(run_id) == {"primary"}


def test_intake_checkpoints_flush_their_artefacts_before_the_intake_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    base = _intake(tmp_path)
    source = base.media_dir / "ingest" / "source.json"
    atomic_write_json(source, {"media_key": base.inputs.media_key})
    intake = PreparedIntake(
        base.inputs,
        base.media_dir,
        base.duration_ms,
        {"ingest": {"artefacts": [str(source)], "state": {}}},
    )
    flushed: list[str] = []
    real_fsync = worker_module.fsync_artefact

    def traced(path: Path) -> None:
        flushed.append(Path(path).name)
        real_fsync(path)

    monkeypatch.setattr(worker_module, "fsync_artefact", traced)
    queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    worker = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(intake),
        service_runner=_complete,
    )
    assert worker.run_once().state == "complete"
    assert flushed == ["source.json"]

    # An intake checkpoint naming an artefact that is not there is refused, not committed.
    missing = PreparedIntake(
        base.inputs,
        base.media_dir,
        base.duration_ms,
        {"ingest": {"artefacts": [str(base.media_dir / "gone.json")], "state": {}}},
    )
    second_id = queue.enqueue(PlatformUrl("https://soundcloud.com/example/two"), FREE_RECIPE)
    failed = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(missing),
        service_runner=_complete,
    ).run_once()
    assert failed is not None and failed.id == second_id and failed.state == "intake"
    assert failed.run_id is None


def test_cancelling_a_resumed_paid_job_keeps_its_reservation_spend_and_attempts(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    intake = _intake(tmp_path)
    run_id = "paid-run"
    _insert_run(database, intake, run_id=run_id, status="analysis")
    artifact = intake.media_dir / "primary.json"
    atomic_write_json(artifact, {"durable": True})
    # What the killed pass left behind: a durable primary checkpoint carrying real money.
    with database.write() as connection:
        connection.execute(
            "UPDATE analysis_runs SET checkpoints=? WHERE run_id=?",
            (
                json.dumps(
                    {
                        "primary": {
                            "artefacts": [str(artifact)],
                            "state": {
                                "usd_e6_reserved": 40_000,
                                "usd_e6_spent": 35_000,
                                "attempts": 7,
                            },
                        }
                    }
                ),
                run_id,
            ),
        )
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, run_id=run_id)
    with database.write() as connection:
        connection.execute("UPDATE jobs SET state='analysis' WHERE id=?", (job_id,))
    assert queue.request_cancel(job_id)

    def must_not_run(_request):
        raise AssertionError("a cancelled job started a pipeline")

    result = Worker(
        database,
        tmp_path / "work",
        service_runner=must_not_run,
    ).run_once()
    assert result is not None and result.state == "cancelled"
    run = _run_row(database, run_id)
    assert run["status"] == "cancelled"
    assert (run["usd_e6_reserved"], run["usd_e6_spent"], run["attempts"]) == (40_000, 35_000, 7)


def test_cancel_requested_mid_phase_reaches_token_and_keeps_checkpoints(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    intake = _intake(tmp_path)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)

    def service(request) -> RunResult:
        artifact = intake.media_dir / "primary.json"
        atomic_write_json(artifact, {"durable": True})
        request.checkpoint_store.write(request.run_id, "primary", artefacts=(artifact,))
        assert queue.request_cancel(job_id)
        assert request.cancel_token.is_set()
        request.progress("recognise", 1, 2, "mid phase")
        raise AssertionError("cancelled progress must not return")

    worker = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(intake),
        service_runner=service,
    )
    result = worker.run_once()
    assert result is not None and result.state == "cancelled"
    assert SQLiteCheckpointStore(database, tmp_path / "work").completed_phases(result.run_id) == {
        "primary"
    }


def test_checkpoint_failure_after_artifact_does_not_mark_phase_complete(tmp_path: Path) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path)
    queue = JobQueue(database)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    calls = [0]

    def fail_before_checkpoint(_run_id: str, phase: str) -> None:
        if phase == "primary" and calls[0] == 0:
            calls[0] += 1
            raise RuntimeError("power lost after artifact fsync")

    worker = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(intake),
        service_runner=lambda request: _write_primary(request, intake),
        checkpoint_before_commit=fail_before_checkpoint,
    )
    assert worker.run_once().state == "analysis"
    run_id = queue.get(job_id).run_id
    store = SQLiteCheckpointStore(database, tmp_path / "work")
    assert run_id is not None and "primary" not in store.completed_phases(run_id)
    assert (intake.media_dir / "primary.json").is_file()

    worker.checkpoint_before_commit = None
    assert worker.run_once().state == "complete"
    assert calls == [1]
    assert "primary" in store.completed_phases(run_id)


def _write_primary(request, intake: PreparedIntake) -> RunResult:
    artifact = intake.media_dir / "primary.json"
    atomic_write_json(artifact, {"durable": True})
    request.checkpoint_store.write(request.run_id, "primary", artefacts=(artifact,))
    return _complete(request)


def test_compatible_result_is_served_during_intake_without_service_run(tmp_path: Path) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path)
    identifier, _directory = _publish_bundle(database, intake, run_id="stored-run")
    queue = JobQueue(database)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)

    def must_not_run(_request):
        raise AssertionError("compatible intake started a pipeline")

    result = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(intake),
        service_runner=must_not_run,
    ).run_once()
    assert result is not None and result.id == job_id
    assert result.state == "complete" and result.result_bundle_id == identifier
    assert result.attempt == 1


def test_retained_result_intake_serves_without_decoding_a_pruned_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After retention the PCM and the original are gone; the stored bundle still answers."""

    database = _database(tmp_path)
    work = tmp_path / "work"
    media_dir = work / "source" / ("a" * 8)
    media_dir.mkdir(parents=True)
    options = PipelineOptions(project_root=ROOT, no_hints=True)
    identity = AnalysisInputs(
        media_key="a" * 8,
        recipe_id=FREE_RECIPE.recipe_id,
        source_kind="platform",
        tenant_scope="user:alice",
        hints_snapshot_id=worker_module.hints_snapshot(()),
        manual_tracklist_sha256="",
        panako_index_id=worker_module.index_identity(options.index_root, options.local_index_label),
    )
    probe = PreparedIntake(identity, media_dir, 60_000, {})
    identifier, directory = _publish_bundle(database, probe, run_id="retained-run")
    retained = SimpleNamespace(
        record=SimpleNamespace(media_key=identity.media_key),
        media_dir=media_dir,
        source_path=directory / "source.json",  # what ``_load_cached`` returns for a bundle
        original_path=media_dir / "ingest" / "original.wav",  # pruned after 7 days
        retained=True,
    )
    monkeypatch.setattr(worker_module, "_load_cached", lambda _root, _target: retained)

    async def refuse_decode(_ingested):
        raise AssertionError("intake decoded a retained result")

    monkeypatch.setattr(worker_module, "decode", refuse_decode)

    queue = JobQueue(database)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, tenant_scope="user:alice")
    worker = Worker(
        database,
        work,
        options=options,
        service_runner=lambda _request: (_ for _ in ()).throw(
            AssertionError("retained intake started a pipeline")
        ),
    )
    prepared = worker._prepare_intake(queue.get(job_id), worker._store(queue.get(job_id)))
    assert prepared.duration_ms == 60_000
    assert set(prepared.checkpoints) == {"ingest"}
    assert prepared.inputs.tenant_scope == "user:alice"  # the job's scope, not a derived one

    result = worker.run_once()
    assert result is not None and result.id == job_id
    assert result.state == "complete" and result.result_bundle_id == identifier


def test_intake_and_the_pipeline_agree_on_the_job_tenant_scope(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    intake = _intake(tmp_path, scope="user:alice")
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, tenant_scope="user:alice")
    seen: dict[str, object] = {}

    def service(request) -> RunResult:
        seen["scope"] = request.checkpoint_store.options.tenant_scope
        return _complete(request)

    worker = Worker(
        database,
        tmp_path / "work",
        options=PipelineOptions(project_root=ROOT, no_hints=True),
        intake_resolver=_resolver(intake),
        service_runner=service,
    )
    assert worker.run_once().state == "complete"
    assert seen["scope"] == "user:alice"
    run = _run_row(database, queue.get(job_id).run_id)
    assert json.loads(run["analysis_inputs"])["tenant_scope"] == "user:alice"
    assert run["tenant_scope"] == "user:alice"


def _locked(path: Path) -> bool:
    """Whether ``path`` is held as a :class:`ProcessLock` right now (Windows keeps no lock file)."""

    probe = ProcessLock(path)
    try:
        probe.acquire()
    except JobStoreLocked:
        return True
    probe.release()
    return False


def test_intake_holds_its_heartbeat_and_the_source_and_media_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    work = tmp_path / "work"
    queue = JobQueue(database)
    options = PipelineOptions(project_root=ROOT, no_hints=True)
    media_dir = work / "source" / ("a" * 8)
    media_dir.mkdir(parents=True)
    identity = AnalysisInputs(
        media_key="a" * 8,
        recipe_id=FREE_RECIPE.recipe_id,
        source_kind="platform",
        tenant_scope="public",
        hints_snapshot_id=worker_module.hints_snapshot(()),
        manual_tracklist_sha256="",
        panako_index_id=worker_module.index_identity(options.index_root, options.local_index_label),
    )
    _identifier, directory = _publish_bundle(
        database, PreparedIntake(identity, media_dir, 60_000, {}), run_id="locked-run"
    )
    held: dict[str, bool] = {}
    source_lock_path = work / ".locks" / f"{sha256(MIX.encode('utf-8')).hexdigest()}.lock"

    def slow_load(_root, _target):
        # Intake is long: an unrenewed lease is reclaimed mid-fetch and the replacement repeats
        # the whole fetch and decode.
        time.sleep(1.0)
        held["source"] = _locked(source_lock_path)
        return SimpleNamespace(
            record=SimpleNamespace(media_key="a" * 8),
            media_dir=media_dir,
            source_path=directory / "source.json",
            original_path=media_dir / "ingest" / "missing.wav",
        )

    real_manifest = worker_module.read_bundle_manifest

    def watched_manifest(path: Path):
        # Reached after the media directory is known, i.e. while its lock must be held.
        held.setdefault("media", _locked(media_dir / ".media.lock"))
        return real_manifest(path)

    monkeypatch.setattr(worker_module, "_load_cached", slow_load)
    monkeypatch.setattr(worker_module, "read_bundle_manifest", watched_manifest)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    worker = Worker(
        database,
        work,
        options=options,
        service_runner=_complete,
        lease_seconds=0.5,
        heartbeat_seconds=0.1,
    )
    thief_saw: list[Job | None] = []

    def steal() -> None:
        time.sleep(0.8)
        thief_saw.append(JobQueue(database).claim("thief", lease_seconds=5))

    thief = threading.Thread(target=steal)
    thief.start()
    result = worker.run_once()
    thief.join(10)

    assert held["source"] is True  # the pipeline's source lock, taken by intake too
    assert held["media"] is True
    assert thief_saw == [None]  # the lease never lapsed while intake ran
    assert result is not None and result.id == job_id and result.state == "complete"
    assert not _locked(source_lock_path)  # released before the pipeline re-acquires them
    assert not _locked(media_dir / ".media.lock")


def test_a_busy_source_defers_intake_without_burning_an_attempt(tmp_path: Path) -> None:
    now = [10.0]
    database = _database(tmp_path)
    work = tmp_path / "work"
    queue = JobQueue(database, clock=lambda: now[0])
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    holder = ProcessLock(work / ".locks" / f"{sha256(MIX.encode('utf-8')).hexdigest()}.lock")
    holder.acquire()

    def must_not_run(_request):
        raise AssertionError("a deferred job started a pipeline")

    worker = Worker(
        database,
        work,
        options=PipelineOptions(project_root=ROOT, no_hints=True),
        service_runner=must_not_run,
        clock=lambda: now[0],
        wait_cooldown_seconds=30,
    )
    try:
        parked = worker.run_once()
    finally:
        holder.release()
    # A busy source is not a failed attempt, and the job has no run to resume: it waits in intake.
    assert parked is not None and parked.id == job_id
    assert parked.state == "intake" and parked.attempt == 0
    assert worker.run_once() is None  # held for its cooldown
    now[0] += 31
    claimed = queue.claim("probe", lease_seconds=5)
    assert claimed is not None and claimed.state == "intake" and claimed.attempt == 1


def test_a_breaker_waiting_job_resumes_without_exhausting_its_attempts(tmp_path: Path) -> None:
    now = [500.0]
    database = _database(tmp_path)
    queue = JobQueue(database, clock=lambda: now[0])
    intake = _intake(tmp_path)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)

    def waiting(request) -> RunResult:
        return RunResult(
            request.run_id, "waiting", "shazam_breaker:a_failure_rate", None, None, 0, 0, 0
        )

    worker = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(intake),
        service_runner=waiting,
        clock=lambda: now[0],
        wait_cooldown_seconds=30,
    )
    for _ in range(4):
        assert worker.run_once() is not None
        parked = queue.get(job_id)
        assert parked.state == "waiting"
        assert parked.attempt == 0  # waiting is not a failed attempt
        assert worker.run_once() is None  # and it is not re-claimed during its cooldown
        now[0] += 31
    assert _run_row(database, queue.get(job_id).run_id)["status"] == "waiting"

    worker.service_runner = _complete
    finished = worker.run_once()
    assert finished is not None and finished.state == "complete"
    assert finished.attempt == 1


def test_one_unreadable_row_is_quarantined_and_the_consumer_carries_on(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    broken_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    good_id = queue.enqueue(PlatformUrl("https://soundcloud.com/example/two"), FREE_RECIPE)
    with database.write() as connection:
        connection.execute("UPDATE jobs SET recipe_id='not-a-recipe' WHERE id=?", (broken_id,))

    worker = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(_intake(tmp_path)),
        service_runner=_complete,
    )
    result = worker.run_once()
    assert result is not None and result.id == good_id and result.state == "complete"
    broken = _job_row(database, broken_id)
    assert broken["state"] == "dead_letter"
    assert "unusable job row" in broken["dead_letter_reason"]
    assert worker.run_once() is None


def test_local_path_is_refused_in_hosted_mode_on_write_and_untrusted_read(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    with pytest.raises(TargetRefused, match="only in local mode"):
        queue.enqueue(LocalPath(AUDIO), FREE_RECIPE)

    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    with database.write() as connection:
        connection.execute(
            "UPDATE jobs SET target=? WHERE id=?",
            (json.dumps({"kind": "local", "path": str(AUDIO)}), job_id),
        )
    with pytest.raises(TargetRefused, match="only in local mode"):
        queue.get(job_id)
    # A tampered row must not take the consumer down with it either.
    assert Worker(database, tmp_path / "work", service_runner=_complete).run_once() is None
    assert _job_row(database, job_id)["state"] == "dead_letter"


def test_provider_attempt_events_are_append_only(tmp_path: Path) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path, DEEP_RECIPE)
    token = _claim_run(database, intake, "attempt-run", DEEP_RECIPE)
    journal = SQLiteAttemptJournal(
        tmp_path / "attempts.jsonl",
        database=database,
        run_id="attempt-run",
        provider="audd",
        unit_usd_e6=5_000,
        egress_id="egress-one",
        claim_token=token,
        hosted=False,  # the queue's local mode: hosted paid dispatch is refused (round 6)
    )
    attempt = journal.prepare(
        query_id="a" * 64, window_id="b" * 40, ordinal=1, parent_attempt_id=None
    )
    journal.dispatched(attempt)
    journal.resolved(attempt, "match")
    with database.read() as connection:
        rows = connection.execute(
            "SELECT attempt_id, seq, run_id, provider, egress_id, query_id, parent_attempt_id, "
            "state, outcome, http_status, unit_usd_e6, at FROM provider_attempt_events ORDER BY seq"
        ).fetchall()
    assert [row["state"] for row in rows] == ["prepared", "dispatched", "resolved"]
    assert rows[-1]["outcome"] == "match"
    with database.write() as connection, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE provider_attempt_events SET outcome='no_match'")
    with database.write() as connection, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM provider_attempt_events")


def test_attempt_projection_is_backfilled_from_the_durable_journal(tmp_path: Path) -> None:
    """A crash between the JSONL append and the SQLite insert must not lose an attempt."""

    database = _database(tmp_path)
    intake = _intake(tmp_path, DEEP_RECIPE)
    token = _claim_run(database, intake, "crash-run", DEEP_RECIPE)
    path = tmp_path / "attempts.jsonl"
    orphan = AttemptJournal(path, run_id="crash-run", provider="audd", unit_usd_e6=5_000)
    attempt = orphan.prepare(
        query_id="c" * 64, window_id="d" * 40, ordinal=1, parent_attempt_id=None
    )
    orphan.dispatched(attempt)
    orphan.resolved(attempt, "no_match")
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_attempt_events").fetchone()[0] == 0

    journal = SQLiteAttemptJournal(
        path,
        database=database,
        run_id="crash-run",
        provider="audd",
        unit_usd_e6=5_000,
        egress_id="egress-one",
        claim_token=token,
    )
    assert journal.backfill() == 3
    assert journal.backfill() == 3  # idempotent: the rows are already there
    with database.read() as connection:
        states = [
            row["state"]
            for row in connection.execute("SELECT state FROM provider_attempt_events ORDER BY seq")
        ]
        total = connection.execute("SELECT COUNT(*) FROM provider_attempt_events").fetchone()[0]
    assert states == ["prepared", "dispatched", "resolved"]
    assert total == 3


def test_shazam_attempts_reach_the_ledger_and_are_fenced(tmp_path: Path) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path)
    token = _claim_run(database, intake, "shazam-run")
    # Round 4: ONE Shazam attempt identity. The journal's own event (deterministic attempt id, the
    # clip query id) is what reaches the ledger; the process breaker writes no rows of its own.
    journal = SQLiteAttemptJournal(
        tmp_path / "shazam.jsonl",
        database=database,
        run_id="shazam-run",
        provider="shazam",
        unit_usd_e6=0,
        egress_id="egress-one",
        claim_token=token,
    )
    query_id = "e" * 64
    for ordinal, outcome in enumerate(("http_429", "match")):
        attempt = journal.prepare(
            query_id=query_id, window_id="f" * 40, ordinal=ordinal, parent_attempt_id=None
        )
        journal.dispatched(attempt)
        journal.resolved(attempt, outcome)
    breaker = LedgerShazamBreaker(
        None,
        database=database,
        run_id="shazam-run",
        egress_id="egress-one",
        claim_token=token,
    )
    breaker.dispatch(running_free=True)
    breaker.resolved("match")
    with database.read() as connection:
        rows = connection.execute(
            "SELECT provider, state, outcome, http_status, query_id, unit_usd_e6 "
            "FROM provider_attempt_events ORDER BY event_id"
        ).fetchall()
    assert {row["provider"] for row in rows} == {"shazam"}
    assert [row["state"] for row in rows] == [
        "prepared",
        "dispatched",
        "resolved",
        "prepared",
        "dispatched",
        "resolved",
    ]  # the breaker's dispatch/resolve added nothing
    resolved = [row for row in rows if row["state"] == "resolved"]
    assert [row["outcome"] for row in resolved] == ["http_429", "match"]
    assert resolved[0]["http_status"] == 429
    assert all(row["query_id"] == query_id and row["unit_usd_e6"] == 0 for row in rows)

    with database.write() as connection:
        connection.execute("UPDATE analysis_runs SET claim_token='token-four'")
    with pytest.raises(StaleClaim):
        journal.prepare(query_id=query_id, window_id="f" * 40, ordinal=2, parent_attempt_id=None)


def test_worker_contract_has_ten_second_heartbeat_and_no_http_framework() -> None:
    assert HEARTBEAT_SECONDS == 10
    # 4a-ii adds the HTTP application to ``idea_web``.  The boundary is that neither the worker
    # package nor the pipeline package ever imports an HTTP framework: not in their source text,
    # and not at runtime when the worker process loads everything it runs.
    worker_sources = list((ROOT / "src" / "idea_web" / "jobs").rglob("*.py"))
    pipeline_sources = list((ROOT / "src" / "id_detector").rglob("*.py"))
    assert worker_sources and pipeline_sources
    for path in [*worker_sources, *pipeline_sources]:
        text = path.read_text(encoding="utf-8")
        for name in ("fastapi", "starlette", "uvicorn"):
            assert f"import {name}" not in text and f"from {name}" not in text, path
    probe = (
        "import sys\n"
        "import idea_web.jobs, idea_web.jobs.local, id_detector.cli, id_detector.present\n"
        "import id_detector.webapp.runner, id_detector.truth_review\n"
        "print(','.join(m for m in ('fastapi', 'starlette', 'uvicorn') if m in sys.modules))\n"
    )
    finished = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT, timeout=120
    )
    assert finished.returncode == 0, finished.stderr
    assert finished.stdout.strip() == "", f"worker-side imports loaded: {finished.stdout.strip()}"


def test_draining_worker_claims_no_new_job(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    worker = Worker(database, tmp_path / "work", service_runner=_complete)
    worker.drain()
    assert worker.run_once() is None
    assert queue.get(job_id).state == "intake"


def _child_run_until_primary(database_path: str, work_root: str, ready) -> None:
    database = Database(Path(database_path))

    def after_checkpoint(_run_id: str, phase: str) -> None:
        if phase == "primary":
            ready.set()
            threading.Event().wait()

    options = PipelineOptions(
        project_root=ROOT,
        no_hints=True,
        max_requests=100,
        max_generations=0,
        enabled_engines=("audd",),
        paid_scan_adapters={"audd": FakeAudD(SCRIPT)},
        shazam_http_client=FakeShazamHTTP(SCRIPT),
        paid_sleep=no_backoff,
        app_config=AppConfig(),
    )
    Worker(
        database,
        Path(work_root),
        worker_id="killed-worker",
        local_mode=True,
        options=options,
        # A live heartbeat holds the short lease while the child works; once it is killed the
        # lease lapses within a second and the replacement may claim.
        heartbeat_seconds=0.1,
        lease_seconds=1.0,
        checkpoint_after_commit=after_checkpoint,
    ).run_once()


def test_real_worker_kill_after_primary_resumes_with_zero_audd_requests(tmp_path: Path) -> None:
    database = _database(tmp_path)
    work = tmp_path / "work"
    queue = JobQueue(database, local_mode=True)
    job_id = queue.enqueue(LocalPath(AUDIO), DEEP_RECIPE, run_id="killed-primary")
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(
        target=_child_run_until_primary,
        args=(str(database.path), str(work), ready),
    )
    process.start()
    assert ready.wait(180), "child did not durably checkpoint primary"
    process.terminate()
    process.join(20)
    assert process.exitcode is not None
    time.sleep(1.5)
    with database.read() as connection:
        original_money = connection.execute(
            "SELECT usd_e6_reserved, usd_e6_spent FROM analysis_runs WHERE run_id='killed-primary'"
        ).fetchone()

    audd = FakeAudD(SCRIPT)
    resumed_checkpoints: list[str] = []
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
    resumed = Worker(
        database,
        work,
        worker_id="replacement-worker",
        local_mode=True,
        options=options,
        lease_seconds=30,
        checkpoint_after_commit=lambda _run_id, phase: resumed_checkpoints.append(phase),
    ).run_once()
    assert resumed is not None and resumed.id == job_id
    assert resumed.state in {"complete", "degraded"}
    assert audd.calls == 0
    assert resumed_checkpoints[0] == "hints"
    store = SQLiteCheckpointStore(database, work, mode="local", options=options)
    assert store.completed_phases("killed-primary") == frozenset(CHECKPOINT_PHASES)
    with database.read() as connection:
        paid = connection.execute(
            "SELECT COUNT(*) FROM provider_attempt_events "
            "WHERE run_id='killed-primary' AND provider='audd'"
        ).fetchone()[0]
        shazam = connection.execute(
            "SELECT COUNT(*) FROM provider_attempt_events WHERE run_id='killed-primary' "
            "AND provider='shazam' AND state='resolved'"
        ).fetchone()[0]
        run = connection.execute(
            "SELECT usd_e6_reserved, usd_e6_spent, claim_token FROM analysis_runs "
            "WHERE run_id='killed-primary'"
        ).fetchone()
    assert paid == 21
    # §2.3.5's breaker denominator: the secondary's Shazam attempts are in the hosted ledger too.
    assert shazam > 0
    assert (run["usd_e6_reserved"], run["usd_e6_spent"]) == tuple(original_money)
    assert run["usd_e6_reserved"] >= run["usd_e6_spent"] > 0
    assert run["claim_token"] is None  # settled runs accept no further writes
