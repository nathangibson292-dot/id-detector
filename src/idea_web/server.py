"""Uvicorn lifecycle for the loopback apps (``idea serve``, ``idea truth review`` and tests)."""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import uvicorn

from id_detector.io import native_path
from id_detector.providers.base import AppConfig

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def require_loopback(host: str, *, what: str = "present server") -> None:
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"the {what} only binds the loopback interface")


def refresh_stale_pages(work_root: Path, config: AppConfig | None = None) -> int:
    """Bring result pages rendered by an older ``PAGE_VERSION`` up to date, outside any request.

    This is the start-up half of the stale-page contract: a page rendered by an older build
    becomes current (a new immutable bundle for the same run) before the server answers its first
    request, so no GET ever publishes a bundle or moves a pointer.  Hosted mode has no local
    pointer and never calls this.  A page that cannot be regenerated stays exactly as it was.
    """

    from id_detector.present.refresh import ensure_fresh_page

    root = Path(native_path(work_root))
    if not root.is_dir():
        return 0
    refreshed = 0
    for present in sorted(root.glob("*/*/present")):
        media_dir = present.parent
        if media_dir.parent.name.startswith("."):
            continue  # .trash, .locks and other bookkeeping directories are not results
        try:
            if ensure_fresh_page(media_dir, config=config):
                refreshed += 1
        except Exception:  # noqa: BLE001 - one damaged result must never stop the server starting
            continue
    return refreshed


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
        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=host,
                port=self.server_address[1],
                log_level="warning",
                access_log=False,
                lifespan="off",
            )
        )
        self._closed = False

    def serve_forever(self) -> None:
        # The socket is already listening, so a browser that connects during the start-up pass
        # simply waits in the backlog and is answered from current pages.
        if self._before_serving is not None:
            self._before_serving()
        self._server.run(sockets=[self._socket])

    def shutdown(self) -> None:
        self._server.should_exit = True

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
) -> LoopbackServer:
    """The loopback ``idea serve`` server: the one FastAPI application in local mode."""

    require_loopback(host)
    from idea_web.application import create_app

    adapter = jobs if jobs is not None else job_manager
    app = create_app(
        work_root,
        local=True,
        analyse_enabled=adapter is not None,
        config=config,
        jobs=adapter,
    )
    root = Path(work_root)
    return LoopbackServer(
        app, host=host, port=port, before_serving=lambda: refresh_stale_pages(root, config)
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
