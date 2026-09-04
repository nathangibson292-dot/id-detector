"""Stage 7 local server and rescan queue.

A read-only, ``127.0.0.1``-only server over ``work/**/present/`` plus a single ``POST /rescan``
endpoint that only ever *appends a request to a queue file* — it makes no provider calls and writes
nothing else.  The queue (``present/rescan_queue.jsonl``) is later consumed by ``id-detector
rescan <url>`` to run another generation.

The index page lists analysed sets by their ``source.json`` title only — never a username or any
comment text.
"""

from __future__ import annotations

import html
import json
import re
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    EpisodesFile,
    RescanRequestRecord,
    SourceRecord,
    compose_natural_key,
    make_id,
)
from id_detector.io import (
    atomic_write_bytes,
    canonical_json_bytes,
    native_path,
    path_is_file,
    read_text,
    sha256_file,
)
from id_detector.providers.base import AppConfig
from id_detector.rescan import policy_for_trigger, priority_for_trigger

_SHA = re.compile(r"^[0-9a-f]{64}$")
_MANUAL_TRIGGERS = {"gap", "edge", "contested", "long_episode", "novelty", "hint_cluster"}
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".cue": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


# --------------------------------------------------------------------------------------------------
# Rescan queue (pure file operations — no network)
# --------------------------------------------------------------------------------------------------
def rescan_queue_path(media_dir: Path) -> Path:
    return media_dir / "present" / "rescan_queue.jsonl"


def build_rescan_request(
    *,
    source: SourceRecord,
    media_dir: Path,
    trigger: str,
    start_ms: int,
    end_ms: int,
    config: AppConfig | None = None,
) -> RescanRequestRecord:
    """Construct a schema-valid ``rescan_request`` for a manual page request."""

    if trigger not in _MANUAL_TRIGGERS:
        raise ValueError(f"unsupported manual rescan trigger: {trigger!r}")
    start = max(0, int(start_ms))
    end = max(start + 1, int(end_ms))
    policy = policy_for_trigger(trigger, config=config)
    generation = 0
    input_hashes: dict[str, str] = {}
    episodes_path = media_dir / "fuse" / "episodes.json"
    if path_is_file(episodes_path):
        try:
            generation = EpisodesFile.model_validate_json(read_text(episodes_path)).generation
        except (ValueError, OSError):
            generation = 0
        input_hashes["fuse/episodes.json"] = sha256_file(episodes_path)
    natural = {
        "generation": generation,
        "trigger": trigger,
        "start_ms": start,
        "end_ms": end,
        "policy": policy.model_dump(mode="json"),
    }
    natural_key = compose_natural_key("rescan_request", natural)
    return RescanRequestRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(source.media_key, "rescan_request", natural_key),
        generation=generation,
        trigger=trigger,
        start_ms=start,
        end_ms=end,
        policy=policy,
        priority=priority_for_trigger(trigger),
        input_hashes=input_hashes,
    )


def append_rescan_request(media_dir: Path, request: RescanRequestRecord) -> Path:
    """Append one request as a canonical JSON line, skipping a duplicate id already queued."""

    path = rescan_queue_path(media_dir)
    existing = read_rescan_queue(media_dir)
    if any(item.id == request.id for item in existing):
        return path
    line = canonical_json_bytes(request) + b"\n"
    previous = b""
    if path_is_file(path):
        with open(native_path(path), "rb") as handle:
            previous = handle.read()
    atomic_write_bytes(path, previous + line)
    return path


def read_rescan_queue(media_dir: Path) -> list[RescanRequestRecord]:
    path = rescan_queue_path(media_dir)
    if not path_is_file(path):
        return []
    records: list[RescanRequestRecord] = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if line:
            records.append(RescanRequestRecord.model_validate_json(line))
    return records


def consume_rescan_queue(media_dir: Path) -> list[RescanRequestRecord]:
    """Read the queue and move it aside to ``rescan_queue.consumed.jsonl`` (append-preserving)."""

    records = read_rescan_queue(media_dir)
    path = rescan_queue_path(media_dir)
    if not path_is_file(path):
        return records
    consumed = media_dir / "present" / "rescan_queue.consumed.jsonl"
    previous = b""
    if path_is_file(consumed):
        with open(native_path(consumed), "rb") as handle:
            previous = handle.read()
    with open(native_path(path), "rb") as handle:
        current = handle.read()
    atomic_write_bytes(consumed, previous + current)
    atomic_write_bytes(path, b"")
    return records


# --------------------------------------------------------------------------------------------------
# Read-only server
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AnalysedSet:
    source_key: str
    media_key: str
    media_dir: Path
    title: str
    platform: str


def _discover_sets(work_root: Path) -> list[AnalysedSet]:
    sets: list[AnalysedSet] = []
    if not work_root.is_dir():
        return sets
    for source_json in sorted(work_root.glob("*/*/ingest/source.json")):
        index_html = source_json.parents[1] / "present" / "index.html"
        if not path_is_file(index_html):
            continue
        try:
            source = SourceRecord.model_validate_json(read_text(source_json))
        except (ValueError, OSError):
            continue
        sets.append(
            AnalysedSet(
                source_key=source.source_key,
                media_key=source.media_key,
                media_dir=source_json.parents[1],
                title=source.title or "(untitled set)",
                platform=source.platform,
            )
        )
    return sets


