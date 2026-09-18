"""The FastAPI application behind ``idea serve`` (loopback) and a future hosted launcher.

One application, two policies: local mode serves the owner's fetched audio, hosted mode never does.
Request handlers validate, authorise, enqueue and read state.  They never run a pipeline (the job
queue adapter only enqueues; ``idea serve`` runs the analysis in a separate worker process) and
never write results: a GET serves sealed bundles exactly as they are on disk.  A page rendered by
an older ``PAGE_VERSION`` is refreshed by the server's start-up pass, before the first request.

Routing deliberately mirrors the retired stdlib handler's dispatch order (``GET`` then ``POST``),
because that order is part of the contract its tests and the local gate pin: the loopback gate
runs before routing, a malformed route is a 404 before any token check, and ``HEAD`` is ``GET``
without a body.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request
from fastapi.responses import Response
from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.concurrency import run_in_threadpool

from id_detector.ingest import _load_cached
from id_detector.io import native_path
from id_detector.present import server as legacy
from id_detector.present.bundles import read_bundle_manifest, result_dir
from id_detector.providers.base import AppConfig
from id_detector.webapp.jobs import TargetValidationError
from idea_web import pages
from idea_web.http import (
    CSS,
    HTML,
    IMMUTABLE,
    IMMUTABLE_PRIVATE,
    JAVASCRIPT,
    JSON,
    REVALIDATE,
    TEXT,
    UNSUPPORTED_METHODS,
    bytes_response,
    contained_directory,
    contained_file,
    content_security_policy,
    csrf_matches,
    deliver,
    drain_request,
    etag_matches,
    html_response,
    install_middleware,
    json_response,
    not_found,
    not_modified,
    range_response,
    read_bounded,
    redirect,
    weak_etag,
)
from idea_web.pages import ASSET_VERSION, STATIC_CSS, STATIC_JS

if TYPE_CHECKING:
    # Accounts are hosted-mode code: the local application (``idea serve``) never imports them,
    # so local start-up loads neither ``idea_web.auth`` nor argon2. Hosted mode imports them in
    # ``create_app``.
    from idea_web.auth import HostedSettings, SignedIn
    from idea_web.hosted import HostedAuth

_SHA = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_CSRF_HEADER = "X-CSRF-Token"
_CSRF_FIELD = "csrf_token"
_PROFILES = ("free", "max_accuracy")
_CONTENT_TYPES = {
    ".html": HTML,
    ".json": JSON,
    ".cue": TEXT,
    ".md": TEXT,
    ".css": CSS,
    ".js": JAVASCRIPT,
}
_PLATFORM_LINK = re.compile(
    r"(^|\.)(soundcloud\.com|mixcloud\.com|youtube\.com|youtu\.be)(/|$)", re.IGNORECASE
)
_CSRF_REFUSAL = {"error": f"missing or invalid CSRF token (GET /csrf, then send {_CSRF_HEADER})"}
#: The versioned static assets (``idea_web.pages``), under the URLs the live pages link.
_STATIC = {
    pages.STYLESHEET_HREF: (STATIC_CSS, CSS),
    pages.SCRIPT_SRC: (STATIC_JS, JAVASCRIPT),
}
#: Hosted routes that answer a script, so a missing session is a 401 rather than a redirect to the
#: sign-in page: the JSON and fragment reads, and every POST a page script makes.
_SCRIPT_GET = re.compile(r"/csrf|/activity|/jobs/[0-9a-f]{32}/status|/playlists/state")
_SCRIPT_POST = re.compile(r"/jobs/[0-9a-f]{32}/cancel|/playlists(/.*)?")
#: The export files of a result (plan §4.4: export routes are limited to 10 per minute per
#: address on a hosted server). The result page itself and a bundle's page resources are not
#: exports.
_EXPORT_SUFFIXES = frozenset({".cue", ".md", ".json"})
__all__ = [
    "ASSET_VERSION",
    "STATIC_CSS",
    "STATIC_JS",
    "WebSettings",
    "create_app",
]


class JobQueueAdapter(Protocol):
    """What the web layer may do with jobs: enqueue, read, request cancellation, dismiss.

    ``user_id`` is the signed-in account a hosted submission belongs to (stored in
    ``jobs.user_id``); local mode never passes one.
    """

    def submit(
        self,
        target: str,
        profile: str | None = None,
        *,
        acquire: bool = False,
        build_index: bool = False,
        known_tracklist: str | None = None,
        user_id: str | None = None,
    ) -> str: ...

    def get(self, job_id: str) -> Any: ...

    def recent(self, limit: int = 25) -> list[Any]: ...

    def cancel(self, job_id: str) -> bool: ...

    def dismiss(self, job_id: str) -> bool: ...


@dataclass(frozen=True)
class WebSettings:
    """Small mode boundary shared by ``idea serve`` and a future hosted launcher."""

    work_root: Path
    local: bool = True
    analyse_enabled: bool = False
    config: AppConfig | None = None

    @property
    def emit_local_audio(self) -> bool:
        return self.local


@dataclass(frozen=True)
class _Viewer:
    """Who one request is from, as far as pages and token checks care.

    Local mode has one viewer: no account, and the process-wide token for both checking and
    rendering (exactly as before accounts existed). Hosted mode's viewer is a signed-in session.
    """

    csrf: str
    request_csrf: str
    session: SignedIn | None = None


@dataclass
class _State:
    settings: WebSettings
    jobs: JobQueueAdapter | None
    csrf_token: str
    templates: Environment
    hosted: HostedAuth | None = None
    #: Resolved fetched-audio paths per job.  Resolution verifies the download, so it is done once
    #: per job rather than on every 2.5-second status poll.
    audio_paths: dict[str, str] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.settings.analyse_enabled and self.jobs is not None

    @property
    def local_viewer(self) -> _Viewer:
        return _Viewer(self.csrf_token, self.csrf_token)


def _topbar(viewer: _Viewer, *, back: bool, new: bool) -> str:
    bar = legacy.topbar_html(back=back, new=new)
    if viewer.session is None:
        return bar
    account = viewer.session.account
    return bar + pages.account_strip_html(account.email, viewer.csrf, admin=account.is_admin)


def _parse_form(raw: bytes) -> dict[str, str]:
    parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


def _template(state: _State, name: str, **context: object) -> bytes:
    return state.templates.get_template(name).render(**context).encode("utf-8")


def _active_jobs(state: _State) -> list[Any]:
    """The home's activity list: running first, then queued, then what stopped (U-F18)."""

    assert state.jobs is not None
    active = [job for job in state.jobs.recent() if job.status != "succeeded"]
    rank = {"running": 0, "queued": 1, "waiting": 2, "failed": 3, "cancelled": 4}
    active.sort(key=lambda job: (rank.get(job.status, 5), -job.created_at))
    return active


