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
from urllib.parse import parse_qs

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
from id_detector.webapp.jobs import Job, JobManager, TargetValidationError

_SHA = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_PROFILES = ("free", "max_accuracy")
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


# --------------------------------------------------------------------------------------------------
# Web-app pages (self-contained inline HTML/CSS/JS; no usernames or comment text)
# --------------------------------------------------------------------------------------------------
_APP_CSS = (
    "body{font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:820px;"
    "margin:32px auto;padding:0 16px;color:#1c1c1c;background:#faf9f7}"
    "@media(prefers-color-scheme:dark){body{background:#16171a;color:#e9e9ea}}"
    "h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:24px 0 8px}"
    ".sub{color:#888;font-size:13px;margin:0 0 18px}"
    "form{background:#fff2;border:1px solid #8883;border-radius:10px;padding:16px;margin:0 0 8px}"
    "@media(prefers-color-scheme:dark){form{background:#212228}}"
    ".row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:8px 0}"
    "label{font-size:13px}input[type=text]{flex:1;min-width:260px;padding:7px 9px;border:1px solid "
    "#8886;border-radius:6px;background:transparent;color:inherit}"
    "select{padding:6px;border:1px solid #8886;border-radius:6px;background:transparent;"
    "color:inherit}"
    "button{font:inherit;padding:8px 16px;border:1px solid #2b6cb0;border-radius:7px;"
    "background:#2b6cb0;color:#fff;cursor:pointer}button.ghost{background:transparent;color:#2b6cb0}"
    "table{width:100%;border-collapse:collapse}td,th{padding:6px 8px;text-align:left;"
    "border-bottom:1px solid #8883;font-size:13px;vertical-align:top}"
    "th{color:#888;text-transform:uppercase;font-size:11px;letter-spacing:.03em}"
    "a{color:#2b6cb0;text-decoration:none}.st{font-weight:600}"
    ".st-succeeded{color:#1f7a4d}.st-failed{color:#b23b3b}.st-running{color:#2b6cb0}"
    ".st-queued{color:#b8860b}.st-cancelled{color:#888}ul{list-style:none;padding:0}"
    "li{padding:6px 0;border-bottom:1px solid #8883}.bar{height:12px;border-radius:6px;"
    "background:#8883;overflow:hidden;margin:6px 0}.bar>span{display:block;height:100%;"
    "background:#2b6cb0;width:0}pre{white-space:pre-wrap;word-break:break-word;background:#0000000a;"
    "border:1px solid #8883;border-radius:8px;padding:10px;font-size:12px;max-height:240px;"
    "overflow:auto}@media(prefers-color-scheme:dark){pre{background:#ffffff0a}}.mono{font-variant-"
    "numeric:tabular-nums}"
)


def _job_row_html(job: Job) -> str:
    status = html.escape(job.status)
    label = html.escape(job.display)
    phase = html.escape(job.phase)
    extras = []
    if job.acquire:
        extras.append("acquire")
    if job.build_index:
        extras.append("index")
    tags = f' <span class="sub">({html.escape(", ".join(extras))})</span>' if extras else ""
    result = ""
    if job.status == "succeeded" and job.result_path:
        result = f' · <a href="/{html.escape(job.result_path)}">result</a>'
    return (
        f'<tr><td><a href="/jobs/{html.escape(job.id)}">{label}</a>{tags}</td>'
        f'<td><span class="st st-{status}">{status}</span></td>'
        f'<td class="sub">{phase}{result}</td></tr>'
    )


