"""Stage 10 — browser-driven web app: job manager state machine and server routes.

Every test here is network-free and deterministic: the pipeline is replaced by a fake runner, and
each test tears its job manager (and any background server) down with a bounded join so no worker
thread or process can leak.  A single ``@pytest.mark.live`` end-to-end that would run a real analyse
is deliberately *not* included; the real pipeline is exercised by the CLI stages, so here we prove
only the web plumbing.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
import pytest

from id_detector.present.server import (
    _home_html,
    _job_page_html,
    make_server,
    serve_in_background,
)
from id_detector.webapp.jobs import (
    Job,
    JobContext,
    JobManager,
    TargetValidationError,
    validate_target,
)
from scripts.audit_fixtures import _HANDLE, _ID_FIELD

TIMEOUT = httpx.Timeout(5.0)
CLEAN_URL = "https://soundcloud.com/example/live-mix"


# --------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------
def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _no_worker_thread_alive() -> bool:
    return not any(t.name == "webapp-jobs" and t.is_alive() for t in threading.enumerate())


def _fast_runner_factory(work_root: Path):
    """A runner that reports every phase, ends succeeded, and writes a real result file."""

    def runner(ctx: JobContext) -> None:
        ctx.progress("ingest", 1, 1, "resolved")
        ctx.progress("decode", 1, 1, "decoded")
        for done in range(1, 4):
            ctx.progress("recognise", done, 3, "recognising windows")
        ctx.progress("fuse", 1, 1, "fused")
        index = work_root / "src" / "med" / "present" / "index.html"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_bytes(b"<!doctype html><title>result</title>")
        ctx.progress("present", 1, 1, "page ready")
        ctx.set_result(index)

    return runner


# --------------------------------------------------------------------------------------------------
# validate_target
# --------------------------------------------------------------------------------------------------
def test_validate_target_accepts_http_and_local_file(tmp_path: Path) -> None:
    assert validate_target(" https://soundcloud.com/a/b ") == "https://soundcloud.com/a/b"
    local = tmp_path / "mix.wav"
    local.write_bytes(b"x")
    assert validate_target(str(local)) == str(local)


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "ftp://host/x", "javascript:alert(1)", "data:text/html,x", "https://"],
)
def test_validate_target_rejects_non_http_or_missing(bad: str) -> None:
    with pytest.raises(TargetValidationError):
        validate_target(bad)


def test_validate_target_rejects_credential_urls() -> None:
    with pytest.raises(TargetValidationError):
        validate_target("https://user:secret@example.com/mix")


# --------------------------------------------------------------------------------------------------
# Job manager state machine
# --------------------------------------------------------------------------------------------------
def test_job_lifecycle_queued_running_succeeded(tmp_path: Path) -> None:
    manager = JobManager(tmp_path, _fast_runner_factory(tmp_path))
    try:
        job_id = manager.submit(CLEAN_URL, "free")
        assert _wait_until(lambda: manager.get(job_id).status == "succeeded")
        job = manager.get(job_id)
        assert job.phase == "done"
        assert job.windows_done == 3 and job.windows_total == 3
        assert job.result_path == "src/med/present/index.html"
        assert job.status_dict()["result_url"] == "/src/med/present/index.html"
        assert job.status_dict()["terminal"] is True
    finally:
        manager.shutdown()
    assert _wait_until(_no_worker_thread_alive)


def test_job_failure_is_recorded_not_raised(tmp_path: Path) -> None:
    def boom(ctx: JobContext) -> None:
        ctx.progress("ingest", 1, 1, "start")
        raise RuntimeError("decode blew up")

    manager = JobManager(tmp_path, boom)
    try:
        job_id = manager.submit(CLEAN_URL, "free")
        assert _wait_until(lambda: manager.get(job_id).status == "failed")
        job = manager.get(job_id)
        assert job.error is not None and "decode blew up" in job.error
    finally:
        manager.shutdown()
    assert _wait_until(_no_worker_thread_alive)


def test_jobs_run_one_at_a_time(tmp_path: Path) -> None:
    gate = threading.Event()
    first_running = threading.Event()

    def gated(ctx: JobContext) -> None:
        first_running.set()
        gate.wait(timeout=5)
        ctx.progress("present", 1, 1, "done")

    manager = JobManager(tmp_path, gated)
    try:
        first = manager.submit(CLEAN_URL, "free")
        assert first_running.wait(timeout=5)
        second = manager.submit("https://soundcloud.com/example/other", "free")
        # While the first job is gated, the second must stay queued (single worker).
        assert manager.get(first).status == "running"
        assert manager.get(second).status == "queued"
        gate.set()
        assert _wait_until(lambda: manager.get(second).status == "succeeded")
        assert manager.get(first).status == "succeeded"
    finally:
        gate.set()
        manager.shutdown()
    assert _wait_until(_no_worker_thread_alive)


def test_cancel_queued_job(tmp_path: Path) -> None:
    gate = threading.Event()
    running = threading.Event()

    def gated(ctx: JobContext) -> None:
        running.set()
        gate.wait(timeout=5)

    manager = JobManager(tmp_path, gated)
    try:
        first = manager.submit(CLEAN_URL, "free")
        assert running.wait(timeout=5)
        second = manager.submit("https://soundcloud.com/example/other", "free")
        assert manager.cancel(second) is True
        assert manager.get(second).status == "cancelled"
        gate.set()
        assert _wait_until(lambda: manager.get(first).status == "succeeded")
    finally:
        gate.set()
        manager.shutdown()
    assert _wait_until(_no_worker_thread_alive)


def test_cancel_running_job_stops_at_next_progress_tick(tmp_path: Path) -> None:
    running = threading.Event()

    def spin(ctx: JobContext) -> None:
        running.set()
        for done in range(1, 1000):
            ctx.progress("recognise", done, 1000, "recognising windows")
            time.sleep(0.005)

    manager = JobManager(tmp_path, spin)
    try:
        job_id = manager.submit(CLEAN_URL, "free")
        assert running.wait(timeout=5)
        assert _wait_until(lambda: manager.get(job_id).status == "running")
        assert manager.cancel(job_id) is True
        assert _wait_until(lambda: manager.get(job_id).status == "cancelled")
    finally:
        manager.shutdown()
    assert _wait_until(_no_worker_thread_alive)


def test_progress_updates_are_visible_and_secret_free(tmp_path: Path) -> None:
    def runner(ctx: JobContext) -> None:
        ctx.progress("recognise", 2, 5, "recognising windows")
        # A log line that happens to carry a credential URL must be redacted.
        ctx.log("fetching https://user:hunter2@cdn.example.com/audio?token=abcdef")

    manager = JobManager(tmp_path, runner)
    try:
        job_id = manager.submit(CLEAN_URL, "free")
        assert _wait_until(lambda: manager.get(job_id).status == "succeeded")
        status = manager.get(job_id).status_dict()
        joined = "\n".join(status["log"])
        assert "hunter2" not in joined and "abcdef" not in joined
        assert "user:" not in joined
        # Nothing secret leaks into the flat status payload either.
        assert "hunter2" not in str(status)
    finally:
        manager.shutdown()
    assert _wait_until(_no_worker_thread_alive)


def test_submit_rejects_bad_target_before_creating_a_job(tmp_path: Path) -> None:
    manager = JobManager(tmp_path, _fast_runner_factory(tmp_path))
    try:
        with pytest.raises(TargetValidationError):
            manager.submit("ftp://nope/x")
        assert manager.recent() == []
    finally:
        manager.shutdown()


# --------------------------------------------------------------------------------------------------
# Page privacy (reuse the fixture-audit handle / identifier patterns)
# --------------------------------------------------------------------------------------------------
def _sample_job() -> Job:
    return Job(
        id="a" * 32,
        target=CLEAN_URL,
        display=CLEAN_URL,
        profile="free",
        acquire=True,
        build_index=True,
        status="succeeded",
        phase="done",
        result_path="k/m/present/index.html",
    )


def test_home_and_job_pages_contain_no_usernames_or_identifier_fields() -> None:
    import re

    home = _home_html([], [_sample_job()]).decode("utf-8")
    job = _job_page_html(_sample_job()).decode("utf-8")
    for page in (home, job):
        without_style = re.sub(r"<style>.*?</style>", "", page, flags=re.DOTALL)
        assert _HANDLE.search(without_style) is None
        assert _ID_FIELD.search(page) is None
    assert "Analyse" in home and "Recent analyses" in home


# --------------------------------------------------------------------------------------------------
# Server routes (loopback-only, fake runner)
# --------------------------------------------------------------------------------------------------
def test_make_server_only_binds_loopback(tmp_path: Path) -> None:
    manager = JobManager(tmp_path, _fast_runner_factory(tmp_path))
    try:
        with pytest.raises(ValueError, match="loopback"):
            make_server(tmp_path, host="0.0.0.0", port=0, job_manager=manager)
    finally:
        manager.shutdown()


def test_home_renders_form_when_analyse_enabled(tmp_path: Path) -> None:
    manager = JobManager(tmp_path, _fast_runner_factory(tmp_path))
    running = serve_in_background(tmp_path, port=0, job_manager=manager)
    try:
        page = httpx.get(running.base_url + "/", timeout=TIMEOUT)
        assert page.status_code == 200
        assert 'action="/analyse"' in page.text
        assert "max_accuracy" in page.text
    finally:
        running.shutdown()
        manager.shutdown()
    assert not running.thread.is_alive()
    assert _wait_until(_no_worker_thread_alive)


def test_read_only_server_has_no_analyse_routes(tmp_path: Path) -> None:
    # No job manager => the Stage 7 read-only index and a 404 for /analyse.
    running = serve_in_background(tmp_path, port=0)
    try:
        home = httpx.get(running.base_url + "/", timeout=TIMEOUT)
        assert "Analysed sets" in home.text
        rejected = httpx.post(
            running.base_url + "/analyse", json={"url": CLEAN_URL}, timeout=TIMEOUT
        )
        assert rejected.status_code == 404
    finally:
        running.shutdown()


def test_post_analyse_json_creates_job_and_status_shape(tmp_path: Path) -> None:
    manager = JobManager(tmp_path, _fast_runner_factory(tmp_path))
    running = serve_in_background(tmp_path, port=0, job_manager=manager)
    try:
        created = httpx.post(
            running.base_url + "/analyse",
            json={"url": CLEAN_URL, "profile": "free", "acquire": True},
            timeout=TIMEOUT,
        )
        assert created.status_code == 200
        job_id = created.json()["id"]
        assert _wait_until(
            lambda: (
                httpx.get(f"{running.base_url}/jobs/{job_id}/status", timeout=TIMEOUT).json()[
                    "status"
                ]
                == "succeeded"
            )
        )
        status = httpx.get(f"{running.base_url}/jobs/{job_id}/status", timeout=TIMEOUT).json()
        for key in ("status", "phase", "windows_done", "windows_total", "eta_seconds", "log"):
            assert key in status
        assert status["result_url"] == "/src/med/present/index.html"
        # The progress page itself renders and polls this job.
        page = httpx.get(f"{running.base_url}/jobs/{job_id}", timeout=TIMEOUT)
        assert page.status_code == 200
        assert job_id in page.text
    finally:
        running.shutdown()
        manager.shutdown()
    assert _wait_until(_no_worker_thread_alive)


def test_post_analyse_form_redirects_to_job_page(tmp_path: Path) -> None:
    manager = JobManager(tmp_path, _fast_runner_factory(tmp_path))
    running = serve_in_background(tmp_path, port=0, job_manager=manager)
    try:
        resp = httpx.post(
            running.base_url + "/analyse",
            data={"url": CLEAN_URL, "profile": "free"},
            timeout=TIMEOUT,
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["Location"]
        assert location.startswith("/jobs/")
        job_id = location.rsplit("/", 1)[1]
        assert manager.get(job_id) is not None
    finally:
        running.shutdown()
        manager.shutdown()
    assert _wait_until(_no_worker_thread_alive)


def test_post_analyse_rejects_invalid_url(tmp_path: Path) -> None:
    manager = JobManager(tmp_path, _fast_runner_factory(tmp_path))
    running = serve_in_background(tmp_path, port=0, job_manager=manager)
    try:
        resp = httpx.post(
            running.base_url + "/analyse", json={"url": "ftp://nope/x"}, timeout=TIMEOUT
        )
        assert resp.status_code == 400
        assert "error" in resp.json()
        bad_profile = httpx.post(
            running.base_url + "/analyse",
            json={"url": CLEAN_URL, "profile": "made_up"},
            timeout=TIMEOUT,
        )
        assert bad_profile.status_code == 400
    finally:
        running.shutdown()
        manager.shutdown()
    assert _wait_until(_no_worker_thread_alive)


def test_cancel_route(tmp_path: Path) -> None:
    gate = threading.Event()
    running_event = threading.Event()

    def gated(ctx: JobContext) -> None:
        running_event.set()
        gate.wait(timeout=5)

    manager = JobManager(tmp_path, gated)
    running = serve_in_background(tmp_path, port=0, job_manager=manager)
    try:
        job_id = manager.submit(CLEAN_URL, "free")
        assert running_event.wait(timeout=5)
        resp = httpx.post(f"{running.base_url}/jobs/{job_id}/cancel", timeout=TIMEOUT)
        assert resp.status_code == 200
        assert resp.json()["cancelled"] is True
        unknown = httpx.post(f"{running.base_url}/jobs/{'f' * 32}/cancel", timeout=TIMEOUT)
        assert unknown.status_code == 404
    finally:
        gate.set()
        running.shutdown()
        manager.shutdown()
    assert _wait_until(_no_worker_thread_alive)
