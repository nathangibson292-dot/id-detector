"""Look-up evidence: Discogs release styles and Last.fm tags, mapped onto the 400-style vocabulary.

Both sources are optional.  Discogs needs a personal token (``DISCOGS_API_TOKEN``, or the alias
``DISCOGS_TOKEN``); Last.fm needs an API key (``LASTFM_API_KEY``).  Without them the lookup step is
skipped and says so.  Each source makes at most two small requests per check, and a failure of
either is swallowed into "no evidence" so one dead source never stops the answer.

What each source is worth (owner-verified behaviour, 2026-10):

* Discogs release ``styles`` are curated but per *release*, and can be coarse (a famous house
  track's release lists only ``House``) or diluted (a compilation lists everything).  Releases are
  weighted by search rank and by how well their title matches, compilations count a third, and a
  release's weight is shared between the styles it lists.
* Last.fm ``track.getTopTags`` is often empty even for famous tracks, so ``artist.getTopTags`` is
  the fallback, and artist-level tags count for less than track tags (an artist's tags describe
  their whole catalogue, not this record).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

import httpx

from id_detector.genre import vocab

DISCOGS_SEARCH = "https://api.discogs.com/database/search"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "IDer-genre/1.0 (personal music library tool)"
TIMEOUT = httpx.Timeout(8.0, connect=5.0)


def discogs_token() -> str | None:
    for name in ("DISCOGS_API_TOKEN", "DISCOGS_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def lastfm_key() -> str | None:
    value = os.environ.get("LASTFM_API_KEY", "").strip()
    return value or None


@dataclass
class SourceEvidence:
    """One lookup source's answer: a share per label (summing to 1) and how much it is trusted."""

    source: str  # "discogs" | "lastfm"
    shares: dict[str, float] = field(default_factory=dict)
    #: 0..1: how well the record matched (Discogs), or track (1) vs artist-level (0.5) tags.
    quality: float = 0.0
    #: Plain words for the page: what was found ("4 releases", "artist tags").
    detail: str = ""
    #: The raw names that mapped (style or tag -> labels), kept for the "why" panel.
    raw: list[str] = field(default_factory=list)
    parents: dict[str, float] = field(default_factory=dict)
    status: str = "ok"  # "ok" | "off" | "none" | "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "shares": {k: round(v, 4) for k, v in self.shares.items()},
            "quality": round(self.quality, 3),
            "detail": self.detail,
            "raw": self.raw[:12],
            "parents": {k: round(v, 4) for k, v in self.parents.items()},
            "status": self.status,
        }


_BRACKETS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")


