"""Local-mode durable jobs: the web-side client, the worker and its supervisor (cycle 4a-ii).

``idea serve`` never runs a pipeline (plan §4.2).  The web process validates a submission and
enqueues it in the 4b-i SQLite queue at ``<work root>/.idea/app.db``.  A separate worker *process*,
which ``idea serve`` starts, restarts if it dies and stops when the server stops, claims the job
through the queue's fenced lease API and runs the owner's local analysis runner
(:func:`id_detector.webapp.runner.make_pipeline_runner`: profiles, Deep scans, acquisition, the
reference index and a pasted tracklist) inside that process.

The job's progress state is the same :class:`id_detector.webapp.jobs.Job` the progress page has
always rendered.  The worker drives it with the unchanged in-memory state machine and publishes a
snapshot through the claim-fenced ``progress`` column every half second; the web process rebuilds
the object from that snapshot, so the page, its polling, cancellation and dismissal read exactly as
they did when the job lived in the server's memory.

Nothing in this module imports an HTTP framework, and the web-side half never imports the pipeline
runner: the worker resolves its runner only inside :func:`main`, in its own process.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import importlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from id_detector.io import redact_text
from id_detector.jobs import JobStoreLocked, ProcessLock
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE
from id_detector.service import LocalPath, PlatformUrl, RunResult, TargetRefused
from id_detector.webapp.jobs import (
    CANCELLED,
    FAILED,
    LOG_RING,
    QUEUED,
    SUCCEEDED,
    TERMINAL_STATES,
    WAITING,
    Job,
    JobManager,
    Runner,
    TargetValidationError,
    validate_target,
)
from idea_web.database import Database
from idea_web.jobs.worker import (
    ACTIVE_RUN_STATES,
    DEFAULT_LEASE_SECONDS,
    HEARTBEAT_SECONDS,
    JobQueue,
    StaleClaim,
)
from idea_web.jobs.worker import Job as QueueJob

LOCAL_DATABASE = Path(".idea") / "app.db"
SUPERVISOR_LOCK = Path(".idea") / "worker-supervisor.lock"
#: What a job still in flight says when ``idea serve`` stops (or stopped without warning).
ABANDONED = "stopped when ID'er was closed"
#: How often the worker publishes a changed progress snapshot (the page polls every 2.5 s).
FLUSH_SECONDS = 0.5
#: A worker told to stop gets this long to settle its job before the process exits regardless.
EXIT_GRACE_SECONDS = 30.0
_LOCAL = "local"
_SNAPSHOT_FIELDS = tuple(
    item.name
    for item in dataclasses.fields(Job)
    if item.name not in {"target", "log", "cancel_event"}
)
_TO_QUEUE = {
    SUCCEEDED: "complete",
    FAILED: "failed",
    CANCELLED: "cancelled",
    # Local mode stops a Shazam-paused job rather than retrying it; the page shows "waiting".
    WAITING: "provider_unavailable",
}
_FROM_QUEUE = {
    "complete": SUCCEEDED,
    "degraded": SUCCEEDED,
    "partial": SUCCEEDED,
    "cancelled": CANCELLED,
    "provider_unavailable": WAITING,
}


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def local_database(work_root: Path) -> Database:
    database = Database(Path(work_root).resolve() / LOCAL_DATABASE)
    database.migrate()
    return database


def snapshot(job: Job) -> dict[str, Any]:
    """The JSON-safe state of one job: every field the progress page and home cards read."""

    document = {name: getattr(job, name) for name in _SNAPSHOT_FIELDS}
    document["phase_seconds"] = dict(job.phase_seconds)
    document["log"] = list(job.log)
    return document


def _target_text(target: object) -> str:
    if isinstance(target, PlatformUrl):
        return target.url
    if isinstance(target, LocalPath):
        return str(target.path)
    raise TargetRefused("a local job is a web link or a local file")


def job_view(row: QueueJob) -> Job | None:
    """Rebuild the page's job object from a queue row; ``None`` for a dismissed or foreign row."""

    document = row.progress.get(_LOCAL)
    if not isinstance(document, dict) or row.progress.get("dismissed"):
        return None
    values = {name: document[name] for name in _SNAPSHOT_FIELDS if name in document}
    values["id"] = row.id
    try:
        job = Job(target=_target_text(row.target), **values)
    except (TypeError, TargetRefused):
        return None
    job.log = deque((str(line) for line in document.get("log") or ()), maxlen=LOG_RING)
    if row.state not in ACTIVE_RUN_STATES and job.status not in TERMINAL_STATES:
        # The queue settled this job without a final snapshot from the worker (a dead letter).
        job.status = _FROM_QUEUE.get(row.state, FAILED)
        job.finished_at = job.finished_at or row.updated_at
        if job.status == FAILED:
            job.failed_phase = job.failed_phase or job.phase
            reason = row.dead_letter_reason or "the local analysis worker stopped"
            job.error = job.error or redact_text(reason)[:500]
        job.phase = "done" if job.status == SUCCEEDED else job.status
    return job


