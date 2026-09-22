"""Local mode: the web process only enqueues; a separate, supervised worker process runs the job.

Cycle 4a-ii review P0-1.  ``idea serve`` used to run every analysis on a thread inside the web
process.  These tests pin the replacement: submissions become durable queue rows, a worker claims
them through the fenced lease API and drives the unchanged job state machine, the progress page
reads the same state it always did, and the worker process is started, restarted and stopped by the
server — and never outlives it.  Everything is offline.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import httpx
import psutil
import pytest

from id_detector.recipes import DEEP_RECIPE
from id_detector.webapp.jobs import RUNNING, Job, JobContext, JobWaiting
from idea_web.application import ASSET_VERSION, STATIC_JS, create_app
from idea_web.jobs.local import (
    ABANDONED,
    LocalJobs,
    LocalWorker,
    LocalWorkerSupervisor,
    local_database,
    snapshot,
)

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8765"
MIX = "https://soundcloud.com/example/live-mix"
FAKES = "tests.idea_web.local_runner_fakes"


def _request(app, method: str, path: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def _until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            if predicate():
                return True
        time.sleep(0.05)
    return bool(predicate())


def _alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _worker(root: Path, runner, **kwargs) -> LocalWorker:
    return LocalWorker(local_database(root), root, runner, flush_seconds=0.05, **kwargs)


def test_submission_only_enqueues_and_the_worker_drives_the_same_job_state(tmp_path: Path) -> None:
    jobs = LocalJobs(tmp_path)
    ran: list[str] = []
    gate = threading.Event()

    def runner(ctx: JobContext) -> None:
        ran.append(ctx.target)
        ctx.progress("ingest", 1, 1, "Fixture Live Set")
        ctx.progress("recognise", 3, 10, "listening")
        gate.wait(timeout=10)
        ctx.log("done listening")

    job_id = jobs.submit(MIX, "free", acquire=True, known_tracklist="Artist - Title")
    queued = jobs.get(job_id)
    assert queued is not None and queued.status == "queued"
    assert queued.acquire is True and queued.known_tracklist == "Artist - Title"
    assert ran == []  # submitting ran nothing
    row = jobs.queue.get(job_id)
    assert row.state == "intake" and row.claim_token is None

    worker = _worker(tmp_path, runner)
    thread = threading.Thread(target=worker.run_once, daemon=True)
    thread.start()
    try:
        assert _until(lambda: jobs.get(job_id).windows_done == 3)
        running = jobs.get(job_id)
        assert running.status == "running" and running.resolved_title == "Fixture Live Set"
        status = running.status_dict()
        assert status["phase"] == "recognise" and status["windows_total"] == 10
        assert status["terminal"] is False and 0 <= status["progress_pct"] < 100
    finally:
        gate.set()
        thread.join(timeout=10)
    assert ran == [MIX]
    done = jobs.get(job_id)
    assert done.status == "succeeded" and done.status_dict()["terminal"] is True
    assert any("done listening" in line for line in done.log)
    assert jobs.queue.get(job_id).state == "complete"
    # The page reads exactly the fields the in-memory job always exposed.
    reference = Job(
        id="a" * 32, target=MIX, display=MIX, profile=None, acquire=False, build_index=False
    )
    assert set(done.status_dict()) == set(reference.status_dict())


def test_a_local_worker_restart_mid_recognition_carries_the_real_bar_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement attempt restores elapsed work from the durable queue row."""

    now = [10_000.0]
    jobs = LocalJobs(tmp_path, clock=lambda: now[0])
    job_id = jobs.submit(MIX)
    first_worker = _worker(tmp_path, lambda context: None, clock=lambda: now[0])
    row = first_worker.queue.claim("first-worker", lease_seconds=300.0)
    assert row is not None and row.id == job_id and row.attempt == 1
    job = Job(
        id=job_id,
        target=MIX,
        display="Fixture mix",
        profile="free",
        acquire=False,
        build_index=False,
        created_at=9_990.0,
        started_at=10_000.0,
        status=RUNNING,
        phase="recognise",
        phase_started_at=10_027.0,
        phase_seconds={"intake": 2.0, "ingest": 20.0, "decode": 3.0, "windows": 2.0},
        windows_done=135,
        windows_total=450,
        run_id=row.run_id,
    )
    now[0] = 10_207.0  # three minutes into recognition
    before = job.progress_percent(now[0])
    assert before > 0
    assert first_worker.queue.update_progress(
        row.id,
        row.token,
        {"local": snapshot(job)},
        lease_seconds=300.0,
    )
    first_worker.queue.fail(row, row.token, "simulated worker loss")
    persisted = jobs.queue.get(job_id)
    assert persisted.attempt == 1 and persisted.claim_token is None

    observed: dict[str, float | int] = {}

    def replacement(context: JobContext) -> None:
        restarted = context._job
        observed["carried"] = restarted.carried_seconds
        observed["before"] = restarted.progress_percent(now[0])
        observed["windows_done"] = restarted.windows_done
        observed["windows_total"] = restarted.windows_total
        now[0] += 17.0
        context.progress("intake", 0, 1, "resuming")
        observed["after"] = restarted.progress_percent(now[0])

    monkeypatch.setattr("id_detector.webapp.jobs.time.time", lambda: now[0])
    replacement_worker = _worker(tmp_path, replacement, clock=lambda: now[0])
    assert replacement_worker.run_once() == job_id

    assert observed["carried"] == pytest.approx(207.0)
    assert observed["before"] >= before and observed["before"] > 0
    assert observed["after"] >= observed["before"]
    assert (observed["windows_done"], observed["windows_total"]) == (135, 450)
    assert jobs.queue.get(job_id).attempt == 2


