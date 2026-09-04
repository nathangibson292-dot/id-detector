"""SoundCloud api-v2 comments and local ingest-description connectors."""

from __future__ import annotations

import json
import re
from hashlib import sha1
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from id_detector.hints.connectors.base import (
    ConnectorContext,
    ConnectorError,
    ConnectorOutput,
    bounded_get,
    read_json_response,
    write_raw_json,
    write_raw_text,
)
from id_detector.hints.parse import HintInput
from id_detector.io import read_text

_ASSET = re.compile(r"(?:https:)?//a-v2\.sndcdn\.com/assets/[^\"'<>\s]+\.js")
_CLIENT_ID = re.compile(r"client_id\s*:\s*[\"']([0-9A-Za-z]{32})[\"']")


def _strip_client_id(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parts.query) if key.casefold() != "client_id"]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _with_client_id(url: str, client_id: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "client_id"]
    query.append(("client_id", client_id))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


async def discover_client_id(context: ConnectorContext, *, refresh: bool = False) -> str:
    cache = context.cache_root / "sc_comments" / "client-token.json"
    if not refresh and cache.is_file():
        try:
            value = str(json.loads(read_text(cache))["value"])
            if re.fullmatch(r"[0-9A-Za-z]{32}", value):
                return value
        except (OSError, ValueError, KeyError, TypeError):
            pass

    homepage = await bounded_get(context, "https://soundcloud.com/")
    if homepage.status_code != 200:
        raise ConnectorError(f"SoundCloud client discovery returned HTTP {homepage.status_code}")
    write_raw_text(context.raw_path("soundcloud-home.html"), homepage.text)
    assets = sorted(
        {urljoin("https://soundcloud.com/", value) for value in _ASSET.findall(homepage.text)}
    )
    for index, asset in enumerate(assets[:60]):
        response = await bounded_get(context, asset)
        if response.status_code != 200:
            continue
        match = _CLIENT_ID.search(response.text)
        if match is not None:
            # This public token is local-only, never placed in an artefact, status, or log.
            write_raw_json(cache, {"value": match.group(1)})
            return match.group(1)
        if index < 3:
            write_raw_text(context.raw_path(f"asset-{index:02d}.js"), response.text)
    raise ConnectorError("SoundCloud client discovery found no api-v2 client token")


async def _api_json(
    context: ConnectorContext,
    url: str,
    client_id: str,
    *,
    params: dict[str, str | int] | None = None,
) -> tuple[dict[str, object], str]:
    query = dict(params or {})
    query["client_id"] = client_id
    response = await bounded_get(context, url, params=query)
    if response.status_code in {401, 403}:
        client_id = await discover_client_id(context, refresh=True)
        query["client_id"] = client_id
        response = await bounded_get(context, url, params=query)
    return read_json_response(response, "sc_comments"), client_id


async def resolve_track(context: ConnectorContext) -> dict[str, object]:
    client_id = await discover_client_id(context)
    resolved, _ = await _api_json(
        context,
        "https://api-v2.soundcloud.com/resolve",
        client_id,
        params={"url": context.source.canonical_url},
    )
    write_raw_json(context.raw_path("resolve.json"), resolved)
    return resolved


def parse_comments_page(
    payload: dict[str, object], *, media_key: str, uploader_user_id: str | None
) -> list[HintInput]:
    collection = payload.get("collection")
    if not isinstance(collection, list):
        return []
    result: list[HintInput] = []
    for index, raw in enumerate(collection):
        if not isinstance(raw, dict) or not isinstance(raw.get("body"), str):
            continue
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        user_id = str(raw.get("user_id")) if raw.get("user_id") is not None else "unknown"
        raw_id = raw.get("id")
        if raw_id is None:
            raw_id = sha1(
                f"{raw.get('timestamp')}|{user_id}|{raw['body']}|{index}".encode(),
                usedforsecurity=False,
            ).hexdigest()
        timestamp = raw.get("timestamp")
        result.append(
            HintInput(
                connector="sc_comments",
                source_record_id=str(raw_id),
                text=str(raw["body"]),
                position_ms=int(timestamp) if isinstance(timestamp, int) else None,
                position_kind="comment_timestamp",
                author_pseudo_id=sha1(
                    f"{media_key}|sc-author|{user_id}".encode(), usedforsecurity=False
                ).hexdigest(),
                author_permalink=(
                    str(user.get("permalink")) if user.get("permalink") is not None else None
                ),
                is_uploader=uploader_user_id is not None and user_id == uploader_user_id,
                is_verified=bool(user.get("verified", False)),
                follower_count=(
                    int(user["followers_count"])
                    if isinstance(user.get("followers_count"), int)
                    else None
                ),
            )
        )
    return result