def _mark_stopped(job: Job, message: str, now: float) -> Job:
    job.status = CANCELLED
    job.phase = CANCELLED
    job.finished_at = now
    job.log.append(f"{_stamp()} {message}")
    return job


class LocalJobs:
    """The web process's view of the local queue: enqueue, read, cancel, dismiss — nothing else."""

    def __init__(
        self,
        work_root: Path,
        *,
        database: Database | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.work_root = Path(work_root)
        self.database = database or local_database(self.work_root)
        self.queue = JobQueue(self.database, local_mode=True, clock=clock)
        self.clock = clock
        #: The home page lists jobs from this server session (and anything still in flight), as
        #: it did when jobs lived in memory; earlier finished jobs stay reachable by their URL.
        self.since = clock()

    def submit(
        self,
        target: str,
        profile: str | None = None,
        *,
        acquire: bool = False,
        build_index: bool = False,
        known_tracklist: str | None = None,
    ) -> str:
        validated = validate_target(target)
        try:
            is_file = Path(validated).is_file()
        except (OSError, ValueError):
            is_file = False
        job_id = uuid.uuid4().hex
        job = Job(
            id=job_id,
            target=validated,
            display=redact_text(validated),
            profile=profile,
            acquire=acquire,
            build_index=build_index,
            known_tracklist=known_tracklist,
            created_at=self.clock(),
        )
        try:
            queue_target = LocalPath(Path(validated)) if is_file else PlatformUrl(validated)
            self.queue.enqueue(
                queue_target,
                DEEP_RECIPE if profile == "max_accuracy" else FREE_RECIPE,
                job_id=job_id,
                progress={_LOCAL: snapshot(job)},
            )
        except TargetRefused as exc:
            raise TargetValidationError(str(exc)) from None
        return job_id

    def get(self, job_id: str) -> Job | None:
        try:
            return job_view(self.queue.get(job_id))
        except (KeyError, ValueError, TargetRefused):
            return None

    def recent(self, limit: int = 25) -> list[Job]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE COALESCE(json_extract(progress, '$.dismissed'), 0) = 0 "
                "AND (created_at >= ? OR (state IN ('intake','waiting','analysis') "
                "AND cancel_requested = 0)) ORDER BY created_at DESC, id DESC LIMIT ?",
                (self.since, limit),
            ).fetchall()
        jobs = [self.get(row["id"]) for row in rows]
        return [job for job in jobs if job is not None]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status in TERMINAL_STATES:
            return False
        if job.status == QUEUED:
            stopped = _mark_stopped(job, "cancelled before it started", self.clock())
            if self.queue.cancel_unclaimed(job_id, {_LOCAL: snapshot(stopped)}):
                return True
        # A worker holds it: the worker sees the request within half a second and stops the run
        # at its next progress tick, exactly as the in-memory cancel flag did.
        return self.queue.request_cancel(job_id)

    def dismiss(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status not in TERMINAL_STATES:
            return False
        return self.queue.dismiss(job_id)

    def cancel_all(self, message: str = ABANDONED) -> int:
        """Stop every job still in flight — when the server stops, or starts after it died."""

        with self.database.read() as connection:
            ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM jobs WHERE state IN ('intake','waiting','analysis')"
                ).fetchall()
            ]
        stopped = 0
        for job_id in ids:
            job = self.get(job_id)
            if job is not None and job.status == QUEUED:
                document = snapshot(_mark_stopped(job, message, self.clock()))
                if self.queue.cancel_unclaimed(job_id, {_LOCAL: document}):
                    stopped += 1
                    continue
            if self.queue.request_cancel(job_id):
                stopped += 1
        return stopped


