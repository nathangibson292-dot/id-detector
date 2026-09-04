"""Strict allow-listed pointer import with redirect and body limits."""

from __future__ import annotations

import asyncio
import html
import json
import re
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from urllib.parse import urljoin, urlsplit

import httpx

from id_detector.hints.connectors.base import (
    ConnectorContext,
    ConnectorError,
    ConnectorOutput,
    MirrorCandidate,
    RetryableConnectorError,
    write_raw_text,
)
from id_detector.hints.parse import HintInput
from id_detector.io import url_has_credentials

ALLOWED_HOSTS = frozenset(
    {
        "www.1001tracklists.com",
        "1001.tl",
        "www.mixesdb.com",
        "boilerroom.tv",
        "www.boilerroom.tv",
        "blrrm.tv",
    }
)
MAX_REDIRECTS = 3
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 20


def validate_pointer_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ConnectorError("pointer URL is malformed") from exc
    if parts.scheme.casefold() != "https":
        raise ConnectorError("pointer URL must use https")
    if url_has_credentials(url):
        raise ConnectorError("pointer URL must not contain credentials")
    if (parts.hostname or "").casefold() not in ALLOWED_HOSTS:
        raise ConnectorError("pointer URL host is not allow-listed")
    if parts.port not in {None, 443}:
        raise ConnectorError("pointer URL must use the default https port")
    return url


async def fetch_limited(context: ConnectorContext, url: str) -> tuple[str, str, bool, int]:
    async def transfer() -> tuple[str, str, bool, int]:
        current = validate_pointer_url(url)
        redirects = 0
        while True:
            context.breaker.before_request()
            try:
                request = context.http.build_request("GET", current, timeout=TIMEOUT_SECONDS)
                response = await context.http.send(request, stream=True, follow_redirects=False)
            except httpx.HTTPError as exc:
                context.breaker.failure()
                raise RetryableConnectorError(
                    f"pointer transport failure: {type(exc).__name__}"
                ) from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                await response.aclose()
                if location is None:
                    raise ConnectorError("pointer redirect has no Location")
                if redirects >= MAX_REDIRECTS:
                    raise ConnectorError("pointer exceeded 3 redirects")
                current = validate_pointer_url(urljoin(current, location))
                redirects += 1
                continue
            if response.status_code >= 400:
                status = response.status_code
                await response.aclose()
                if status >= 500 or status in {408, 429}:
                    context.breaker.failure()
                    raise RetryableConnectorError(f"pointer returned retryable HTTP {status}")
                raise ConnectorError(f"pointer returned HTTP {status}")
            body = bytearray()
            truncated = False
            try:
                async for chunk in response.aiter_bytes():
                    remaining = MAX_BYTES - len(body)
                    if len(chunk) > remaining:
                        body.extend(chunk[:remaining])
                        truncated = True
                        break
                    body.extend(chunk)
            finally:
                await response.aclose()
            return current, bytes(body).decode("utf-8", errors="replace"), truncated, redirects

    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            return await transfer()
    except TimeoutError as exc:
        context.breaker.failure()
        raise RetryableConnectorError("pointer exceeded 20-second wall-clock limit") from exc


def _plain_text(value: str) -> str:
    value = re.sub(r"(?is)<(?:script|style).*?>.*?</(?:script|style)>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>|</(?:p|li|div|tr|h\d)>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    return "\n".join(
        re.sub(r"\s+", " ", line).strip() for line in value.splitlines() if line.strip()
    )