def _home_html(sets: list[AnalysedSet], jobs: list[Job]) -> bytes:
    profile_options = "".join(
        f'<option value="{html.escape(name)}">{html.escape(name)}</option>' for name in _PROFILES
    )
    if jobs:
        job_rows = "".join(_job_row_html(job) for job in jobs)
        jobs_table = (
            "<table><thead><tr><th>Mix</th><th>Status</th><th>Phase</th></tr></thead>"
            f"<tbody>{job_rows}</tbody></table>"
        )
    else:
        jobs_table = '<p class="sub">No analyses yet — paste a mix URL above and click Analyse.</p>'
    set_rows = "".join(
        f'<li><a href="/{html.escape(item.source_key)}/{html.escape(item.media_key)}'
        f'/present/index.html">{html.escape(item.title)}</a> '
        f'<span class="sub">{html.escape(item.platform)}</span></li>'
        for item in sets
    )
    sets_block = f"<ul>{set_rows}</ul>" if set_rows else '<p class="sub">None yet.</p>'
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>id-detector</title><style>" + _APP_CSS + "</style></head><body>"
        "<h1>id-detector</h1>"
        '<p class="sub">Paste a mix link and analyse it — everything runs on this machine.</p>'
        '<form method="post" action="/analyse">'
        '<div class="row"><label for="url">Mix URL</label>'
        '<input id="url" name="url" type="text" required '
        'placeholder="https://soundcloud.com/... (or a local file path)"></div>'
        '<div class="row"><label for="profile">Profile</label>'
        f'<select id="profile" name="profile">{profile_options}</select>'
        '<label><input type="checkbox" name="acquire" value="1"> also fetch acquire links</label>'
        '<label><input type="checkbox" name="build_index" value="1"> '
        "build reference index first (unreleased tracks)</label></div>"
        '<div class="row"><button type="submit">Analyse</button></div>'
        "</form>"
        "<h2>Recent analyses</h2>"
        + jobs_table
        + "<h2>Finished result pages</h2>"
        + sets_block
        + "</body></html>"
    )
    return page.encode("utf-8")