def _index_html(sets: list[AnalysedSet]) -> bytes:
    rows = "".join(
        f'<li><a href="/{html.escape(item.source_key)}/{html.escape(item.media_key)}'
        f'/present/index.html">{html.escape(item.title)}</a> '
        f'<span class="p">{html.escape(item.platform)}</span></li>'
        for item in sets
    )
    if not rows:
        rows = "<li>No analysed sets found under this work root.</li>"
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>id-detector — analysed sets</title><style>"
        "body{font:14px/1.5 system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;"
        "color:#1c1c1c}@media(prefers-color-scheme:dark){body{background:#16171a;color:#e9e9ea}}"
        "h1{font-size:19px}ul{list-style:none;padding:0}li{padding:8px 0;border-bottom:1px solid "
        "#8883}a{color:#2b6cb0;text-decoration:none}.p{color:#888;font-size:12px}"
        "</style></head><body><h1>Analysed sets</h1><ul>" + rows + "</ul></body></html>"
    )
    return page.encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "id-detector-present/1.0"
    work_root: Path
    config: AppConfig | None = None

    def log_message(self, *args: object) -> None:  # noqa: D401 - silence default stderr logging
        return

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _resolve_served_file(self, path: str) -> Path | None:
        """Map a URL path to a file strictly inside ``work_root`` and under a ``present/`` dir."""

        segments = [segment for segment in path.split("/") if segment not in ("", ".")]
        if any(segment == ".." for segment in segments):
            return None
        candidate = self.work_root
        for segment in segments:
            candidate = candidate / segment
        try:
            resolved = candidate.resolve()
            root = self.work_root.resolve()
        except OSError:
            return None
        if root != resolved and root not in resolved.parents:
            return None
        if "present" not in resolved.parts:
            return None
        if resolved.suffix.lower() not in _CONTENT_TYPES:
            return None
        return resolved if path_is_file(resolved) else None

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        route = self.path.split("?", 1)[0]
        if route in ("/", "/index.html"):
            body = _index_html(_discover_sets(self.work_root))
            self._send(HTTPStatus.OK, body, _CONTENT_TYPES[".html"])
            return
        served = self._resolve_served_file(route)
        if served is None:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        with open(native_path(served), "rb") as handle:
            body = handle.read()
        self._send(HTTPStatus.OK, body, _CONTENT_TYPES[served.suffix.lower()])

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/rescan":
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 8192:
            self._send(HTTPStatus.BAD_REQUEST, b'{"error":"bad length"}', _CONTENT_TYPES[".json"])
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            media_key = str(payload["media_key"])
            trigger = str(payload["trigger"])
            start_ms = int(payload["start_ms"])
            end_ms = int(payload["end_ms"])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            self._send(HTTPStatus.BAD_REQUEST, b'{"error":"bad request"}', _CONTENT_TYPES[".json"])
            return
        if not _SHA.match(media_key):
            body = b'{"error":"bad media_key"}'
            self._send(HTTPStatus.BAD_REQUEST, body, _CONTENT_TYPES[".json"])
            return
        target = next(
            (item for item in _discover_sets(self.work_root) if item.media_key == media_key), None
        )
        if target is None:
            self._send(HTTPStatus.NOT_FOUND, b'{"error":"unknown set"}', _CONTENT_TYPES[".json"])
            return
        try:
            source = SourceRecord.model_validate_json(
                read_text(target.media_dir / "ingest" / "source.json")
            )
            request = build_rescan_request(
                source=source,
                media_dir=target.media_dir,
                trigger=trigger,
                start_ms=start_ms,
                end_ms=end_ms,
                config=self.config,
            )
            append_rescan_request(target.media_dir, request)
        except (ValueError, OSError) as exc:
            body = json.dumps({"error": str(exc)[:120]}).encode("utf-8")
            self._send(HTTPStatus.BAD_REQUEST, body, _CONTENT_TYPES[".json"])
            return
        body = json.dumps({"queued": True, "id": request.id, "trigger": request.trigger}).encode()
        self._send(HTTPStatus.OK, body, _CONTENT_TYPES[".json"])


def make_server(
    work_root: Path, *, host: str = "127.0.0.1", port: int = 8765, config: AppConfig | None = None
) -> ThreadingHTTPServer:
    """Create a ``127.0.0.1``-bound threading server (never binds a routable interface)."""

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the present server only binds the loopback interface")

    handler = type(
        "BoundHandler",
        (_Handler,),
        {"work_root": work_root.resolve(), "config": config},
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


@dataclass
class RunningServer:
    server: ThreadingHTTPServer
    thread: threading.Thread

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[0], self.server.server_address[1]
        return f"http://{host}:{port}"

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def serve_in_background(
    work_root: Path, *, host: str = "127.0.0.1", port: int = 0, config: AppConfig | None = None
) -> RunningServer:
    """Start the server on a background thread (port 0 picks a free port). For tests and the CLI."""

    server = make_server(work_root, host=host, port=port, config=config)
    thread = threading.Thread(target=server.serve_forever, name="present-server", daemon=True)
    thread.start()
    return RunningServer(server, thread)