def test_cancelling_a_queued_job_is_immediate_and_it_never_runs(tmp_path: Path) -> None:
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(MIX)
    assert jobs.cancel(job_id) is True
    job = jobs.get(job_id)
    assert job.status == "cancelled" and "cancelled before it started" in job.log[-1]
    assert jobs.queue.get(job_id).state == "cancelled"
    ran: list[int] = []
    assert _worker(tmp_path, lambda ctx: ran.append(1)).run_once() is None
    assert ran == [] and jobs.cancel(job_id) is False


def test_cancelling_a_running_job_stops_it_at_the_next_progress_tick(tmp_path: Path) -> None:
    jobs = LocalJobs(tmp_path)

    def runner(ctx: JobContext) -> None:
        while True:
            ctx.progress("recognise", 1, 100, "listening")
            time.sleep(0.02)

    job_id = jobs.submit(MIX)
    thread = threading.Thread(target=_worker(tmp_path, runner).run_once, daemon=True)
    thread.start()
    assert _until(lambda: jobs.get(job_id).status == "running")
    assert jobs.cancel(job_id) is True
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert jobs.get(job_id).status == "cancelled"
    assert jobs.queue.get(job_id).state == "cancelled"


def test_failure_waiting_and_dismissal_read_as_they_always_did(tmp_path: Path) -> None:
    jobs = LocalJobs(tmp_path)

    def boom(ctx: JobContext) -> None:
        ctx.progress("decode", 0, 1, "preparing")
        raise RuntimeError("decoder exploded")

    failed_id = jobs.submit(MIX)
    assert _worker(tmp_path, boom).run_once() == failed_id
    failed = jobs.get(failed_id)
    assert failed.status == "failed" and "decoder exploded" in failed.error
    assert failed.failed_phase == "decode" and jobs.queue.get(failed_id).state == "failed"

    def paused(ctx: JobContext) -> None:
        raise JobWaiting("waiting: the free recognition service is paused")

    waiting_id = jobs.submit(MIX)
    assert _worker(tmp_path, paused).run_once() == waiting_id
    waiting = jobs.get(waiting_id)
    assert waiting.status == "waiting" and waiting.status_dict()["terminal"] is True
    assert jobs.queue.get(waiting_id).state == "provider_unavailable"

    assert {job.id for job in jobs.recent()} == {failed_id, waiting_id}
    assert jobs.dismiss(failed_id) is True
    assert jobs.get(failed_id) is None
    assert [job.id for job in jobs.recent()] == [waiting_id]
    running_id = jobs.submit(MIX)
    assert jobs.dismiss(running_id) is False  # work in flight is never dismissed


def test_the_web_app_enqueues_reads_cancels_and_dismisses_without_running_anything(
    tmp_path: Path,
) -> None:
    jobs = LocalJobs(tmp_path)
    app = create_app(tmp_path, jobs=jobs)
    token = _request(app, "GET", "/csrf").json()["token"]
    created = _request(
        app,
        "POST",
        "/analyse",
        json={"url": MIX, "profile": "max_accuracy", "csrf_token": token},
    )
    assert created.status_code == 200
    job_id = created.json()["id"]
    row = jobs.queue.get(job_id)
    assert row.state == "intake" and row.claim_token is None
    assert row.recipe.recipe_id == DEEP_RECIPE.recipe_id
    assert not any(thread.name == "webapp-jobs" for thread in threading.enumerate())

    page = _request(app, "GET", f"/jobs/{job_id}")
    assert page.status_code == 200 and f'src="/static/app.{ASSET_VERSION}.js"' in page.text
    assert "setTimeout(tick, 2500)" in STATIC_JS.decode()  # the page script is static (4a-iii)
    assert _request(app, "GET", f"/jobs/{job_id}/status").json()["status"] == "queued"
    assert f'data-job="{job_id}"' in _request(app, "GET", "/").text

    cancelled = _request(app, "POST", f"/jobs/{job_id}/cancel", headers={"X-CSRF-Token": token})
    assert cancelled.status_code == 200 and cancelled.json() == {"cancelled": True}
    assert _request(app, "GET", f"/jobs/{job_id}/status").json()["status"] == "cancelled"
    dismissed = _request(app, "POST", f"/jobs/{job_id}/dismiss", data={"csrf_token": token})
    assert dismissed.status_code == 303 and dismissed.headers["location"] == "/"
    assert _request(app, "GET", f"/jobs/{job_id}").status_code == 404

    refused = _request(
        app, "POST", "/analyse", json={"url": "file:///C:/nowhere.wav", "csrf_token": token}
    )
    assert refused.status_code == 400 and "error" in refused.json()