def _norm(text: str) -> str:
    text = _BRACKETS.sub("", text or "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


def similarity(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def parse_discogs(payload: Any, artist: str, title: str) -> SourceEvidence:
    """Discogs ``/database/search`` results -> style shares.  Pure: the tests drive it directly."""

    evidence = SourceEvidence("discogs")
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        evidence.status = "none"
        evidence.detail = "no matching release"
        return evidence
    weights: dict[str, float] = {}
    parents: dict[str, float] = {}
    total = 0.0
    matched = 0
    best_match = 0.0
    for rank, item in enumerate(results[:8]):
        if not isinstance(item, dict):
            continue
        styles = [str(s) for s in item.get("style") or [] if isinstance(s, str)]
        genres = [str(g) for g in item.get("genre") or [] if isinstance(g, str)]
        release_title = str(item.get("title") or "")  # "Artist - Release"
        artist_part, _, _ = release_title.partition(" - ")
        artist_match = similarity(artist, artist_part) if artist else 0.5
        if artist and artist_match < 0.6 and not _norm(artist_part).startswith("various"):
            continue  # a different artist's record that happened to share a word
        formats = [str(f).casefold() for f in item.get("format") or []]
        weight = 1.0 / (1.0 + 0.5 * rank)
        if "compilation" in formats or _norm(artist_part).startswith("various"):
            weight /= 3.0
        best_match = max(best_match, artist_match)
        matched += 1
        for genre in genres:
            parents[genre] = parents.get(genre, 0.0) + weight
        labels = [label for name in styles for label in vocab.map_discogs_style(name)]
        if not labels:
            continue
        evidence.raw.extend(name for name in styles if name not in evidence.raw)
        share = weight / len(labels)
        for label in labels:
            weights[label] = weights.get(label, 0.0) + share
        total += weight
    if not weights or total <= 0:
        evidence.status = "none"
        evidence.detail = "no matching release" if not matched else "releases list no style"
        return evidence
    evidence.shares = {label: value / total for label, value in weights.items()}
    parent_total = sum(parents.values()) or 1.0
    evidence.parents = {k: v / parent_total for k, v in parents.items()}
    evidence.quality = max(0.4, min(1.0, best_match)) * (1.0 if matched >= 2 else 0.8)
    evidence.detail = f"{matched} matching release{'s' if matched != 1 else ''}"
    return evidence


def parse_lastfm(payload: Any, *, artist_level: bool) -> SourceEvidence:
    """Last.fm ``toptags`` -> style shares (junk tags ignored, weak tags under 10 dropped)."""

    evidence = SourceEvidence("lastfm")
    tags: list[Any] = []
    if isinstance(payload, dict):
        block = payload.get("toptags")
        if isinstance(block, dict):
            raw = block.get("tag")
            tags = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    weights: dict[str, float] = {}
    parents: dict[str, float] = {}
    for item in tags[:30]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        try:
            count = float(item.get("count") or 0)
        except (TypeError, ValueError):
            count = 0.0
        if count < 10:
            continue
        top_parent = vocab.parent_tag(name)
        if top_parent:
            parents[top_parent] = parents.get(top_parent, 0.0) + count
        labels = vocab.map_tag(name)
        if not labels:
            continue
        evidence.raw.append(name)
        for label in labels:
            weights[label] = weights.get(label, 0.0) + count / len(labels)
    if not weights:
        evidence.status = "none"
        evidence.detail = "no style tags"
        return evidence
    total = sum(weights.values())
    evidence.shares = {label: value / total for label, value in weights.items()}
    for label, value in weights.items():
        top = vocab.parent(label)
        parents[top] = parents.get(top, 0.0) + value
    parent_total = sum(parents.values()) or 1.0
    evidence.parents = {k: v / parent_total for k, v in parents.items()}
    evidence.quality = 0.5 if artist_level else 1.0
    evidence.detail = "artist tags" if artist_level else "track tags"
    return evidence


class Lookups:
    """The two optional lookup sources.  ``transport`` lets tests answer with fakes, offline."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    @staticmethod
    def enabled() -> dict[str, bool]:
        return {"discogs": discogs_token() is not None, "lastfm": lastfm_key() is not None}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    async def discogs(self, artist: str, title: str) -> SourceEvidence:
        token = discogs_token()
        if token is None:
            return SourceEvidence("discogs", status="off", detail="no Discogs token")
        if not title:
            return SourceEvidence("discogs", status="none", detail="no title to look up")
        headers = {"Authorization": f"Discogs token={token}"}
        try:
            async with self._client() as client:
                params = {"type": "release", "track": title, "per_page": "10"}
                if artist:
                    params["artist"] = artist
                response = await client.get(DISCOGS_SEARCH, params=params, headers=headers)
                response.raise_for_status()
                evidence = parse_discogs(response.json(), artist, title)
                if evidence.status == "none" and artist:
                    # A loose query catches titles Discogs spells differently ("Ripgroove").
                    params = {"type": "release", "q": f"{artist} {title}", "per_page": "10"}
                    response = await client.get(DISCOGS_SEARCH, params=params, headers=headers)
                    response.raise_for_status()
                    evidence = parse_discogs(response.json(), artist, title)
                    evidence.quality *= 0.8
                return evidence
        except (httpx.HTTPError, ValueError) as exc:
            return SourceEvidence("discogs", status="error", detail=_error_words(exc))

    async def lastfm(self, artist: str, title: str) -> SourceEvidence:
        key = lastfm_key()
        if key is None:
            return SourceEvidence("lastfm", status="off", detail="no Last.fm key")
        if not artist:
            return SourceEvidence("lastfm", status="none", detail="no artist to look up")
        base = {"api_key": key, "format": "json", "autocorrect": "1", "artist": artist}
        try:
            async with self._client() as client:
                evidence = SourceEvidence("lastfm", status="none", detail="no style tags")
                if title:
                    response = await client.get(
                        LASTFM_API, params={**base, "method": "track.gettoptags", "track": title}
                    )
                    if response.status_code < 500:
                        evidence = parse_lastfm(_json_or_none(response), artist_level=False)
                if evidence.status != "ok":
                    response = await client.get(
                        LASTFM_API, params={**base, "method": "artist.gettoptags"}
                    )
                    response.raise_for_status()
                    evidence = parse_lastfm(_json_or_none(response), artist_level=True)
                return evidence
        except (httpx.HTTPError, ValueError) as exc:
            return SourceEvidence("lastfm", status="error", detail=_error_words(exc))


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _error_words(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return "the key was refused"
        if code == 429:
            return "rate limited, try again in a minute"
        return f"service error {code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    return "unreachable"
