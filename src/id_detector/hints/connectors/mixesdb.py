"""MixesDB URL lookup, wikitext tracklist, and mirror extraction."""

from __future__ import annotations

import re

from id_detector.hints.connectors.base import (
    ConnectorContext,
    ConnectorError,
    ConnectorOutput,
    bounded_get,
    read_json_response,
    write_raw_json,
)
from id_detector.hints.parse import HintInput
from id_detector.io import url_has_credentials

API_URL = "https://www.mixesdb.com/w/api.php"
_PLAYER = re.compile(r"\{\{Player\b(.*?)\}\}", re.IGNORECASE | re.DOTALL)
_HTTPS_URL = re.compile(r"https://[^\s|}\]]+", re.IGNORECASE)
#: MediaWiki emphasis: a run of two or more apostrophes is markup, not text. MixesDB sets an
#: unidentified or unconfirmed entry in italics (``''Artist - ID''``), and the markup is not part
#: of the artist or the title.
_WIKI_EMPHASIS = re.compile(r"'{2,}")


def _emphasis_text(match: re.Match[str]) -> str:
    """What a run of apostrophes leaves as text, by MediaWiki's own rule.

    Two (italic), three (bold) and five (both) are pure markup. Four is one literal apostrophe
    followed by bold; more than five keeps the extras as literal apostrophes. A single apostrophe
    is never matched, so ``Don't``, ``Lady B's`` and ``Rockin' Steady`` are untouched.
    """

    run = len(match.group(0))
    if run == 4:
        return "'"
    if run > 5:
        return "'" * (run - 5)
    return ""


def strip_wiki_emphasis(value: str) -> str:
    """``value`` without MediaWiki italic/bold markup; apostrophes inside names are kept."""

    return re.sub(r"\s{2,}", " ", _WIKI_EMPHASIS.sub(_emphasis_text, value)).strip()


def parse_search(payload: dict[str, object]) -> tuple[str, ...]:
    results = payload.get("mixesdb_player_search")
    if not isinstance(results, list):
        return ()
    return tuple(
        str(item["pageid"])
        for item in results
        if isinstance(item, dict) and item.get("pageid") is not None
    )


def revision_content(payload: dict[str, object], page_id: str) -> str | None:
    query = payload.get("query")
    pages = query.get("pages") if isinstance(query, dict) else None
    if isinstance(pages, list):
        page = next(
            (
                item
                for item in pages
                if isinstance(item, dict) and str(item.get("pageid")) == page_id
            ),
            None,
        )
    elif isinstance(pages, dict):
        page = pages.get(page_id)
    else:
        return None
    revisions = page.get("revisions") if isinstance(page, dict) else None
    if not isinstance(revisions, list) or not revisions or not isinstance(revisions[0], dict):
        return None
    revision = revisions[0]
    slots = revision.get("slots")
    main = slots.get("main") if isinstance(slots, dict) else None
    content = (
        (main.get("*") or main.get("content")) if isinstance(main, dict) else revision.get("*")
    )
    return str(content) if content is not None else None


def parse_wikitext(text: str, *, page_id: str, mirror_of: str | None = None) -> ConnectorOutput:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    in_tracklist = False
    tracklist: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.fullmatch(r"={2,}\s*Tracklist\s*={2,}", stripped, re.IGNORECASE):
            in_tracklist = True
            continue
        if in_tracklist and re.match(r"^={2,}.*={2,}$", stripped):
            break
        if in_tracklist and stripped.startswith("#"):
            value = re.sub(r"^#+\s*", "", stripped)
            value = re.sub(r"<!--.*?-->", "", value).strip()
            value = strip_wiki_emphasis(value)
            if value:
                tracklist.append(value)
    mirrors = sorted(
        {
            cleaned
            for player in _PLAYER.findall(text)
            for url in _HTTPS_URL.findall(player)
            if not url_has_credentials(cleaned := url.rstrip(".,"))
        }
    )
    inputs: tuple[HintInput, ...] = ()
    if tracklist:
        inputs = (
            HintInput(
                connector="mixesdb",
                source_record_id=f"page-{page_id}-tracklist",
                text="\n".join(tracklist),
                author_pseudo_id="mixesdb",
                mirror_of=mirror_of,
                mirror_status="verified" if mirror_of is None else "quarantined",
                structured_tracklist=True,
            ),
        )
    return ConnectorOutput(
        inputs=inputs,
        mirrors=tuple(mirrors),
        items_fetched=len(tracklist),
        tracklist_blocks=1 if tracklist else 0,
    )


async def fetch(context: ConnectorContext) -> ConnectorOutput:
    search_response = await bounded_get(
        context,
        API_URL,
        params={
            "action": "mixesdb_player_search",
            "url": context.source.canonical_url,
            "format": "json",
        },
    )
    search = read_json_response(search_response, "mixesdb")
    write_raw_json(context.raw_path("search.json"), search)
    page_ids = parse_search(search)
    if not page_ids:
        return ConnectorOutput()
    page_id = page_ids[0]
    page_response = await bounded_get(
        context,
        API_URL,
        params={
            "action": "query",
            "pageids": page_id,
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "format": "json",
            "formatversion": "2",
        },
    )
    page = read_json_response(page_response, "mixesdb")
    write_raw_json(context.raw_path(f"page-{page_id}.json"), page)
    content = revision_content(page, page_id)
    if content is None:
        raise ConnectorError("MixesDB revision response has no main-slot content")
    return parse_wikitext(content, page_id=page_id)