def _activity_html(state: _State, viewer: _Viewer) -> str:
    active = _active_jobs(state)
    return pages.activity_list_html(active, viewer.csrf) if active else ""


def _home_document(state: _State, form_state: legacy._FormState, viewer: _Viewer) -> bytes:
    assert state.jobs is not None
    work_root = state.settings.work_root
    sets = legacy._discover_sets(work_root)
    csrf = viewer.csrf
    return _template(
        state,
        "home.html",
        head=pages.head_html("ID'er — your mixes", hosted=state.hosted is not None),
        topbar=_topbar(viewer, back=False, new=True),
        form=legacy._form_html(csrf_token=csrf, state=form_state),
        activity=_activity_html(state, viewer),
        stats=legacy._library_stats_html(sets),
        mixes=pages.mixes_block(
            sets, csrf_token=csrf, failed_runs=legacy._discover_failed_runs(work_root)
        ),
        footer=legacy._footer_html(),
        script_src=pages.SCRIPT_SRC,
    )


def _index_document(state: _State, viewer: _Viewer) -> bytes:
    sets = legacy._discover_sets(state.settings.work_root)
    return _template(
        state,
        "index.html",
        head=pages.head_html("ID'er — analysed sets", hosted=state.hosted is not None),
        topbar=_topbar(viewer, back=False, new=False),
        stats=legacy._library_stats_html(sets),
        mixes=pages.mixes_block(sets, allow_new=False),
        footer=legacy._footer_html(),
    )


