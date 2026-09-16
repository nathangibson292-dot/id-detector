"""Cycle 4a-iii: security headers, CSP, static assets, transport, and the server-side UI fixes.

Everything here runs offline against the ASGI application (or the real uvicorn server on a free
loopback port) with the same fixtures the parity suite uses.  The CSP proof is independent of the
server's own hashing: the page is parsed with ``html.parser``, every inline script is hashed here,
and the header must name each one — while the parsed markup must carry no inline handler at all.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import shutil
import subprocess
import threading
from html import unescape as html_unescape
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest

from id_detector.present import server as legacy
from id_detector.webapp.jobs import Job, JobContext, JobManager, _strip_extended_prefix
from idea_web import application, pages
from idea_web.application import ASSET_VERSION, STATIC_CSS, STATIC_JS, create_app
from idea_web.http import DEFAULT_HEADERS, content_security_policy, inline_script_hashes
from idea_web.jobs.local import LocalJobs, LocalWorker, local_database
from idea_web.server import serve_in_background
from tests.idea_web.test_parity import request, token
from tests.test_phase1a_bundles import publish, seed
from tests.test_stage7_page import _source
from tests.test_stage7_server import _seed_work_root

MIX = "https://soundcloud.com/example/live-mix"
OTHER = "https://www.mixcloud.com/example/other-mix/"
TIMEOUT = httpx.Timeout(10.0)
STYLESHEET = f"/static/app.{ASSET_VERSION}.css"
SCRIPT = f"/static/app.{ASSET_VERSION}.js"
#: The defensive headers as a client sees them; Cache-Control is asserted per route.
DEFENSIVE = {
    name.lower(): value
    for name, value in DEFAULT_HEADERS
    if name not in ("Cache-Control", "Content-Security-Policy")
}


class _Markup(HTMLParser):
    """Inline script bodies, inline event handlers and ``javascript:`` URLs of one document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self.handlers: list[tuple[str, str]] = []
        self.script_urls: list[str] = []
        self.forms = 0
        self._inline = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "form":
            self.forms += 1
        for key, value in attrs:
            if key.lower().startswith("on"):
                self.handlers.append((tag, key))
            if (
                key.lower() in {"href", "src", "action"}
                and value
                and value.strip().lower().startswith(("javascript:", "vbscript:", "data:text"))
            ):
                self.script_urls.append(value)
        if tag == "script":
            self._inline = not any(key == "src" for key, _ in attrs)
            if self._inline:
                self.scripts.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inline = False

    def handle_data(self, data: str) -> None:
        if self._inline:
            self.scripts[-1] += data


def _markup(html: str) -> _Markup:
    parser = _Markup()
    parser.feed(html)
    parser.close()
    return parser


def _expected_hashes(scripts: list[str]) -> set[str]:
    return {
        "sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
        for body in scripts
        if body
    }


def _assert_csp_names_exactly_the_inline_scripts(response: httpx.Response, name: str) -> _Markup:
    parsed = _markup(response.text)
    csp = response.headers["content-security-policy"]
    script_src = re.search(r"script-src ([^;]*)", csp)
    assert script_src is not None, name
    named = set(re.findall(r"'(sha256-[^']+)'", script_src.group(1)))
    assert named == _expected_hashes(parsed.scripts), name
    assert "'unsafe-inline'" not in script_src.group(1) and "'unsafe-eval'" not in csp, name
    assert parsed.handlers == [] and parsed.script_urls == [], name
    for directive in ("default-src 'none'", "frame-ancestors 'self'", "base-uri 'none'"):
        assert directive in csp, name
    return parsed


def _bundle_route(work_root: Path, bundle: Path, name: str) -> str:
    """The immutable bundle file's URL (``publish`` returns a ``\\\\?\\`` path on Windows)."""

    return "/" + _strip_extended_prefix(bundle / name).relative_to(work_root).as_posix()


def _blocking_manager(work_root: Path, release: threading.Event) -> JobManager:
    """A job manager whose first job reports a title and a phase, then waits to be released."""

    def runner(ctx: JobContext) -> None:
        ctx.progress("ingest", 1, 1, "Resolved mix title")
        ctx.progress("recognise", 3, 10, "")
        release.wait(timeout=20)

    return JobManager(work_root, runner)


# --------------------------------------------------------------------------------------------------
# Headers on every answer (plan §4.4)
# --------------------------------------------------------------------------------------------------
def test_every_answer_carries_the_defensive_headers_and_is_not_stored(tmp_path: Path) -> None:
    media = seed(tmp_path)
    publish(media)
    source = _source("soundcloud")
    jobs = LocalJobs(tmp_path)
    app = create_app(tmp_path, jobs=jobs)
    job_id = jobs.submit(MIX)
    prefix = f"/{source.source_key}/{source.media_key}/present"
    answers = [
        ("GET", "/", {}),
        ("HEAD", "/", {}),
        ("GET", "/healthz", {}),
        ("GET", "/csrf", {}),
        ("GET", "/new", {}),
        ("GET", f"/jobs/{job_id}", {}),
        ("GET", f"/jobs/{job_id}/status", {}),
        ("GET", "/playlists", {}),
        ("GET", "/missing", {}),
        ("POST", "/analyse", {"headers": {"Origin": "https://evil.example"}}),
        ("POST", "/rescan", {}),
        ("PUT", "/analyse", {}),
    ]
    for method, path, kwargs in answers:
        response = request(app, method, path, **kwargs)
        for name, value in DEFENSIVE.items():
            assert response.headers.get(name) == value, (method, path, name)
        assert response.headers.get("cache-control") == "no-store", (method, path)
        csp = response.headers.get("content-security-policy", "")
        assert "frame-ancestors 'self'" in csp, (method, path)
        # Same-origin framing stays allowed on purpose: the local cached-open path frames a
        # result page from its own origin to probe that audio loads and seeks
        # (scripts/gate_local_mode.ps1), which 'none'/DENY broke. Another site still cannot frame.
        assert "frame-ancestors 'none'" not in csp, (method, path)
        assert response.headers.get("x-frame-options") == "SAMEORIGIN", (method, path)
        if response.headers.get("content-type", "").startswith("text/html"):
            assert "script-src 'self'" in csp, (method, path)
            # A page must also be allowed to frame its OWN origin: the local cached-open probe
            # frames a result page (scripts/gate_local_mode.ps1), and a frame-src listing only the
            # platform players blocked it, so the probe never reached the audio element.
            assert "frame-src 'self'" in csp, (method, path)
        else:
            assert csp == "default-src 'none'; frame-ancestors 'self'; base-uri 'none'", path
    # Result files revalidate rather than being refused storage; the headers are still there.
    page = request(app, "GET", prefix + "/index.html")
    assert page.headers["cache-control"] == "no-cache"
    for name, value in DEFENSIVE.items():
        assert page.headers[name] == value