def _job_page_html(job: Job) -> bytes:
    label = html.escape(job.display)
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>id-detector — analysis</title><style>" + _APP_CSS + "</style></head><body>"
        '<p class="sub"><a href="/">← all analyses</a></p>'
        f'<h1>Analysing</h1><p class="sub">{label}</p>'
        '<p>Status: <span class="st" id="status">…</span> '
        '<button class="ghost" id="cancel" type="button">Cancel</button></p>'
        '<p id="phase" class="sub"></p>'
        '<div class="bar"><span id="fill"></span></div>'
        '<p class="sub mono" id="windows"></p><p class="sub mono" id="eta"></p>'
        '<p id="result"></p>'
        '<h2>Log</h2><pre id="log"></pre>'
        "<script>"
        f"var JOB_ID={json.dumps(job.id)};"
        "function fmt(s){s=Math.max(0,Math.round(s));var m=Math.floor(s/60);"
        "return m?(m+'m '+(s%60)+'s'):(s+'s');}"
        "function tick(){fetch('/jobs/'+JOB_ID+'/status').then(function(r){return r.json();})"
        ".then(function(j){"
        "var st=document.getElementById('status');st.textContent=j.status;"
        "st.className='st st-'+j.status;"
        "document.getElementById('phase').textContent=j.message?(j.phase+' — '+j.message):j.phase;"
        "var total=j.windows_total||0,done=j.windows_done||0;"
        "var pct=total?Math.round(done*100/total):(j.terminal?100:0);"
        "document.getElementById('fill').style.width=pct+'%';"
        "document.getElementById('windows').textContent="
        "total?('windows '+done+' / '+total):'';"
        "document.getElementById('eta').textContent="
        "(j.eta_seconds&&!j.terminal)?('~'+fmt(j.eta_seconds)+' left at the rate limit'):'';"
        "var res=document.getElementById('result');"
        "if(j.status==='succeeded'&&j.result_url){res.innerHTML="
        "'<a href=\"'+j.result_url+'\">Open the result page →</a>';}"
        "else if(j.error){res.textContent='Error: '+j.error;}"
        "document.getElementById('log').textContent=(j.log||[]).join('\\n');"
        "if(!j.terminal){setTimeout(tick,2000);}});}"
        "document.getElementById('cancel').addEventListener('click',function(){"
        "fetch('/jobs/'+JOB_ID+'/cancel',{method:'POST'}).then(tick);});"
        "tick();"
        "</script></body></html>"
    )
    return page.encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "id-detector-present/1.0"
    work_root: Path
    config: AppConfig | None = None
    job_manager: JobManager | None = None
    analyse_enabled: bool = False

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

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, _CONTENT_TYPES[".json"])

    def _app_active(self) -> bool:
        return self.analyse_enabled and self.job_manager is not None

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        route = self.path.split("?", 1)[0]
        if route in ("/", "/index.html"):
            if self._app_active():
                assert self.job_manager is not None
                body = _home_html(_discover_sets(self.work_root), self.job_manager.recent())
            else:
                body = _index_html(_discover_sets(self.work_root))
            self._send(HTTPStatus.OK, body, _CONTENT_TYPES[".html"])
            return
        if self._app_active() and route.startswith("/jobs/"):
            self._handle_job_get(route)
            return
        served = self._resolve_served_file(route)
        if served is None:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        with open(native_path(served), "rb") as handle:
            body = handle.read()
        self._send(HTTPStatus.OK, body, _CONTENT_TYPES[served.suffix.lower()])

    def _handle_job_get(self, route: str) -> None:
        assert self.job_manager is not None
        segments = route.strip("/").split("/")
        if len(segments) == 2 and _JOB_ID.match(segments[1]):
            job = self.job_manager.get(segments[1])
            if job is None:
                self._send(HTTPStatus.NOT_FOUND, b"unknown job", "text/plain; charset=utf-8")
                return
            self._send(HTTPStatus.OK, _job_page_html(job), _CONTENT_TYPES[".html"])
            return
        if len(segments) == 3 and _JOB_ID.match(segments[1]) and segments[2] == "status":
            job = self.job_manager.get(segments[1])
            if job is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown job"})
                return
            self._send_json(HTTPStatus.OK, job.status_dict())
            return
        self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]
        if self._app_active() and route == "/analyse":
            self._handle_analyse()
            return
        if self._app_active() and route.startswith("/jobs/") and route.endswith("/cancel"):
            self._handle_job_cancel(route)
            return
        if route != "/rescan":
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

    def _read_body(self, limit: int = 8192) -> bytes | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > limit:
            return None
        return self.rfile.read(length) if length else b""

    def _handle_analyse(self) -> None:
        assert self.job_manager is not None
        raw = self._read_body()
        if raw is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad length"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        wants_json = content_type == "application/json"
        try:
            if wants_json:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                url = str(payload.get("url", ""))
                profile = payload.get("profile")
                profile = str(profile) if profile is not None else None
                acquire = bool(payload.get("acquire"))
                build_index = bool(payload.get("build_index"))
            else:
                form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                url = (form.get("url") or [""])[0]
                profile = (form.get("profile") or [None])[0]
                acquire = bool(form.get("acquire"))
                build_index = bool(form.get("build_index"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
            return
        if profile is not None and profile not in _PROFILES:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "unknown profile"})
            return
        try:
            job_id = self.job_manager.submit(url, profile, acquire=acquire, build_index=build_index)
        except TargetValidationError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        location = f"/jobs/{job_id}"
        if wants_json:
            self._send_json(HTTPStatus.OK, {"id": job_id, "location": location})
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_job_cancel(self, route: str) -> None:
        assert self.job_manager is not None
        segments = route.strip("/").split("/")
        if len(segments) != 3 or not _JOB_ID.match(segments[1]) or segments[2] != "cancel":
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        cancelled = self.job_manager.cancel(segments[1])
        if self.job_manager.get(segments[1]) is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown job"})
            return
        self._send_json(HTTPStatus.OK, {"cancelled": cancelled})


def make_server(
    work_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    config: AppConfig | None = None,
    job_manager: JobManager | None = None,
) -> ThreadingHTTPServer:
    """Create a ``127.0.0.1``-bound threading server (never binds a routable interface).

    When ``job_manager`` is supplied the home page becomes the analyse form and the ``/analyse`` /
    ``/jobs/<id>`` routes are enabled; without it the server stays the read-only Stage 7 index.
    """

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the present server only binds the loopback interface")

    handler = type(
        "BoundHandler",
        (_Handler,),
        {
            "work_root": work_root.resolve(),
            "config": config,
            "job_manager": job_manager,
            "analyse_enabled": job_manager is not None,
        },
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
    work_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    config: AppConfig | None = None,
    job_manager: JobManager | None = None,
) -> RunningServer:
    """Start the server on a background thread (port 0 picks a free port). For tests and the CLI."""

    server = make_server(work_root, host=host, port=port, config=config, job_manager=job_manager)
    thread = threading.Thread(target=server.serve_forever, name="present-server", daemon=True)
    thread.start()
    return RunningServer(server, thread)
