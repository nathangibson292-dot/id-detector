"""Offline runners for the local worker *process* tests.

The worker loads one of these only through its hidden ``--runner`` option, which it refuses unless
``IDEA_TEST_MODE=1``.  Nothing here fetches, decodes or calls a provider.
"""

from __future__ import annotations

import time
from pathlib import Path

from id_detector.webapp.jobs import JobContext


def slow_runner(work_root: Path, config: Path):
    del work_root, config

    def runner(ctx: JobContext) -> None:
        for step in range(20):
            ctx.progress("recognise", step, 20, f"window {step}")
            time.sleep(0.05)
        ctx.log("fake analysis finished")

    return runner


def forever_runner(work_root: Path, config: Path):
    del work_root, config

    def runner(ctx: JobContext) -> None:
        step = 0
        while True:
            ctx.progress("recognise", step, 100_000, "listening")
            step += 1
            time.sleep(0.1)

    return runner
