"""Find the song: read the input, search, resolve a playable copy, cut short excerpts.

Input is a SoundCloud or YouTube link (analysed directly), a Spotify track link (Spotify gives no
audio, so its public page gives artist + title, then a playable copy is searched for), or free
text such as "Double 99 - RIP Groove" (searched on SoundCloud and YouTube through yt-dlp, no keys).

Only a few seconds of audio are fetched: yt-dlp resolves the stream address once, then ffmpeg
seeks straight to three points in the body of the song and decodes 15 s from each, in parallel,
to 16 kHz mono float - the model's input format.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import numpy as np

from id_detector.io import url_has_credentials
from id_detector.process import ProcessError, ProcessResult, ProcessTimeout, run_process

SAMPLE_RATE = 16_000
SEGMENT_SECONDS = 15.0
SEGMENT_COUNT = 3

#: ``run(args, timeout)`` -> ProcessResult; the tests replace it with a fake yt-dlp / ffmpeg.
Runner = Callable[[Sequence[str], float], Awaitable[ProcessResult]]


async def default_runner(args: Sequence[str], timeout: float) -> ProcessResult:
    return await run_process(list(args), timeout=timeout)


class SourceError(RuntimeError):
    """A plain-English reason the song could not be found or played."""


@dataclass
class Candidate:
    url: str
    title: str
    uploader: str = ""
    platform: str = "other"  # soundcloud | youtube | other
    duration: float | None = None
    artwork: str | None = None
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Resolved:
    """A playable copy: what is shown as "the track we analysed"."""

    url: str  # canonical public page
    title: str
    artist: str
    track: str
    uploader: str
    platform: str
    duration: float | None
    artwork: str | None
    stream_url: str
    headers: dict[str, str] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("stream_url")
        data.pop("headers")
        return data


@dataclass
class Query:
    kind: str  # "link" | "spotify" | "search"
    text: str
    platform: str = "other"


_SPOTIFY = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?track/([A-Za-z0-9]{22})"
)
_BARE_LINK = re.compile(
    r"^(www\.|m\.)?(soundcloud\.com|youtube\.com|youtu\.be|open\.spotify\.com)/", re.I
)
_SPOTIFY_URI = re.compile(r"^spotify:track:([A-Za-z0-9]{22})$")


def classify(text: str) -> Query:
    value = " ".join((text or "").split())
    if not value:
        raise SourceError(
            "Type an artist and title, or paste a SoundCloud, YouTube or Spotify link."
        )
    if len(value) > 500:
        raise SourceError("That is too long for a song search.")
    uri = _SPOTIFY_URI.match(value)
    if uri:
        return Query("spotify", f"https://open.spotify.com/track/{uri.group(1)}", "spotify")
    link = value if "://" in value else None
    if link is None and _BARE_LINK.match(value):
        link = "https://" + value
    if link is None:
        return Query("search", value)
    if url_has_credentials(link):
        raise SourceError("Links with a password in them are not accepted.")
    host = (urlsplit(link).hostname or "").casefold()
    spotify = _SPOTIFY.match(link)
    if spotify:
        return Query("spotify", f"https://open.spotify.com/track/{spotify.group(1)}", "spotify")
    if host.endswith("spotify.com"):
        raise SourceError("That Spotify link is not a single track. Paste a track link.")
    if host.endswith("soundcloud.com"):
        return Query("link", link, "soundcloud")
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}:
        return Query("link", link, "youtube")
    raise SourceError("Paste a SoundCloud, YouTube or Spotify link, or type artist - title.")


# --------------------------------------------------------------------------------------------
# Search and ranking
# --------------------------------------------------------------------------------------------
_JUNK_WORDS = re.compile(
    r"\b(mix|set|live|podcast|megamix|full album|hour|hours|boiler room|radio show|"
    r"reaction|tutorial|cover|karaoke|slowed|sped up|nightcore|8d)\b",
    re.I,
)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").casefold()) if len(w) > 1}


def score_candidate(query: str, candidate: Candidate, expected_duration: float | None) -> float:
    """Higher is a better match: shared words, a song-like length, no mix/live/cover words."""

    wanted = _words(query)
    have = _words(f"{candidate.uploader} {candidate.title}")
    score = len(wanted & have) / len(wanted) if wanted else 0.0
    duration = candidate.duration
    if duration is not None:
        if duration <= 31:
            score -= 0.6  # a 30 s preview clip
        elif duration > 900:
            score -= 0.8  # a mix or a full album
        elif expected_duration:
            score -= min(0.4, abs(duration - expected_duration) / expected_duration)
    for word in _JUNK_WORDS.findall(candidate.title or ""):
        if word.casefold() not in (query or "").casefold():
            score -= 0.3
    if candidate.uploader.endswith(" - Topic"):
        score += 0.15  # YouTube's official audio upload
    return round(score, 4)


def _thumbnail(entry: dict[str, Any]) -> str | None:
    thumbs = entry.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        sized = [t for t in thumbs if isinstance(t, dict) and t.get("url")]
        preferred = [t for t in sized if str(t.get("id")) in {"t500x500", "t300x300", "crop"}]
        pick = preferred[0] if preferred else sized[-1] if sized else None
        if pick:
            return str(pick["url"])
    thumb = entry.get("thumbnail")
    return str(thumb) if isinstance(thumb, str) else None


def _entry_candidate(entry: dict[str, Any], platform: str) -> Candidate | None:
    url = entry.get("webpage_url") or entry.get("url")
    if not isinstance(url, str) or not url:
        return None
    if platform == "youtube" and not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={entry.get('id')}"
    duration = entry.get("duration")
    return Candidate(
        url=url,
        title=str(entry.get("title") or ""),
        uploader=str(entry.get("uploader") or entry.get("channel") or ""),
        platform=platform,
        duration=float(duration) if isinstance(duration, (int, float)) else None,
        artwork=_thumbnail(entry),
    )


async def _yt_dlp_json(runner: Runner, args: list[str], timeout: float) -> dict[str, Any]:
    command = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist", *args]
    try:
        result = await runner(command, timeout)
    except ProcessTimeout as exc:
        raise SourceError("The music site took too long to answer.") from exc
    except ProcessError as exc:
        raise SourceError(_yt_dlp_words(str(exc))) from exc
    text = result.stdout.strip()
    try:
        payload = json.loads(text.splitlines()[-1] if text else "{}")
    except (ValueError, IndexError) as exc:
        raise SourceError("The music site's answer could not be read.") from exc
    if not isinstance(payload, dict):
        raise SourceError("The music site's answer could not be read.")
    return payload


def _yt_dlp_words(detail: str) -> str:
    lowered = detail.casefold()
    if "drm" in lowered:
        return "That upload is copy-protected (DRM), so it can't be listened to."
    if "private" in lowered or "sign in" in lowered or "login" in lowered:
        return "That upload is private or needs a sign-in."
    if "unavailable" in lowered or "not available" in lowered or "404" in lowered:
        return "That upload is unavailable."
    if "geo" in lowered or "country" in lowered:
        return "That upload is blocked in this country."
    return "The music site refused to play that upload."


async def search(
    query: str,
    runner: Runner,
    *,
    expected_duration: float | None = None,
    per_site: int = 5,
) -> list[Candidate]:
    """SoundCloud and YouTube results for ``query``, best match first."""

    async def one(prefix: str, platform: str) -> list[Candidate]:
        try:
            payload = await _yt_dlp_json(
                runner, ["--flat-playlist", "-J", f"{prefix}{per_site}:{query}"], 45
            )
        except SourceError:
            return []
        entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
        found = [_entry_candidate(e, platform) for e in entries if isinstance(e, dict)]
        return [c for c in found if c is not None]

    groups = await asyncio.gather(one("scsearch", "soundcloud"), one("ytsearch", "youtube"))
    candidates = [c for group in groups for c in group]
    for candidate in candidates:
        candidate.score = score_candidate(query, candidate, expected_duration)
    candidates.sort(key=lambda c: (-c.score, c.platform != "youtube"))
    return candidates


# --------------------------------------------------------------------------------------------
# Spotify (metadata only)
# --------------------------------------------------------------------------------------------
@dataclass
class SpotifyTrack:
    artist: str
    title: str
    duration: float | None
    artwork: str | None


def parse_spotify_page(page: str) -> SpotifyTrack | None:
    """Artist, title and length from a public Spotify track page's meta tags."""

    def meta(name: str) -> str | None:
        key = re.escape(name)
        found = re.search(
            rf'<meta[^>]+(?:property|name)="{key}"[^>]+content="([^"]*)"', page
        ) or re.search(rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{key}"', page)
        return html.unescape(found.group(1)) if found else None

    title = meta("og:title")
    description = meta("og:description") or ""
    artist = meta("music:musician_description")
    if not artist:
        # "Artist · Album · Song · 2020"
        parts = [p.strip() for p in description.split("·")]
        artist = parts[0] if parts and parts[0] else None
    if not artist:
        found = re.search(r"<title>([^<]*)</title>", page)
        if found:
            heading = html.unescape(found.group(1))
            byline = re.search(r" by (.+?) \| Spotify", heading)
            artist = byline.group(1) if byline else None
    if not title or not artist:
        return None
    seconds = meta("music:duration")
    try:
        duration = float(seconds) if seconds else None
    except ValueError:
        duration = None
    return SpotifyTrack(artist=artist, title=title, duration=duration, artwork=meta("og:image"))


#: ``fetch(url) -> text``; tests pass a fake.
TextFetch = Callable[[str], Awaitable[str]]


async def default_fetch_text(url: str) -> str:
    import httpx

    headers = {"User-Agent": "Mozilla/5.0 (compatible; IDer/1.0)", "Accept-Language": "en"}
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


async def spotify_track(url: str, fetch_text: TextFetch) -> SpotifyTrack:
    try:
        page = await fetch_text(url)
    except Exception as exc:  # noqa: BLE001 - any network failure reads the same to the owner
        raise SourceError("Spotify did not answer. Try typing artist - title instead.") from exc
    track = parse_spotify_page(page)
    if track is None:
        raise SourceError("Couldn't read that Spotify track. Try typing artist - title instead.")
    return track


# --------------------------------------------------------------------------------------------
# Resolve a playable copy and cut excerpts
# --------------------------------------------------------------------------------------------
_TITLE_JUNK = re.compile(
    r"\s*[\(\[](?:official[^\)\]]*|[^\)\]]*video[^\)\]]*|[^\)\]]*audio[^\)\]]*|lyrics?[^\)\]]*|"
    r"hd|hq|4k|free download[^\)\]]*|free dl|premiere|out now[^\)\]]*|"
    r"original(?: mix| version)?|extended(?: mix| version)?|radio (?:edit|mix)|"
    r"remaster(?:ed)?(?: \d{4})?|clip|visuali[sz]er)[\)\]]",
    re.I,
)


def split_artist_title(title: str, uploader: str = "") -> tuple[str, str]:
    """ "Artist - Title (Official Video)" -> ("Artist", "Title"); else (uploader, title)."""

    clean = _TITLE_JUNK.sub("", title or "").strip()
    clean = re.sub(r"^(premiere|free download|free dl)\s*[:|-]\s*", "", clean, flags=re.I)
    for separator in (" - ", " – ", " — ", " ~ "):
        if separator in clean:
            artist, _, rest = clean.partition(separator)
            return artist.strip(), rest.strip()
    artist = re.sub(r"\s*-\s*topic$", "", uploader or "", flags=re.I).strip()
    return artist, clean


def _pick_format(info: dict[str, Any]) -> tuple[str, dict[str, str]]:
    if isinstance(info.get("url"), str):
        headers = info.get("http_headers") if isinstance(info.get("http_headers"), dict) else {}
        return info["url"], {str(k): str(v) for k, v in headers.items()}
    downloads = info.get("requested_downloads") or info.get("requested_formats") or []
    for item in downloads:
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            headers = item.get("http_headers") if isinstance(item.get("http_headers"), dict) else {}
            return item["url"], {str(k): str(v) for k, v in headers.items()}
    raise SourceError("The music site gave no playable audio for that upload.")


async def resolve(url: str, runner: Runner, platform: str = "other") -> Resolved:
    """Ask yt-dlp for one upload's details and its best audio stream address (no download)."""

    info = await _yt_dlp_json(runner, ["-J", "-f", "ba/b", url], 60)
    if info.get("_type") == "playlist":
        raise SourceError("That link is a playlist or a profile. Paste a single track.")
    stream_url, headers = _pick_format(info)
    page = str(info.get("webpage_url") or url)
    title = str(info.get("title") or info.get("track") or "")
    uploader = str(info.get("uploader") or info.get("channel") or "")
    artist = str(info.get("artist") or info.get("creator") or "")
    track = str(info.get("track") or "")
    if not (artist and track):
        guessed_artist, guessed_track = split_artist_title(title, uploader)
        artist = artist or guessed_artist
        track = track or guessed_track
    artist = artist.split(",")[0].strip()
    extractor = str(info.get("extractor_key") or "").casefold()
    found_platform = platform
    if "soundcloud" in extractor:
        found_platform = "soundcloud"
    elif "youtube" in extractor:
        found_platform = "youtube"
    duration = info.get("duration")
    artwork = _thumbnail(info)
    video_id = info.get("id")
    if (
        found_platform == "youtube"
        and isinstance(video_id, str)
        and re.fullmatch(r"[\w-]{6,20}", video_id)
    ):
        # The listed "maxres" picture is often missing; mqdefault (16:9, no bars) always exists.
        artwork = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
    return Resolved(
        url=page,
        title=title,
        artist=artist,
        track=track,
        uploader=uploader,
        platform=found_platform,
        duration=float(duration) if isinstance(duration, (int, float)) else None,
        artwork=artwork,
        stream_url=stream_url,
        headers=headers,
    )


def segment_starts(
    duration: float | None, count: int = SEGMENT_COUNT, length: float = SEGMENT_SECONDS
) -> list[float]:
    """Where to listen: evenly through the body (30 %..70 %), never the intro or the outro."""

    if not duration or duration <= length + 1:
        return [0.0]
    if duration < 60:
        return [max(0.0, (duration - length) / 2)]
    first, last = 0.3 * duration, 0.7 * duration
    last = min(last, duration - length - 2)
    first = min(first, last)
    if count == 1:
        return [round((first + last) / 2, 2)]
    step = (last - first) / (count - 1)
    return [round(first + i * step, 2) for i in range(count)]


async def fetch_segments(
    resolved: Resolved,
    runner: Runner,
    *,
    starts: list[float] | None = None,
    length: float = SEGMENT_SECONDS,
) -> list[np.ndarray]:
    """Decode ``length`` seconds at each start, in parallel, to 16 kHz mono float32."""

    starts = starts if starts is not None else segment_starts(resolved.duration)
    header_text = "".join(f"{k}: {v}\r\n" for k, v in resolved.headers.items())
    with tempfile.TemporaryDirectory(prefix="idea-genre-") as folder:

        async def one(index: int, start: float) -> np.ndarray | None:
            target = Path(folder) / f"seg{index}.f32"
            command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
            if header_text:
                command += ["-headers", header_text]
            command += [
                "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", resolved.stream_url,
                "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", str(target),
            ]  # fmt: skip
            try:
                await runner(command, 90)
            except (ProcessError, ProcessTimeout):
                return None
            if not target.is_file():
                return None
            data = np.fromfile(target, dtype=np.float32)
            return data if data.size >= SAMPLE_RATE * 2 else None

        results = await asyncio.gather(*(one(i, s) for i, s in enumerate(starts)))
    segments = [r for r in results if r is not None]
    if not segments:
        raise SourceError("Couldn't fetch any audio from that upload.")
    return segments