def _job_document(state: _State, job: Any, viewer: _Viewer) -> bytes:
    plan = legacy.plan_embed_from_url(job.target)
    platform = plan.kind if plan.kind in ("soundcloud", "youtube", "mixcloud") else "file"
    steps = legacy._job_steps(job)
    # The per-job constants are the page's only inline script; the shared script is static.
    # ``</`` cannot appear: the id and token are URL-safe and the tables are the server's own.
    config = (
        f"var JOB_ID={json.dumps(job.id)};"
        f"var RETRY_HREF={json.dumps(f'/?job={job.id}')};"
        f"var OUTCOME_COPY={json.dumps(legacy._outcome_copy_js())};"
        f"var STEPS={json.dumps(steps)};var CSRF_TOKEN={json.dumps(viewer.csrf)};"
    )
    return _template(
        state,
        "job.html",
        head=pages.head_html("Analysing — ID'er", hosted=state.hosted is not None),
        topbar=_topbar(viewer, back=True, new=True),
        title=legacy._job_title(job),
        platform_chip=legacy.platform_chip(platform),
        player=legacy._job_player_html(plan, job),
        steps=steps,
        footer=legacy._footer_html(),
        config=config,
        script_src=pages.SCRIPT_SRC,
    )


def _safe_parts(path: str) -> list[str] | None:
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if any(part == ".." or "\\" in part or ":" in part for part in parts):
        return None
    return parts


def _is_export(route: str) -> bool:
    """Whether a route names a result's export file (the shape ``_result_file`` serves)."""

    parts = _safe_parts(route)
    if parts is None or len(parts) not in {4, 6} or parts[2] != "present":
        return False
    return Path(parts[-1]).suffix.lower() in _EXPORT_SUFFIXES


def _result_file(settings: WebSettings, route: str) -> Path | None:
    """A sealed result file, read-only: the selected result's page/exports or a manifest member."""

    parts = _safe_parts(route)
    if parts is None or len(parts) not in {4, 6} or parts[2] != "present":
        return None
    name = parts[-1]
    if Path(name).suffix.lower() not in _CONTENT_TYPES:
        return None
    media_dir = settings.work_root / parts[0] / parts[1]
    if len(parts) == 6:
        if parts[3] != "bundles" or not _SHA.fullmatch(parts[4]):
            return None
        directory = media_dir / "present" / "bundles" / parts[4]
        manifest = read_bundle_manifest(directory)
        if manifest is None or name not in manifest["files"]:
            return None
        return contained_file(settings.work_root, directory / name)
    return contained_file(settings.work_root, result_dir(media_dir) / name)


def _served_audio(settings: WebSettings, route: str) -> Path | None:
    """The result page's ``<audio>`` source: a contained local audio file (local mode only)."""

    if not settings.emit_local_audio:
        return None
    parts = _safe_parts(route)
    if parts is None or len(parts) < 3:
        return None
    audio = contained_file(settings.work_root, settings.work_root.joinpath(*parts))
    if audio is None or audio.suffix.lstrip(".").casefold() not in legacy._AUDIO_TYPES:
        return None
    return audio


def _job_audio(state: _State, job: Any) -> Path | None:
    if not state.settings.emit_local_audio:
        return None
    if not job.audio_path and job.id in state.audio_paths:
        job.audio_path = state.audio_paths[job.id]
    audio = legacy._resolve_job_audio(job, state.settings.work_root)
    if audio is not None:
        state.audio_paths[job.id] = str(audio)
    return audio


def _job_get(state: _State, request: Request, route: str, viewer: _Viewer) -> Response:
    assert state.jobs is not None
    segments = route.strip("/").split("/")
    if len(segments) == 2 and _JOB_ID.fullmatch(segments[1]):
        job = state.jobs.get(segments[1])
        if job is None:
            return not_found(b"unknown job")
        return html_response(HTTPStatus.OK, _job_document(state, job, viewer))
    if len(segments) == 3 and _JOB_ID.fullmatch(segments[1]) and segments[2] == "status":
        job = state.jobs.get(segments[1])
        if job is None:
            return json_response(HTTPStatus.NOT_FOUND, {"error": "unknown job"})
        _job_audio(state, job)
        payload = job.status_dict()
        if not state.settings.emit_local_audio:
            # Hosted mode serves no local audio, so it must not advertise any either.
            payload["audio_url"] = None
        return json_response(HTTPStatus.OK, payload)
    if len(segments) == 3 and _JOB_ID.fullmatch(segments[1]) and segments[2] == "audio":
        job = state.jobs.get(segments[1])
        audio = _job_audio(state, job) if job is not None else None
        audio = contained_file(state.settings.work_root, audio) if audio is not None else None
        if audio is None:
            return not_found(b"no audio yet")
        return range_response(
            request.method,
            request.headers.get("range", ""),
            audio,
            legacy._audio_content_type(audio),
        )
    return not_found()


