"""Uvicorn lifecycle for the loopback apps (``idea serve``, ``idea truth review`` and tests)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import uvicorn

from id_detector.contracts import SourceRecord
from id_detector.io import native_path, read_text
from id_detector.providers.base import AppConfig

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
RecipeCost = Literal["free", "deep", "unknown"]


def require_loopback(host: str, *, what: str = "present server") -> None:
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"the {what} only binds the loopback interface")


#: Production start-up spends NO time bringing stored results up to date before serving.  The
#: launcher's readiness check must never scale with the number of saved mixes: the whole pass runs
#: on the background upkeep thread while HTTP is already able to answer.
READINESS_BUDGET_SECONDS = 0.0
_LOG = logging.getLogger("idea_web.upkeep")


@dataclass
class UpkeepReport:
    """What the upkeep pass did, mix by mix (paths are work-root relative: ``source/media``)."""

    updated: list[str] = field(default_factory=list)  # re-fused under today's fusion version
    refreshed: list[str] = field(default_factory=list)  # page re-rendered only
    busy: list[str] = field(default_factory=list)  # a run holds the media: nothing published
    skipped: list[tuple[str, str, RecipeCost]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (mix, error) — and carried on
    finished: threading.Event = field(default_factory=threading.Event)

    @property
    def changed(self) -> int:
        return len(self.updated) + len(self.refreshed)

    def owner_status_lines(self) -> list[str]:
        """Completed skips, in the same actionable words used by the browser and CLI."""

        if not self.finished.is_set() or not self.skipped:
            return []
        skipped_titles = {title for title, _why, _recipe in self.skipped}
        improved = len((set(self.updated) | set(self.refreshed)) - skipped_titles)
        changed = f"{improved} mix{'es' if improved != 1 else ''} improved"
        left_verb = "they are" if len(self.skipped) != 1 else "it is"
        left = f"{len(self.skipped)} deliberately left as {left_verb}"
        lines = [f"Saved-result upkeep complete: {changed}; {left}."]
        for title, why, recipe in self.skipped:
            lines.append(skipped_result_line(title, why, recipe))
        lines.append(
            "To analyse a skipped mix again, re-run it with --refresh; each line above says "
            "whether that re-run costs money."
        )
        return lines


def rerun_cost_words(recipe: RecipeCost) -> str:
    """Owner wording for the cost of rebuilding one refused result."""

    if recipe == "free":
        return "Re-running this one is free: it costs nothing, but it will take time."
    if recipe == "deep":
        return "Warning: re-running this one will spend real AudD credit."
    return (
        "Warning: its recipe could not be determined, so ID'er must treat it as paid; "
        "re-running this one may spend real AudD credit."
    )


def skipped_result_line(title: str, why: str, recipe: RecipeCost) -> str:
    """The one refusal sentence shared by startup logging, CLI output and browser status."""

    reason = why.rstrip(". ") or "its stored provenance could not be proved"
    return f"{title}: The result was left as it is because {reason}. {rerun_cost_words(recipe)}"


class Upkeep:
    """Brings stored results up to date OUTSIDE any request, and OFF the readiness path.

    Two jobs per mix, both publications under the media lock: a result decided by an older FUSION
    version is re-fused offline from its own recorded observations
    (:func:`id_detector.refusion.refuse_stale_result` — no provider, no network, no recognition
    cache, no money), and a page rendered by an older ``PAGE_VERSION`` is re-rendered.  No GET
    ever publishes a bundle or moves a pointer (4a-ii); hosted mode never runs this.

    Production gives :meth:`before_serving` a zero-second budget, then
    :meth:`continue_in_background` performs the whole pass on a daemon thread while the server is
    answering.  Tests and synchronous tooling may supply a non-zero budget explicitly.  A mix
    whose media a run holds is left alone
    (``busy`` — that run publishes a current result itself; never an unlocked publication); one
    that cannot be re-fused gets only its page refreshed, with the reason recorded; one that
    raises is recorded and the pass carries on.  Nothing here can stop the server starting.
    """

    def __init__(
        self,
        work_root: Path,
        config: AppConfig | None = None,
        *,
        budget_seconds: float = READINESS_BUDGET_SECONDS,
        on_finished: Callable[[UpkeepReport], None] | None = None,
    ) -> None:
        self.work_root = Path(native_path(work_root))
        self.config = config
        self.budget_seconds = budget_seconds
        self.report = UpkeepReport()
        self._stop = threading.Event()
        self._pending: list[Path] | None = None
        self._thread: threading.Thread | None = None
        self._on_finished = on_finished

    def _media_directories(self) -> list[Path]:
        if not self.work_root.is_dir():
            return []
        found: list[tuple[float, str, Path]] = []
        for present in self.work_root.glob("*/*/present"):
            media_dir = present.parent
            if media_dir.parent.name.startswith("."):
                continue  # .trash, .locks and other bookkeeping directories are not results
            try:
                modified = present.stat().st_mtime
            except OSError:
                modified = 0.0
            found.append((-modified, media_dir.as_posix(), media_dir))
        return [media_dir for _, _, media_dir in sorted(found)]

    def _name(self, media_dir: Path) -> str:
        try:
            source = SourceRecord.model_validate_json(read_text(media_dir / "ingest/source.json"))
        except (OSError, ValueError):
            return f"{media_dir.parent.name[:12]}/{media_dir.name[:12]}"
        return source.title or source.canonical_url or source.input_url

    def bring_up_to_date(self, media_dir: Path) -> None:
        from id_detector.present.refresh import refresh_page
        from id_detector.refusion import refuse_stale_result

        name = self._name(media_dir)
        try:
            outcome = refuse_stale_result(media_dir, config=self.config)
            if outcome.state == "refused":
                self.report.updated.append(name)
                return
            if outcome.state == "busy":
                self.report.busy.append(name)
                return  # never fall through to a publication the lock did not cover
            if outcome.state == "unrebuildable":
                self.report.skipped.append((name, outcome.why or "", outcome.recipe))
                _LOG.warning("%s", skipped_result_line(name, outcome.why or "", outcome.recipe))
                if not outcome.may_refresh_page:
                    return
            page = refresh_page(media_dir, config=self.config)
            if page == "refreshed":
                self.report.refreshed.append(name)
            elif page == "busy":
                self.report.busy.append(name)
        except Exception as exc:  # noqa: BLE001 - one damaged mix never stops the rest
            self.report.failed.append((name, f"{type(exc).__name__}: {exc}"))
            _LOG.warning("%s: could not be brought up to date: %s", name, exc)

    def _work(self, deadline: float | None) -> None:
        # With production's zero readiness budget, even discovering hundreds of saved mixes is
        # background work.  Check the deadline before the first directory scan as well as between
        # mixes, so start-up cost never scales with the library.
        if deadline is not None and time.monotonic() >= deadline:
            return
        if self._pending is None:
            self._pending = self._media_directories()
        while self._pending and not self._stop.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return
            self.bring_up_to_date(self._pending.pop(0))
        if self._pending:
            return  # shutdown interrupted the pass; do not announce a partial report as complete
        self.report.finished.set()
        if self._on_finished is not None:
            try:
                self._on_finished(self.report)
            except Exception as exc:  # noqa: BLE001 - reporting cannot stop or undo completed upkeep
                _LOG.warning("could not display the completed upkeep report: %s", exc)

    def before_serving(self) -> None:
        """The bounded synchronous part; production's zero budget performs no per-mix work."""

        self._work(time.monotonic() + self.budget_seconds)

    def continue_in_background(self) -> None:
        if self.report.finished.is_set() or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._work, args=(None,), name="idea-upkeep", daemon=True
        )
        self._thread.start()

    def run_all(self) -> UpkeepReport:
        """The whole pass, synchronously (``idea`` tooling and the tests)."""

        self._work(None)
        return self.report

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)


