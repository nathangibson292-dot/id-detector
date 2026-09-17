"""The durable queue's progress document — the page's own job object (U-F9, U-F33).

A queued job's progress has to answer two questions that are not the worker's internal phase
counters: "how far along is this, in wall-clock terms?" (U-F9) and "what happened to the run that
stopped?" (U-F33). Both already have one answer in this codebase — :class:`id_detector.webapp.jobs.
Job`, which the progress page, the home cards and the polling script have always rendered — and the
local worker already publishes exactly that object through ``jobs.progress``.

This module lets the *hosted* worker publish the same document from its own ``(phase, done, total,
message)`` ticks, by driving the unchanged state machine: a real ``Job`` updated through a real
``JobContext.progress``. So the measured per-phase durations, the observed-rate recognise estimate
and the monotonic clamp behind the bar are the shipped ones, not a second implementation.

A retry **resumes** the document the previous attempt left (:meth:`PageProgress.resume`): measured
phase time and the monotonic high-water mark carry over, so a job that is reclaimed after two
phases does not show the bar snapping back to zero.

**The weighting is deliberately untouched here.** The owner has asked for a different bar and his
re-weighting request is parked; nothing in this module changes how progress is weighted or
estimated. It reuses ``Job.progress_percent`` and ``PHASE_EXPECTED_SECONDS`` as they stand.
"""

from __future__ import annotations

import dataclasses
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from id_detector.webapp.jobs import (
    CANCELLED,
    FAILED,
    LOG_RING,
    RUNNING,
    SUCCEEDED,
    WAITING,
    Job,
    JobContext,
    JobManager,
)

#: The key the page document lives under in ``jobs.progress``. ``idea_web.jobs.local`` writes the
#: same key for local jobs, so ``local.job_view`` rebuilds a hosted job exactly as it rebuilds a
#: local one; ``test_ops`` pins the two to each other.
PAGE_DOCUMENT_KEY = "local"
#: The snapshotted fields of a page job: everything except the target (a column), the log (kept
#: separately) and the runtime-only objects. Pinned against ``local._SNAPSHOT_FIELDS`` by a test.
SNAPSHOT_FIELDS = tuple(
    item.name
    for item in dataclasses.fields(Job)
    if item.name not in {"target", "log", "cancel_event", "dispatch_admission", "settlement_writer"}
)
#: Fields a retry must NOT inherit: the previous attempt's identity, its outcome and its clock.
#: Measured time (``phase_seconds``) and the high-water mark (``progress_max``) deliberately carry.
_NOT_RESUMED = frozenset(
    {
        "id",
        "display",
        "profile",
        "acquire",
        "build_index",
        "run_id",
        "created_at",
        "status",
        "finished_at",
        "error",
        "failed_phase",
        "phase_started_at",
        "known_tracklist",
    }
)
#: Terminal run statuses (§2.3.5) mapped to the status the page shows for them.
_PAGE_STATUS = {
    "complete": SUCCEEDED,
    "degraded": SUCCEEDED,
    "partial": SUCCEEDED,
    "cancelled": CANCELLED,
    "provider_unavailable": WAITING,
    "budget_exhausted": FAILED,
    "source_changed": FAILED,
    "quota_exceeded": FAILED,
    "failed": FAILED,
    "dead_letter": FAILED,
}


def snapshot(job: Job) -> dict[str, Any]:
    """The JSON-safe state of one page job: every field the progress page and home cards read."""

    document = {name: getattr(job, name) for name in SNAPSHOT_FIELDS}
    document["phase_seconds"] = dict(job.phase_seconds)
    document["log"] = list(job.log)
    return document


