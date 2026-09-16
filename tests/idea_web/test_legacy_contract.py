"""The retired stdlib handler's HTTP contract, recorded before it was deleted (4a-ii review P1-8).

Cycle 4a-ii's first parity suite never ran the old ``present/server.py`` handler, which is how it
missed a lost ``HEAD /jobs/<id>/status``, missing ``nosniff`` headers on ``HEAD`` answers and a
changed ordering for malformed cancellation.  The review pass probed that handler (at ``19170ea``)
over real HTTP and recorded every answer below; the handler is now retired, so the recording is the
contract.  Each case runs against the FastAPI application over real uvicorn HTTP.

Volatile values are not pinned: the CSRF token, and the byte length of rendered HTML and status
JSON (template whitespace and wall-clock progress).  For those, ``HEAD`` is pinned to its ``GET``.

One divergence is intended and recorded as such: the old handler ignored a *chunked* request body
entirely (it read ``Content-Length`` only), so an oversized chunked ``/analyse`` answered 403 for a
missing token it never looked for.  The new application reads chunked bodies with the same bound
and answers 400 ``bad length``.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

from id_detector.webapp.jobs import JobContext, JobManager
from idea_web.server import serve_in_background
from tests.test_phase1a_bundles import publish, seed
from tests.test_stage7_page import _source

TIMEOUT = httpx.Timeout(10.0)
MIX = "https://soundcloud.com/example/live-mix"
TEXT = "text/plain; charset=utf-8"
JSON = "application/json; charset=utf-8"
HTML = "text/html; charset=utf-8"
UNKNOWN = "f" * 32
OTHER = "a" * 32
CSRF_REFUSAL = b'{"error": "missing or invalid CSRF token (GET /csrf, then send X-CSRF-Token)"}'

#: (name, path, status, content type, nosniff, exact body, extra headers) — GET and HEAD.
READS = [
    ("healthz", "/healthz", 200, JSON, True, b'{"ok": true}', {}),
    ("csrf", "/csrf", 200, JSON, True, None, {}),
    ("home", "/", 200, HTML, True, None, {}),
    ("index_html", "/index.html", 200, HTML, True, None, {}),
    # 4a-iii puts ``nosniff`` (and the other defensive headers) on every answer, redirects and
    # audio included; the retired handler sent it only with a body it had typed itself.
    ("new", "/new", 303, None, True, b"", {"location": "/", "content-length": "0"}),
    ("playlists", "/playlists", 200, HTML, True, None, {}),
    ("playlists_state", "/playlists/state", 200, "application/json", True, None, {}),
    ("job_page", "/jobs/{job}", 200, HTML, True, None, {}),
    ("job_page_unknown", f"/jobs/{UNKNOWN}", 404, TEXT, True, b"unknown job", {}),
    ("job_status", "/jobs/{job}/status", 200, JSON, True, None, {}),
    (
        "job_status_unknown",
        f"/jobs/{UNKNOWN}/status",
        404,
        JSON,
        True,
        b'{"error": "unknown job"}',
        {},
    ),
    ("job_audio_none", "/jobs/{job}/audio", 404, TEXT, True, b"no audio yet", {}),
    ("result_index", "{prefix}/index.html", 200, HTML, True, None, {}),
    ("result_json", "{prefix}/tracklist.json", 200, JSON, True, None, {}),
    (
        "media_audio",
        "/media/{media}/audio",
        200,
        "audio/webm",
        True,
        None,
        {"content-length": "1024"},
    ),
    ("missing", "/missing", 404, TEXT, True, b"not found", {}),
]
READ_ONLY_READS = [
    ("read_only_home", "/", 200, HTML, True, None, {}),
    ("read_only_new", "/new", 404, TEXT, True, b"not found", {}),
    ("read_only_job_status", "/jobs/{job}/status", 404, TEXT, True, b"not found", {}),
]
#: (Range header, status, content-range, content-length) against the 1024-byte fixture mix.
RANGES = [
    ("bytes=-4", 206, "bytes 1020-1023/1024", "4"),
    ("bytes=0-", 206, "bytes 0-1023/1024", "1024"),
    ("bytes=2-5", 206, "bytes 2-5/1024", "4"),
    ("bytes=5000-", 416, "bytes */1024", "0"),
    ("bytes=abc", 200, None, "1024"),
]
#: (name, path, headers, status, content type, exact body) — for the analyse-enabled server.
POSTS = [
    (
        "analyse_no_csrf_json",
        "/analyse",
        {"Content-Type": "application/json"},
        403,
        JSON,
        b'{"error": "CSRF token was invalid"}',
    ),
    ("cancel_bad_route_no_csrf", "/jobs/bad/cancel", {}, 404, TEXT, b"not found"),
    (
        "cancel_bad_route_csrf",
        "/jobs/bad/cancel",
        {"X-CSRF-Token": "{csrf}"},
        404,
        TEXT,
        b"not found",
    ),
    ("cancel_four_seg_no_csrf", f"/jobs/{OTHER}/x/cancel", {}, 404, TEXT, b"not found"),
    ("cancel_valid_unknown_no_csrf", f"/jobs/{OTHER}/cancel", {}, 403, JSON, CSRF_REFUSAL),
    (
        "cancel_valid_unknown_csrf",
        f"/jobs/{OTHER}/cancel",
        {"X-CSRF-Token": "{csrf}"},
        404,
        JSON,
        b'{"error": "unknown job"}',
    ),
    ("dismiss_bad", "/jobs/bad/dismiss", {}, 400, TEXT, b"bad request"),
    ("library_remove_no_csrf", "/library/remove", {}, 400, TEXT, b"bad request"),
    ("unknown", "/rescan", {}, 404, TEXT, b"not found"),
    (
        "foreign_origin",
        "/analyse",
        {"Origin": "https://evil.example"},
        403,
        JSON,
        b'{"error": "cross-site request refused (origin)"}',
    ),
    (
        "foreign_host",
        "/analyse",
        {"Host": "evil.example"},
        403,
        JSON,
        b'{"error": "cross-site request refused (host)"}',
    ),
]
#: Every mutation on a read-only server is the same plain 404.
READ_ONLY_POSTS = [
    "/analyse",
    "/jobs/bad/cancel",
    f"/jobs/{OTHER}/x/cancel",
    f"/jobs/{OTHER}/cancel",
    "/jobs/bad/dismiss",
    "/library/remove",
    "/rescan",
]


@pytest.fixture(scope="module")
def servers(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("legacy-contract")
    media = seed(root)
    publish(media)
    source = _source("soundcloud")
    original = media / source.original.path
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(bytes(range(256)) * 4)
    release = threading.Event()

    def runner(ctx: JobContext) -> None:
        ctx.progress("ingest", 0, 1, "fetching")
        release.wait(timeout=120)

    manager = JobManager(root, runner)
    app = serve_in_background(root, port=0, job_manager=manager)
    read_only = serve_in_background(root, port=0)
    try:
        job_id = manager.submit(MIX, "free")
        deadline = time.monotonic() + 10
        while manager.get(job_id).status != "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        values = {
            "job": job_id,
            "prefix": f"/{source.source_key}/{source.media_key}/present",
            "media": source.media_key,
            "csrf": httpx.get(app.base_url + "/csrf", timeout=TIMEOUT).json()["token"],
        }
        yield app, read_only, values
    finally:
        release.set()
        read_only.shutdown()
        app.shutdown()
        manager.shutdown()


def _fill(text: str, values: dict[str, str]) -> str:
    return text.format(**values) if "{" in text else text


def _check_read(base: str, case: tuple, values: dict[str, str]) -> None:
    name, path, status, content_type, nosniff, body, extra = case
    url = base + _fill(path, values)
    got = httpx.get(url, timeout=TIMEOUT)
    head = httpx.head(url, timeout=TIMEOUT)
    for response in (got, head):
        assert response.status_code == status, (name, response.request.method)
        assert response.headers.get("content-type") == content_type, name
        assert ("x-content-type-options" in response.headers) is nosniff, name
        for header, value in extra.items():
            assert response.headers.get(header) == value, (name, header)
    if body is not None:
        assert got.content == body, name
    assert head.content == b"", name
    assert head.headers.get("content-length") == got.headers.get("content-length"), name


@pytest.mark.parametrize("case", READS, ids=[case[0] for case in READS])
def test_recorded_reads_and_their_head_answers(servers, case) -> None:
    app, _, values = servers
    _check_read(app.base_url, case, values)


@pytest.mark.parametrize("case", READ_ONLY_READS, ids=[case[0] for case in READ_ONLY_READS])
def test_recorded_read_only_reads(servers, case) -> None:
    _, read_only, values = servers
    _check_read(read_only.base_url, case, values)


@pytest.mark.parametrize(("value", "status", "content_range", "length"), RANGES)
def test_recorded_audio_ranges(servers, value, status, content_range, length) -> None:
    app, _, values = servers
    response = httpx.get(
        f"{app.base_url}/media/{values['media']}/audio", headers={"Range": value}, timeout=TIMEOUT
    )
    assert response.status_code == status
    assert response.headers.get("content-range") == content_range
    assert response.headers["content-length"] == length
    assert response.headers["x-content-type-options"] == "nosniff"  # on every answer (4a-iii)
    if status == 416:
        assert "content-type" not in response.headers and response.content == b""
    else:
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("case", POSTS, ids=[case[0] for case in POSTS])
def test_recorded_posts(servers, case) -> None:
    app, _, values = servers
    name, path, headers, status, content_type, body = case
    sent = {key: _fill(value, values) for key, value in headers.items()}
    response = httpx.post(
        app.base_url + _fill(path, values), content=b"{}", headers=sent, timeout=TIMEOUT
    )
    assert response.status_code == status, name
    assert response.headers.get("content-type") == content_type, name
    assert response.headers.get("x-content-type-options") == "nosniff", name
    assert response.content == body, name


@pytest.mark.parametrize("path", READ_ONLY_POSTS)
def test_recorded_read_only_posts(servers, path) -> None:
    _, read_only, values = servers
    for headers in ({}, {"X-CSRF-Token": values["csrf"]}):
        response = httpx.post(
            read_only.base_url + path, content=b"{}", headers=headers, timeout=TIMEOUT
        )
        assert (response.status_code, response.content) == (404, b"not found"), (path, headers)
        assert response.headers.get("content-type") == TEXT


@pytest.mark.parametrize(
    ("path", "headers", "status"),
    [
        ("/analyse", {"Content-Type": "application/json"}, 400),
        ("/playlists/create", {}, 400),
    ],
)
def test_recorded_oversized_bodies_answer_bad_length(servers, path, headers, status) -> None:
    app, _, _ = servers
    response = httpx.post(
        app.base_url + path, content=b"x" * 9000, headers=headers, timeout=TIMEOUT
    )
    assert response.status_code == status
    assert response.json() == {"error": "bad length"}


def _raw_post(
    base: str, path: str, headers: dict[str, str], chunks: list[bytes], *, chunked: bool
) -> str:
    host, port = base.removeprefix("http://").rsplit(":", 1)
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        lines = [f"POST {path} HTTP/1.1", f"Host: {host}:{port}"]
        lines += [f"{key}: {value}" for key, value in headers.items()]
        if chunked:
            lines.append("Transfer-Encoding: chunked")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        for chunk in chunks:
            sock.sendall((f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n") if chunked else chunk)
        if chunked:
            sock.sendall(b"0\r\n\r\n")
        data = b""
        while b"\r\n" not in data:
            part = sock.recv(65536)
            if not part:
                break
            data += part
    return data.split(b"\r\n", 1)[0].decode("latin-1")


def test_recorded_refusals_of_half_megabyte_bodies_are_delivered(servers) -> None:
    app, _, _ = servers
    chunk = b"x" * 65536
    size = str(512 * 1024)
    foreign = _raw_post(
        app.base_url,
        "/analyse",
        {"Origin": "https://evil.example", "Content-Length": size},
        [chunk] * 8,
        chunked=False,
    )
    assert foreign.split(" ")[1] == "403"
    oversized = _raw_post(
        app.base_url,
        "/analyse",
        {"Content-Type": "application/json", "Content-Length": size},
        [chunk] * 8,
        chunked=False,
    )
    assert oversized.split(" ")[1] == "400"
    small = _raw_post(
        app.base_url,
        "/analyse",
        {"Content-Type": "application/json"},
        [b'{"url":"x"}'],
        chunked=True,
    )
    assert small.split(" ")[1] == "403"  # recorded: no token
    # Intended divergence (see the module docstring): the old handler never read a chunked body.
    chunked = _raw_post(
        app.base_url, "/analyse", {"Content-Type": "application/json"}, [chunk] * 2, chunked=True
    )
    assert chunked.split(" ")[1] == "400"
