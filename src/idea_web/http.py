"""HTTP helpers shared by IDea's loopback applications (plan cycles 4a-ii and 4a-iii).

Everything security-sensitive that the retired stdlib handler hand-rolled lives here once: the
loopback ``Host``/``Origin`` gate, request bodies that are read and drained in bounded increments
(chunked bodies included), ``HEAD`` answers that carry exactly the ``GET`` headers, contained file
resolution that refuses symlinks and junctions, and single-range file streaming in 64 KiB chunks.

4a-iii adds the transport layer the UI review found missing (U-F29, plan §4.4): every answer
carries the same defensive headers, an HTML document's Content-Security-Policy names the hash of
each inline script it really contains (so a page works without ``'unsafe-inline'`` and an injected
script does not), result files carry ETags, and compressible answers are gzipped.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, MutableMapping
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response, StreamingResponse

from id_detector.io import native_path

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: The largest form or JSON body any IDea route accepts.
BODY_LIMIT = 8192
#: How much of a refused body is taken off the socket before answering (the legacy 1 MiB drain).
#: Answering without draining makes Windows report an aborted connection instead of the refusal;
#: draining without a bound lets a hostile page pin memory or a connection.  Bytes are discarded as
#: they arrive, so the bound is on time spent, never on memory held.
DRAIN_LIMIT = 1 << 20
#: Audio is streamed in chunks of this size, never read whole.
FILE_CHUNK = 64 * 1024
TEXT = "text/plain; charset=utf-8"
JSON = "application/json; charset=utf-8"
HTML = "text/html; charset=utf-8"
CSS = "text/css; charset=utf-8"
JAVASCRIPT = "text/javascript; charset=utf-8"
UNSUPPORTED_METHODS = ["PUT", "DELETE", "PATCH", "OPTIONS"]
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")

#: Cache policies (U-F29).  Dynamic pages and JSON carry a CSRF token or live state and are never
#: stored; a canonical result file is revalidated (its ETag answers 304) because a refresh can move
#: it; a versioned static asset or an immutable bundle file never changes under its URL.
NO_STORE = "no-store"
REVALIDATE = "no-cache"
IMMUTABLE = "public, max-age=31536000, immutable"
IMMUTABLE_PRIVATE = "private, max-age=31536000, immutable"
#: Smaller answers are not worth compressing; audio (streamed, ranged) is never compressed, and
#: neither is an original served as ``application/octet-stream`` (an unknown container).
GZIP_MINIMUM = 1024
_GZIP_EXCLUDED = (*DEFAULT_EXCLUDED_CONTENT_TYPES, "application/octet-stream")

#: The headers every answer carries unless the handler set its own (plan §4.4).  The default
#: Content-Security-Policy is for a non-document answer (JSON, text, audio): should one ever be
#: rendered as a document it can load nothing.  An HTML page gets :func:`content_security_policy`.
#: The referrer policy keeps result URLs (which hold source and media keys) off third-party sites
#: while still sending the origin, which the YouTube embed requires to play at all.  HSTS is not
#: set here: local mode is plain HTTP on the loopback, and hosted TLS termination (Caddy) owns it.
#: Framing is same-origin only, in both directions: another site still cannot frame a page
#: (clickjacking), but the local cached-open path frames a result page from its own origin —
#: scripts/gate_local_mode.ps1 probes audio that way. So a page may be framed by its own origin
#: (``frame-ancestors 'self'``, ``SAMEORIGIN``) and may frame it (``frame-src 'self'``); ``DENY``,
#: ``frame-ancestors 'none'`` or a frame-src of only the platform players each break that flow.
DEFAULT_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
    ("Cache-Control", NO_STORE),
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'self'; base-uri 'none'"),
)
#: The only third parties a page may load script from or frame: the platform players the result
#: page embeds when the fetched original is not played locally (``page._embed_html``).
_SCRIPT_HOSTS = "https://w.soundcloud.com https://www.youtube.com https://widget.mixcloud.com"
_FRAME_HOSTS = (
    "https://w.soundcloud.com https://www.youtube.com https://www.youtube-nocookie.com "
    "https://player-widget.mixcloud.com"
)
_INLINE_SCRIPT = re.compile(rb"<script\b([^>]*)>(.*?)</script", re.IGNORECASE | re.DOTALL)
_SRC_ATTRIBUTE = re.compile(rb"\bsrc\s*=", re.IGNORECASE)


def inline_script_hashes(body: bytes) -> list[str]:
    """The CSP ``sha256-`` digest of every inline ``<script>`` body, in document order.

    A browser hashes the script text exactly as it appears between the tags, after the HTML
    parser's newline normalisation (CR and CRLF become LF), with no entity decoding — a script is
    raw text.  A ``src=`` script has no inline body, and an empty one never runs.
    """

    digests: list[str] = []
    for attributes, script in _INLINE_SCRIPT.findall(body):
        if _SRC_ATTRIBUTE.search(attributes) or not script:
            continue
        text = script.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest = base64.b64encode(hashlib.sha256(text).digest()).decode("ascii")
        if digest not in digests:
            digests.append(digest)
    return digests


def content_security_policy(body: bytes) -> str:
    """The policy for one HTML document: its own inline scripts by hash, nothing else inline.

    Styles stay ``'unsafe-inline'``: every page inlines its stylesheet and positions timeline
    lanes with ``style=`` attributes, and a hash list cannot cover element attributes.  Script is
    the injection surface, and script is hashed.
    """

    hashes = "".join(f" 'sha256-{digest}'" for digest in inline_script_hashes(body))
    return (
        "default-src 'none'; base-uri 'none'; object-src 'none'; frame-ancestors 'self'; "
        f"form-action 'self'; script-src 'self'{hashes} {_SCRIPT_HOSTS}; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; "
        f"connect-src 'self'; frame-src 'self' {_FRAME_HOSTS}"
    )


def weak_etag(body: bytes) -> str:
    """A weak validator: the same bytes under any transfer encoding are the same resource."""

    return f'W/"{hashlib.sha256(body).hexdigest()[:32]}"'


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """RFC 9110 weak comparison of an ``If-None-Match`` list against one entity tag."""

    if not if_none_match:
        return False
    wanted = etag.removeprefix("W/")
    for candidate in if_none_match.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == wanted:
            return True
    return False


def bytes_response(
    status: int,
    body: bytes,
    content_type: str,
    *,
    cache: str | None = None,
    etag: str | None = None,
) -> Response:
    headers = {"Content-Type": content_type, "X-Content-Type-Options": "nosniff"}
    if cache is not None:
        headers["Cache-Control"] = cache
    if etag is not None:
        headers["ETag"] = etag
    return Response(content=body, status_code=int(status), headers=headers)


def html_response(
    status: int, body: bytes, *, cache: str = NO_STORE, etag: str | None = None
) -> Response:
    """An HTML document with the policy that names exactly its inline scripts."""

    response = bytes_response(status, body, HTML, cache=cache, etag=etag)
    response.headers["Content-Security-Policy"] = content_security_policy(body)
    return response


def deliver(status: int, body: bytes, content_type: str, **kwargs: Any) -> Response:
    """``bytes_response`` unless the body is a document, which gets its hashed policy."""

    if content_type.split(";", 1)[0].strip().lower() == "text/html":
        return html_response(status, body, **kwargs)
    return bytes_response(status, body, content_type, **kwargs)


def not_modified(etag: str, cache: str, *, policy: str | None = None) -> Response:
    """A 304 carries the headers a cache will copy onto the stored answer — a document's own
    Content-Security-Policy included, or the reloaded page would inherit the resource default."""

    headers = {"ETag": etag, "Cache-Control": cache}
    if policy is not None:
        headers["Content-Security-Policy"] = policy
    return Response(status_code=HTTPStatus.NOT_MODIFIED, headers=headers)


def with_default_headers(response: Response) -> Response:
    """:data:`DEFAULT_HEADERS` on a response built outside the middleware stack."""

    for name, value in DEFAULT_HEADERS:
        response.headers.setdefault(name, value)
    return response


async def internal_error(request: Request, exc: Exception) -> Response:
    """The answer to an exception no route caught.

    Starlette's outermost ``ServerErrorMiddleware`` builds this answer itself, outside every
    middleware added to the app, so the defensive headers are applied here by hand.  The server
    still logs the exception after the answer is sent.
    """

    del request, exc
    return with_default_headers(
        bytes_response(HTTPStatus.INTERNAL_SERVER_ERROR, b"internal server error", TEXT)
    )


def json_response(status: int, payload: object) -> Response:
    return bytes_response(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), JSON)


def not_found(body: bytes = b"not found") -> Response:
    return bytes_response(HTTPStatus.NOT_FOUND, body, TEXT)


def redirect(location: str) -> Response:
    return Response(status_code=HTTPStatus.SEE_OTHER, headers={"Location": location})


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def cross_site(scope: Scope) -> str | None:
    """Why a request must be refused: a non-loopback ``Host`` or a foreign ``Origin`` (U-F5).

    Browsers send ``Origin`` on every cross-site POST, so a page on another site cannot reach a
    mutation even from the owner's own browser; the ``Host`` check defeats DNS rebinding.  The
    synchroniser token on each route covers what these headers cannot.
    """

    server = scope.get("server") or ("127.0.0.1", 80)
    port = server[1]
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    if _header(scope, b"host").strip().casefold() not in allowed:
        return "host"
    origin = _header(scope, b"origin").strip()
    if origin:
        try:
            parts = urlsplit(origin)
        except ValueError:
            return "origin"
        if parts.scheme != "http" or (parts.netloc or "").casefold() not in allowed:
            return "origin"
    return None


def csrf_matches(request: Request, presented: object, token: str) -> bool:
    """The synchroniser token, from the header or a form/JSON field, compared in constant time."""

    value = request.headers.get("X-CSRF-Token") or (presented if isinstance(presented, str) else "")
    value = value.strip()
    return bool(value) and secrets.compare_digest(value.encode("utf-8"), token.encode("utf-8"))


async def drain_receive(receive: Receive, *, seen: int = 0) -> None:
    """Discard a request body straight off the ASGI channel, stopping after :data:`DRAIN_LIMIT`."""

    while seen <= DRAIN_LIMIT:
        message = await receive()
        if message.get("type") != "http.request":
            return
        seen += len(message.get("body") or b"")
        if not message.get("more_body"):
            return


async def _drain_stream(stream: AsyncIterator[bytes], seen: int) -> None:
    try:
        async for chunk in stream:
            seen += len(chunk)
            if seen > DRAIN_LIMIT:
                break
    except ClientDisconnect:
        pass
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()


async def drain_request(request: Request) -> None:
    await _drain_stream(request.stream(), 0)


async def read_bounded(request: Request, limit: int = BODY_LIMIT) -> bytes | None:
    """At most ``limit`` body bytes, or ``None`` after a bounded drain of an oversized body.

    A declared ``Content-Length`` over the limit is refused before a byte is buffered; a chunked
    body (no declared length) is accumulated only until it passes the limit.
    """

    stream = request.stream()
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            await _drain_stream(stream, 0)
            return None
    body = bytearray()
    try:
        async for chunk in stream:
            body += chunk
            if len(body) > limit:
                await _drain_stream(stream, len(body))
                return None
    except ClientDisconnect:
        return None
    return bytes(body)


class LoopbackPostGuard:
    """Refuse a non-loopback ``Host`` or a foreign ``Origin`` on every POST, before any routing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("method") == "POST":
            refusal = cross_site(scope)
            if refusal is not None:
                await drain_receive(receive)
                response = json_response(
                    HTTPStatus.FORBIDDEN, {"error": f"cross-site request refused ({refusal})"}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class HeadResponseGuard:
    """A ``HEAD`` answer is the ``GET`` answer's status and headers, with no body bytes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("method") != "HEAD":
            await self.app(scope, receive, send)
            return

        async def send_head(message: Message) -> None:
            if message.get("type") == "http.response.body":
                message = {
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": bool(message.get("more_body")),
                }
            await send(message)

        await self.app(scope, receive, send_head)


class SecurityHeaders:
    """Add :data:`DEFAULT_HEADERS` to every answer that did not set the header itself."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_defaults(message: Message) -> None:
            if message.get("type") == "http.response.start":
                raw = list(message.get("headers") or [])
                headers = MutableHeaders(raw=raw)
                for name, value in DEFAULT_HEADERS:
                    headers.setdefault(name, value)
                message["headers"] = raw
            await send(message)

        await self.app(scope, receive, send_with_defaults)


def install_middleware(app: Any) -> None:
    """The one middleware stack both loopback apps run, in the one order that is correct.

    Starlette runs the middleware added last first: the defensive headers go on every answer,
    the loopback gate's own refusals included; the loopback gate answers a foreign POST before any
    routing; a ``HEAD`` answer is the finished ``GET`` answer (compressed, with its headers) minus
    the body; and gzip, innermost, sees the handler's own headers and skips audio, ranges and
    small bodies.
    """

    app.add_middleware(
        GZipMiddleware, minimum_size=GZIP_MINIMUM, exclude_content_types=_GZIP_EXCLUDED
    )
    app.add_middleware(HeadResponseGuard)
    app.add_middleware(LoopbackPostGuard)
    app.add_middleware(SecurityHeaders)
    # An uncaught exception is answered outside that stack (``ServerErrorMiddleware`` is the
    # outermost layer Starlette builds), so its answer applies the same headers itself.
    app.add_exception_handler(Exception, internal_error)


def _plain(path: Path | str) -> str:
    """An absolute, *unresolved* spelling with any Win32 extended-length prefix removed.

    ``native_path`` resolves, which follows every link on the way: a containment walk over that
    result never sees the link it exists to refuse.
    """

    text = os.path.abspath(os.fspath(path))
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[len("\\\\?\\UNC\\") :]
    if text.startswith("\\\\?\\"):
        return text[len("\\\\?\\") :]
    return text


def _unresolved_native(text: str) -> str:
    """``text`` in Win32 extended form without resolving it, so deep paths still stat."""

    if os.name != "nt":
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _is_link(path: Path | str) -> bool:
    """True for a symlink or a junction (mount point); an unreadable entry is treated as one."""

    try:
        info = os.lstat(_unresolved_native(_plain(path)))
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    tag = getattr(info, "st_reparse_tag", 0)
    return tag in {
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
    }


def _contained(root: Path, candidate: Path) -> Path | None:
    try:
        # Two independent checks.  The resolved target must lie inside the resolved root (no
        # escape by ``..``, a link or an alias), and no component below the root, walked in its
        # unresolved spelling, may itself be a symlink or a junction (no aliasing inside it).
        real_root = Path(native_path(root)).resolve(strict=True)
        resolved = Path(native_path(candidate)).resolve(strict=True)
        if resolved != real_root and not resolved.is_relative_to(real_root):
            return None
        relative = Path(_plain(candidate)).relative_to(Path(_plain(root)))
        cursor = _plain(root)
        for part in relative.parts:
            cursor = os.path.join(cursor, part)
            if _is_link(cursor):
                return None
        return resolved
    except (OSError, ValueError, RuntimeError):
        return None


def contained_file(root: Path, candidate: Path) -> Path | None:
    """Resolve a file without letting a symlink, junction or ``..`` escape ``root``."""

    resolved = _contained(root, candidate)
    return resolved if resolved is not None and resolved.is_file() else None


def contained_directory(root: Path, candidate: Path) -> Path | None:
    resolved = _contained(root, candidate)
    if resolved is None or resolved == Path(native_path(root)).resolve():
        return None
    return resolved if resolved.is_dir() else None


def _file_chunks(native: str, start: int, length: int) -> Iterator[bytes]:
    with open(native, "rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(FILE_CHUNK, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk


def range_response(method: str, range_header: str, path: Path, content_type: str) -> Response:
    """Serve a file with single-range support (200/206/416) — what makes ``<audio>`` seeking work.

    The body is streamed from disk in :data:`FILE_CHUNK` pieces, so ``Range: bytes=0-`` on a
    two-hour mix never holds the mix in memory.
    """

    native = native_path(path)
    size = os.stat(native).st_size
    start, end = 0, size - 1
    status = HTTPStatus.OK
    match = _RANGE.fullmatch(range_header.strip())
    if match and size:
        first, last = match.groups()
        if first:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
        elif last:  # a suffix range: the final N bytes
            start = max(0, size - int(last))
        if start > end or start >= size:
            return Response(
                status_code=HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"},
            )
        status = HTTPStatus.PARTIAL_CONTENT
    length = max(0, end - start + 1)
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    if status == HTTPStatus.PARTIAL_CONTENT:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if method == "HEAD" or not length:
        return Response(status_code=status, headers=headers)
    return StreamingResponse(
        _file_chunks(native, start, length), status_code=status, headers=headers
    )
