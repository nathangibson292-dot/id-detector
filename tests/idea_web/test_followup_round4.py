"""Round-4 class-closing design pass of the money/resume follow-up (round-3 review, FIX_FIRST).

Every test here was run against the code before this pass and failed there; see
``docs/reviews/followup-money-resume.md``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from id_detector.compat import LOCAL_OWNER_SCOPE
from id_detector.io import native_path
from id_detector.providers.base import AppConfig
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE
from id_detector.service import LocalPath, PipelineOptions
from id_detector.webapp.jobs import Job
from idea_web.database import Database
from idea_web.jobs.local import LocalJobs, LocalWorker, local_database, snapshot
from idea_web.jobs.worker import JobQueue, Worker
from tests.fakes.providers import FakeAudD, FakeShazamHTTP
from tests.idea_web.local_runner_fakes import fake_pipeline_runner
from tests.idea_web.test_followup_queue_money import _audd_rows, _deep_options
from tests.idea_web.test_followup_review_fixes import UNIT, _attempt_events, _invocations
from tests.idea_web.test_followup_round2 import (
    _child_worker_dies,
    _dispatched_queries,
    _env,
    _expire,
    _nothing_runs,
)
from tests.idea_web.test_worker import AUDIO, ROOT, SCRIPT, _database
from tests.test_followup_money_resume import _KillShazamAfterSend

# ------------------------------------------------------------------------ item 1: one run id


def test_an_upgraded_0001_local_row_gets_one_run_id_and_three_deaths_still_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _env(tmp_path, monkeypatch)
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
            created_at=time.time(),
        )
    )
    legacy.pop("run_id", None)  # a snapshot written before durable run ids existed
    now = time.time()
    with database.write() as connection:
        connection.execute(
            "INSERT INTO jobs(id, run_id, target, recipe_id, state, tenant_scope, progress, "
            "created_at, updated_at) VALUES (?, NULL, ?, ?, 'intake', ?, ?, ?, ?)",
            (
                job_id,
                json.dumps({"kind": "local", "path": str(AUDIO)}),
                DEEP_RECIPE.recipe_id,
                LOCAL_OWNER_SCOPE,
                json.dumps({"local": legacy}),
                now,
                now,
            ),
        )

    jobs = LocalJobs(tmp_path)  # the next start migrates the live 0001 database
    row = jobs.queue.get(job_id)
    assert row.run_id, "the upgrade left the normalised run id NULL"
    assert row.progress["local"]["run_id"] == row.run_id  # one id, in both places

    for _ in range(3):
        _child_worker_dies(tmp_path, config, kill_on=1)
        _expire(jobs, job_id)
    LocalWorker(local_database(tmp_path), tmp_path, _nothing_runs, flush_seconds=0.05).run_once()
    assert jobs.queue.get(job_id).state == "dead_letter"
    run_id, dispatched = _dispatched_queries(tmp_path)
    assert run_id == row.run_id and dispatched
    (entry,) = _invocations(tmp_path, run_id)
    assert entry["status"] == "failed"
    assert entry["usd_e6_spent"] == len(dispatched) * UNIT


# ------------------------------------------------------------------ item 2: derived settlement

_DIE_AFTER_DEAD_LETTER_COMMIT = """
import os, sys
from pathlib import Path
from idea_web.jobs.local import LocalWorker, local_database

# The process dies the instant the dead-letter transaction has committed: the post-commit
# settlement callback never gets to run.
LocalWorker._settle_abandoned = lambda self, *args, **kwargs: os._exit(9)
root = Path(sys.argv[1])
LocalWorker(local_database(root), root, lambda ctx: None).run_once()
sys.exit(0)
"""


def test_a_death_right_after_the_dead_letter_commit_is_settled_once_by_the_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(str(AUDIO), "max_accuracy")
    _child_worker_dies(tmp_path, config, kill_on=3)
    _expire(jobs, job_id)
    with jobs.database.write() as connection:  # its attempts are already exhausted
        connection.execute("UPDATE jobs SET attempt=3 WHERE id=?", (job_id,))
    finished = subprocess.run(
        [sys.executable, "-c", _DIE_AFTER_DEAD_LETTER_COMMIT, str(tmp_path)],
        cwd=ROOT,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert finished.returncode == 9, finished.stderr[-3000:]
    assert jobs.queue.get(job_id).state == "dead_letter"
    run_id, dispatched = _dispatched_queries(tmp_path)
    assert _invocations(tmp_path, run_id) == []  # the settlement was lost with the process

    def next_start() -> None:
        worker = LocalWorker(local_database(tmp_path), tmp_path, _nothing_runs, flush_seconds=0.05)
        worker.stopped.set()  # start, then stop at once: only its startup work runs
        worker.run_forever(poll_seconds=0.01)

    next_start()
    (entry,) = _invocations(tmp_path, run_id)
    assert entry["status"] == "failed"
    assert entry["usd_e6_spent"] == len(dispatched) * UNIT
    next_start()  # idempotent: exactly one settlement, unchanged
    assert _invocations(tmp_path, run_id) == [entry]


# ----------------------------------------------------------- item 3: one dispatch admission


class _CancelBeforeAdmission:
    """Commits a cancellation on call ``on_call`` after the sweep's halt check, before admission."""

    def __init__(self, inner: FakeAudD, on_call: int, cancel, dispatched) -> None:
        self.inner = inner
        self.on_call = on_call
        self.cancel = cancel
        self.dispatched = dispatched
        self.calls = 0
        self.at_cancel: int | None = None

    async def recognize_clip(self, path: Path, on_attempt):
        self.calls += 1
        if self.calls == self.on_call:
            assert self.cancel()
            self.at_cancel = self.dispatched()
        return await self.inner.recognize_clip(path, on_attempt)