class _ProgressPublisher:
    """Publishes the running job's snapshot through the claim fence and relays cancellation."""

    def __init__(
        self,
        queue: JobQueue,
        job_id: str,
        claim_token: str,
        job: Job,
        lock: Any,
        *,
        interval: float,
        heartbeat_seconds: float,
        lease_seconds: float,
    ) -> None:
        self.queue = queue
        self.job_id = job_id
        self.claim_token = claim_token
        self.job = job
        self.lock = lock
        self.interval = interval
        self.heartbeat_seconds = heartbeat_seconds
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"idea-local-progress-{job_id}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        last: str | None = None
        last_write = 0.0
        while not self._stop.wait(self.interval):
            try:
                # True when the owner cancelled, and also when this claim was lost or its lease
                # expired: either way the run must stop, and it stops at its next progress tick.
                if self.queue.cancel_requested(self.job_id, self.claim_token):
                    self.job.cancel_event.set()
                with self.lock:
                    self.job.progress_percent()
                    document = snapshot(self.job)
                encoded = json.dumps(document, sort_keys=True, default=str)
                now = time.monotonic()
                if encoded != last or now - last_write >= self.heartbeat_seconds:
                    if not self.queue.update_progress(
                        self.job_id,
                        self.claim_token,
                        {_LOCAL: document},
                        lease_seconds=self.lease_seconds,
                    ):
                        self.job.cancel_event.set()
                    last, last_write = encoded, now
            except Exception:  # noqa: BLE001 - a busy database must not end the job's heartbeat
                continue