def _media_audio(state: _State, request: Request, route: str) -> Response:
    match = re.fullmatch(r"/media/([a-f0-9]{64})/audio", route)
    audio = None
    if match and state.settings.emit_local_audio:
        cached = _load_cached(state.settings.work_root, match.group(1))
        if cached is not None:
            audio = contained_file(cached.media_dir, cached.original_path)
    if audio is None:
        return not_found()
    return range_response(
        request.method, request.headers.get("range", ""), audio, legacy._audio_content_type(audio)
    )


def _get(state: _State, request: Request, viewer: _Viewer) -> Response:
    settings = state.settings
    route = request.url.path
    if route == "/healthz":
        return json_response(HTTPStatus.OK, {"ok": True})
    if route == "/csrf":
        # Readable only by this origin's own scripts (no CORS header is ever sent).
        return json_response(HTTPStatus.OK, {"token": viewer.csrf})
    if route.startswith("/static/"):
        asset = _STATIC.get(route)
        if asset is None and state.hosted is not None and route == pages.HOSTED_STYLESHEET_HREF:
            asset = (pages.HOSTED_CSS, CSS)  # the hosted-only stylesheet: not a local asset
        if asset is None:
            return not_found()
        body, content_type = asset
        return bytes_response(HTTPStatus.OK, body, content_type, cache=IMMUTABLE)
    if route == "/playlists" or route.startswith("/playlists/"):
        # The playlists feature owns its storage and views; the web layer only routes to it.
        from id_detector import playlists

        status, body, content_type = playlists.handle_get(
            route, parse_qs(request.url.query), work_root=settings.work_root
        )
        return deliver(status, body, content_type)
    if route in ("/", "/index.html"):
        if state.active:
            assert state.jobs is not None
            query = parse_qs(request.url.query)
            prefill = (query.get("url") or [""])[0][:2048]
            # "Try again" links by job id, never by URL: the submitted target (possibly a private
            # share token) is looked up here and never reaches the job page (U-F31).
            requested = (query.get("job") or [""])[0]
            if not prefill and _JOB_ID.fullmatch(requested):
                earlier = state.jobs.get(requested)
                if earlier is not None:
                    prefill = earlier.target[:2048]
            body = _home_document(state, legacy._FormState(url=prefill), viewer)
        else:
            body = _index_document(state, viewer)
        return html_response(HTTPStatus.OK, body)
    if state.active and route == "/new":
        prefill = (parse_qs(request.url.query).get("url") or [""])[0][:2048]
        return redirect("/" + (f"?url={quote(prefill, safe='')}" if prefill else ""))
    if state.active and route == "/activity":
        # The home's activity list, freshly rendered: what the card polling swaps in when a job
        # ends or starts, so the list never needs a reload to be right (U-F18).
        return html_response(HTTPStatus.OK, _activity_html(state, viewer).encode("utf-8"))
    if state.active and route.startswith("/jobs/"):
        return _job_get(state, request, route, viewer)
    if route.startswith("/media/"):
        return _media_audio(state, request, route)
    audio = _served_audio(settings, route)
    if audio is not None:
        return range_response(
            request.method,
            request.headers.get("range", ""),
            audio,
            legacy._audio_content_type(audio),
        )
    served = _result_file(settings, route)
    if served is None:
        return not_found()
    with open(native_path(served), "rb") as handle:
        body = handle.read()
    # A bundle file never changes under its URL; the canonical route follows the newest run, so a
    # browser revalidates it and the ETag answers with nothing when nothing moved.
    content_type = _CONTENT_TYPES[served.suffix.lower()]
    cache = IMMUTABLE_PRIVATE if "/bundles/" in route else REVALIDATE
    etag = weak_etag(body)
    if etag_matches(request.headers.get("if-none-match"), etag):
        # A 304 updates the cached document's headers, so it must carry the document's own
        # policy: the resource default would leave the reloaded page unable to run or style.
        policy = content_security_policy(body) if content_type == HTML else None
        return not_modified(etag, cache, policy=policy)
    return deliver(HTTPStatus.OK, body, content_type, cache=cache, etag=etag)


