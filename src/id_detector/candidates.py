"""Candidate reference-pool discovery and indexing for Panako (Stage 8).

The reference pool that catches unreleased / SoundCloud-only long-tail tracks is built from audio
the owner is entitled to fingerprint: the set uploader's own SoundCloud uploads, artists named in
the parsed hints/tracklist, and any extra artist or URL the owner supplies.  Per the plan,
**discovery emits links and never auto-rips** — :func:`discover_candidates` only lists candidates;
audio is downloaded and indexed exclusively for candidates the owner confirms (an explicit
``--index`` flag) or supplies directly (an explicit URL or local file).

Only fingerprints are kept: :func:`index_candidates` deletes each downloaded/decoded audio file
immediately after Panako fingerprints it, so the on-disk footprint is the Panako DB alone.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from id_detector.io import atomic_write_bytes, canonical_json_bytes, native_path
from id_detector.process import run_process
from id_detector.providers.panako import (
    PANAKO_JAR_SHA256,
    PANAKO_VERSION,
    PanakoProvider,
)

#: yt-dlp search prefix and default breadth for artist-name discovery on SoundCloud.
SCSEARCH_LIMIT = 10


@dataclass(frozen=True)
class Candidate:
    """One discovered or supplied reference track — a link, never yet ripped."""

    url: str
    title: str | None
    uploader: str | None
    source: str
    platform_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "title": self.title,
            "uploader": self.uploader,
            "source": self.source,
            "platform_id": self.platform_id,
        }


# --------------------------------------------------------------------------------------------------
# Parsing yt-dlp listings (network-free; unit-tested on a recorded fixture)
# --------------------------------------------------------------------------------------------------
def _entry_url(entry: Mapping[str, object]) -> str | None:
    for key in ("webpage_url", "url", "original_url"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def candidate_from_entry(entry: Mapping[str, object], *, source: str) -> Candidate | None:
    """Map one yt-dlp playlist/search entry to a :class:`Candidate` (skips entries with no URL)."""

    url = _entry_url(entry)
    if url is None:
        return None
    title = entry.get("title")
    uploader = entry.get("uploader") or entry.get("channel") or entry.get("uploader_id")
    platform_id = entry.get("id")
    return Candidate(
        url=url,
        title=str(title) if isinstance(title, str) else None,
        uploader=str(uploader) if isinstance(uploader, str) else None,
        source=source,
        platform_id=str(platform_id) if platform_id is not None else None,
    )


def parse_flat_playlist(json_text: str, *, source: str) -> list[Candidate]:
    """Parse ``yt-dlp --flat-playlist --dump-single-json`` output into candidates.

    Accepts either a playlist object (``{"entries": [...]}``) or a single-entry object, and
    tolerates nested playlists (an entry that itself carries ``entries``).
    """

    payload = json.loads(json_text)
    if not isinstance(payload, Mapping):
        raise ValueError("yt-dlp listing must be a JSON object")
    entries = payload.get("entries")
    candidates: list[Candidate] = []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            nested = entry.get("entries")
            if isinstance(nested, list):
                for inner in nested:
                    if isinstance(inner, Mapping):
                        candidate = candidate_from_entry(inner, source=source)
                        if candidate is not None:
                            candidates.append(candidate)
                continue
            candidate = candidate_from_entry(entry, source=source)
            if candidate is not None:
                candidates.append(candidate)
    else:
        candidate = candidate_from_entry(payload, source=source)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def artists_from_hints(hints_path: Path) -> list[str]:
    """Collect distinct, order-preserving artist names from a ``hints/hints.jsonl`` artefact."""

    artists: list[str] = []
    seen: set[str] = set()
    if not hints_path.is_file():
        return artists
    for line in hints_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        record = json.loads(stripped)
        artist = record.get("artist")
        if isinstance(artist, str) and artist.strip() and artist not in seen:
            seen.add(artist)
            artists.append(artist.strip())
    return artists


def deduplicate_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Collapse candidates that resolve to the same URL, keeping first-seen order and source."""

    result: list[Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        result.append(candidate)
    return result


# --------------------------------------------------------------------------------------------------
# Live discovery (yt-dlp; network — driven only on explicit request)
# --------------------------------------------------------------------------------------------------
async def _ytdlp_flat_json(target: str, *, timeout: float = 300) -> str:
    result = await run_process(
        [
            sys.executable,
            "-m",
            "yt_dlp",
            "--flat-playlist",
            "--dump-single-json",
            "--no-warnings",
            target,
        ],
        timeout=timeout,
    )
    return result.stdout


async def discover_uploader_uploads(uploader_url: str, *, timeout: float = 300) -> list[Candidate]:
    """List the uploader's own public uploads (``.../tracks``) as candidates."""

    listing = await _ytdlp_flat_json(uploader_url, timeout=timeout)
    return parse_flat_playlist(listing, source="uploader_uploads")


async def discover_artist(
    name: str, *, limit: int = SCSEARCH_LIMIT, timeout: float = 300
) -> list[Candidate]:
    """Search SoundCloud for an artist name and return public results as candidates."""

    listing = await _ytdlp_flat_json(f"scsearch{limit}:{name}", timeout=timeout)
    return parse_flat_playlist(listing, source=f"artist_search:{name}")


def uploader_uploads_url(set_listing_json: str) -> str | None:
    """Derive the uploader's own uploads URL (``.../tracks``) from a set's flat-playlist JSON."""

    payload = json.loads(set_listing_json)
    if not isinstance(payload, Mapping):
        return None
    for key in ("uploader_url", "channel_url", "uploader_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value.rstrip("/") + "/tracks"
    return None


async def resolve_uploader_uploads_url(set_url: str, *, timeout: float = 120) -> str | None:
    """Fetch a set's listing and return the uploader's uploads URL (best-effort)."""

    listing = await _ytdlp_flat_json(set_url, timeout=timeout)
    return uploader_uploads_url(listing)


async def discover_candidates(
    *,
    set_url: str | None = None,
    uploader_url: str | None = None,
    artists: Sequence[str] = (),
    extra_urls: Sequence[str] = (),
    search_limit: int = SCSEARCH_LIMIT,
) -> list[Candidate]:
    """Discover candidate reference tracks; emits links only (never downloads)."""

    candidates: list[Candidate] = []
    resolved_uploader = uploader_url
    if resolved_uploader is None and set_url:
        resolved_uploader = await resolve_uploader_uploads_url(set_url)
    if resolved_uploader:
        candidates.extend(await discover_uploader_uploads(resolved_uploader))
    for name in artists:
        candidates.extend(await discover_artist(name, limit=search_limit))
    for url in extra_urls:
        candidates.append(Candidate(url=url, title=None, uploader=None, source="explicit_url"))
    return deduplicate_candidates(candidates)


# --------------------------------------------------------------------------------------------------
# Downloading + indexing (only user-confirmed / explicit candidates)
# --------------------------------------------------------------------------------------------------
async def download_audio(candidate: Candidate, dest_dir: Path, *, timeout: float = 3600) -> Path:
    """Download one candidate's best audio into ``dest_dir`` and return the file path."""

    os.makedirs(native_path(dest_dir), exist_ok=True)
    template = str(dest_dir / "candidate.%(ext)s")
    await run_process(
        [
            sys.executable,
            "-m",
            "yt_dlp",
            "-f",
            "ba",
            "--no-playlist",
            "--no-progress",
            "--no-write-comments",
            "-o",
            template,
            candidate.url,
        ],
        timeout=timeout,
    )
    files = [
        item
        for item in dest_dir.iterdir()
        if item.is_file() and not item.name.endswith((".part", ".ytdl"))
    ]
    if len(files) != 1:
        raise ValueError(f"yt-dlp produced {len(files)} files for {candidate.url}; expected one")
    return files[0]


Downloader = Callable[[Candidate, Path], Awaitable[Path]]


@dataclass(frozen=True)
class IndexedResource:
    resource_id: str
    fingerprint_count: int
    title: str | None
    uploader: str | None
    url: str
    source: str


async def index_candidates(
    provider: PanakoProvider,
    candidates: Sequence[Candidate],
    *,
    download_dir: Path,
    downloader: Downloader | None = None,
    keep_audio: bool = False,
) -> list[IndexedResource]:
    """Download, fingerprint, then **delete** each candidate's audio, keeping only fingerprints.

    The audio file is removed in a ``finally`` block, so a fingerprinting failure never leaves the
    downloaded track behind.  ``keep_audio`` is provided only for debugging and defaults to off.
    """

    downloader = download_audio if downloader is None else downloader
    os.makedirs(native_path(download_dir), exist_ok=True)
    indexed: list[IndexedResource] = []
    for position, candidate in enumerate(candidates):
        item_dir = download_dir / f"cand-{position:04d}"
        os.makedirs(native_path(item_dir), exist_ok=True)
        audio = await downloader(candidate, item_dir)
        try:
            stored = await provider.store([audio])
        finally:
            if not keep_audio:
                _delete_audio(audio)
        for resource in stored:
            indexed.append(
                IndexedResource(
                    resource_id=resource.resource_id,
                    fingerprint_count=resource.fingerprint_count,
                    title=candidate.title,
                    uploader=candidate.uploader,
                    url=candidate.url,
                    source=candidate.source,
                )
            )
    return indexed


def _delete_audio(audio: Path) -> None:
    audio.unlink(missing_ok=True)


# --------------------------------------------------------------------------------------------------
# Index manifest
# --------------------------------------------------------------------------------------------------
def derive_index_id(label: str) -> str:
    return sha256(label.encode("utf-8")).hexdigest()[:16]


def derive_index_version(resources: Sequence[IndexedResource]) -> str:
    payload = sorted((resource.resource_id, resource.fingerprint_count) for resource in resources)
    return sha256(canonical_json_bytes(payload)).hexdigest()[:16]


def build_manifest(*, index_label: str, resources: Sequence[IndexedResource]) -> dict[str, object]:
    """Build the (float-free) index manifest recording index_id/index_version for cache keys."""

    return {
        "index_label": index_label,
        "index_id": derive_index_id(index_label),
        "index_version": derive_index_version(resources),
        "panako_version": PANAKO_VERSION,
        "panako_jar_sha256": PANAKO_JAR_SHA256,
        "resources": [
            {
                "resource_id": resource.resource_id,
                "fingerprint_count": resource.fingerprint_count,
                "title": resource.title,
                "uploader": resource.uploader,
                "url": resource.url,
                "source": resource.source,
            }
            for resource in resources
        ],
    }


def write_manifest(manifest_path: Path, manifest: Mapping[str, object]) -> None:
    atomic_write_bytes(manifest_path, canonical_json_bytes(manifest))


def format_candidate_list(candidates: Sequence[Candidate]) -> str:
    """Human-readable candidate listing printed by ``build-index`` (links only, never auto-rips)."""

    if not candidates:
        return "no candidates discovered"
    lines = [f"{len(candidates)} candidate reference track(s) discovered (no audio downloaded):"]
    for position, candidate in enumerate(candidates, start=1):
        title = candidate.title or "(untitled)"
        lines.append(f"  {position:>3}. [{candidate.source}] {title} — {candidate.url}")
    lines.append(
        "Re-run with --index to download and fingerprint these, or pass explicit "
        "--extra-url / --file items to index directly."
    )
    return "\n".join(lines)
