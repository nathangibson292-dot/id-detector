"""Stage 10 browser-driven web app.

An in-process, background-thread job manager (:mod:`id_detector.webapp.jobs`) plus the real
pipeline runner (:mod:`id_detector.webapp.runner`) that lets the owner start and watch a full
``analyse`` (optionally ``acquire`` and reference-index) run from the local, ``127.0.0.1``-only
present server without touching the CLI again.
"""

from __future__ import annotations

from id_detector.webapp.jobs import (
    Job,
    JobCancelled,
    JobContext,
    JobManager,
    TargetValidationError,
    validate_target,
)

__all__ = [
    "Job",
    "JobCancelled",
    "JobContext",
    "JobManager",
    "TargetValidationError",
    "validate_target",
]