def _analyse(
    state: _State,
    request: Request,
    raw: bytes | None,
    wants_json: bool,
    viewer: _Viewer,
    *,
    admit: Callable[[], float | None] | None = None,
) -> Response:
    """The analyse form or JSON submission.

    Hosted mode passes ``admit`` (the per-IP analyse limit, counted only for a submission that is
    about to be queued), and the signed-in account becomes the job's ``user_id``. Local mode
    passes neither and behaves exactly as it always has.
    """

    assert state.jobs is not None

    def _form_error(form_state: legacy._FormState, status: int) -> Response:
        return html_response(status, _home_document(state, form_state, viewer))

    if raw is None:
        if wants_json:
            return json_response(HTTPStatus.BAD_REQUEST, {"error": "bad length"})
        return _form_error(
            legacy._FormState(error="That submission was too large."), HTTPStatus.BAD_REQUEST
        )
    # ``upload_consent`` is read from no body (E-H8): no client field can unlock a larger charge.
    try:
        if wants_json:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("JSON body is not an object")
            url = str(payload.get("url", ""))
            profile_value = payload.get("profile")
            profile = str(profile_value) if profile_value is not None else None
            acquire = bool(payload.get("acquire"))
            build_index = bool(payload.get("build_index"))
            known_tracklist: Any = payload.get("known_tracklist")
            token: Any = payload.get(_CSRF_FIELD)
        else:
            form = _parse_form(raw)
            url = form.get("url", "")
            profile = form.get("profile")
            acquire = "acquire" in form
            build_index = "build_index" in form
            known_tracklist = form.get("known_tracklist", "")
            token = form.get(_CSRF_FIELD)
    except (ValueError, UnicodeDecodeError):
        if wants_json:
            return json_response(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
        return _form_error(
            legacy._FormState(error="The form could not be read."), HTTPStatus.BAD_REQUEST
        )
    if not csrf_matches(request, token, viewer.request_csrf):
        if wants_json:
            return json_response(HTTPStatus.FORBIDDEN, {"error": "CSRF token was invalid"})
        return _form_error(
            legacy._FormState(
                url=url,
                profile=profile or "free",
                acquire=acquire,
                known_tracklist=str(known_tracklist or ""),
                error="This page expired. Reload it and try again.",
            ),
            HTTPStatus.FORBIDDEN,
        )
    if profile is not None and profile not in _PROFILES:
        if wants_json:
            return json_response(HTTPStatus.BAD_REQUEST, {"error": "unknown profile"})
        # Every inline error re-renders the form with what was typed (U-F1); a bad mode value
        # must not eat the link, the pasted tracklist or the links choice either.
        return _form_error(
            legacy._FormState(
                url=url,
                acquire=acquire,
                known_tracklist=str(known_tracklist or ""),
                error="Choose Free scan or Deep scan.",
            ),
            HTTPStatus.BAD_REQUEST,
        )
    # A pasted tracklist is an optional hint seed; blank means "audio only", and it is capped.
    if not isinstance(known_tracklist, str) or not known_tracklist.strip():
        known_tracklist = None
    elif len(known_tracklist) > 64_000:
        known_tracklist = known_tracklist[:64_000]
    if not wants_json and "://" not in url and _PLATFORM_LINK.search(url):
        url = "https://" + url
    submit_options: dict[str, Any] = {}
    if viewer.session is not None:
        submit_options["user_id"] = viewer.session.account.id
    if admit is not None:
        wait = admit()
        if wait is not None:
            minutes = max(1, -(-int(wait) // 60))
            message = f"Too many analyses from your network. Try again in {minutes} minutes."
            if wants_json:
                refused = json_response(HTTPStatus.TOO_MANY_REQUESTS, {"error": message})
            else:
                refused = _form_error(
                    legacy._FormState(
                        url=url,
                        profile=profile or "free",
                        acquire=acquire,
                        known_tracklist=known_tracklist or "",
                        error=message,
                    ),
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
            refused.headers["Retry-After"] = str(max(1, int(wait)))
            return refused
    try:
        job_id = state.jobs.submit(
            url,
            profile,
            acquire=acquire,
            build_index=build_index,
            known_tracklist=known_tracklist,
            **submit_options,
        )
    except TargetValidationError as exc:
        if wants_json:
            return json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        return _form_error(
            legacy._FormState(
                url=url,
                profile=profile or "free",
                acquire=acquire,
                known_tracklist=known_tracklist or "",
                error="Use a complete web link or choose an audio file that exists.",
            ),
            HTTPStatus.BAD_REQUEST,
        )
    location = f"/jobs/{job_id}"
    if wants_json:
        return json_response(HTTPStatus.OK, {"id": job_id, "location": location})
    return redirect(location)


def _playlists_post(
    state: _State, request: Request, route: str, raw: bytes | None, viewer: _Viewer
) -> Response:
    """Playlist mutations reuse the analyse route's guards: loopback Origin/Host plus CSRF."""

    if raw is None:
        return json_response(HTTPStatus.BAD_REQUEST, {"error": "bad length"})
    try:
        form = _parse_form(raw)
    except (ValueError, UnicodeDecodeError):
        return json_response(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
    if not csrf_matches(request, form.get(_CSRF_FIELD), viewer.request_csrf):
        return json_response(HTTPStatus.FORBIDDEN, _CSRF_REFUSAL)
    from id_detector import playlists

    status, body, content_type = playlists.handle_post(
        route, form, work_root=state.settings.work_root
    )
    return bytes_response(status, body, content_type)


def _library_remove(
    state: _State, request: Request, raw: bytes | None, viewer: _Viewer
) -> Response:
    """Move one local user's result to recoverable trash and return to the library."""

    bad = bytes_response(HTTPStatus.BAD_REQUEST, b"bad request", TEXT)
    if raw is None:
        return bad
    try:
        form = _parse_form(raw)
    except (ValueError, UnicodeDecodeError):
        form = {}
    source_key = form.get("source_key", "")
    media_key = form.get("media_key", "")
    if (
        not csrf_matches(request, form.get(_CSRF_FIELD), viewer.request_csrf)
        or not _SHA.fullmatch(source_key)
        or not _SHA.fullmatch(media_key)
    ):
        return bad
    work_root = state.settings.work_root
    items = [*legacy._discover_sets(work_root), *legacy._discover_failed_runs(work_root)]
    item = next((x for x in items if x.source_key == source_key and x.media_key == media_key), None)
    if item is None:
        return not_found()
    target = contained_directory(work_root, item.media_dir)
    if target is None:
        return bad
    trash = work_root / ".trash" / "library" / f"{source_key}-{media_key}-{secrets.token_hex(4)}"
    trash.parent.mkdir(parents=True, exist_ok=True)
    target.replace(trash)
    return redirect("/")


def _dismiss(
    state: _State, request: Request, route: str, raw: bytes | None, viewer: _Viewer
) -> Response:
    assert state.jobs is not None
    parts = route.strip("/").split("/")
    try:
        form = _parse_form(raw) if raw is not None else {}
    except (ValueError, UnicodeDecodeError):
        form = {}
    if (
        len(parts) != 3
        or not _JOB_ID.fullmatch(parts[1])
        or not csrf_matches(request, form.get(_CSRF_FIELD), viewer.request_csrf)
        or not state.jobs.dismiss(parts[1])
    ):
        return bytes_response(HTTPStatus.BAD_REQUEST, b"bad request", TEXT)
    return redirect("/")


def _cancel(state: _State, request: Request, route: str, viewer: _Viewer) -> Response:
    assert state.jobs is not None
    segments = route.strip("/").split("/")
    # The route is validated before the token, exactly as before: a malformed cancel path is a
    # plain 404 whatever headers it carries.
    if len(segments) != 3 or not _JOB_ID.fullmatch(segments[1]) or segments[2] != "cancel":
        return not_found()
    if not csrf_matches(request, None, viewer.request_csrf):
        return json_response(HTTPStatus.FORBIDDEN, _CSRF_REFUSAL)
    cancelled = state.jobs.cancel(segments[1])
    if state.jobs.get(segments[1]) is None:
        return json_response(HTTPStatus.NOT_FOUND, {"error": "unknown job"})
    return json_response(HTTPStatus.OK, {"cancelled": cancelled})


def _hosted_get(state: _State, request: Request) -> Response:
    """Hosted GET: the public few (health, static, sign-in, set-password), else a live session."""

    from idea_web.hosted import unauthenticated

    auth = state.hosted
    assert auth is not None
    route = request.url.path
    if route == "/healthz" or route.startswith("/static/"):
        return _get(state, request, _Viewer("", ""))
    session = auth.session(request)
    if route == "/login":
        response = auth.login_get(request, session)
    elif route.startswith("/reset/"):
        response = auth.password_get(request, route)
    elif session is None:
        response = unauthenticated(request, wants_json=_SCRIPT_GET.fullmatch(route) is not None)
    elif route == "/admin":
        # The admin surface is invisible to everyone but an admin.
        response = auth.admin_page(session) if session.account.is_admin else not_found()
    elif _is_export(route) and (wait := auth.admit("export", request)) is not None:
        response = auth.refuse_export(wait)
    else:
        response = _get(state, request, _Viewer(session.csrf, session.request_csrf, session))
    return auth.finish(request, response, session)


def _hosted_post(
    state: _State, request: Request, route: str, raw: bytes | None, wants_json: bool
) -> Response:
    """Hosted POST. The hosted guard has already checked ``Host`` and ``Origin``; every route
    here also checks its synchroniser token."""

    from idea_web.hosted import unauthenticated

    auth = state.hosted
    assert auth is not None
    if route == "/login":
        return auth.login_post(request, raw)
    if route.startswith("/reset/"):
        return auth.password_post(request, route, raw)
    session = auth.session(request)
    if session is None:
        json_route = wants_json or _SCRIPT_POST.fullmatch(route) is not None
        return auth.finish(request, unauthenticated(request, wants_json=json_route), None)
    viewer = _Viewer(session.csrf, session.request_csrf, session)
    if route == "/logout":
        return auth.logout_post(request, raw, session)
    if route == "/admin/users" or route.startswith("/admin/users/"):
        if not session.account.is_admin:
            response = not_found()
        else:
            response = auth.admin_post(request, route, raw, session)
    elif state.active and route == "/analyse":
        response = _analyse(
            state,
            request,
            raw,
            wants_json,
            viewer,
            admit=lambda: auth.admit("analyse", request),
        )
    elif state.active and route == "/library/remove":
        # Removing a mix moves the shared work tree's files; until results are owned per account
        # (4d) only an admin may do that on a hosted server.
        if not session.account.is_admin:
            response = not_found()
        else:
            response = _library_remove(state, request, raw, viewer)
    elif route == "/playlists" or route.startswith("/playlists/"):
        response = _playlists_post(state, request, route, raw, viewer)
    elif state.active and route.startswith("/jobs/") and route.endswith("/dismiss"):
        response = _dismiss(state, request, route, raw, viewer)
    elif state.active and route.startswith("/jobs/") and route.endswith("/cancel"):
        response = _cancel(state, request, route, viewer)
    else:
        response = not_found()
    return auth.finish(request, response, session)


def create_app(
    work_root: Path,
    *,
    local: bool = True,
    analyse_enabled: bool | None = None,
    config: AppConfig | None = None,
    jobs: JobQueueAdapter | None = None,
    job_manager: JobQueueAdapter | None = None,
    hosted: HostedSettings | None = None,
) -> FastAPI:
    """Create the one IDea application; local mode changes policy, not routing.

    ``jobs`` is the queue adapter (``idea serve`` passes the durable local queue, whose work runs
    in a separate worker process).  ``job_manager`` is the historical keyword for the same thing.

    Hosted mode (``local=False``) requires ``hosted``: the account database and the public origin.
    Every hosted page then needs a signed-in session, and the sign-in, set-password and admin
    routes exist. Local mode refuses ``hosted`` and has none of that: no sign-in, no cookie, the
    loopback gate and one process-wide CSRF token, exactly as before accounts existed.
    """

    if local and hosted is not None:
        raise ValueError("accounts are hosted-mode only; local mode is sign-in free")
    if not local and hosted is None:
        raise ValueError("hosted mode needs HostedSettings (accounts database and public origin)")
    adapter = jobs if jobs is not None else job_manager
    enabled = adapter is not None if analyse_enabled is None else analyse_enabled
    if enabled and adapter is None:
        raise ValueError("analyse_enabled requires a queue adapter")
    settings = WebSettings(Path(work_root).resolve(), local, enabled, config)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Environment(
        loader=PackageLoader("idea_web", "templates"),
        autoescape=select_autoescape(("html", "xml"), default=True),
    )
    hosted_auth = None
    if hosted is not None:
        from idea_web.hosted import HostedAuth, redact_secret_paths

        if hosted.database.supervisor_lock_path is None:
            # Migrations must coordinate with the worker supervisor lock, as local mode's do.
            raise ValueError(
                "hosted mode needs a supervised database: open it with "
                "idea_web.database.hosted_database(path)"
            )
        hosted.database.migrate()  # under the supervisor lock; refused while a worker is live
        redact_secret_paths()
        hosted_auth = HostedAuth(hosted, templates)
        install_middleware(app, hosted=(hosted.host, hosted.public_origin))
    else:
        install_middleware(app)
    state = _State(
        settings=settings,
        jobs=adapter,
        csrf_token=secrets.token_urlsafe(32),
        templates=templates,
        hosted=hosted_auth,
    )
    app.state.web = state
    app.state.settings = settings
    app.state.job_manager = adapter
    app.state.csrf_token = state.csrf_token

    @app.api_route("/{rest:path}", methods=["GET", "HEAD"])
    async def read(request: Request, rest: str) -> Response:
        del rest
        if state.hosted is not None:
            return await run_in_threadpool(_hosted_get, state, request)
        return await run_in_threadpool(_get, state, request, state.local_viewer)

    @app.post("/{rest:path}")
    async def write(request: Request, rest: str) -> Response:
        del rest
        route = request.url.path
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        wants_json = content_type.strip().lower() == "application/json"
        if state.hosted is not None:
            if route.startswith("/jobs/") and route.endswith("/cancel"):
                await drain_request(request)
                raw: bytes | None = b""
            else:
                raw = await read_bounded(request)
            return await run_in_threadpool(_hosted_post, state, request, route, raw, wants_json)
        viewer = state.local_viewer
        if route == "/playlists" or route.startswith("/playlists/"):
            raw = await read_bounded(request)
            return await run_in_threadpool(_playlists_post, state, request, route, raw, viewer)
        if state.active and route == "/analyse":
            raw = await read_bounded(request)
            return await run_in_threadpool(_analyse, state, request, raw, wants_json, viewer)
        if state.active and route == "/library/remove":
            raw = await read_bounded(request)
            return await run_in_threadpool(_library_remove, state, request, raw, viewer)
        if state.active and route.startswith("/jobs/") and route.endswith("/dismiss"):
            raw = await read_bounded(request)
            return await run_in_threadpool(_dismiss, state, request, route, raw, viewer)
        if state.active and route.startswith("/jobs/") and route.endswith("/cancel"):
            # Cancel carries no body, but a client's is never left on the socket.
            await drain_request(request)
            return await run_in_threadpool(_cancel, state, request, route, viewer)
        # §2.5 removed the rescan route; a read-only server has no analyse routes.  Drain first
        # so the 404 reaches the client instead of an aborted connection.
        await drain_request(request)
        return not_found()

    @app.api_route("/{rest:path}", methods=UNSUPPORTED_METHODS)
    async def unsupported(request: Request, rest: str) -> Response:
        del rest
        await drain_request(request)
        return bytes_response(HTTPStatus.NOT_IMPLEMENTED, b"unsupported method", TEXT)

    return app