def test_the_content_security_policy_names_exactly_each_pages_inline_scripts(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("IDEA_PLAYLISTS_PATH", str(tmp_path / "playlists.json"))
    media = seed(tmp_path)
    bundle = publish(media)
    source = _source("soundcloud")
    prefix = f"/{source.source_key}/{source.media_key}/present"
    bundle_page = _bundle_route(tmp_path, bundle, "index.html")
    release = threading.Event()
    manager = _blocking_manager(tmp_path, release)
    try:
        running = manager.submit(MIX, "free")
        queued = manager.submit(OTHER, "max_accuracy")
        deadline = 100
        while manager.get(running).phase != "recognise" and deadline:
            threading.Event().wait(0.05)
            deadline -= 1
        manager.cancel(queued)
        app = create_app(tmp_path, jobs=manager)
        read_only = create_app(tmp_path)

        home = _assert_csp_names_exactly_the_inline_scripts(request(app, "GET", "/"), "home")
        # The live pages ship no inline script except the job page's per-job constants; the
        # shared script and stylesheet are static assets (U-F29).
        assert home.scripts == [] and "<style>" not in request(app, "GET", "/").text
        job = _assert_csp_names_exactly_the_inline_scripts(
            request(app, "GET", f"/jobs/{running}"), "job page"
        )
        assert len(job.scripts) == 1 and job.scripts[0].startswith("var JOB_ID=")
        index = _assert_csp_names_exactly_the_inline_scripts(
            request(read_only, "GET", "/"), "read-only index"
        )
        assert index.scripts == []
        for name, path in (("canonical result", prefix + "/index.html"), ("bundle", bundle_page)):
            result = _assert_csp_names_exactly_the_inline_scripts(request(app, "GET", path), name)
            assert len(result.scripts) >= 3, name  # config + page script + playlists script
        playlists = _assert_csp_names_exactly_the_inline_scripts(
            request(read_only, "GET", "/playlists"), "playlists"
        )
        assert len(playlists.scripts) == 1
        # An inline error page re-renders the same home page and keeps its policy honest.
        csrf = token(app)
        error = request(app, "POST", "/analyse", data={"url": "nope", "csrf_token": csrf})
        assert error.status_code == 400
        _assert_csp_names_exactly_the_inline_scripts(error, "inline form error")
    finally:
        release.set()
        manager.shutdown()


def test_the_truth_review_page_is_served_under_the_same_hashed_policy(tmp_path: Path) -> None:
    from id_detector.truth_review import TruthReviewSession
    from idea_web.truth_review import create_truth_review_app

    corpus = tmp_path / "corpus"
    shutil.copytree(Path(__file__).resolve().parents[1] / "fixtures" / "truth-review", corpus)
    session = TruthReviewSession(
        corpus / "fixture-set" / "ground_truth.json", work_root=tmp_path / "w"
    )
    app = create_truth_review_app(session)
    page = request(app, "GET", "/")
    assert page.status_code == 200
    parsed = _assert_csp_names_exactly_the_inline_scripts(page, "truth review")
    # The page module (closed to Phase-4 work) still writes four inline handlers; the served
    # page has none: each became an id, wired from a second script the policy names by hash.
    from id_detector.truth_review import _page

    assert _markup(_page(session, "t").decode("utf-8")).handlers  # the unwrapped page has them
    assert len(parsed.scripts) == 2 and parsed.scripts[1].startswith("function wire(")
    for control in ("play-pause", "help-open", "help-close"):
        assert f'id="{control}"' in page.text, control
    for control in ("play-pause", "help-open", "help-close", "use-suggestion"):
        assert f"wire('{control}'" in parsed.scripts[1], control
    assert "document.getElementById('undo-offset').onclick=undo" in parsed.scripts[0]
    for name, value in DEFENSIVE.items():
        assert page.headers[name] == value
    assert request(app, "GET", "/csrf").headers["cache-control"] == "no-store"


def test_inline_script_hashing_follows_the_html_parsers_rules() -> None:
    body = (
        b'<script src="/static/x.js"></script><script>alert(1)\r\n</script>'
        b"<script></script><SCRIPT type=text/javascript>alert(1)\n</SCRIPT>"
    )
    expected = base64.b64encode(hashlib.sha256(b"alert(1)\n").digest()).decode()
    assert inline_script_hashes(body) == [expected]  # src, empty and duplicate bodies collapse
    policy = content_security_policy(body)
    assert f"script-src 'self' 'sha256-{expected}' https://w.soundcloud.com" in policy
    assert content_security_policy(b"<p>plain</p>").startswith(
        "default-src 'none'; base-uri 'none'; object-src 'none'; frame-ancestors 'self'; "
        "form-action 'self'; script-src 'self' https://"
    )


# --------------------------------------------------------------------------------------------------
# Transport: static assets, gzip, ETags, no Server header (U-F29)
# --------------------------------------------------------------------------------------------------
def test_static_assets_are_versioned_immutable_and_linked_by_every_live_page(
    tmp_path: Path,
) -> None:
    """U-F29 is narrowed to the live pages (home, job, read-only index) by decision.

    A result page is an immutable bundle that must open offline as a plain file (the header of
    ``present/page.py``), and ``PAGE_VERSION`` lives in another session's file, so result pages
    keep their inline CSS/JS byte-for-byte and are only gzipped; see build-4a-iii "Decisions".
    """

    jobs = LocalJobs(tmp_path)
    app = create_app(tmp_path, jobs=jobs)
    job_id = jobs.submit(MIX)
    read_only = create_app(tmp_path)
    stylesheet = request(app, "GET", STYLESHEET)
    script = request(app, "GET", SCRIPT)
    assert stylesheet.status_code == 200 and stylesheet.content == STATIC_CSS
    assert stylesheet.headers["content-type"] == "text/css; charset=utf-8"
    assert script.status_code == 200 and script.content == STATIC_JS
    assert script.headers["content-type"] == "text/javascript; charset=utf-8"
    for asset in (stylesheet, script):
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert asset.headers["x-content-type-options"] == "nosniff"
    assert re.fullmatch(r"[0-9a-f]{12}", ASSET_VERSION)
    for path in ("/static/app.css", "/static/app.js", "/static/", "/static/../pyproject.toml"):
        assert request(app, "GET", path).status_code == 404, path
    assert request(read_only, "GET", STYLESHEET).status_code == 200

    for name, page in (
        ("home", request(app, "GET", "/")),
        ("job", request(app, "GET", f"/jobs/{job_id}")),
        ("read-only index", request(read_only, "GET", "/")),
    ):
        assert f'<link rel="stylesheet" href="{STYLESHEET}">' in page.text, name
        assert "<style>" not in page.text and "wallProgress" not in page.text, name
        assert len(page.content) < 40_000, name
    assert f'<script src="{SCRIPT}"></script>' in request(app, "GET", "/").text
    assert f'<script src="{SCRIPT}"></script>' in request(app, "GET", f"/jobs/{job_id}").text
    # The one script serves every live page: each fragment is present and the poll is paced.
    bundle = STATIC_JS.decode("utf-8")
    for fragment in (
        legacy._PROGRESS_JS,
        legacy._FORM_JS,
        legacy._PLAYER_JS,
        pages.HOME_JS,
        pages.CONFIRM_JS,
        pages.NEW_MIX_JS,
    ):
        assert fragment in bundle
    assert legacy._HOME_JS not in bundle  # the web layer's own card script replaces it
    assert "setTimeout(tick, 2500)" in bundle and "setTimeout(tick, 1500)" not in bundle
    assert "if(typeof JOB_ID === 'undefined') return;" in bundle


def test_gzip_compresses_documents_but_never_audio_ranges_or_small_answers(
    tmp_path: Path,
) -> None:
    media = seed(tmp_path)
    publish(media)
    source = _source("soundcloud")
    original = media / source.original.path
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(bytes(range(256)) * 32)
    unknown = tmp_path / "blob.bin"
    unknown.write_bytes(b"a" * 8192)
    release = threading.Event()
    manager = JobManager(tmp_path, lambda ctx: release.wait(timeout=20))
    try:
        job_id = manager.submit(MIX, "free")
        manager.get(job_id).audio_path = str(unknown)
        app = create_app(tmp_path, jobs=manager)
        gz = {"Accept-Encoding": "gzip"}
        home = request(app, "GET", "/", headers=gz)
        assert home.headers["content-encoding"] == "gzip" and "Drop a mix" in home.text
        assert "accept-encoding" in home.headers["vary"].lower()
        assert int(home.headers["content-length"]) < len(home.content)
        page = request(app, "GET", f"/{source.source_key}/{source.media_key}/present/index.html")
        assert page.headers.get("content-encoding") == "gzip"
        script = request(app, "GET", SCRIPT, headers=gz)
        assert script.headers["content-encoding"] == "gzip" and script.content == STATIC_JS
        assert "content-encoding" not in request(app, "GET", "/healthz", headers=gz).headers
        plain = request(app, "GET", "/", headers={"Accept-Encoding": "identity"})
        assert "content-encoding" not in plain.headers

        audio = f"/media/{source.media_key}/audio"
        whole = request(app, "GET", audio, headers=gz)
        assert whole.status_code == 200 and "content-encoding" not in whole.headers
        assert whole.content == original.read_bytes()
        part = request(app, "GET", audio, headers={**gz, "Range": "bytes=0-99"})
        assert part.status_code == 206 and "content-encoding" not in part.headers
        assert part.headers["content-length"] == "100" and len(part.content) == 100
        blob = request(app, "GET", f"/jobs/{job_id}/audio", headers=gz)
        assert (
            blob.status_code == 200 and blob.headers["content-type"] == "application/octet-stream"
        )
        assert "content-encoding" not in blob.headers and blob.content == unknown.read_bytes()
        # HEAD is still the compressed GET answer without its body.
        headed = request(app, "HEAD", "/", headers=gz)
        assert headed.content == b""
        for name in ("content-encoding", "content-length", "content-security-policy", "vary"):
            assert headed.headers.get(name) == home.headers.get(name), name
    finally:
        release.set()
        manager.shutdown()


def test_real_server_sends_no_server_header_and_compresses(tmp_path: Path) -> None:
    running = serve_in_background(tmp_path, port=0)
    try:
        health = httpx.get(running.base_url + "/healthz", timeout=TIMEOUT)
        home = httpx.get(running.base_url + "/", timeout=TIMEOUT)
        head = httpx.head(running.base_url + "/", timeout=TIMEOUT)
    finally:
        running.shutdown()
    for response in (health, home, head):
        assert response.status_code == 200
        assert "server" not in response.headers, dict(response.headers)
        for name, value in DEFENSIVE.items():
            assert response.headers[name] == value, name
    assert home.headers["content-encoding"] == "gzip" and "Analysed sets" in home.text
    assert head.headers["content-length"] == home.headers["content-length"]


def test_result_files_carry_etags_and_answer_304_when_unchanged(tmp_path: Path) -> None:
    media = seed(tmp_path)
    bundle = publish(media)
    source = _source("soundcloud")
    app = create_app(tmp_path)
    canonical = f"/{source.source_key}/{source.media_key}/present/index.html"
    immutable = _bundle_route(tmp_path, bundle, "tracklist.json")
    page = request(app, "GET", canonical)
    etag = page.headers["etag"]
    assert etag.startswith('W/"') and page.headers["cache-control"] == "no-cache"
    unchanged = request(app, "GET", canonical, headers={"If-None-Match": etag})
    assert unchanged.status_code == 304 and unchanged.content == b""
    assert unchanged.headers["etag"] == etag and unchanged.headers["cache-control"] == "no-cache"
    assert request(app, "HEAD", canonical, headers={"If-None-Match": etag}).status_code == 304
    listed = request(app, "GET", canonical, headers={"If-None-Match": f'"other", {etag}'})
    assert listed.status_code == 304
    assert request(app, "GET", canonical, headers={"If-None-Match": '"stale"'}).status_code == 200
    export = request(app, "GET", immutable)
    assert export.status_code == 200 and export.headers["etag"].startswith('W/"')
    assert export.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert (
        request(
            app, "GET", immutable, headers={"If-None-Match": export.headers["etag"]}
        ).status_code
        == 304
    )
    # A newer run moves the canonical pointer; its page has a new tag, so a revalidating browser
    # fetches the new bundle instead of being told nothing changed.
    publish(media, "run-2", metadata={"started_at": "2026-09-12T00:00:00Z"})
    moved = request(app, "GET", canonical, headers={"If-None-Match": etag})
    assert moved.status_code == 200 and moved.headers["etag"] != etag


# --------------------------------------------------------------------------------------------------
# U-F1: a bad submission re-renders the form with everything kept; JSON only for JSON
# --------------------------------------------------------------------------------------------------
def test_bad_submissions_rerender_the_one_form_with_every_field_kept(tmp_path: Path) -> None:
    jobs = LocalJobs(tmp_path)
    app = create_app(tmp_path, jobs=jobs)
    csrf = token(app)
    pasted = "12:34 Artist - Title\n19:20 Other - Tune"
    fields = {
        "url": "not a url at all",
        "profile": "max_accuracy",
        "known_tracklist": pasted,
        "csrf_token": csrf,
    }
    bad_url = request(app, "POST", "/analyse", data=fields)
    assert bad_url.status_code == 400
    assert bad_url.headers["content-type"].startswith("text/html")
    parsed = _markup(bad_url.text)
    assert parsed.forms >= 1 and bad_url.text.count('class="dropform"') == 1
    assert 'id="form-error" role="alert">Use a complete web link' in bad_url.text
    assert 'value="not a url at all"' in bad_url.text
    assert 'value="max_accuracy" checked' in bad_url.text
    assert "12:34 Artist - Title\n19:20 Other - Tune</textarea>" in bad_url.text
    assert 'name="acquire" value="1">' in bad_url.text  # links were off and stay off
    assert "Drop a mix" in bad_url.text and "Your mixes" in bad_url.text
    assert jobs.recent() == []

    bad_mode = request(app, "POST", "/analyse", data={**fields, "profile": "premium"})
    assert bad_mode.status_code == 400 and "Choose Free scan or Deep scan." in bad_mode.text
    assert 'value="not a url at all"' in bad_mode.text and pasted.split("\n")[1] in bad_mode.text
    assert 'name="acquire" value="1">' in bad_mode.text

    expired = request(app, "POST", "/analyse", data={**fields, "csrf_token": "stale"})
    assert expired.status_code == 403 and "This page expired" in expired.text
    assert 'value="not a url at all"' in expired.text

    # JSON callers keep their JSON errors; anything else is a page, never a raw error dump.
    as_json = request(app, "POST", "/analyse", json={"url": "not a url", "csrf_token": csrf})
    assert as_json.status_code == 400 and as_json.headers["content-type"].startswith("application")
    assert "error" in as_json.json()
    as_text = request(
        app,
        "POST",
        "/analyse",
        content=b"url=not+a+url",
        headers={"Content-Type": "text/plain", "X-CSRF-Token": csrf},
    )
    assert as_text.status_code == 400 and as_text.headers["content-type"].startswith("text/html")
    assert not as_text.text.startswith("{")


# --------------------------------------------------------------------------------------------------
# U-F15 and U-F30: failure copy with a credit statement; no auto-redirect on success
# --------------------------------------------------------------------------------------------------
def test_failure_copy_credit_statement_and_no_auto_redirect(tmp_path: Path) -> None:
    jobs = LocalJobs(tmp_path)
    app = create_app(tmp_path, jobs=jobs)
    job_id = jobs.submit(MIX, "max_accuracy")

    def failing(ctx: JobContext) -> None:
        ctx.progress("ingest", 0, 1, "")
        raise RuntimeError("HTTP 403 from the platform (bot challenge). yt-dlp: boom")

    LocalWorker(local_database(tmp_path), tmp_path, failing, flush_seconds=0.05).run_once()
    job = jobs.get(job_id)
    assert job.status == "failed"
    page = request(app, "GET", f"/jobs/{job_id}").text
    config = _markup(page).scripts[0]
    table = json.loads(re.search(r"var OUTCOME_COPY=(\{.*?\});var STEPS", config).group(1))
    assert table == legacy._outcome_copy_js()
    assert table["failed:ingest"][0] == "The platform would not hand over the audio."
    assert "yt-dlp" not in page and "bot challenge" not in page
    status = request(app, "GET", f"/jobs/{job_id}/status").json()
    assert status["failed_phase"] == "ingest" and status["terminal"] is True
    assert "spend_known" in status and "usd_e2_spent" in status

    script = STATIC_JS.decode("utf-8")
    # The failed step turns red, the cost is stated from the journal, and the raw error is never
    # shown; the tracker's failed state is driven by the server's ``failed_phase``.
    assert "classList.add('failed')" in script and "j.failed_phase" in script
    assert "function costSentence" in script and "was spent before it stopped" in script
    assert "Nothing was spent" in script and "err.textContent = j.error" not in script
    # U-F30: the success screen offers the tracklist; it never navigates on its own.
    assert "window.location.href" not in script and "countdown(" not in script
    assert "Open the tracklist" in script and "stay here" not in script
    assert legacy._cost_sentence(usd_e2_spent=12, spend_known=True, status="failed") == (
        "$0.12 of paid checks was spent before it stopped."
    )
    assert legacy._cost_sentence(usd_e2_spent=None, spend_known=False, status="failed") == (
        "This run's cost record could not be read."
    )
    # The home card explains the same failure in the same words, with a way to dismiss it.
    home = request(app, "GET", "/").text
    assert f'data-job="{job_id}" data-terminal="1" data-status="failed"' in home
    assert f'action="/jobs/{job_id}/dismiss"' in home and ">Dismiss</button>" in home


# --------------------------------------------------------------------------------------------------
# U-F18: home activity cards — titles, one label table, running first, dismissable
# --------------------------------------------------------------------------------------------------
def test_home_activity_cards_share_labels_show_titles_and_sort_running_first(
    tmp_path: Path,
) -> None:
    release = threading.Event()
    manager = _blocking_manager(tmp_path, release)
    try:
        running = manager.submit(MIX, "free")
        deadline = 100
        while manager.get(running).phase != "recognise" and deadline:
            threading.Event().wait(0.05)
            deadline -= 1
        queued = manager.submit(OTHER, "free")  # newer, and behind the running job
        assert manager.get(queued).status == "queued"
        assert manager.get(queued).created_at >= manager.get(running).created_at
        app = create_app(tmp_path, jobs=manager)
        csrf = token(app)
        home = request(app, "GET", "/").text
        cards = re.findall(r'data-job="([0-9a-f]{32})"', home)
        assert cards == [running, queued]  # running first, whatever was submitted last
        assert "Resolved mix title" in home and MIX not in home and OTHER not in home
        assert "Mixcloud mix" in home  # a job with no title yet gets a plain platform label
        # The card says exactly what the progress tracker says for the same job: the step's
        # own name from the tracker's step list, and the tracker's "In the queue" before it.
        tracker = request(app, "GET", f"/jobs/{running}").text
        steps = json.loads(re.search(r"var STEPS=(\[.*?\]);var CSRF", tracker).group(1))
        listen = next(name for key, name, _ in steps if key == "recognise")
        assert listen == "Listen" and f'<div class="act-phase">{listen}</div>' in home
        assert '<div class="act-phase">In the queue</div>' in home
        assert "Listening" not in home and "Waiting in the queue" not in home
        # Each card carries its own step list for the polling script, in the tracker's shape.
        card = re.search(rf'<li class="act" data-job="{running}"[^>]*data-steps="([^"]*)"', home)
        assert card is not None and json.loads(html_unescape(card.group(1))) == steps
        assert home.count("act-actions") == 0  # nothing in flight can be dismissed

        cancelled = request(app, "POST", f"/jobs/{queued}/cancel", headers={"X-CSRF-Token": csrf})
        assert cancelled.status_code == 200
        home = request(app, "GET", "/").text
        assert f'data-job="{queued}" data-terminal="1" data-status="cancelled"' in home
        assert f'action="/jobs/{queued}/dismiss"' in home
        dismissed = request(app, "POST", f"/jobs/{queued}/dismiss", data={"csrf_token": csrf})
        assert dismissed.status_code == 303
        assert queued not in request(app, "GET", "/").text
        assert (
            request(app, "POST", f"/jobs/{running}/dismiss", data={"csrf_token": csrf}).status_code
            == 400
        )
    finally:
        release.set()
        manager.shutdown()


# --------------------------------------------------------------------------------------------------
# U-F34: one form; ``/new`` lands on it; "+ New mix" focuses it; no inline handlers anywhere
# --------------------------------------------------------------------------------------------------
def test_one_form_new_redirects_to_it_and_new_mix_focuses_it(tmp_path: Path) -> None:
    source = _source("soundcloud")
    _seed_work_root(tmp_path, source)
    jobs = LocalJobs(tmp_path)
    app = create_app(tmp_path, jobs=jobs)
    home = request(app, "GET", "/")
    assert home.text.count('class="dropform"') == 1 and home.text.count('id="url"') == 1
    assert 'href="/new"' in home.text  # the top bar keeps its button; the page keeps the field
    assert request(app, "GET", "/new").headers["location"] == "/"
    assert request(app, "GET", "/new?url=soundcloud.com/a/b").headers["location"] == (
        "/?url=soundcloud.com%2Fa%2Fb"
    )
    prefilled = request(app, "GET", "/?url=soundcloud.com/a/b").text
    assert 'value="soundcloud.com/a/b"' in prefilled and prefilled.count('class="dropform"') == 1
    assert request(create_app(tmp_path), "GET", "/new").status_code == 404
    script = STATIC_JS.decode("utf-8")
    assert "querySelector('.topbar a[href=\"/new\"]')" in script
    assert "event.preventDefault();" in script and "input.focus(); input.select();" in script
    # The library's Remove confirmation moved out of the markup: no inline handler survives on
    # a page with a removable mix, and the confirm text rides along as data for the script.
    parsed = _markup(home.text)
    assert (
        parsed.handlers == [] and 'data-confirm="Remove this mix from your library?"' in home.text
    )
    assert "hasAttribute('data-confirm')" in script
    # The legacy card still asks from an inline handler; the served block never carries it.
    sets = legacy._discover_sets(tmp_path)
    assert "onclick" in legacy._mixes_block(sets, csrf_token="tok")
    served = pages.mixes_block(sets, csrf_token="tok")
    assert "onclick" not in served and _markup(served).handlers == []


# --------------------------------------------------------------------------------------------------
# Fix pass: a 304 keeps the document's policy; an uncaught error keeps the headers; one label rule
# --------------------------------------------------------------------------------------------------
def test_a_304_for_a_document_carries_exactly_the_policy_its_200_carried(tmp_path: Path) -> None:
    """A cache copies a 304's headers onto the stored page: the resource default would then
    block the page's own scripts, styles, embeds and audio on every routine revalidation."""

    media = seed(tmp_path)
    bundle = publish(media)
    source = _source("soundcloud")
    app = create_app(tmp_path)
    for route in (
        f"/{source.source_key}/{source.media_key}/present/index.html",
        _bundle_route(tmp_path, bundle, "index.html"),
    ):
        fresh = request(app, "GET", route)
        assert fresh.status_code == 200 and "'sha256-" in fresh.headers["content-security-policy"]
        etag = fresh.headers["etag"]
        for method in ("GET", "HEAD"):
            cached = request(app, method, route, headers={"If-None-Match": etag})
            assert cached.status_code == 304, (route, method)
            assert (
                cached.headers["content-security-policy"]
                == (fresh.headers["content-security-policy"])
            ), (route, method)
            assert cached.headers["cache-control"] == fresh.headers["cache-control"]
            for name, value in DEFENSIVE.items():
                assert cached.headers[name] == value, name
    # A non-document 304 keeps the resource default, as its 200 did.
    export = f"/{source.source_key}/{source.media_key}/present/tracklist.json"
    tag = request(app, "GET", export).headers["etag"]
    cached = request(app, "GET", export, headers={"If-None-Match": tag})
    assert cached.status_code == 304
    assert cached.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'self'; base-uri 'none'"
    )


