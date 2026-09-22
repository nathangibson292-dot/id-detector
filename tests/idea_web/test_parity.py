"""FastAPI parity and the 4a-ii review fixes, route by route (cycle 4a-ii).

The retired stdlib handler's observable contract is pinned in ``test_legacy_contract.py``, recorded
from that handler before it was deleted.  This file pins what the review found and fixed: no GET
ever publishes a bundle (stale pages become current at server start instead), refused and oversized
bodies are read and drained in bounded increments, ``HEAD`` answers carry the ``GET`` headers, audio
is streamed rather than loaded, hosted mode advertises no local audio, symlinks and junctions cannot
escape the work root, third-party links are web URLs only, and the five playlist guarantees hold.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
from pathlib import Path

import httpx
import pytest

from id_detector.present.page import PAGE_VERSION
from id_detector.present.refresh import page_version
from id_detector.webapp.jobs import JobContext, JobManager, _strip_extended_prefix
from idea_web.application import ASSET_VERSION, create_app
from idea_web.http import DRAIN_LIMIT, FILE_CHUNK
from idea_web.jobs.local import LocalJobs, LocalWorker, local_database
from idea_web.server import serve_in_background
from tests.idea_web.hosted_helpers import Browser, hosted_settings, make_account
from tests.test_phase1a_bundles import publish, seed
from tests.test_stage7_page import _source
from tests.test_stage7_server import _seed_work_root

BASE = "http://127.0.0.1:8765"
MIX = "https://soundcloud.com/example/live-mix"
TIMEOUT = httpx.Timeout(10.0)
MIB = 1 << 20


def request(app, method: str, path: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def token(app) -> str:
    response = request(app, "GET", "/csrf")
    assert response.status_code == 200
    return response.json()["token"]


def asgi(app, method: str, path: str, headers: dict[str, str], body_bytes: int = 0):
    """Drive the ASGI app directly, counting every body byte the application pulls."""

    pulled = 0
    remaining = body_bytes
    delivered = False
    messages: list[dict] = []

    async def receive() -> dict:
        nonlocal pulled, remaining, delivered
        if delivered:
            # The whole body has been handed over; a real client now just waits for the answer.
            await asyncio.Event().wait()
        size = min(FILE_CHUNK, remaining)
        remaining -= size
        pulled += size
        delivered = remaining <= 0
        return {"type": "http.request", "body": b"x" * size, "more_body": remaining > 0}

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8765),
    }

    async def run() -> None:
        await asyncio.wait_for(app(scope, receive, send), timeout=30)

    asyncio.run(run())
    start = next(message for message in messages if message["type"] == "http.response.start")
    chunks = [m.get("body", b"") for m in messages if m["type"] == "http.response.body"]
    return pulled, start, chunks


# --------------------------------------------------------------------------------------------------
# Routes the owner uses every day
# --------------------------------------------------------------------------------------------------
def test_health_home_index_new_and_head(tmp_path: Path) -> None:
    read_only = create_app(tmp_path)
    health = request(read_only, "GET", "/healthz")
    assert health.status_code == 200
    assert health.headers["content-type"] == "application/json; charset=utf-8"
    assert health.json() == {"ok": True}
    assert request(read_only, "HEAD", "/healthz").content == b""
    home = request(read_only, "GET", "/")
    assert home.status_code == 200 and "Analysed sets" in home.text
    assert request(read_only, "GET", "/index.html").status_code == 200
    assert request(read_only, "GET", "/new").status_code == 404

    app = create_app(tmp_path, jobs=LocalJobs(tmp_path))
    home = request(app, "GET", "/")
    assert home.status_code == 200 and "Drop a mix" in home.text
    assert f'name="csrf_token" value="{token(app)}"' in request(app, "GET", "/").text
    redirect = request(app, "GET", "/new?url=https://example.test/a")
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/?url=https%3A%2F%2Fexample.test%2Fa"
    headed = request(app, "HEAD", "/")
    assert headed.status_code == 200 and headed.content == b""
    assert int(headed.headers["content-length"]) > 0


def test_analyse_json_only_for_exact_json_content_type_and_form_redirect(tmp_path: Path) -> None:
    jobs = LocalJobs(tmp_path)
    app = create_app(tmp_path, jobs=jobs)
    csrf = token(app)
    created = request(
        app,
        "POST",
        "/analyse",
        json={"url": MIX, "profile": "free"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200 and set(created.json()) == {"id", "location"}
    form = request(
        app,
        "POST",
        "/analyse",
        data={"url": "soundcloud.com/fake/mix", "profile": "free", "csrf_token": csrf},
    )
    assert form.status_code == 303 and form.headers["location"].startswith("/jobs/")
    assert jobs.get(form.headers["location"].rsplit("/", 1)[1]).target == (
        "https://soundcloud.com/fake/mix"
    )
    not_json = request(
        app,
        "POST",
        "/analyse",
        content=b'{"url":"https://example.test/mix"}',
        headers={"Content-Type": "application/problem+json", "X-CSRF-Token": csrf},
    )
    assert not_json.status_code == 400
    assert not_json.headers["content-type"].startswith("text/html")


def test_analyse_validation_and_oversize_keep_json_and_form_contracts(tmp_path: Path) -> None:
    app = create_app(tmp_path, jobs=LocalJobs(tmp_path))
    csrf = token(app)
    bad_json = request(
        app, "POST", "/analyse", json={"url": "javascript:alert(1)", "csrf_token": csrf}
    )
    assert bad_json.status_code == 400 and "error" in bad_json.json()
    bad_form = request(
        app, "POST", "/analyse", data={"url": "javascript:alert(1)", "csrf_token": csrf}
    )
    assert bad_form.status_code == 400 and "Use a complete web link" in bad_form.text
    oversized = request(
        app,
        "POST",
        "/analyse",
        content=b"x" * 9000,
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 400 and oversized.json() == {"error": "bad length"}


def test_origin_host_and_csrf_refusals_have_bodies_and_do_not_mutate(tmp_path: Path) -> None:
    jobs = LocalJobs(tmp_path)
    app = create_app(tmp_path, jobs=jobs)
    csrf = token(app)
    body = {"url": MIX, "csrf_token": csrf}
    foreign = request(
        app, "POST", "/analyse", json=body, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403 and b"origin" in foreign.content
    rebound = request(app, "POST", "/analyse", json=body, headers={"Host": "evil.example"})
    assert rebound.status_code == 403 and b"host" in rebound.content
    expired = request(app, "POST", "/analyse", json={"url": MIX, "csrf_token": "wrong"})
    assert expired.status_code == 403 and b"CSRF" in expired.content
    assert jobs.recent() == []


def test_job_page_status_cancel_dismiss_and_2500ms_polling(tmp_path: Path) -> None:
    jobs = LocalJobs(tmp_path)
    app = create_app(tmp_path, jobs=jobs)
    csrf = token(app)
    job_id = jobs.submit(MIX, "free")
    page = request(app, "GET", f"/jobs/{job_id}")
    assert page.status_code == 200
    # The page script is a static asset since 4a-iii; the 2.5 s poll is paced in that file.
    script = request(app, "GET", f"/static/app.{ASSET_VERSION}.js")
    assert f'src="/static/app.{ASSET_VERSION}.js"' in page.text
    assert script.status_code == 200 and "setTimeout(tick, 2500)" in script.text
    assert "setTimeout(tick, 1500)" not in script.text and "setTimeout(tick" not in page.text
    status = request(app, "GET", f"/jobs/{job_id}/status")
    assert status.status_code == 200 and status.json()["id"] == job_id
    refused = request(app, "POST", f"/jobs/{job_id}/cancel", content=b"payload")
    assert refused.status_code == 403 and refused.content
    cancelled = request(app, "POST", f"/jobs/{job_id}/cancel", headers={"X-CSRF-Token": csrf})
    assert cancelled.status_code == 200 and cancelled.json()["cancelled"] is True
    dismissed = request(app, "POST", f"/jobs/{job_id}/dismiss", data={"csrf_token": csrf})
    assert dismissed.status_code == 303 and dismissed.headers["location"] == "/"
    assert request(app, "GET", f"/jobs/{job_id}").status_code == 404


def test_job_titles_are_escaped_on_the_progress_page(tmp_path: Path) -> None:
    started, release = threading.Event(), threading.Event()

    def runner(ctx: JobContext) -> None:
        ctx.progress("ingest", 1, 1, '<script>alert("x")</script>')
        started.set()
        release.wait(timeout=10)

    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(MIX)
    worker = LocalWorker(local_database(tmp_path), tmp_path, runner, flush_seconds=0.05)
    thread = threading.Thread(target=worker.run_once, daemon=True)
    thread.start()
    try:
        assert started.wait(timeout=10)
        app = create_app(tmp_path, jobs=jobs)
        deadline = 100
        while jobs.get(job_id).resolved_title is None and deadline:
            threading.Event().wait(0.05)
            deadline -= 1
        page = request(app, "GET", f"/jobs/{job_id}")
        assert "&lt;script&gt;" in page.text and '<script>alert("x")</script>' not in page.text
    finally:
        release.set()
        thread.join(timeout=10)


# --------------------------------------------------------------------------------------------------
# P0-2: GET serves sealed artefacts read-only; unsafe legacy pages stay untouched at server start
# --------------------------------------------------------------------------------------------------
def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(Path(os.fspath(root)).rglob("*"))
        if path.is_file() and ".idea" not in path.relative_to(root).parts
    }


def test_get_and_startup_leave_a_legacy_page_without_invocation_metadata_untouched(
    tmp_path: Path,
) -> None:
    source = _source("soundcloud")
    media = _seed_work_root(tmp_path, source)
    (media / "decode").mkdir()
    pcm = Path(__file__).resolve().parents[1] / "golden" / "pcm.json"
    (media / "decode" / "pcm.json").write_bytes(pcm.read_bytes())
    old = media / "present" / "index.html"
    old.write_text("<!doctype html><title>old</title>", encoding="utf-8")
    prefix = f"/{source.source_key}/{source.media_key}/present"
    before = _tree(tmp_path)

    for app in (create_app(tmp_path), create_app(tmp_path, jobs=LocalJobs(tmp_path))):
        # A pre-bundle result has only its page; reads never try to manufacture provenance.
        for path in ("/", "/index.html", prefix + "/index.html"):
            assert request(app, "GET", path).status_code == 200
            assert request(app, "HEAD", path).status_code == 200
        served = request(app, "GET", prefix + "/index.html")
        assert "<title>old</title>" in served.text  # served as sealed, not regenerated
    assert _tree(tmp_path) == before, "a GET wrote beneath the work root"
    assert not (media / "present" / "bundles").exists()

    running = serve_in_background(tmp_path, port=0)
    try:
        assert httpx.get(f"{running.base_url}/healthz", timeout=TIMEOUT).status_code == 200
        assert running.server.upkeep.report.finished.wait(timeout=10.0)
        page = httpx.get(running.base_url + prefix + "/index.html", timeout=TIMEOUT)
    finally:
        running.shutdown()
    assert page.status_code == 200 and "<title>old</title>" in page.text
    assert f'content="{PAGE_VERSION}"' not in page.text
    assert not (media / "present" / "bundles").exists()
    assert page_version(old) == 0  # the legacy bytes themselves are never rewritten


def test_result_exports_traversal_head_and_safe_fixture_keys(tmp_path: Path) -> None:
    source = _source("soundcloud")
    publish(seed(tmp_path))
    app = create_app(tmp_path)
    prefix = f"/{source.source_key}/{source.media_key}/present"
    for name, content_type in (
        ("tracklist.json", "application/json; charset=utf-8"),
        ("tracklist.md", "text/plain; charset=utf-8"),
        ("tracklist.cue", "text/plain; charset=utf-8"),
    ):
        response = request(app, "GET", prefix + "/" + name)
        assert response.status_code == 200 and response.headers["content-type"] == content_type
    headed = request(app, "HEAD", prefix + "/index.html")
    assert headed.status_code == 200 and headed.content == b""
    assert headed.headers["x-content-type-options"] == "nosniff"
    assert request(app, "GET", "/../../pyproject.toml").status_code == 404
    assert request(app, "GET", prefix + "/../source.json").status_code == 404
    # The local-mode browser gate uses safe human-readable fixture keys; containment, not a hash
    # spelling rule, is the security boundary for this four-segment compatibility URL.
    probe = tmp_path / "gate" / "probe" / "present" / "index.html"
    probe.parent.mkdir(parents=True)
    probe.write_text("<!doctype html><title>probe</title>", encoding="utf-8")
    assert request(app, "GET", "/gate/probe/present/index.html").status_code == 200


# --------------------------------------------------------------------------------------------------
# P0-5 and P1-7: HEAD is GET without a body, everywhere, over real HTTP too
# --------------------------------------------------------------------------------------------------
def test_head_answers_carry_the_get_status_and_headers_on_every_route(tmp_path: Path) -> None:
    media = seed(tmp_path)
    publish(media)
    source = _source("soundcloud")
    jobs = LocalJobs(tmp_path)
    job_id = jobs.submit(MIX)
    LocalWorker(local_database(tmp_path), tmp_path, lambda ctx: None, flush_seconds=0.05).run_once()
    assert jobs.get(job_id).status == "succeeded"
    prefix = f"/{source.source_key}/{source.media_key}/present"
    paths = [
        "/healthz",
        "/csrf",
        "/",
        "/index.html",
        "/playlists",
        "/playlists/state",
        f"/jobs/{job_id}",
        f"/jobs/{job_id}/status",
        f"/jobs/{'f' * 32}/status",
        prefix + "/index.html",
        prefix + "/tracklist.json",
        "/missing",
    ]
    running = serve_in_background(tmp_path, port=0, jobs=jobs)
    try:
        for path in paths:
            get = httpx.get(running.base_url + path, timeout=TIMEOUT)
            head = httpx.head(running.base_url + path, timeout=TIMEOUT)
            assert head.status_code == get.status_code, path
            assert head.content == b"", path
            for header in ("content-type", "content-length", "x-content-type-options"):
                assert head.headers.get(header) == get.headers.get(header), (path, header)
    finally:
        running.shutdown()
    # The named P0-5 case, spelled out.
    app = create_app(tmp_path, jobs=jobs)
    headed = request(app, "HEAD", f"/jobs/{job_id}/status")
    got = request(app, "GET", f"/jobs/{job_id}/status")
    assert headed.status_code == 200 and headed.content == b""
    assert headed.headers["content-type"] == "application/json; charset=utf-8"
    assert headed.headers["content-length"] == got.headers["content-length"]
    assert headed.headers["x-content-type-options"] == "nosniff"


# --------------------------------------------------------------------------------------------------
# P1-7: malformed cancellation is routed before the token; read-only servers expose no mutations
# --------------------------------------------------------------------------------------------------
def test_malformed_cancel_routes_404_before_the_token_and_read_only_has_no_mutations(
    tmp_path: Path,
) -> None:
    source = _source("soundcloud")
    media = _seed_work_root(tmp_path, source)
    app = create_app(tmp_path, jobs=LocalJobs(tmp_path))
    read_only = create_app(tmp_path)
    csrf = token(app)
    for headers in ({}, {"X-CSRF-Token": csrf}):
        for path in ("/jobs/bad/cancel", f"/jobs/{'a' * 32}/x/cancel"):
            response = request(app, "POST", path, content=b"{}", headers=headers)
            assert response.status_code == 404, (path, headers)
            assert response.headers["content-type"] == "text/plain; charset=utf-8"
            assert response.content == b"not found"
    no_token = request(app, "POST", f"/jobs/{'a' * 32}/cancel", content=b"{}")
    assert no_token.status_code == 403 and no_token.json()["error"].startswith("missing or invalid")
    unknown = request(app, "POST", f"/jobs/{'a' * 32}/cancel", headers={"X-CSRF-Token": csrf})
    assert unknown.status_code == 404 and unknown.json() == {"error": "unknown job"}
    assert request(app, "POST", "/jobs/a/b/dismiss", data={"csrf_token": csrf}).status_code == 400

    ro_token = token(read_only)
    keys = {"source_key": source.source_key, "media_key": source.media_key, "csrf_token": ro_token}
    for path, kwargs in (
        (f"/jobs/{'a' * 32}/cancel", {"headers": {"X-CSRF-Token": ro_token}}),
        ("/jobs/bad/dismiss", {"data": {"csrf_token": ro_token}}),
        ("/library/remove", {"data": keys}),
        ("/analyse", {"json": {"url": MIX, "csrf_token": ro_token}}),
    ):
        response = request(read_only, "POST", path, **kwargs)
        assert (response.status_code, response.content) == (404, b"not found"), path
    assert media.is_dir()
    assert request(read_only, "GET", f"/jobs/{'a' * 32}/status").content == b"not found"


# --------------------------------------------------------------------------------------------------
# P0-4: every POST body is read and drained in bounded increments, chunked bodies included
# --------------------------------------------------------------------------------------------------
LOCAL = {"host": "127.0.0.1:8765"}
BOUND = DRAIN_LIMIT + FILE_CHUNK


@pytest.mark.parametrize(
    ("path", "headers", "declared", "status"),
    [
        ("/analyse", {"origin": "https://evil.example"}, True, 403),
        ("/analyse", {"host": "evil.example"}, True, 403),
        ("/analyse", {"content-type": "application/json"}, True, 400),
        ("/analyse", {"content-type": "application/json"}, False, 400),
        ("/analyse", {"content-type": "application/x-www-form-urlencoded"}, False, 400),
        (f"/jobs/{'a' * 32}/cancel", {}, False, 403),
        ("/jobs/bad/cancel", {}, True, 404),
        (f"/jobs/{'a' * 32}/dismiss", {}, False, 400),
        ("/library/remove", {}, False, 400),
        ("/playlists/create", {}, False, 400),
        ("/playlists/create", {}, True, 400),
        ("/rescan", {}, False, 404),
        ("/rescan", {"origin": "https://evil.example"}, False, 403),
    ],
)
def test_refused_and_oversized_bodies_are_never_buffered(
    tmp_path: Path, path: str, headers: dict[str, str], declared: bool, status: int
) -> None:
    app = create_app(tmp_path, jobs=LocalJobs(tmp_path))
    total = 100 * MIB
    sent = {**LOCAL, **headers}
    if declared:
        sent["content-length"] = str(total)
    pulled, start, chunks = asgi(app, "POST", path, sent, total)
    assert start["status"] == status
    assert b"".join(chunks), "a refusal must carry a body"
    assert pulled <= BOUND, f"{path} pulled {pulled} bytes of a {total}-byte body"


def test_read_only_and_unsupported_methods_drain_in_bounded_increments(tmp_path: Path) -> None:
    read_only = create_app(tmp_path)
    for method, path, status in (("POST", "/analyse", 404), ("PUT", "/analyse", 501)):
        pulled, start, chunks = asgi(read_only, method, path, dict(LOCAL), 100 * MIB)
        assert start["status"] == status and b"".join(chunks)
        assert pulled <= BOUND


def test_truth_review_bodies_are_bounded_too(tmp_path: Path) -> None:
    from id_detector.truth_review import _MAX_BODY, TruthReviewSession
    from idea_web.truth_review import create_truth_review_app

    corpus = tmp_path / "corpus"
    shutil.copytree(Path(__file__).resolve().parents[1] / "fixtures" / "truth-review", corpus)
    session = TruthReviewSession(
        corpus / "fixture-set" / "ground_truth.json", work_root=tmp_path / "w"
    )
    app = create_truth_review_app(session)
    for headers, status in (({"origin": "https://evil.example"}, 403), ({}, 400)):
        pulled, start, chunks = asgi(app, "POST", "/save", {**LOCAL, **headers}, 100 * MIB)
        assert start["status"] == status and b"".join(chunks)
        assert pulled <= max(BOUND, _MAX_BODY + DRAIN_LIMIT + FILE_CHUNK)


# --------------------------------------------------------------------------------------------------
# P1-6: audio is streamed from disk in bounded chunks
# --------------------------------------------------------------------------------------------------
def test_audio_is_streamed_in_bounded_chunks_with_ranges_and_head(tmp_path: Path) -> None:
    media = seed(tmp_path)
    publish(media)
    source = _source("soundcloud")
    original = media / source.original.path
    original.parent.mkdir(parents=True, exist_ok=True)
    data = os.urandom(3 * MIB + 17)
    original.write_bytes(data)
    app = create_app(tmp_path)
    route = f"/media/{source.media_key}/audio"
    _, start, chunks = asgi(app, "GET", route, {**LOCAL, "range": "bytes=0-"})
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    assert start["status"] == 206 and headers["content-length"] == str(len(data))
    assert headers["content-range"] == f"bytes 0-{len(data) - 1}/{len(data)}"
    non_empty = [chunk for chunk in chunks if chunk]
    assert len(non_empty) > 1 and max(len(chunk) for chunk in non_empty) <= FILE_CHUNK
    assert b"".join(non_empty) == data

    whole = request(app, "GET", route)
    assert whole.status_code == 200 and whole.content == data
    part = request(app, "GET", route, headers={"Range": "bytes=2-5"})
    assert part.status_code == 206 and part.content == data[2:6]
    assert part.headers["content-range"] == f"bytes 2-5/{len(data)}"
    invalid = request(app, "GET", route, headers={"Range": f"bytes={len(data) + 5}-"})
    assert invalid.status_code == 416 and invalid.headers["content-range"] == f"bytes */{len(data)}"
    headed = request(app, "HEAD", route)
    assert headed.status_code == 200 and headed.content == b""
    assert headed.headers["content-length"] == str(len(data))
    immutable = request(
        app,
        "GET",
        "/"
        + _strip_extended_prefix(publish(media) / "index.html").relative_to(tmp_path).as_posix(),
    )
    assert immutable.status_code == 200


# --------------------------------------------------------------------------------------------------
# P1-10: hosted mode neither serves nor advertises local audio
# --------------------------------------------------------------------------------------------------
def test_hosted_mode_suppresses_audio_urls_and_audio_routes(tmp_path: Path) -> None:
    media = seed(tmp_path)
    publish(media)
    source = _source("soundcloud")
    original = media / source.original.path
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"0123456789")
    release = threading.Event()
    manager = JobManager(tmp_path, lambda ctx: release.wait(timeout=10))
    try:
        job_id = manager.submit(MIX, "free")
        manager.get(job_id).audio_path = str(original)
        local = create_app(tmp_path, jobs=manager)
        # Hosted mode needs accounts (4c-i): the same checks, made by a signed-in browser.
        settings = hosted_settings(tmp_path / "accounts")
        make_account(settings, "listener@example.com")
        hosted = Browser(create_app(tmp_path, local=False, jobs=manager, hosted=settings))
        assert hosted.sign_in("listener@example.com").status_code == 303
        assert request(local, "GET", f"/jobs/{job_id}/status").json()["audio_url"] == (
            f"/jobs/{job_id}/audio"
        )
        assert hosted.get(f"/jobs/{job_id}/status").json()["audio_url"] is None
        assert hosted.get(f"/jobs/{job_id}/audio").status_code == 404
        assert hosted.get(f"/media/{source.media_key}/audio").status_code == 404
        assert request(local, "GET", f"/media/{source.media_key}/audio").status_code == 200
    finally:
        release.set()
        manager.shutdown()


# --------------------------------------------------------------------------------------------------
# P1-9: symlinks and junctions cannot escape (and the inability to create one is a visible skip)
# --------------------------------------------------------------------------------------------------
def test_result_route_rejects_a_symlink_escape(tmp_path: Path) -> None:
    source = _source("soundcloud")
    media = _seed_work_root(tmp_path, source)
    outside = tmp_path.parent / f"{tmp_path.name}-secret.html"
    outside.write_text("secret", encoding="utf-8")
    link = media / "present" / "leak.html"
    try:
        try:
            link.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"this account cannot create symlinks ({exc}); the junction test runs")
        app = create_app(tmp_path)
        route = f"/{source.source_key}/{source.media_key}/present/leak.html"
        assert request(app, "GET", route).status_code == 404
    finally:
        outside.unlink()


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows reparse point")
def test_a_junction_cannot_escape_or_alias_inside_the_work_root(tmp_path: Path) -> None:
    import _winapi

    work = tmp_path / "work"
    source = _source("soundcloud")
    media = _seed_work_root(work, source)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.mp3").write_bytes(b"secret audio")
    inside = work / "elsewhere"
    inside.mkdir()
    (inside / "alias.mp3").write_bytes(b"other audio")
    (media / "ingest" / "real.mp3").write_bytes(b"real audio")
    escape = media / "ingest" / "escape"
    alias = media / "ingest" / "alias"
    _winapi.CreateJunction(str(outside), str(escape))
    _winapi.CreateJunction(str(inside), str(alias))
    try:
        assert escape.is_junction() and alias.is_junction()
        app = create_app(work)
        base = f"/{source.source_key}/{source.media_key}/ingest"
        assert request(app, "GET", base + "/real.mp3").content == b"real audio"
        assert request(app, "GET", base + "/escape/leak.mp3").status_code == 404
        assert request(app, "GET", base + "/alias/alias.mp3").status_code == 404
        # Result, bundle and audio routes all resolve through the same ``contained_file``; a
        # junction at a real 64-hex bundle depth cannot be created here (MAX_PATH), so the two
        # cases above are the proof for all of them.
    finally:
        os.rmdir(escape)
        os.rmdir(alias)


# --------------------------------------------------------------------------------------------------
# Library, playlists, unknown routes, result-page link safety, playlist guarantees
# --------------------------------------------------------------------------------------------------
def test_library_removal_is_recoverable_and_protected(tmp_path: Path) -> None:
    source = _source("soundcloud")
    media = _seed_work_root(tmp_path, source)
    app = create_app(tmp_path, jobs=LocalJobs(tmp_path))
    csrf = token(app)
    fields = {"source_key": source.source_key, "media_key": source.media_key}
    assert request(app, "GET", "/library/remove").status_code == 404
    refused = request(app, "POST", "/library/remove", data=fields)
    assert refused.status_code == 400 and media.is_dir()
    removed = request(app, "POST", "/library/remove", data={**fields, "csrf_token": csrf})
    assert removed.status_code == 303 and not media.exists()
    assert len(list((tmp_path / ".trash" / "library").iterdir())) == 1


def test_playlists_get_post_and_guards(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IDEA_PLAYLISTS_PATH", str(tmp_path / "playlists.json"))
    app = create_app(tmp_path)
    assert request(app, "GET", "/playlists").status_code == 200
    state = request(app, "GET", "/playlists/state")
    assert state.status_code == 200 and "playlists" in state.json()
    csrf = token(app)
    refused = request(app, "POST", "/playlists/create", data={"name": "No token"})
    assert refused.status_code == 403
    foreign = request(
        app,
        "POST",
        "/playlists/create",
        data={"name": "Foreign", "csrf_token": csrf},
        headers={"Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403 and foreign.content
    created = request(app, "POST", "/playlists/create", data={"name": "My set", "csrf_token": csrf})
    assert created.status_code == 200 and created.json()["ok"] is True
    bogus = request(app, "POST", "/playlists/bogus", data={"csrf_token": csrf})
    assert bogus.status_code == 405
    assert request(app, "GET", "/playlists/nope").status_code == 404


def test_unknown_routes_preserve_404_and_post_origin_order(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    assert request(app, "GET", "/missing").status_code == 404
    local = request(app, "POST", "/rescan", content=b"payload")
    assert local.status_code == 404 and local.content == b"not found"
    foreign = request(
        app, "POST", "/rescan", content=b"payload", headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403 and b"origin" in foreign.content


def test_result_page_links_are_plain_web_urls_only() -> None:
    from id_detector.present.page import _acquire_links_html, _safe_href

    hostile = {
        "free_download": True,
        "gate": True,
        "buy": True,
        "soundcloud": {"permalink_url": "javascript:alert(1)", "purchase_url": " JaVaScRiPt:x"},
        "direct": [
            {"kind": "purchase", "url": "data:text/html,<script>alert(1)</script>"},
            {"kind": "stream", "url": "vbscript:msgbox(1)", "source": "Deezer"},
        ],
        "search_links": [{"url": "java\tscript:alert(1)", "source": "Bandcamp"}],
    }
    rendered = _acquire_links_html(hostile).casefold()
    for scheme in ("javascript", "data:", "vbscript", "<script"):
        assert scheme not in rendered
    safe = _acquire_links_html(
        {
            "free_download": True,
            "soundcloud": {"permalink_url": "https://soundcloud.com/a/b?x=1&y=2"},
            "direct": [{"kind": "stream", "url": "https://deezer.com/t/1", "source": "Deezer"}],
        }
    )
    assert 'href="https://soundcloud.com/a/b?x=1&amp;y=2"' in safe
    assert 'href="https://deezer.com/t/1"' in safe
    assert _safe_href('https://x.test/"><svg onload=alert(1)>') == (
        "https://x.test/&quot;&gt;&lt;svg onload=alert(1)&gt;"
    )
    assert _safe_href("/relative") is None and _safe_href(None) is None


def test_result_rows_keep_all_five_playlist_injection_guarantees() -> None:
    from id_detector.playlists import PLAYLIST_CSS, PLAYLIST_JS
    from id_detector.present.exports import build_projection
    from id_detector.present.page import render_page
    from tests.test_projection import _inputs
    from tests.test_projection import _source as projection_source

    fixture, episodes, identities, acquire = _inputs("crowd")
    projection = build_projection(
        episodes, identities, acquire, min_track_ms=fixture["min_track_ms"]
    )
    rendered = render_page(
        source=projection_source("file"),
        episodes=episodes,
        identities=identities,
        duration_ms=fixture["duration_ms"],
        acquire=acquire,
        min_track_ms=fixture["min_track_ms"],
        projection=projection,
    )
    shown = [entry for entry in projection.shown_entries if entry["kind"] == "track"]
    hidden = [entry for entry in projection.entries if entry["hidden_reason"] is not None]
    for entry in shown:
        assert f'id="{entry["episode_id"]}"' in rendered
        assert f'data-candidate-id="{entry["candidate_id"]}"' in rendered
        assert f'data-episode-id="{entry["episode_id"]}"' in rendered
    assert rendered.count('class="pl-actions"') == len(shown)
    assert all(str(entry["episode_id"]) not in rendered for entry in hidden)
    assert PLAYLIST_CSS in rendered and PLAYLIST_JS in rendered
    assert "location.protocol" in PLAYLIST_JS and "el.hidden = true" in PLAYLIST_JS


def test_the_canonical_result_url_shape_is_served(tmp_path: Path) -> None:
    media = seed(tmp_path)
    publish(media)
    source = _source("soundcloud")
    app = create_app(tmp_path)
    page = request(app, "GET", f"/{source.source_key}/{source.media_key}/present/index.html")
    assert page.status_code == 200 and 'class="pl-actions"' in page.text
