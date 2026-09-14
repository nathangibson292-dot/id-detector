"""HTTP helpers shared by IDea's loopback applications (plan cycle 4a-ii).

Everything security-sensitive that the retired stdlib handler hand-rolled lives here once: the
loopback ``Host``/``Origin`` gate, request bodies that are read and drained in bounded increments
(chunked bodies included), ``HEAD`` answers that carry exactly the ``GET`` headers, contained file
resolution that refuses symlinks and junctions, and single-range file streaming in 64 KiB chunks.
"""

from __future__ import annotations

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
UNSUPPORTED_METHODS = ["PUT", "DELETE", "PATCH", "OPTIONS"]
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def bytes_response(status: int, body: bytes, content_type: str) -> Response:
    return Response(
        content=body,
        status_code=int(status),
        headers={"Content-Type": content_type, "X-Content-Type-Options": "nosniff"},
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