def _metadata_values(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for tag in re.findall(r"(?is)<meta\b[^>]*>", value):
        attributes = {
            name.casefold(): html.unescape(content)
            for name, _, content in re.findall(r"([\w:-]+)\s*=\s*([\"'])(.*?)\2", tag, re.DOTALL)
        }
        key = attributes.get("property") or attributes.get("name") or attributes.get("itemprop")
        content = attributes.get("content")
        if key and content:
            result[key.casefold().replace("-", "_")] = content.strip()
    for block in re.findall(
        r"(?is)<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        value,
    ):
        try:
            payload = json.loads(html.unescape(block))
        except (TypeError, ValueError):
            continue

        def visit(item: object) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if isinstance(child, (str, int)):
                        result.setdefault(key.casefold().replace("-", "_"), str(child))
                    else:
                        visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(payload)
    return result


def _duration_ms(value: str | None, *, milliseconds: bool = False) -> int | None:
    if not value:
        return None
    if value.isdigit():
        duration = int(value)
        return duration if milliseconds else duration * 1_000
    match = re.fullmatch(
        r"P(?:\d+D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value, re.IGNORECASE
    )
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    try:
        seconds_value = Decimal(seconds or 0)
    except InvalidOperation:
        return None
    return int((Decimal(int(hours or 0) * 3_600 + int(minutes or 0) * 60) + seconds_value) * 1_000)


def _mirror_metadata(value: str) -> tuple[str | None, str | None, str | None, int | None]:
    metadata = _metadata_values(value)

    def first(*keys: str) -> str | None:
        return next((metadata[key] for key in keys if metadata.get(key)), None)

    platform_id = first("platform_id", "platformid", "soundcloud:id", "youtube:id")
    uploader_id = first("uploader_id", "uploaderid", "author_id", "authorid")
    upload_date = first("upload_date", "uploaddate", "datepublished")
    duration_raw = first("duration_ms", "durationms")
    duration = _duration_ms(duration_raw, milliseconds=True)
    if duration is None:
        duration = _duration_ms(first("duration"))
    return platform_id, uploader_id, upload_date, duration


def parse_pointer_html(
    value: str,
    *,
    final_url: str,
    mirror_of: str,
    truncated: bool,
    requested_url: str | None = None,
) -> ConnectorOutput:
    host = (urlsplit(final_url).hostname or "").casefold()
    page_digest = sha256(final_url.encode("utf-8")).hexdigest()
    inputs: list[HintInput] = []
    if host in {"1001.tl", "www.1001tracklists.com"}:
        pairs = re.findall(
            r"(?is)class=[\"'][^\"']*cueValueField[^\"']*[\"'][^>]*>(.*?)</[^>]+>"
            r".*?class=[\"'][^\"']*trackValue[^\"']*[\"'][^>]*>(.*?)</[^>]+>",
            value,
        )
        lines: list[str] = []
        for cue_html, track_html in pairs:
            cue = _plain_text(cue_html)
            track = _plain_text(track_html)
            if re.fullmatch(r"(?:\d+:)?\d{1,3}:\d{2}", cue) and " - " in track:
                lines.append(f"{cue} {track}")
        if lines:
            inputs.append(
                HintInput(
                    connector="1001tl",
                    source_record_id=f"imported-tracklist-{page_digest}",
                    text="\n".join(lines),
                    author_pseudo_id="1001tl",
                    mirror_of=mirror_of,
                    mirror_status="quarantined",
                    truncated=truncated,
                    structured_tracklist=True,
                )
            )
    if not inputs:
        text = _plain_text(value)
        inputs.append(
            HintInput(
                connector="mixesdb" if host == "www.mixesdb.com" else "pointer_import",
                source_record_id=f"imported-page-{page_digest}",
                text=text,
                author_pseudo_id="pointer-import",
                mirror_of=mirror_of,
                mirror_status="quarantined",
                truncated=truncated,
                structured_tracklist=True,
            )
        )
    urls = tuple(
        sorted(
            {
                match.group(0).rstrip(".,;:")
                for match in re.finditer(r"https://[^\s<>\"']+", value, re.IGNORECASE)
                if (urlsplit(match.group(0).rstrip(".,;:")).hostname or "").casefold()
                in ALLOWED_HOSTS
                and not url_has_credentials(match.group(0).rstrip(".,;:"))
            }
        )
    )
    platform_id, uploader_id, upload_date, duration_ms = _mirror_metadata(value)
    return ConnectorOutput(
        inputs=tuple(inputs),
        pointers=urls,
        items_fetched=len(inputs),
        truncated=truncated,
        tracklist_blocks=1 if inputs else 0,
        mirror_candidate=MirrorCandidate(
            requested_url=requested_url or final_url,
            final_url=final_url,
            platform_id=platform_id,
            uploader_id=uploader_id,
            upload_date=upload_date,
            duration_ms=duration_ms,
            source_record_ids=tuple(item.source_record_id for item in inputs),
        ),
    )


async def fetch(context: ConnectorContext, url: str) -> ConnectorOutput:
    final_url, body, truncated, _ = await fetch_limited(context, url)
    write_raw_text(context.raw_path("page.html"), body)
    return parse_pointer_html(
        body,
        final_url=final_url,
        mirror_of=context.source.canonical_url,
        truncated=truncated,
        requested_url=url,
    )
