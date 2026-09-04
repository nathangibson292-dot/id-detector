"""YouTube description/chapter reuse and bounded yt-dlp top-comment extraction."""

from __future__ import annotations

import json
import sys
from hashlib import sha1

from id_detector.hints.connectors.base import (
    ConnectorContext,
    ConnectorError,
    ConnectorOutput,
    write_raw_json,
)
from id_detector.hints.parse import HintInput
from id_detector.process import run_process

MAX_PARENT_THREADS = 200
MAX_REPLIES_PER_THREAD = 20
MAX_REPLIES = MAX_PARENT_THREADS * MAX_REPLIES_PER_THREAD
MAX_COMMENTS = MAX_PARENT_THREADS + MAX_REPLIES
COMMENT_ARGS = "youtube:comment_sort=top;max_comments=4200,200,4000,20,1"


def description(context: ConnectorContext) -> ConnectorOutput:
    text = context.source.metadata.description
    if not text:
        return ConnectorOutput()
    return ConnectorOutput(
        inputs=(
            HintInput(
                connector="yt_description",
                source_record_id="description",
                text=text,
                author_pseudo_id="uploader",
                is_uploader=True,
            ),
        ),
        items_fetched=1,
        tracklist_blocks=1 if "tracklist" in text.casefold() else 0,
    )


def chapters(context: ConnectorContext) -> ConnectorOutput:
    raw_chapters = context.source.metadata.chapters
    inputs: list[HintInput] = []
    for index, chapter in enumerate(raw_chapters):
        if not isinstance(chapter, dict):
            continue
        title = chapter.get("title")
        start = chapter.get("start_time_ms")
        end = chapter.get("end_time_ms")
        if not isinstance(title, str) or not isinstance(start, int):
            continue
        inputs.append(
            HintInput(
                connector="yt_chapters",
                source_record_id=f"chapter-{index}",
                text=title,
                position_ms=start,
                position_end_ms=end if isinstance(end, int) else None,
                position_kind="chapter",
                author_pseudo_id="uploader",
                is_uploader=True,
                structured_tracklist=True,
            )
        )
    return ConnectorOutput(
        inputs=tuple(inputs),
        items_fetched=len(inputs),
        tracklist_blocks=1 if len(inputs) >= 2 else 0,
    )


def parse_comments(payload: dict[str, object], *, media_key: str) -> tuple[HintInput, ...]:
    comments = payload.get("comments")
    if not isinstance(comments, list):
        return ()
    parents = [
        raw
        for raw in comments
        if isinstance(raw, dict)
        and isinstance(raw.get("text"), str)
        and raw.get("parent") in {None, "root"}
    ][:MAX_PARENT_THREADS]
    parent_ids = {str(raw.get("id") or f"comment-{index}") for index, raw in enumerate(parents)}
    replies_by_parent: dict[str, int] = {}
    selected: list[dict[str, object]] = list(parents)
    for raw in comments:
        if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
            continue
        parent = raw.get("parent")
        if parent in {None, "root"} or str(parent) not in parent_ids:
            continue
        parent_id = str(parent)
        if replies_by_parent.get(parent_id, 0) >= MAX_REPLIES_PER_THREAD:
            continue
        if sum(replies_by_parent.values()) >= MAX_REPLIES:
            break
        replies_by_parent[parent_id] = replies_by_parent.get(parent_id, 0) + 1
        selected.append(raw)
    inputs: list[HintInput] = []
    for index, raw in enumerate(selected[:MAX_COMMENTS]):
        author_id = str(raw.get("author_id") or raw.get("author") or "unknown")
        comment_id = str(raw.get("id") or f"comment-{index}")
        parent = raw.get("parent")
        inputs.append(
            HintInput(
                connector="yt_comments",
                source_record_id=comment_id,
                text=str(raw["text"]),
                author_pseudo_id=sha1(
                    f"{media_key}|yt-author|{author_id}".encode(), usedforsecurity=False
                ).hexdigest(),
                is_uploader=bool(raw.get("author_is_uploader", False)),
                is_verified=bool(raw.get("author_is_verified", False)),
                like_count=int(raw["like_count"])
                if isinstance(raw.get("like_count"), int)
                else None,
                is_pinned=bool(raw.get("is_pinned", False)),
                parent_source_id=str(parent) if parent not in {None, "root"} else None,
            )
        )
    return tuple(inputs)


async def fetch_comments(context: ConnectorContext) -> ConnectorOutput:
    try:
        result = await run_process(
            [
                sys.executable,
                "-m",
                "yt_dlp",
                "--skip-download",
                "--write-comments",
                "--no-playlist",
                "--no-progress",
                "--js-runtimes",
                "node",
                "--extractor-args",
                COMMENT_ARGS,
                "-j",
                context.source.canonical_url,
            ],
            timeout=1_800,
        )
        payload = json.loads(result.stdout)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ConnectorError(f"yt_comments extraction failed: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise ConnectorError("yt_comments extractor output is not an object")
    if result.returncode != 0:
        raise ConnectorError(f"yt_comments extraction exited {result.returncode}")
    write_raw_json(context.raw_path("comments.json"), payload)
    inputs = parse_comments(payload, media_key=context.source.media_key)
    return ConnectorOutput(
        inputs=inputs,
        items_fetched=len(inputs),
        tracklist_blocks=sum("tracklist" in item.text.casefold() for item in inputs),
    )
