"""In-process background-thread job manager for the browser-driven web app.

The present server is a :class:`http.server.ThreadingHTTPServer`, so this manager runs analyse
jobs on a **single** dedicated worker thread (one job at a time — a queue — to respect the Shazam
rate limit).  Every job records live progress, a bounded ring-buffer log, a terminal status and the
path of the finished ``present/index.html``; state lives in memory keyed by job id so a page reload
just re-reads it.  Jobs are cancellable, and :meth:`JobManager.shutdown` joins the worker so tearing
the server down never leaks a hung thread.

Privacy: the manager only ever stores what it is given, and it runs the submitted target and every
log line through :func:`id_detector.io.redact_text`.  No provider secrets, usernames or comment text
enter job state — the runner emits only coarse phase messages.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from id_detector.io import redact_text, url_has_credentials

#: Ring-buffer size for a job's human-readable log tail.
LOG_RING = 200
#: The Shazam self-imposed ceiling used to estimate a windows-remaining ETA on the progress page.
SHAZAM_RATE_PER_MINUTE = 18
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED})


class JobCancelled(asyncio.CancelledError):
    """Raised inside the pipeline (from a progress tick) to abort a running job.

    It subclasses :class:`asyncio.CancelledError` so ``_analyse``'s cancellation handler records a
    clean ``cancelled`` invocation and unwinds its locks/DB owner before the worker sees it.
    """


class TargetValidationError(ValueError):
    """The submitted analyse target is neither an http(s) URL nor an existing local file."""


def validate_target(raw: str) -> str:
    """Validate and normalise a submitted analyse target (server-side).

    Accepts an ``http``/``https`` mix URL (rejecting credential-bearing ones) or a local file the
    owner passes (a ``file://`` URI or an existing path).  Everything else — ``javascript:``,
    ``data:``, ``ftp:``, an unknown scheme, a non-existent path — is refused.
    """

    text = (raw or "").strip()
    if not text:
        raise TargetValidationError("a mix URL or local file is required")
    parts = urlsplit(text)
    scheme = parts.scheme.casefold()
    if scheme in {"http", "https"}:
        if not parts.netloc:
            raise TargetValidationError("the URL is missing a host")
        if url_has_credentials(text):
            raise TargetValidationError("credential-bearing URLs are not accepted")
        return text
    if scheme == "file":
        return text
    # Anything else must be a local file the owner passes.  A Windows path like ``C:\mix.wav``
    # parses with a single-letter scheme, so accept any non-web target that names an existing file.
    if Path(text).is_file():
        return text
    raise TargetValidationError("only an http(s) URL or a local file is accepted")


@dataclass
class Job:
    """One submitted analysis, its live progress, and its result — all in memory."""

    id: str
    target: str
    display: str
    profile: str | None
    acquire: bool
    build_index: bool
    status: str = QUEUED
    phase: str = QUEUED
    phase_done: int = 0
    phase_total: int = 0
    windows_done: int = 0
    windows_total: int = 0
    message: str = ""
    error: str | None = None
    result_path: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_RING))
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def eta_seconds(self) -> int:
        remaining = max(0, self.windows_total - self.windows_done)
        if not remaining or self.status in TERMINAL_STATES:
            return 0
        return int(round(remaining / SHAZAM_RATE_PER_MINUTE * 60))

    def status_dict(self) -> dict[str, Any]:
        """A JSON-safe snapshot for ``GET /jobs/<id>/status`` — never contains a secret."""

        return {
            "id": self.id,
            "display": self.display,
            "profile": self.profile,
            "acquire": self.acquire,
            "build_index": self.build_index,
            "status": self.status,
            "phase": self.phase,
            "phase_done": self.phase_done,
            "phase_total": self.phase_total,
            "windows_done": self.windows_done,
            "windows_total": self.windows_total,
            "eta_seconds": self.eta_seconds(),
            "message": self.message,
            "error": self.error,
            "result_url": ("/" + self.result_path) if self.result_path else None,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "terminal": self.status in TERMINAL_STATES,
            "log": list(self.log),
        }


class JobContext:
    """The handle a runner uses to report progress, log, check cancellation, and set the result."""

    def __init__(self, manager: JobManager, job: Job) -> None:
        self._manager = manager
        self._job = job
        self._last_phase: str | None = None

    @property
    def target(self) -> str:
        return self._job.target

    @property
    def profile(self) -> str | None:
        return self._job.profile

    @property
    def acquire(self) -> bool:
        return self._job.acquire

    @property
    def build_index(self) -> bool:
        return self._job.build_index

    @property
    def work_root(self) -> Path:
        return self._manager.work_root

    def check_cancel(self) -> None:
        if self._job.cancel_event.is_set():
            raise JobCancelled(self._job.id)

    def progress(self, phase: str, done: int, total: int, message: str = "") -> None:
        """Record a phase tick.  Every tick is also a cancellation point."""

        self.check_cancel()
        safe = redact_text(message) if message else ""
        with self._manager.lock:
            self._job.phase = phase
            self._job.phase_done = done
            self._job.phase_total = total
            self._job.message = safe
            if phase == "recognise":
                self._job.windows_done = done
                self._job.windows_total = total
            if phase != self._last_phase:
                self._job.log.append(f"{_stamp()} {phase}: {safe or 'started'}")
        self._last_phase = phase

    def log(self, message: str) -> None:
        with self._manager.lock:
            self._job.log.append(f"{_stamp()} {redact_text(message)}")

    def set_result(self, index_html: Path) -> None:
        try:
            relative = index_html.resolve().relative_to(self.work_root.resolve()).as_posix()
        except ValueError:
            return
        with self._manager.lock:
            self._job.result_path = relative


#: A runner takes a :class:`JobContext` and runs the work, raising to fail (or ``JobCancelled``).
Runner = Callable[[JobContext], None]


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


class JobManager:
    """A single-worker, one-at-a-time queue of analyse jobs kept entirely in memory."""

    def __init__(self, work_root: Path, runner: Runner, *, max_recent: int = 50) -> None:
        self.work_root = Path(work_root)
        self._runner = runner
        self._max_recent = max_recent
        self.lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._queue: deque[str] = deque()
        self._worker: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()

    # -- submission / inspection ---------------------------------------------------------------
    def submit(
        self,
        target: str,
        profile: str | None = None,
        *,
        acquire: bool = False,
        build_index: bool = False,
    ) -> str:
        validated = validate_target(target)
        job = Job(
            id=uuid.uuid4().hex,
            target=validated,
            display=redact_text(validated),
            profile=profile,
            acquire=acquire,
            build_index=build_index,
        )
        with self.lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._queue.append(job.id)
            self._prune_locked()
            self._ensure_worker_locked()
        self._wake.set()
        return job.id

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 25) -> list[Job]:
        with self.lock:
            ids = list(reversed(self._order))[:limit]
            return [self._jobs[job_id] for job_id in ids if job_id in self._jobs]

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in TERMINAL_STATES:
                return False
            job.cancel_event.set()
            if job.status == QUEUED:
                # A queued job never reaches the runner: mark it terminal now for a snappy UI. The
                # worker loop skips any cancelled id it pops.
                job.status = CANCELLED
                job.phase = CANCELLED
                job.finished_at = time.time()
                job.log.append(f"{_stamp()} cancelled before it started")
        self._wake.set()
        return True

    # -- lifecycle -----------------------------------------------------------------------------
    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the worker and join it — cancelling any in-flight job so teardown never hangs."""

        self._stop.set()
        with self.lock:
            for job in self._jobs.values():
                if job.status in {QUEUED, RUNNING}:
                    job.cancel_event.set()
            worker = self._worker
        self._wake.set()
        if worker is not None:
            worker.join(timeout=timeout)

    # -- internals -----------------------------------------------------------------------------
    def _prune_locked(self) -> None:
        while len(self._order) > self._max_recent:
            oldest = self._order[0]
            job = self._jobs.get(oldest)
            if job is not None and job.status not in TERMINAL_STATES:
                break  # never drop a queued/running job
            self._order.pop(0)
            self._jobs.pop(oldest, None)

    def _ensure_worker_locked(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._stop.clear()
            self._worker = threading.Thread(target=self._run_loop, name="webapp-jobs", daemon=True)
            self._worker.start()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._next_job()
            if job_id is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            self._execute(job_id)

    def _next_job(self) -> str | None:
        with self.lock:
            while self._queue:
                candidate = self._queue.popleft()
                job = self._jobs.get(candidate)
                if job is None or job.cancel_event.is_set():
                    continue
                return candidate
        return None

    def _execute(self, job_id: str) -> None:
        with self.lock:
            job = self._jobs[job_id]
            job.status = RUNNING
            job.started_at = time.time()
            job.phase = "starting"
            job.log.append(f"{_stamp()} started")
        ctx = JobContext(self, job)
        outcome = SUCCEEDED
        error: str | None = None
        try:
            self._runner(ctx)
        except asyncio.CancelledError:
            outcome = CANCELLED
        except Exception as exc:  # noqa: BLE001 - record on the job, never crash the worker
            outcome = FAILED
            error = redact_text(str(exc))[:500] or exc.__class__.__name__
        with self.lock:
            job.finished_at = time.time()
            if outcome == CANCELLED:
                job.status = CANCELLED
                job.phase = CANCELLED
                job.log.append(f"{_stamp()} cancelled")
            elif outcome == FAILED:
                job.status = FAILED
                job.phase = FAILED
                job.error = error
                job.log.append(f"{_stamp()} failed: {error}")
            else:
                job.status = SUCCEEDED
                job.phase = "done"
                job.log.append(f"{_stamp()} done")
