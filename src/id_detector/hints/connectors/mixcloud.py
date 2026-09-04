"""Optional Mixcloud GraphQL TrackSection connector."""

from __future__ import annotations

from urllib.parse import urlsplit

from id_detector.hints.connectors.base import (
    ConnectorContext,
    ConnectorError,
    ConnectorOutput,
    bounded_post,
    read_json_response,
    write_raw_json,
)
from id_detector.hints.parse import HintInput

GRAPHQL_URL = "https://app.mixcloud.com/graphql"
QUERY = """query HintSections($username: String!, $slug: String!) {
  cloudcastLookup(lookup: {username: $username, slug: $slug}) {
    name
    sections {
      __typename
      ... on TrackSection { startSeconds songName artistName }
      ... on ChapterSection { chapter startSeconds }
    }
  }
}"""


def parse_sections(payload: dict[str, object], *, duration_ms: int) -> ConnectorOutput:
    data = payload.get("data")
    cloudcast = data.get("cloudcastLookup") if isinstance(data, dict) else None
    sections = cloudcast.get("sections") if isinstance(cloudcast, dict) else None
    if not isinstance(sections, list):
        return ConnectorOutput()
    tracks = [
        item
        for item in sections
        if isinstance(item, dict)
        and item.get("__typename") == "TrackSection"
        and isinstance(item.get("startSeconds"), int)
        and isinstance(item.get("songName"), str)
    ]
    inputs: list[HintInput] = []
    for index, item in enumerate(tracks):
        start = int(item["startSeconds"]) * 1_000
        next_start = (
            int(tracks[index + 1]["startSeconds"]) * 1_000
            if index + 1 < len(tracks)
            else duration_ms
        )
        artist = str(item.get("artistName") or "Unknown artist")
        inputs.append(
            HintInput(
                connector="mixcloud_graphql",
                source_record_id=f"section-{index}",
                text=f"{artist} - {item['songName']}",
                position_ms=start,
                position_end_ms=max(start + 1, min(duration_ms, next_start)),
                position_kind="section",
                author_pseudo_id="mixcloud",
                is_uploader=True,
                structured_tracklist=True,
            )
        )
    return ConnectorOutput(
        inputs=tuple(inputs),
        items_fetched=len(inputs),
        tracklist_blocks=1 if inputs else 0,
    )


async def fetch(context: ConnectorContext) -> ConnectorOutput:
    path = [part for part in urlsplit(context.source.canonical_url).path.split("/") if part]
    if len(path) < 2:
        raise ConnectorError("Mixcloud URL does not contain username and slug")
    response = await bounded_post(
        context,
        GRAPHQL_URL,
        json_body={"query": QUERY, "variables": {"username": path[0], "slug": path[1]}},
    )
    payload = read_json_response(response, "mixcloud_graphql")
    write_raw_json(context.raw_path("sections.json"), payload)
    errors = payload.get("errors")
    if errors:
        raise ConnectorError("Mixcloud GraphQL returned errors")
    return parse_sections(payload, duration_ms=context.duration_ms)