def test_a_cancel_committed_before_audd_admission_stops_the_hosted_request(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database, local_mode=True)
    run_id = "hosted-cancel-admission"
    job_id = queue.enqueue(LocalPath(AUDIO), DEEP_RECIPE, run_id=run_id)
    inner = FakeAudD(SCRIPT)
    adapter = _CancelBeforeAdmission(
        inner,
        3,
        cancel=lambda: queue.request_cancel(job_id),
        dispatched=lambda: _audd_rows(database, run_id, "dispatched"),
    )
    Worker(database, tmp_path / "work", local_mode=True, options=_deep_options(adapter)).run_once()
    assert adapter.at_cancel is not None
    assert _audd_rows(database, run_id, "dispatched") == adapter.at_cancel
    assert inner.calls == adapter.at_cancel, "a paid request left after the cancel committed"


def test_a_cancel_committed_before_audd_admission_stops_the_local_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(str(AUDIO), "max_accuracy")

    def dispatched() -> int:
        return sum(
            event["event"] == "dispatched" and event["provider"] == "audd"
            for event in _attempt_events(tmp_path)
        )

    inner = FakeAudD(SCRIPT)
    adapter = _CancelBeforeAdmission(
        inner, 3, cancel=lambda: jobs.queue.request_cancel(job_id), dispatched=dispatched
    )
    runner = fake_pipeline_runner(tmp_path, config, audd=adapter)
    # A long flush interval: the progress relay cannot notice the cancel in time. Only a
    # queue-aware admission check evaluated at the dispatch itself can stop the request.
    LocalWorker(local_database(tmp_path), tmp_path, runner, flush_seconds=30.0).run_once()
    assert adapter.at_cancel is not None
    assert dispatched() == adapter.at_cancel
    assert inner.calls == adapter.at_cancel, "a paid request left after the cancel committed"
    assert jobs.get(job_id).status == "cancelled"


# ------------------------------------------------------------- item 4: fenced settlement


def test_a_worker_whose_lease_was_reclaimed_writes_no_invocation_settlement(
    tmp_path: Path,
) -> None:
    now = [1_000.0]
    database = _database(tmp_path)
    work = tmp_path / "work"
    queue = JobQueue(database, local_mode=True, clock=lambda: now[0])
    run_id = "stale-settlement"
    queue.enqueue(LocalPath(AUDIO), FREE_RECIPE, run_id=run_id)
    stolen: list[object] = []

    def reclaim_before_settlement(_run_id: str, phase: str) -> None:
        if phase == "present":
            # The lease lapses and a replacement claims the job immediately before the terminal
            # settlement is written.
            now[0] += 600
            stolen.append(
                JobQueue(database, local_mode=True, clock=lambda: now[0]).claim(
                    "replacement", lease_seconds=6_000
                )
            )

    options = PipelineOptions(
        project_root=ROOT,
        no_hints=True,
        max_requests=100,
        max_generations=0,
        shazam_http_client=FakeShazamHTTP(SCRIPT),
        app_config=AppConfig(),
    )
    Worker(
        database,
        work,
        local_mode=True,
        options=options,
        clock=lambda: now[0],
        lease_seconds=30,
        heartbeat_seconds=1_000,
        checkpoint_after_commit=reclaim_before_settlement,
    ).run_once()
    assert stolen and stolen[0] is not None
    assert _invocations(work, run_id) == [], "a stale worker wrote the run's settlement"


# ------------------------------------------------- item 5: one Shazam identity, full recovery


def test_hosted_shazam_recovers_every_state_from_sqlite_with_store_and_journals_gone(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    work = tmp_path / "work"
    queue = JobQueue(database, local_mode=True)
    job_id = queue.enqueue(LocalPath(AUDIO), FREE_RECIPE, run_id="shazam-full-recovery")
    script = {"shazam": {"default": "match", "windows": {"0": "http_401"}}}

    def options(shazam) -> PipelineOptions:
        return PipelineOptions(
            project_root=ROOT,
            no_hints=True,
            max_requests=100,
            max_generations=0,
            shazam_http_client=shazam,
            app_config=AppConfig(),
        )

    killer = _KillShazamAfterSend(script, kill_on=4)
    Worker(database, work, local_mode=True, options=options(killer)).run_once()
    assert killer.killed_window is not None
    assert queue.get(job_id).state == "analysis"
    answered = {attempt["window"] for attempt in killer.attempts}
    assert answered, "the first pass received answers before it died"
    # Only SQLite survives: the per-media job store and every Shazam JSONL journal are gone.
    removed = 0
    for root, _dirs, files in os.walk(native_path(work)):
        for name in files:
            if name.startswith("jobs.sqlite") or ("shazam" in name and name.endswith(".jsonl")):
                os.unlink(os.path.join(root, name))
                removed += 1
    assert removed

    resumed = FakeShazamHTTP(script)
    Worker(database, work, local_mode=True, options=options(resumed)).run_once()
    requested = {attempt["window"] for attempt in resumed.attempts}
    resent = requested & (answered | {killer.killed_window})
    assert not resent, f"answered, refused or in-flight Shazam queries were sent again: {resent}"