async def fetch_comments(
    context: ConnectorContext, resolved: dict[str, object] | None = None
) -> ConnectorOutput:
    client_id = await discover_client_id(context)
    if resolved is None:
        resolved, client_id = await _api_json(
            context,
            "https://api-v2.soundcloud.com/resolve",
            client_id,
            params={"url": context.source.canonical_url},
        )
        write_raw_json(context.raw_path("resolve.json"), resolved)
    track_id = resolved.get("id") or context.source.platform_id
    if track_id is None:
        raise ConnectorError("SoundCloud resolve response has no track id")
    uploader_user_id = str(resolved.get("user_id")) if resolved.get("user_id") is not None else None
    cursor = context.job.cursor or (
        f"https://api-v2.soundcloud.com/tracks/{track_id}/comments?threaded=0&limit=200"
    )
    page = context.job.page
    count = context.job.items_fetched
    truncated = bool(context.job.truncated)
    while cursor and page < context.job.page_cap and count < context.job.item_cap:
        response = await bounded_get(context, _with_client_id(cursor, client_id))
        if response.status_code in {401, 403}:
            client_id = await discover_client_id(context, refresh=True)
            response = await bounded_get(context, _with_client_id(cursor, client_id))
        payload = read_json_response(response, "sc_comments")
        collection = payload.get("collection")
        if not isinstance(collection, list):
            raise ConnectorError("SoundCloud comments response has no collection")
        remaining = context.job.item_cap - count
        if len(collection) > remaining:
            payload = dict(payload)
            payload["collection"] = collection[:remaining]
            collection = collection[:remaining]
            truncated = True
        write_raw_json(context.raw_path(f"page-{page:03d}.json"), payload)
        count += len(collection)
        page += 1
        next_href = payload.get("next_href")
        cursor = (
            _strip_client_id(str(next_href)) if next_href and count < context.job.item_cap else None
        )
        if next_href and cursor is None:
            truncated = True
        if count >= context.job.item_cap and next_href:
            truncated = True
        if page >= context.job.page_cap and next_href:
            truncated = True
        await context.store.checkpoint_connector(
            context.job.id,
            context.owner,
            cursor=cursor,
            page=page,
            items_fetched=count,
            result_path=str(context.raw_path("result.json")),
            truncated=truncated,
        )

    inputs: list[HintInput] = []
    # Only pages below this attempt's durable checkpoint belong to the current result. A refresh
    # starts again at page zero, so stale files from a formerly longer feed are never reassembled.
    for page_index in range(page):
        path = context.raw_path(f"page-{page_index:03d}.json")
        try:
            payload = json.loads(read_text(path))
        except (OSError, ValueError):
            continue
        inputs.extend(
            parse_comments_page(
                payload, media_key=context.source.media_key, uploader_user_id=uploader_user_id
            )
        )
    return ConnectorOutput(
        inputs=tuple(inputs[: context.job.item_cap]),
        items_fetched=min(count, context.job.item_cap),
        truncated=truncated,
        tracklist_blocks=sum("tracklist" in item.text.casefold() for item in inputs),
    )


def description(context: ConnectorContext) -> ConnectorOutput:
    text = context.source.metadata.description
    if not text:
        return ConnectorOutput()
    connector = (
        "sc_description" if context.source.platform == "soundcloud" else "mixcloud_description"
    )
    return ConnectorOutput(
        inputs=(
            HintInput(
                connector=connector,
                source_record_id="description",
                text=text,
                author_pseudo_id="uploader",
                is_uploader=True,
            ),
        ),
        items_fetched=1,
        tracklist_blocks=1 if "tracklist" in text.casefold() else 0,
    )