def refresh_stale_pages(work_root: Path, config: AppConfig | None = None) -> int:
    """The whole upkeep pass at once; returns how many results it changed.  See :class:`Upkeep`."""

    return Upkeep(work_root, config).run_all().changed


class LoopbackServer:
    """A pre-bound uvicorn server with the small lifecycle the CLI and the tests use."""

    def __init__(
        self,
        app: Any,
        *,
        host: str,
        port: int,
        what: str = "present server",
        before_serving: Callable[[], object] | None = None,
        upkeep: Upkeep | None = None,
    ) -> None:
        require_loopback(host, what=what)
        family = socket.AF_INET6 if host == "::1" else socket.AF_INET
        self._socket = socket.socket(family, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._socket.bind((host, port))
            self._socket.listen(128)
        except OSError:
            self._socket.close()
            raise
        address = self._socket.getsockname()
        self.server_address = (address[0], address[1])
        self._before_serving = before_serving
        self.upkeep = upkeep
        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=host,
                port=self.server_address[1],
                log_level="warning",
                access_log=False,
                lifespan="off",
                # No ``Server:`` header: the old server advertised its Python version (U-F29).
                server_header=False,
            )
        )
        self._closed = False

    def serve_forever(self) -> None:
        # The socket is already listening, so a browser that connects during the bounded start-up
        # pass simply waits in the backlog.  Whatever upkeep is left runs beside the server.
        if self._before_serving is not None:
            self._before_serving()
        if self.upkeep is not None:
            self.upkeep.continue_in_background()
        self._server.run(sockets=[self._socket])

    def shutdown(self) -> None:
        self._server.should_exit = True
        if self.upkeep is not None:
            self.upkeep.stop()

    def server_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(OSError):
            self._socket.close()


def make_server(
    work_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    config: AppConfig | None = None,
    job_manager: Any = None,
    jobs: Any = None,
    on_upkeep_finished: Callable[[UpkeepReport], None] | None = None,
) -> LoopbackServer:
    """The loopback ``idea serve`` server: the one FastAPI application in local mode."""

    require_loopback(host)
    from idea_web.application import create_app

    adapter = jobs if jobs is not None else job_manager
    root = Path(work_root)
    upkeep = Upkeep(root, config, on_finished=on_upkeep_finished)
    app = create_app(
        work_root,
        local=True,
        analyse_enabled=adapter is not None,
        config=config,
        jobs=adapter,
        upkeep_report=upkeep.report,
    )
    return LoopbackServer(
        app, host=host, port=port, before_serving=upkeep.before_serving, upkeep=upkeep
    )


class RunningServer:
    """A loopback server running on a background thread (tests and the compatibility helpers)."""

    def __init__(self, server: LoopbackServer, thread: threading.Thread) -> None:
        self.server = server
        self.thread = thread

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[0], self.server.server_address[1]
        display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return f"http://{display_host}:{port}"

    def shutdown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def run_in_background(server: LoopbackServer, *, name: str = "present-server") -> RunningServer:
    thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
    thread.start()
    return RunningServer(server, thread)


def serve_in_background(
    work_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    config: AppConfig | None = None,
    job_manager: Any = None,
    jobs: Any = None,
) -> RunningServer:
    """Start the server on a background thread (port 0 picks a free port)."""

    server = make_server(
        work_root, host=host, port=port, config=config, job_manager=job_manager, jobs=jobs
    )
    return run_in_background(server)
