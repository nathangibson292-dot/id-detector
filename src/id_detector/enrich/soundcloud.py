"""SoundCloud api-v2 acquisition-flag lookup.

Reuses Stage 4a ``client_id`` discovery (``hints.connectors.soundcloud.discover_client_id``) via a
lightweight connector context whose raw cache lives under ``data/local/enrich/`` — no job store is
touched.  The public token is credential-stripped from every cache key and never written to an
artefact.  We resolve the best track for an episode through ``search/tracks`` and read only its
acquisition fields; we **never** fetch or automate a download gate.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from id_detector.contracts import SourceRecord
from id_detector.enrich.http import EnrichHttp, EnrichHttpError
from id_detector.enrich.links import classify_soundcloud
from id_detector.enrich.match import Candidate, match_confidence_e4
from id_detector.hints.connectors.base import CircuitBreaker, ConnectorContext, ConnectorError
from id_detector.hints.connectors.soundcloud import discover_client_id
from id_detector.jobs import ConnectorJob

logger = logging.getLogger(__name__)

SEARCH_TRACKS = "https://api-v2.soundcloud.com/search/tracks"


@dataclass(frozen=True)
class SoundcloudFlags:
    classification: str
    downloadable: bool | None
    has_downloads_left: bool | None
    purchase_url: str | None
    purchase_title: str | None
    license: str | None
    permalink_url: str | None
    match_confidence_e4: int | None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def build_token_context(
    source: SourceRecord, cache_root: Path, client: httpx.AsyncClient
) -> ConnectorContext:
    """A store-free context that satisfies ``discover_client_id`` and ``bounded_get`` only."""

    now = _now_iso()
    job = ConnectorJob(
        id=uuid.uuid4().hex,
        media_key=source.media_key,
        connector="enrich_soundcloud",
        target_url="https://soundcloud.com/",
        cursor=None,
        page=0,
        page_cap=1,
        item_cap=1,
        items_fetched=0,
        state="leased",
        lease_owner="enrich",
        lease_expires_at=None,
        heartbeat_at=None,
        attempts=0,
        next_retry_at=None,
        result_path=None,
        truncated=0,
        error=None,
        created_at=now,
        updated_at=now,
    )
    return ConnectorContext(
        source=source,
        duration_ms=0,
        media_dir=cache_root,
        cache_root=cache_root,
        store=None,  # type: ignore[arg-type]  # never used by discover_client_id / bounded_get
        job=job,
        owner="enrich",
        http=client,
        breaker=CircuitBreaker(),
    )


def parse_search_tracks(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("collection"), list):
        return [item for item in payload["collection"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _track_candidate(track: dict[str, Any]) -> Candidate | None:
    permalink = track.get("permalink_url")
    if not isinstance(permalink, str):
        return None
    user = track.get("user") if isinstance(track.get("user"), dict) else {}
    title = str(track.get("title") or "")
    artist = str(user.get("username") or "")
    duration = track.get("duration")
    return Candidate(
        source="soundcloud",
        url=permalink,
        artist=artist,
        title=title,
        duration_ms=int(duration) if isinstance(duration, int) else None,
        recording_ids={"soundcloud": str(track["id"])} if track.get("id") is not None else {},
    )


def flags_from_track(track: dict[str, Any], confidence_e4: int | None) -> SoundcloudFlags:
    downloadable = track.get("downloadable")
    has_downloads_left = track.get("has_downloads_left")
    purchase_url = track.get("purchase_url")
    classification = classify_soundcloud(
        downloadable=downloadable if isinstance(downloadable, bool) else None,
        has_downloads_left=(has_downloads_left if isinstance(has_downloads_left, bool) else None),
        purchase_url=purchase_url if isinstance(purchase_url, str) else None,
    )
    return SoundcloudFlags(
        classification=classification,
        downloadable=downloadable if isinstance(downloadable, bool) else None,
        has_downloads_left=has_downloads_left if isinstance(has_downloads_left, bool) else None,
        purchase_url=purchase_url if isinstance(purchase_url, str) else None,
        purchase_title=(
            str(track["purchase_title"]) if isinstance(track.get("purchase_title"), str) else None
        ),
        license=str(track["license"]) if isinstance(track.get("license"), str) else None,
        permalink_url=(
            str(track["permalink_url"]) if isinstance(track.get("permalink_url"), str) else None
        ),
        match_confidence_e4=confidence_e4,
    )


def best_soundcloud_flags(
    tracks: list[dict[str, Any]],
    *,
    artist: str,
    title: str,
    reference_duration_ms: int | None = None,
) -> SoundcloudFlags | None:
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, track in enumerate(tracks):
        candidate = _track_candidate(track)
        if candidate is None:
            continue
        confidence = match_confidence_e4(
            artist, title, candidate, reference_duration_ms=reference_duration_ms
        )
        scored.append((confidence, -index, track))
    if not scored:
        return None
    confidence, _, track = max(scored, key=lambda item: (item[0], item[1]))
    return flags_from_track(track, confidence)


async def fetch_soundcloud_flags(
    http: EnrichHttp,
    *,
    token_context: ConnectorContext,
    artist: str,
    title: str,
    reference_duration_ms: int | None = None,
) -> SoundcloudFlags | None:
    """Resolve the best SoundCloud track match and read its acquisition flags (never automate)."""

    try:
        client_id = await discover_client_id(token_context)
    except ConnectorError as exc:
        logger.warning("enrich soundcloud client discovery unavailable: %s", exc)
        return None
    term = f"{artist} {title}".strip()
    params = {"q": term, "limit": 5, "client_id": client_id}
    try:
        payload = await http.get_json("soundcloud", SEARCH_TRACKS, params=params)
    except EnrichHttpError:
        # A stale token yields 401/403; refresh once and retry.
        try:
            client_id = await discover_client_id(token_context, refresh=True)
            params["client_id"] = client_id
            payload = await http.get_json("soundcloud", SEARCH_TRACKS, params=params)
        except (EnrichHttpError, ConnectorError) as exc:
            logger.warning("enrich soundcloud search unavailable: %s", exc)
            return None
    if payload is None:
        return None
    tracks = parse_search_tracks(payload)
    return best_soundcloud_flags(
        tracks, artist=artist, title=title, reference_duration_ms=reference_duration_ms
    )
