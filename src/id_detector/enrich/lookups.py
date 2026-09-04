"""Zero-auth (Discogs token-optional) catalogue lookups, split into pure parsers and fetchers.

The parsers take a decoded JSON body and return normalised :class:`Candidate` lists; they are
what the fixture-driven tests exercise. The async fetchers add only URL/parameter shaping and the
cached, rate-limited request through :class:`EnrichHttp`.  A fetcher never raises: a bounded lookup
failure is swallowed to an empty list so one dead source can never abort enrichment.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from id_detector.enrich.http import EnrichHttp, EnrichHttpError
from id_detector.enrich.match import Candidate

logger = logging.getLogger(__name__)

DEEZER_SEARCH = "https://api.deezer.com/search"
ITUNES_SEARCH = "https://itunes.apple.com/search"
MUSICBRAINZ_RECORDING = "https://musicbrainz.org/ws/2/recording"
DISCOGS_SEARCH = "https://api.discogs.com/database/search"


def _clean(value: Any) -> str:
    return str(value).replace('"', "").strip() if value is not None else ""


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def parse_deezer(payload: Any) -> list[Candidate]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    candidates: list[Candidate] = []
    for item in payload["data"]:
        if not isinstance(item, dict) or item.get("type") not in {None, "track"}:
            continue
        track_id = item.get("id")
        link = item.get("link")
        if track_id is None or not isinstance(link, str):
            continue
        artist = item.get("artist") if isinstance(item.get("artist"), dict) else {}
        album = item.get("album") if isinstance(item.get("album"), dict) else {}
        duration = _int_or_none(item.get("duration"))
        candidates.append(
            Candidate(
                source="deezer",
                url=str(link),
                artist=_clean(artist.get("name")),
                title=_clean(item.get("title")),
                album=_clean(album.get("title")) or None,
                duration_ms=duration * 1000 if duration is not None else None,
                recording_ids={"deezer": str(track_id)},
            )
        )
    return candidates


def parse_itunes(payload: Any) -> list[Candidate]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return []
    candidates: list[Candidate] = []
    for item in payload["results"]:
        if not isinstance(item, dict):
            continue
        track_id = item.get("trackId")
        view_url = item.get("trackViewUrl")
        if track_id is None or not isinstance(view_url, str):
            continue
        candidates.append(
            Candidate(
                source="apple",
                url=str(view_url),
                artist=_clean(item.get("artistName")),
                title=_clean(item.get("trackName")),
                album=_clean(item.get("collectionName")) or None,
                duration_ms=_int_or_none(item.get("trackTimeMillis")),
                recording_ids={"apple": str(track_id)},
            )
        )
    return candidates


def parse_musicbrainz(payload: Any) -> list[Candidate]:
    if not isinstance(payload, dict) or not isinstance(payload.get("recordings"), list):
        return []
    candidates: list[Candidate] = []
    for item in payload["recordings"]:
        if not isinstance(item, dict):
            continue
        mbid = item.get("id")
        if not isinstance(mbid, str):
            continue
        credit = item.get("artist-credit")
        artist = ""
        if isinstance(credit, list) and credit and isinstance(credit[0], dict):
            artist = _clean(credit[0].get("name"))
        isrcs = tuple(
            str(code)
            for code in (item.get("isrcs") or [])
            if isinstance(code, str) and code.strip()
        )
        recording_ids = {"mb_recording": mbid}
        if isrcs:
            recording_ids["isrc"] = isrcs[0]
        candidates.append(
            Candidate(
                source="musicbrainz",
                url=f"https://musicbrainz.org/recording/{mbid}",
                artist=artist,
                title=_clean(item.get("title")),
                album=None,
                duration_ms=_int_or_none(item.get("length")),
                recording_ids=recording_ids,
                isrcs=isrcs,
            )
        )
    return candidates


def parse_discogs(payload: Any) -> list[Candidate]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return []
    candidates: list[Candidate] = []
    for item in payload["results"]:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str):
            continue
        uri = item.get("uri")
        url = (
            f"https://www.discogs.com{uri}"
            if isinstance(uri, str) and uri.startswith("/")
            else str(item.get("resource_url") or "")
        )
        if not url:
            continue
        # Discogs release titles are "Artist - Title"; keep the whole label for scoring.
        artist, _, track = title.partition(" - ")
        candidates.append(
            Candidate(
                source="discogs",
                url=url,
                artist=_clean(artist),
                title=_clean(track or title),
                album=None,
                duration_ms=None,
                recording_ids={},
            )
        )
    return candidates


async def fetch_deezer(http: EnrichHttp, artist: str, title: str) -> list[Candidate]:
    query = f'artist:"{_clean(artist)}" track:"{_clean(title)}"'
    return await _safe("deezer", parse_deezer, http, DEEZER_SEARCH, params={"q": query})


async def fetch_itunes(http: EnrichHttp, artist: str, title: str) -> list[Candidate]:
    term = f"{_clean(artist)} {_clean(title)}".strip()
    params = {"term": term, "media": "music", "entity": "song", "limit": 5}
    return await _safe("apple", parse_itunes, http, ITUNES_SEARCH, params=params)


async def fetch_musicbrainz(http: EnrichHttp, artist: str, title: str) -> list[Candidate]:
    query = f'recording:"{_clean(title)}" AND artist:"{_clean(artist)}"'
    params = {"query": query, "fmt": "json", "limit": 5}
    return await _safe("musicbrainz", parse_musicbrainz, http, MUSICBRAINZ_RECORDING, params=params)


def discogs_token() -> str | None:
    token = os.environ.get("DISCOGS_TOKEN", "").strip()
    return token or None


async def fetch_discogs(http: EnrichHttp, artist: str, title: str) -> list[Candidate]:
    token = discogs_token()
    if token is None:
        return []
    params = {"type": "release", "artist": _clean(artist), "track": _clean(title), "per_page": 5}
    headers = {"Authorization": f"Discogs token={token}"}
    return await _safe(
        "discogs", parse_discogs, http, DISCOGS_SEARCH, params=params, headers=headers
    )


async def _safe(source, parser, http: EnrichHttp, url, *, params=None, headers=None):
    try:
        payload = await http.get_json(source, url, params=params, headers=headers)
    except EnrichHttpError as exc:
        logger.warning("enrich lookup %s unavailable: %s", source, exc)
        return []
    if payload is None:
        return []
    return parser(payload)
