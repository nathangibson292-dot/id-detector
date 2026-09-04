"""Optional 1001tracklists title search; discovery never imports a page."""

from __future__ import annotations

from urllib.parse import quote

from id_detector.hints.connectors.base import (
    ConnectorContext,
    ConnectorOutput,
    bounded_get,
    read_json_response,
    write_raw_json,
)
from id_detector.hints.parse import HintInput

SEARCH_URL = "https://www.1001tracklists.com/ajax/search_tracklist.php"


def parse_search(payload: dict[str, object]) -> ConnectorOutput:
    data = payload.get("data")
    if not isinstance(data, list):
        return ConnectorOutput()
    urls: list[str] = []
    inputs: list[HintInput] = []
    for index, item in enumerate(data):
        properties = item.get("properties") if isinstance(item, dict) else None
        if not isinstance(properties, dict):
            continue
        identifier = properties.get("id_unique")
        if identifier is None:
            continue
        url = f"https://1001.tl/{quote(str(identifier), safe='')}"
        urls.append(url)
        inputs.append(
            HintInput(
                connector="tl1001_search",
                source_record_id=f"result-{index}",
                text=url,
                author_pseudo_id="1001tl-search",
                mirror_status="quarantined",
            )
        )
    return ConnectorOutput(inputs=tuple(inputs), pointers=tuple(urls), items_fetched=len(urls))


async def search(context: ConnectorContext) -> ConnectorOutput:
    response = await bounded_get(
        context,
        SEARCH_URL,
        params={
            "p": context.source.title or "",
            "noIDFieldCheck": "true",
            "fixedMode": "true",
            "sf": "p",
        },
    )
    payload = read_json_response(response, "tl1001_search")
    write_raw_json(context.raw_path("search.json"), payload)
    return parse_search(payload)
