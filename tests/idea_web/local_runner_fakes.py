"""Offline runners for the local worker *process* tests.

The worker loads one of these only through its hidden ``--runner`` option, which it refuses unless
``IDEA_TEST_MODE=1``.  Nothing here fetches, decodes a remote source or calls a provider.
"""

from __future__ import annotations

import dataclasses
import os
import time
from pathlib import Path

from id_detector.webapp.jobs import JobContext

ROOT = Path(__file__).resolve().parents[2]


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


class ExitAfterDispatch:
    """An AudD adapter whose ``kill_on``-th request is dispatched, then the PROCESS dies at once.

    ``os._exit`` runs no ``finally`` block, no settlement and no journal write: the worker process
    is gone with a durable ``dispatched`` line on disk, exactly like a power cut or a task kill.
    """

    def __init__(self, inner, kill_on: int) -> None:
        self.inner = inner
        self.kill_on = kill_on
        self.calls = 0

    async def recognize_clip(self, path: Path, on_attempt):
        self.calls += 1
        if self.calls == self.kill_on:
            # ``IDEA_FOLLOWUP_KILL_ONCE`` names a marker file: only the first worker process dies,
            # so a supervisor-restarted worker can finish the job.
            marker = os.environ.get("IDEA_FOLLOWUP_KILL_ONCE")
            if not marker or not Path(marker).exists():
                await on_attempt()
                if marker:
                    Path(marker).write_text("killed", encoding="utf-8")
                os._exit(9)
        return await self.inner.recognize_clip(path, on_attempt)


def priced(unit_usd_e6: int):
    def configure(config):
        if dataclasses.is_dataclass(config):
            return dataclasses.replace(config, audd_usd_e6_per_request=unit_usd_e6)
        return config.model_copy(update={"audd_usd_e6_per_request": unit_usd_e6})

    return configure


def fake_pipeline_runner(work_root: Path, config: Path, *, audd=None, unit_usd_e6=None):
    """The production :func:`make_pipeline_runner` with the offline fake providers injected."""

    from id_detector.webapp.runner import make_pipeline_runner
    from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff

    script = Path(os.environ["IDEA_FOLLOWUP_SCRIPT"])
    return make_pipeline_runner(
        work_root,
        project_root=ROOT,
        config_path=config,
        paid_scan_adapters={"audd": audd if audd is not None else FakeAudD(script)},
        shazam_http_client=FakeShazamHTTP(script),
        paid_sleep=no_backoff,
        configure=priced(unit_usd_e6) if unit_usd_e6 is not None else None,
    )


def deep_runner(work_root: Path, config: Path):
    """The real runner; ``IDEA_FOLLOWUP_KILL_ON`` makes the worker process die mid-sweep."""

    from tests.fakes.providers import FakeAudD

    script = Path(os.environ["IDEA_FOLLOWUP_SCRIPT"])
    kill_on = int(os.environ.get("IDEA_FOLLOWUP_KILL_ON", "0") or 0)
    audd = FakeAudD(script)
    return fake_pipeline_runner(
        work_root, config, audd=ExitAfterDispatch(audd, kill_on) if kill_on else audd
    )
