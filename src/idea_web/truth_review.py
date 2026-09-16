"""FastAPI routing for ``idea truth review`` (ported from the retired stdlib handler in 4a-ii).

Every protection the old handler had is kept, and every file decision still belongs to
:mod:`id_detector.truth_review`: the loopback bind, the Host/Origin gate before any POST, a Host
check before the token is handed out, the synchroniser token on both mutations, no mutation on GET,
the exact media route for this set's audio only, the exposure record written durably before a
prediction leaves the server (``TruthReviewSession.reveal_predictions``), the cross-process save
lock and the refusal to write beneath ``work/`` (``TruthReviewSession.save``).  This module adds
no path handling of its own.
"""

from __future__ import annotations

import json
import secrets
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from id_detector import truth_review as review
from id_detector.present.server import _audio_content_type
from idea_web.http import (
    TEXT,
    UNSUPPORTED_METHODS,
    bytes_response,
    cross_site,
    csrf_matches,
    drain_request,
    html_response,
    install_middleware,
    json_response,
    not_found,
    range_response,
    read_bounded,
)
from idea_web.server import LoopbackServer, RunningServer, run_in_background

#: The review page wires four buttons from inline ``onclick`` attributes, which the hashed
#: Content-Security-Policy refuses.  The page module is closed to Phase-4 work (plan §4.2), so
#: the served page is rewired here: each attribute becomes an id, and one more script — named in
#: the policy by its hash like the page's own — attaches the same behaviour.  Every lookup is
#: guarded: the audio and the scorer-suggestion controls are not always on the page.
_INLINE_HANDLERS = (
    (b' onclick="audio&&audio.paused?audio.play():audio&&audio.pause()"', b' id="play-pause"'),
    (b" onclick=\"help.classList.add('open')\"", b' id="help-open"'),
    (b" onclick=\"help.classList.remove('open')\"", b' id="help-close"'),
    (
        b" onclick=\"document.getElementById('offset').value=this.dataset.offset;preview()\"",
        b' id="use-suggestion"',
    ),
)
_WIRING = (
    b"<script>function wire(id,fn){let el=document.getElementById(id);if(el)el.onclick=fn;}\n"
    b"wire('play-pause',()=>{if(audio){audio.paused?audio.play():audio.pause();}});\n"
    b"wire('help-open',()=>help.classList.add('open'));"
    b"wire('help-close',()=>help.classList.remove('open'));\n"
    b"wire('use-suggestion',function(){offsetInput.value=this.dataset.offset;preview();});"
    b"</script></body>"
)


def review_page(session: review.TruthReviewSession, token: str) -> bytes:
    """``truth_review._page`` with no inline handler in it."""

    page = review._page(session, token)
    for inline, replacement in _INLINE_HANDLERS:
        page = page.replace(inline, replacement)
    return page.replace(b"</body>", _WIRING, 1)


def create_truth_review_app(session: review.TruthReviewSession) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_middleware(app)
    token = secrets.token_urlsafe(32)
    app.state.csrf_token = token
    app.state.session = session

    def get(request: Request) -> Response:
        route = request.url.path
        if route in {"/", "/index.html"}:
            return html_response(HTTPStatus.OK, review_page(session, token))
        if route == "/csrf":
            # Defence in depth against DNS rebinding: a POST already requires a loopback Host,
            # but there is no reason to hand the token to a request that could not use it.
            if cross_site(request.scope) is not None:
                return json_response(HTTPStatus.FORBIDDEN, {"error": "cross-site request refused"})
            return json_response(HTTPStatus.OK, {"token": token})
        expected = f"/media/{session.truth.source.media_key}/audio"
        if route == expected and session.audio_path is not None:
            return range_response(
                request.method,
                request.headers.get("range", ""),
                session.audio_path,
                _audio_content_type(session.audio_path),
            )
        return not_found()

    def save(raw: bytes) -> Response:
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            saved = session.save(payload)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            return json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        return json_response(HTTPStatus.OK, {"saved": True, "episodes": len(saved.episodes)})

    @app.api_route("/{rest:path}", methods=["GET", "HEAD"])
    async def read(request: Request, rest: str) -> Response:
        del rest
        return await run_in_threadpool(get, request)

    @app.post("/{rest:path}")
    async def write(request: Request, rest: str) -> Response:
        del rest
        raw = await read_bounded(request, review._MAX_BODY)
        if raw is None:
            return json_response(HTTPStatus.BAD_REQUEST, {"error": "bad length"})
        if not csrf_matches(request, None, token):
            return json_response(HTTPStatus.FORBIDDEN, {"error": "CSRF token was invalid"})
        route = request.url.path
        if route == "/predictions":
            # ``reveal_predictions`` records the exposure durably first and raises, returning
            # nothing, if it cannot: no prediction is ever assembled for an unrecorded reveal.
            predictions = await run_in_threadpool(session.reveal_predictions)
            return json_response(HTTPStatus.OK, {"predictions": predictions})
        if route != "/save":
            return not_found()
        return await run_in_threadpool(save, raw)

    @app.api_route("/{rest:path}", methods=UNSUPPORTED_METHODS)
    async def unsupported(request: Request, rest: str) -> Response:
        del rest
        await drain_request(request)
        return bytes_response(HTTPStatus.NOT_IMPLEMENTED, b"unsupported method", TEXT)

    return app


def make_truth_review_server(
    session: review.TruthReviewSession, *, host: str = "127.0.0.1", port: int = 8797
) -> LoopbackServer:
    return LoopbackServer(
        create_truth_review_app(session), host=host, port=port, what="truth review server"
    )


def serve_truth_review_in_background(
    session: review.TruthReviewSession, *, host: str = "127.0.0.1", port: int = 0
) -> RunningServer:
    return run_in_background(
        make_truth_review_server(session, host=host, port=port), name="truth-review"
    )