def page_document(document: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The page document inside a queue row's ``progress``, if it carries one."""

    if not isinstance(document, Mapping):
        return None
    carried = document.get(PAGE_DOCUMENT_KEY)
    return dict(carried) if isinstance(carried, Mapping) else None


class PageProgress:
    """Turns a worker's phase ticks into the page's job document, keeping its own measurements.

    The worker calls :meth:`tick` from the service's progress callback and :meth:`settle` once the
    run has a status. Neither call touches the database: the caller decides when to publish, under
    its own claim fence.
    """

    def __init__(
        self,
        work_root: Path,
        job_id: str,
        target: str,
        *,
        display: str | None = None,
        profile: str | None = None,
        acquire: bool = False,
        build_index: bool = False,
        run_id: str | None = None,
        created_at: float | None = None,
        resume: Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # The in-memory manager is used for its state machine and its lock only; its background
        # worker thread starts on submit(), and nothing here submits. The local worker drives the
        # same object the same way.
        self._manager = JobManager(Path(work_root), lambda context: None)
        carried: dict[str, Any] = {}
        if isinstance(resume, Mapping):
            carried = {
                name: value
                for name, value in resume.items()
                if name in SNAPSHOT_FIELDS and name not in _NOT_RESUMED
            }
            if created_at is None and isinstance(resume.get("created_at"), (int, float)):
                created_at = float(resume["created_at"])
        self.job = Job(
            id=job_id,
            target=target,
            display=display if display is not None else target,
            profile=profile,
            acquire=acquire,
            build_index=build_index,
            run_id=run_id,
            created_at=created_at if created_at is not None else clock(),
            **carried,
        )
        if isinstance(resume, Mapping):
            self.job.log = deque((str(line) for line in resume.get("log") or ()), maxlen=LOG_RING)
        with self._manager.lock:
            self._manager._jobs[job_id] = self.job
        self._context = JobContext(self._manager, self.job)
        self._clock = clock

    def start(self) -> None:
        """The claim is running this job now."""

        with self._manager.lock:
            self.job.status = RUNNING
            if self.job.started_at is None:
                self.job.started_at = self._clock()

    def tick(self, phase: str, done: int, total: int, message: str = "") -> dict[str, Any]:
        """Record one phase tick through the shipped state machine and return the document."""

        self._context.progress(phase, done, total, message)
        return self.document()

    def settle(
        self,
        status: str,
        *,
        reason: str | None = None,
        result_path: str | None = None,
        error: str | None = None,
        usd_e2_spent: int | None = None,
        spend_known: bool = False,
    ) -> dict[str, Any]:
        """Freeze the job at its outcome so a finished — or failed — run stays visible (U-F33).

        ``result_path`` is a work-root-relative path the web layer really serves (the page renders
        it as ``/<path>``), never a bare identifier, which would link to the site root.

        ``usd_e2_spent`` is the run's **settled** spend. Without it the page falls back to "this
        run's cost record could not be read" (U-F15) even though SQLite holds the exact figure —
        the one case the UI is allowed to hedge about money, used when it need not.
        """

        with self._manager.lock:
            job = self.job
            job.run_status = status
            job.run_reason = reason
            job.status = _PAGE_STATUS.get(status, FAILED)
            job.finished_at = job.finished_at or self._clock()
            if job.phase_started_at is not None and job.phase:
                spent = max(0.0, job.finished_at - job.phase_started_at)
                job.phase_seconds = {
                    **job.phase_seconds,
                    job.phase: job.phase_seconds.get(job.phase, 0.0) + spent,
                }
                job.phase_started_at = None
            if job.status == FAILED:
                # The card names the step that stopped, never the raw provider error (U-F15/F31).
                job.failed_phase = job.failed_phase or job.phase
                job.error = job.error or error
            if job.status == SUCCEEDED:
                job.phase = "done"
            elif job.status != WAITING:
                job.phase = job.status
            if result_path is not None:
                job.result_path = result_path
            if spend_known:
                job.usd_e2_spent = usd_e2_spent
                job.spend_known = True
            job.progress_percent()
        return self.document()

    def document(self) -> dict[str, Any]:
        """The publishable ``jobs.progress`` document for this job."""

        with self._manager.lock:
            self.job.progress_percent()
            return {PAGE_DOCUMENT_KEY: snapshot(self.job)}