def test_an_uncaught_exception_still_answers_with_every_header(tmp_path: Path, monkeypatch) -> None:
    def boom(state: object) -> bytes:
        raise RuntimeError("the file vanished between resolution and open()")

    monkeypatch.setattr(application, "_index_document", boom)
    app = create_app(tmp_path)

    async def send(method: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
            return await c.request(method, "/")

    for method in ("GET", "HEAD"):
        response = asyncio.run(send(method))
        assert response.status_code == 500, method
        for name, value in DEFENSIVE.items():
            assert response.headers[name] == value, (method, name)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-security-policy"] == (
            "default-src 'none'; frame-ancestors 'self'; base-uri 'none'"
        )
    assert asyncio.run(send("GET")).content == b"internal server error"
    assert asyncio.run(send("HEAD")).content == b""
    # The truth-review app shares the stack and the handler.
    from id_detector.truth_review import TruthReviewSession
    from idea_web import truth_review
    from idea_web.truth_review import create_truth_review_app

    corpus = tmp_path / "corpus"
    shutil.copytree(Path(__file__).resolve().parents[1] / "fixtures" / "truth-review", corpus)
    session = TruthReviewSession(
        corpus / "fixture-set" / "ground_truth.json", work_root=tmp_path / "w"
    )
    monkeypatch.setattr(truth_review, "review_page", lambda session, token: boom(None))
    review_app = create_truth_review_app(session)

    async def send_review() -> httpx.Response:
        transport = httpx.ASGITransport(app=review_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
            return await c.get("/")

    failed = asyncio.run(send_review())
    assert failed.status_code == 500 and failed.headers["x-frame-options"] == "SAMEORIGIN"


def test_activity_labels_are_the_trackers_labels_in_python_and_in_the_polling_script() -> None:
    """One label source (U-F18): the tracker's step list.  The card renderer, the polling script
    and the progress log all read it; no second table exists in Python or in JavaScript."""

    full = Job("a" * 32, MIX, MIX, "max_accuracy", True, True)
    lean = Job("b" * 32, OTHER, OTHER, "free", False, False)
    # The cases come from the tracker's own step list — every entry of it, for a job with every
    # optional step and for one with none — plus the states that are not steps.  The oracle is
    # the step's name as the tracker lists it, not any label function under test.
    all_steps = {key for key, _, _ in legacy._job_steps(full)}
    assert all_steps == {
        "build_index",
        "ingest",
        "decode",
        "windows",
        "recognise",
        "hints",
        "fuse",
        "enrich",
        "present",
    }
    non_steps = [
        ("queued", "queued"),
        ("running", "starting"),
        ("running", "build_index"),  # a step only when the job asked for the index
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("waiting", "waiting"),
    ]
    cases: dict[str, list[tuple[str, str]]] = {}
    expected: dict[str, dict[str, str]] = {}
    for shape, job in (("a", full), ("b", lean)):
        steps = legacy._job_steps(job)
        names = {key: name for key, name, _ in steps}
        cases[shape] = [("running", key) for key, _, _ in steps] + [
            case for case in non_steps if case[1] not in names
        ]
        expected[shape] = {}
        for status, phase in cases[shape]:
            if status == "queued":
                label = "In the queue"  # the tracker's wording before a job starts
            elif phase in names:
                label = names[phase]
            else:
                label = phase[:1].upper() + phase[1:]  # the tracker's fallback
            expected[shape][f"{status}:{phase}"] = label
            # Path 1: the Python card render.
            job.status, job.phase = status, phase
            assert f'<div class="act-phase">{label}</div>' in pages.activity_item_html(job, "tok")
    assert len(cases["a"]) == 9 + 5 and len(cases["b"]) == 7 + 6
    assert expected["a"]["running:build_index"] == "Index"
    assert expected["b"]["running:build_index"] == "Build_index"  # not a step of a lean job
    for step in ("decode", "windows", "hints", "fuse"):
        assert f"running:{step}" in expected["a"] and f"running:{step}" in expected["b"]
    bundle = STATIC_JS.decode("utf-8")
    for literal in ("'Listening'", "'Fetching the mix'", "'Prepare audio'", "'Build page'"):
        assert literal not in bundle, literal
    assert "STEPS.forEach(function(s){ labels[s[0]] = s[1]; });" in bundle  # the progress log
    node = shutil.which("node")
    if node is None:  # pragma: no cover - node is present in the gate environment
        pytest.skip("node is not installed")
    rule = re.search(r"  function phaseLabel\(.*?\n  \}\n", pages.HOME_JS, re.DOTALL).group(0)
    for shape, job in (("a", full), ("b", lean)):
        steps_js = json.dumps(legacy._job_steps(job))
        # Path 2: the polling rule (the card script's ``phaseLabel``) under Node.
        polling = (
            rule
            + f"var STEPS = {steps_js}; var CASES = {json.dumps(cases[shape])}; var out = {{}};"
            "CASES.forEach(function(c){ out[c[0] + ':' + c[1]] = phaseLabel(STEPS, c[0], c[1]); });"
            "console.log(JSON.stringify(out));"
        )
        result = subprocess.run([node, "-e", polling], capture_output=True, text=True, check=True)
        assert json.loads(result.stdout) == expected[shape], ("polling", shape)
        # Path 3: the tracker itself — the served progress script's render() (the frozen legacy
        # script, paced by ``pages.JOB_JS``) run under Node against a DOM stub, reading what it
        # wrote into ``#phase-name``.  A wrong label hard-coded in the tracker fails here.
        tracker = (
            _TRACKER_DOM
            + f"var JOB_ID={json.dumps(job.id)};var RETRY_HREF='/?job=x';var CSRF_TOKEN='t';"
            + f"var OUTCOME_COPY={json.dumps(legacy._outcome_copy_js())};var STEPS={steps_js};"
            + legacy._PROGRESS_JS
            + pages.JOB_JS
            + f"var CASES={json.dumps(cases[shape])}; var TERMINAL=['succeeded','failed',"
            "'cancelled','waiting']; var out = {};"
            "CASES.forEach(function(c){ render({status: c[0], phase: c[1], log: [],"
            "  terminal: TERMINAL.indexOf(c[0]) >= 0, windows_total: 0, windows_done: 0});"
            "  out[c[0] + ':' + c[1]] = byId['phase-name'].textContent; });"
            "console.log(JSON.stringify(out));"
        )
        result = subprocess.run([node, "-e", tracker], capture_output=True, text=True, check=True)
        assert json.loads(result.stdout) == expected[shape], ("tracker", shape)


#: A DOM stub just wide enough for the tracker's ``render()`` (and the outcome it shows when a
#: job is over) to run under Node: every element is a blank slate that records what was set.
_TRACKER_DOM = """
function el(){ return {textContent: '', innerHTML: '', className: '', hidden: false,
  style: {setProperty: function(){}}, children: [], classList: {add: function(){},
  remove: function(){}, toggle: function(){}, contains: function(){ return false; }},
  addEventListener: function(){}, appendChild: function(){}, setAttribute: function(){},
  getAttribute: function(){ return null; }, querySelector: function(){ return el(); },
  querySelectorAll: function(){ return []; }}; }
var byId = {};
var document = {getElementById: function(id){ return byId[id] || (byId[id] = el()); },
  querySelectorAll: function(){ return []; }, body: el(), createElement: function(){ return el(); },
  title: ''};
var window = {};
globalThis.fetch = function(){ return new Promise(function(){}); };
globalThis.setInterval = function(){}; globalThis.setTimeout = function(){};
"""


class _Transition:
    """A live transition on a real job manager: A runs and B waits; then A fails and B starts.

    Captures the list as first rendered (``rendered``, with its cards' attributes), the status
    JSON before and after, and the fresh server render after (``fresh``, which ``GET /activity``
    must equal).  Used with ``with``: the manager is released and shut down on exit.
    """

    def __init__(self, work_root: Path) -> None:
        self.work_root = work_root
        self.fail_a, self.release = threading.Event(), threading.Event()
        self.manager = JobManager(work_root, self._runner)

    def _runner(self, ctx: JobContext) -> None:
        if ctx.target == MIX:
            ctx.progress("recognise", 3, 10, "")
            self.fail_a.wait(timeout=20)
            raise RuntimeError("the platform said no")
        ctx.progress("recognise", 1, 10, "")
        self.release.wait(timeout=20)

    @staticmethod
    def _wait_for(condition, what: str) -> None:
        deadline = 200
        while not condition() and deadline:
            threading.Event().wait(0.05)
            deadline -= 1
        assert condition(), what

    @staticmethod
    def _list(page: str) -> str:
        found = re.search(r'<ul class="acts">.*?</ul>', page, re.S)
        assert found is not None
        return found.group(0)

    def __enter__(self) -> _Transition:
        manager = self.manager
        self.a = a = manager.submit(MIX, "free")
        self._wait_for(lambda: manager.get(a).phase == "recognise", "A running")
        self.b = b = manager.submit(OTHER, "free")
        self.app = app = create_app(self.work_root, jobs=manager)
        self.rendered = self._list(request(app, "GET", "/").text)
        self.pre = {job: request(app, "GET", f"/jobs/{job}/status").json() for job in (a, b)}
        self.fail_a.set()
        self._wait_for(lambda: manager.get(a).status == "failed", "A failed")
        self._wait_for(lambda: manager.get(b).phase == "recognise", "B running")
        self.post = {job: request(app, "GET", f"/jobs/{job}/status").json() for job in (a, b)}
        self.fresh = self._list(request(app, "GET", "/").text)
        self.cards = [
            {
                "id": job,
                "status": re.search(rf'data-job="{job}"[^>]*data-status="([a-z]+)"', self.rendered)[
                    1
                ],
                "terminal": re.search(rf'data-job="{job}"[^>]*data-terminal="(\d)"', self.rendered)[
                    1
                ],
                "steps": json.loads(
                    html_unescape(
                        re.search(rf'data-job="{job}"[^>]*data-steps="([^"]*)"', self.rendered)[1]
                    )
                ),
            }
            for job in (a, b)
        ]
        return self

    def __exit__(self, *exc: object) -> None:
        self.fail_a.set()
        self.release.set()
        self.manager.shutdown()

    def drive(self, mode: str) -> dict:
        """Run the real polling script under Node against the rendered cards.

        Round 1 polls with the "before" statuses; round 2 with the "after" ones while
        ``/activity`` answers per ``mode`` (``ok``, ``500`` or ``malformed``); round 3 polls again
        at once (inside any back-off); round 4 polls after the clock moved past it, with
        ``/activity`` healthy again.
        """

        node = shutil.which("node")
        if node is None:  # pragma: no cover - node is present in the gate environment
            pytest.skip("node is not installed")
        harness = (
            _CARDS_DOM.replace("__CARDS__", json.dumps(self.cards))
            .replace("__PRE__", json.dumps(self.pre))
            .replace("__POST__", json.dumps(self.post))
            .replace("__ACTIVITY__", json.dumps(self.fresh))
            .replace("__MODE__", json.dumps(mode))
            + legacy._PROGRESS_JS
            + pages.HOME_JS
            + _CARDS_DRIVER
        )
        result = subprocess.run([node, "-e", harness], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)


def test_polling_rerenders_the_list_when_a_job_ends_or_another_starts(tmp_path: Path) -> None:
    """U-F18 across a live transition: A runs and B waits; A fails and B starts.  The cards that
    were already rendered must end up exactly as a fresh render shows them — Dismiss on A, B first
    — without a reload.  The polling script runs under Node against the rendered cards."""

    with _Transition(tmp_path) as t:
        a, b, app = t.a, t.b, t.app
        assert re.findall(r'data-job="([0-9a-f]{32})"', t.rendered) == [a, b]
        assert "Dismiss" not in t.rendered
        assert t.pre[a]["status"] == "running" and t.pre[b]["status"] == "queued"
        assert t.post[a]["terminal"] is True and t.post[b]["status"] == "running"
        assert re.findall(r'data-job="([0-9a-f]{32})"', t.fresh) == [b, a]  # running first
        assert f'action="/jobs/{a}/dismiss"' in t.fresh
        assert f'action="/jobs/{b}/dismiss"' not in t.fresh
        fragment = request(app, "GET", "/activity")
        assert fragment.status_code == 200 and fragment.text == t.fresh  # what the script swaps in
        assert fragment.headers["cache-control"] == "no-store"
        assert request(create_app(tmp_path), "GET", "/activity").status_code == 404

        seen = t.drive("ok")
        # Round 1, nothing changed kind: cards patched in place with the tracker's labels.
        assert seen["rounds"][0] == {
            "swapped": 0,
            "phases": ["Listen", "In the queue"],
            "timers": 2,
        }
        # Round 2, A ended and B started: one fresh list replaces the rendered one.
        assert seen["rounds"][1]["swapped"] == 1 and seen["rounds"][1]["cards"] == 0
        assert seen["swapped"] == [t.fresh] and seen["reloads"] == 0
        assert seen["fetched"].count("/activity") == 1


@pytest.mark.parametrize("mode", ["500", "malformed"])
def test_a_failed_activity_refresh_keeps_the_list_and_keeps_polling(
    tmp_path: Path, mode: str
) -> None:
    """A ``/activity`` answer that is not a fresh list (an error status, or some other body) must
    change nothing on screen and must be retried: the rendered list stays, polling goes on, the
    retry waits out a back-off, and a healthy answer later still swaps the list in."""

    with _Transition(tmp_path) as t:
        seen = t.drive(mode)
        rounds = seen["rounds"]
        assert rounds[0]["swapped"] == 0 and rounds[0]["timers"] == 2
        # Round 2: the transition is seen, the refresh fails — the list is untouched, both cards
        # are still connected and polling is re-armed.
        assert rounds[1] == {
            "swapped": 0,
            "phases": ["Listen", "In the queue"],
            "timers": 2,
            "cards": 2,
            "activity": 1,
        }
        # Round 3, straight away: still inside the back-off, so no second attempt yet; the cards
        # keep polling regardless.
        assert rounds[2] == {
            "swapped": 0,
            "phases": ["Listen", "In the queue"],
            "timers": 2,
            "cards": 2,
            "activity": 1,
        }
        # Round 4, after the back-off, with /activity healthy: the fresh list finally swaps in.
        assert rounds[3]["swapped"] == 1 and rounds[3]["cards"] == 0 and rounds[3]["activity"] == 2
        assert seen["swapped"] == [t.fresh] and seen["reloads"] == 0
        # The 500 answer is itself a valid list, so only the response status can have rejected it:
        # nothing it carried ever reached the page.
        assert not any("error-body-never-shown" in html for html in seen["swapped"])


#: The rendered activity list as the polling script sees it: cards with their attributes and the
#: parts the script patches; ``fetch`` answers with the server's real status JSON, and for
#: ``/activity`` the real fragment, a 500, or a body that is not a list, per ``MODE``.
_CARDS_DOM = """
var timers = [], swapped = [], fetched = [], reloads = 0, clock = 1000000;
globalThis.setTimeout = function(fn){ timers.push(fn); };
Date.now = function(){ return clock; };
function makeCard(c){
  var attrs = {'data-job': c.id, 'data-status': c.status, 'data-terminal': c.terminal,
    'data-pct': '0', 'data-steps': JSON.stringify(c.steps)};
  var parts = {'.st': {textContent: '', className: ''}, '.act-phase': {textContent: ''},
    '.act-title': {textContent: ''}, '.bar>span': {style: {}}};
  return {isConnected: true, parts: parts,
    getAttribute: function(k){ return attrs[k]; },
    setAttribute: function(k, v){ attrs[k] = String(v); },
    querySelector: function(s){ return parts[s]; }};
}
var list = {cards: __CARDS__.map(makeCard), querySelectorAll: function(){ return this.cards; }};
Object.defineProperty(list, 'outerHTML', {set: function(html){
  swapped.push(html);
  this.cards.forEach(function(c){ c.isConnected = false; }); this.cards = []; }});
var document = {querySelector: function(sel){ return sel === 'ul.acts' ? list : null; }};
var window = {location: {reload: function(){ reloads++; }}};
var STATUSES = __PRE__, POST = __POST__, ACTIVITY = __ACTIVITY__, MODE = __MODE__;
// The error answer is a VALID list, so only the response status can reject it: if the status
// guard went, this body would be swapped in and the assertions below would catch it.
var ERROR_LIST = '<ul class="acts"><li class="act" data-job="never">'
  + 'error-body-never-shown</li></ul>';
function answer(ok, status, payload, key){ var r = {ok: ok, status: status};
  r[key] = function(){ return Promise.resolve(payload); }; return Promise.resolve(r); }
globalThis.fetch = function(url){
  fetched.push(url);
  if(url === '/activity'){
    if(MODE === '500') return answer(false, 500, ERROR_LIST, 'text');
    if(MODE === 'malformed') return answer(true, 200, '<!doctype html><title>x</title>', 'text');
    return answer(true, 200, ACTIVITY, 'text');
  }
  return answer(true, 200, STATUSES[url.split('/')[2]], 'json');
};
"""
_CARDS_DRIVER = """
(async function(){
  async function flush(){
    for(var i = 0; i < 20; i++) await new Promise(function(r){ setImmediate(r); }); }
  function attempts(){ return fetched.filter(function(u){ return u === '/activity'; }).length; }
  function snapshot(){ return {swapped: swapped.length, timers: timers.length,
    cards: list.cards.length, activity: attempts(),
    phases: list.cards.map(function(c){ return c.parts['.act-phase'].textContent; })}; }
  function round(){ timers.splice(0).forEach(function(fn){ fn(); }); }
  var rounds = [];
  await flush();
  var first = snapshot(); delete first.cards; delete first.activity; rounds.push(first);
  STATUSES = POST; round(); await flush(); rounds.push(snapshot());
  round(); await flush(); rounds.push(snapshot());
  clock += 60000; MODE = 'ok'; round(); await flush(); rounds.push(snapshot());
  console.log(JSON.stringify({rounds: rounds, swapped: swapped, reloads: reloads,
    fetched: fetched}));
})();
"""