class LocalWorker:
    """One local queue consumer.  It imports no HTTP framework and serves no requests."""

    def __init__(
        self,
        database: Database,
        work_root: Path,
        runner: Runner,
        *,
        worker_id: str | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        flush_seconds: float = FLUSH_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.work_root = Path(work_root)
        self.runner = runner
        self.worker_id = worker_id or f"local-worker-{uuid.uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.flush_seconds = flush_seconds
        self.clock = clock
        self.queue = JobQueue(database, local_mode=True, clock=clock)
        self.stopped = threading.Event()
        self._current_lock = threading.Lock()
        self._current: Job | None = None

    def stop(self) -> None:
        """Claim nothing more and cancel the job in hand (it settles as ``cancelled``)."""

        self.stopped.set()
        with self._current_lock:
            current = self._current
        if current is not None:
            current.cancel_event.set()

    def run_forever(self, *, poll_seconds: float = 0.5) -> None:
        while not self.stopped.is_set():
            try:
                claimed = self.run_once()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:  # noqa: BLE001 - one bad row or a busy database never ends the loop
                claimed = None
            if claimed is None:
                self.stopped.wait(poll_seconds)

    def run_once(self) -> str | None:
        if self.stopped.is_set():
            return None
        row = self.queue.claim(self.worker_id, lease_seconds=self.lease_seconds)
        if row is None:
            return None
        token = row.token
        try:
            job = job_view(row)
            if job is None:
                self.queue.fail(row, token, "not a local ID'er job")
                return row.id
            if row.cancel_requested or self.stopped.is_set():
                message = "cancelled before it started" if job.started_at is None else ABANDONED
                self._settle(row.id, token, _mark_stopped(job, message, self.clock()))
                return row.id
            if row.attempt > 1:
                job = self._restarted(job)
            self._execute(row, token, job)
        except StaleClaim:
            pass  # another claim owns this job now; write nothing
        except Exception as exc:  # noqa: BLE001 - return the job to the queue, never crash
            self.queue.fail(row, token, f"{type(exc).__name__}: {redact_text(str(exc))[:300]}")
        return row.id

    @staticmethod
    def _restarted(job: Job) -> Job:
        restarted = Job(
            id=job.id,
            target=job.target,
            display=job.display,
            profile=job.profile,
            acquire=job.acquire,
            build_index=job.build_index,
            known_tracklist=job.known_tracklist,
            created_at=job.created_at,
        )
        restarted.log = deque(job.log, maxlen=LOG_RING)
        restarted.log.append(f"{_stamp()} restarted after the analysis worker stopped unexpectedly")
        return restarted

    def _execute(self, row: QueueJob, token: str, job: Job) -> None:
        # The job state machine is the in-memory manager's own, reused unchanged so progress,
        # cancellation, waiting and failure mean exactly what they meant before; its background
        # thread is never started — this process's run loop is the only consumer.
        machine = JobManager(self.work_root, self.runner)
        with machine.lock:
            machine._jobs[job.id] = job
        with self._current_lock:
            self._current = job
        if self.stopped.is_set():
            job.cancel_event.set()
        publisher = _ProgressPublisher(
            self.queue,
            row.id,
            token,
            job,
            machine.lock,
            interval=self.flush_seconds,
            heartbeat_seconds=self.heartbeat_seconds,
            lease_seconds=self.lease_seconds,
        )
        publisher.start()
        try:
            machine._execute(job.id)
        finally:
            publisher.stop()
            with self._current_lock:
                self._current = None
        self._settle(row.id, token, job, lock=machine.lock)

    def _settle(self, job_id: str, token: str, job: Job, *, lock: Any = None) -> None:
        with lock if lock is not None else contextlib.nullcontext():
            job.progress_percent()
            document = snapshot(job)
            status = job.status
        reason = job.error if status == FAILED else (job.message if status == WAITING else None)
        result = RunResult(
            run_id="",
            status=_TO_QUEUE.get(status, "failed"),
            reason=reason,
            achieved=None,
            bundle_id=None,
            usd_e6_reserved=0,
            usd_e6_spent=max(0, int(job.usd_e2_spent or 0)) * 10_000,
            attempts=0,
        )
        self.queue.terminal(job_id, token, result, bundle_path=None, progress={_LOCAL: document})


def _tie_to_this_process(process: subprocess.Popen[bytes]) -> Any:
    """Put the worker in a kill-on-close Windows job object owned by this process.

    If ``idea serve`` dies for any reason — Ctrl-C, a closed window, a crash, ``taskkill`` — the
    handle closes with it and Windows ends the worker and everything the worker started, so no
    orphaned process is left holding the virtual environment's files.
    """

    if os.name != "nt":
        return None
    try:
        import win32api
        import win32con
        import win32job

        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        handle = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid
        )
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        return job
    except Exception as exc:  # noqa: BLE001 - the pipe and parent watchdogs still stop the worker
        print(
            f"warning: could not tie the analysis worker's lifetime to ID'er: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None


class LocalWorkerSupervisor:
    """Starts the local worker process, restarts it if it dies, and stops it with the server."""

    def __init__(
        self,
        work_root: Path,
        *,
        config_path: Path,
        jobs: LocalJobs,
        python: str | None = None,
        runner_spec: str | None = None,
        stop_grace_seconds: float = 15.0,
        restart_delay_seconds: float = 1.0,
        max_restart_delay_seconds: float = 30.0,
        poll_seconds: float = 0.5,
    ) -> None:
        self.work_root = Path(work_root).resolve()
        self.config_path = Path(config_path)
        self.jobs = jobs
        self.python = python or sys.executable
        self.runner_spec = runner_spec
        self.stop_grace_seconds = stop_grace_seconds
        self.restart_delay_seconds = restart_delay_seconds
        self.max_restart_delay_seconds = max_restart_delay_seconds
        self.poll_seconds = poll_seconds
        self.restarts = 0
        self._lock: ProcessLock | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._job_object: Any = None
        self._started_at = 0.0
        self._stopping = threading.Event()
        self._guard = threading.Lock()
        self._monitor: threading.Thread | None = None

    @property
    def owner(self) -> bool:
        return self._lock is not None

    @property
    def pid(self) -> int | None:
        with self._guard:
            process = self._process
        return process.pid if process is not None and process.poll() is None else None

    def start(self) -> bool:
        """Start supervising; ``False`` when another ``idea serve`` already runs this worker."""

        lock = ProcessLock(self.work_root / SUPERVISOR_LOCK)
        try:
            lock.acquire()
        except JobStoreLocked:
            self._log(
                "another ID'er window already runs analyses for this work folder; "
                "analyses started here will run there"
            )
            return False
        self._lock = lock
        self._stopping.clear()
        try:
            # Anything left in flight by a server that stopped without warning is stopped, not
            # silently resumed: closing ID'er has always stopped its analyses.
            self.jobs.cancel_all(ABANDONED)
            with self._guard:
                self._spawn()
        except BaseException:
            self._release()
            raise
        self._monitor = threading.Thread(
            target=self._watch, name="idea-local-supervisor", daemon=True
        )
        self._monitor.start()
        return True

    def stop(self) -> None:
        if self._lock is None:
            return
        self._stopping.set()
        if self._monitor is not None:
            self._monitor.join(timeout=5)
        with contextlib.suppress(Exception):
            self.jobs.cancel_all(ABANDONED)
        with self._guard:
            process = self._process
        if process is not None:
            with contextlib.suppress(OSError, ValueError):
                if process.stdin is not None:
                    process.stdin.close()  # the worker's stop signal
            try:
                process.wait(timeout=self.stop_grace_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
        self._close_job_object()
        self._release()

    def _command(self) -> list[str]:
        command = [
            self.python,
            "-c",
            "from idea_web.jobs.local import main; raise SystemExit(main())",
            "--work-root",
            str(self.work_root),
            "--config",
            str(self.config_path.resolve()),
            "--parent-pid",
            str(os.getpid()),
            "--parent-pipe",
        ]
        if self.runner_spec:
            command += ["--runner", self.runner_spec]
        return command

    def _spawn(self) -> None:
        # Its own process group: Ctrl-C reaches ``idea serve``, which then stops the worker in
        # order (cancel, close the pipe, wait) instead of both dying mid-write.
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            self._command(),
            stdin=subprocess.PIPE,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
        self._job_object = _tie_to_this_process(process)
        self._process = process
        self._started_at = time.monotonic()

    def _watch(self) -> None:
        delay = self.restart_delay_seconds
        while not self._stopping.wait(self.poll_seconds):
            with self._guard:
                process = self._process
            if process is None or process.poll() is None:
                continue
            if time.monotonic() - self._started_at > 60.0:
                delay = self.restart_delay_seconds
            self._log(
                f"the analysis worker stopped (exit code {process.returncode}); restarting it"
            )
            if self._stopping.wait(delay):
                return
            with self._guard:
                if self._stopping.is_set():
                    return
                self._close_job_object()
                self._spawn()
                self.restarts += 1
            delay = min(delay * 2, self.max_restart_delay_seconds)

    def _close_job_object(self) -> None:
        job_object, self._job_object = self._job_object, None
        if job_object is not None:
            with contextlib.suppress(Exception):
                job_object.Close()

    def _release(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            with contextlib.suppress(Exception):
                lock.release()

    @staticmethod
    def _log(message: str) -> None:
        print(message, file=sys.stderr, flush=True)


def _load_runner(work_root: Path, config: Path, spec: str | None) -> Runner:
    if spec:
        if os.environ.get("IDEA_TEST_MODE") != "1":
            raise SystemExit("--runner is available only when IDEA_TEST_MODE=1")
        module_name, _, attribute = spec.partition(":")
        factory = getattr(importlib.import_module(module_name), attribute)
        return factory(work_root, config)
    from id_detector.cli import PROJECT_ROOT
    from id_detector.webapp.runner import make_pipeline_runner

    return make_pipeline_runner(work_root, project_root=PROJECT_ROOT, config_path=config)


def _watch_parent_pid(worker: LocalWorker, parent_pid: int) -> None:
    import psutil

    try:
        created = psutil.Process(parent_pid).create_time()
    except psutil.Error:
        worker.stop()
        return
    while not worker.stopped.wait(2.0):
        try:
            alive = psutil.Process(parent_pid).create_time() == created
        except psutil.Error:
            alive = False
        if not alive:
            worker.stop()
            return


def _install_stop_triggers(
    worker: LocalWorker, *, parent_pid: int | None, parent_pipe: bool
) -> None:
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(number, lambda *_: worker.stop())
    if parent_pipe and sys.stdin is not None:

        def watch_pipe() -> None:
            with contextlib.suppress(Exception):
                while sys.stdin.buffer.read(4096):
                    pass
            worker.stop()  # the supervisor closed the pipe, or its process is gone

        threading.Thread(target=watch_pipe, name="idea-local-parent-pipe", daemon=True).start()
    if parent_pid:
        threading.Thread(
            target=_watch_parent_pid,
            args=(worker, parent_pid),
            name="idea-local-parent-pid",
            daemon=True,
        ).start()

    def exit_after_grace() -> None:
        worker.stopped.wait()
        time.sleep(EXIT_GRACE_SECONDS)
        os._exit(0)

    threading.Thread(target=exit_after_grace, name="idea-local-exit-guard", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="idea-local-worker", description="ID'er's local analysis worker (run by idea serve)."
    )
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("idea.toml"))
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--parent-pipe", action="store_true")
    parser.add_argument("--runner", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    runner = _load_runner(args.work_root, args.config, args.runner)
    worker = LocalWorker(local_database(args.work_root), args.work_root, runner)
    _install_stop_triggers(worker, parent_pid=args.parent_pid, parent_pipe=args.parent_pipe)
    worker.run_forever()
    return 0