def test_the_web_process_never_imports_the_pipeline_runner() -> None:
    code = textwrap.dedent(
        """
        import sys
        import idea_web.application, idea_web.server, idea_web.truth_review
        from idea_web.jobs.local import LocalJobs, LocalWorkerSupervisor
        assert "id_detector.webapp.runner" not in sys.modules, "web imported the runner"
        print("ok")
        """
    )
    finished = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=120
    )
    assert finished.returncode == 0, finished.stderr
    assert finished.stdout.strip() == "ok"


def test_work_left_in_flight_by_a_dead_server_is_stopped_not_resumed(tmp_path: Path) -> None:
    jobs = LocalJobs(tmp_path)
    queued_id = jobs.submit(MIX)
    claimed_id = jobs.submit(MIX)
    # A worker claims the second job and dies without settling it.
    dead = _worker(tmp_path, lambda ctx: None)
    first_claim = dead.queue.claim("dead-worker", lease_seconds=0.3)
    assert first_claim is not None and first_claim.id == queued_id
    dead.queue.fail(first_claim, first_claim.token, "simulated")  # back to the queue, unclaimed
    abandoned = dead.queue.claim("dead-worker", lease_seconds=0.3)
    assert abandoned is not None

    restarted = LocalJobs(tmp_path)
    assert restarted.cancel_all(ABANDONED) == 2
    time.sleep(0.4)  # the dead claim's lease expires
    ran: list[int] = []
    worker = _worker(tmp_path, lambda ctx: ran.append(1))
    while worker.run_once() is not None:
        pass
    assert ran == []
    assert {restarted.get(queued_id).status, restarted.get(claimed_id).status} == {"cancelled"}


def test_idea_serve_supervises_a_separate_worker_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    jobs = LocalJobs(tmp_path)
    supervisor = LocalWorkerSupervisor(
        tmp_path,
        config_path=tmp_path / "idea.toml",
        jobs=jobs,
        runner_spec=f"{FAKES}:slow_runner",
        restart_delay_seconds=0.2,
    )
    pids: list[int] = []
    assert supervisor.start() is True
    try:
        first = supervisor.pid
        assert first is not None and first != os.getpid()
        pids.append(first)
        job_id = jobs.submit(MIX)
        assert _until(lambda: jobs.get(job_id).status == "succeeded", timeout=90)
        assert any("fake analysis finished" in line for line in jobs.get(job_id).log)

        # One work folder has one worker: a second server leaves supervision to the first.
        second_server = LocalWorkerSupervisor(
            tmp_path, config_path=tmp_path / "idea.toml", jobs=LocalJobs(tmp_path)
        )
        assert second_server.start() is False

        psutil.Process(first).kill()  # the worker dies unexpectedly
        assert _until(lambda: supervisor.pid not in (None, first), timeout=60)
        replacement = supervisor.pid
        assert replacement is not None
        pids.append(replacement)
        assert supervisor.restarts >= 1
        next_id = jobs.submit(MIX)
        assert _until(lambda: jobs.get(next_id).status == "succeeded", timeout=90)
    finally:
        supervisor.stop()
    assert _until(lambda: not any(_alive(pid) for pid in pids), timeout=20)
    assert supervisor.owner is False


def test_the_worker_never_outlives_a_server_that_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    script = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        from idea_web.jobs.local import LocalJobs, LocalWorkerSupervisor
        root = Path(sys.argv[1])
        supervisor = LocalWorkerSupervisor(
            root, config_path=root / "idea.toml", jobs=LocalJobs(root),
            runner_spec="{FAKES}:forever_runner",
        )
        assert supervisor.start()
        print(supervisor.pid, flush=True)
        time.sleep(300)
        """
    )
    server = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdout=subprocess.PIPE,
        text=True,
        cwd=ROOT,
    )
    worker_pid: int | None = None
    try:
        assert server.stdout is not None
        worker_pid = int(server.stdout.readline().strip())
        assert _alive(worker_pid)
        jobs = LocalJobs(tmp_path)
        job_id = jobs.submit(MIX)
        assert _until(lambda: jobs.get(job_id).status == "running", timeout=60)
        server.kill()  # no finally blocks, no clean shutdown: the window was closed
        server.wait(timeout=15)
        assert _until(lambda: not _alive(worker_pid), timeout=20), "orphaned worker process"
    finally:
        if server.poll() is None:
            server.kill()
        if worker_pid is not None and _alive(worker_pid):
            with contextlib.suppress(psutil.Error):
                psutil.Process(worker_pid).kill()
