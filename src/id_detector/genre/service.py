"""One genre check, start to finish, with live steps and a per-track cache.

Steps the page shows: **Finding the track** -> **Listening** -> **Looking up** -> **Done** (plus a
one-time **Downloading the genre model** step).  Listening and looking up run at the same time;
the model download overlaps finding the track.

Results are cached as small JSON files under ``<work root>/.genre/``: by the analysed upload's
address and by the exact text that was asked, so asking again is instant and offline.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from id_detector.genre import fuse as fusion
from id_detector.genre import model as style_model
from id_detector.genre import source, tempo_key, vocab
from id_detector.genre.lookup import Lookups, SourceEvidence

#: Bump whenever the model use, fusion, BPM or key method changes: saved answers are then redone.
RESULT_VERSION = 3
STEPS = (
    ("model", "Downloading the genre model (one time)"),
    ("find", "Finding the track"),
    ("listen", "Listening"),
    ("lookup", "Looking up"),
    ("done", "Done"),
)
#: How many resolved candidates are tried before giving up (DRM / private / blocked uploads).
MAX_TRIES = 4
MAX_CHECKS_KEPT = 40


def _now() -> float:
    return time.time()


@dataclass
class Check:
    id: str
    input: str
    pick: str | None = None
    status: str = "running"  # running | done | failed
    steps: dict[str, str] = field(default_factory=dict)  # key -> waiting|active|done|skipped|failed
    message: str = ""
    download: dict[str, int] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    track: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    started: float = field(default_factory=_now)
    finished: float | None = None

    def public(self) -> dict[str, Any]:
        steps = [
            {"key": key, "name": name, "state": self.steps.get(key, "waiting")}
            for key, name in STEPS
            if key != "model" or key in self.steps
        ]
        return {
            "id": self.id,
            "status": self.status,
            "steps": steps,
            "message": self.message,
            "download": self.download,
            "candidates": self.candidates,
            "track": self.track,
            "result": self.result,
            "error": self.error,
            "elapsed": round((self.finished or _now()) - self.started, 1),
        }


class GenreService:
    """Runs checks on background threads and remembers the recent ones for status polling."""

    def __init__(
        self,
        work_root: Path,
        *,
        model_dir: Path | None = None,
        runner: source.Runner | None = None,
        fetch_text: source.TextFetch | None = None,
        fetch_bytes: Callable[[str], Awaitable[tuple[bytes, str]]] | None = None,
        lookups: Lookups | None = None,
        infer_factory: Callable[[Path], Callable[[np.ndarray], np.ndarray]] | None = None,
        model_fetch: Callable[..., None] | None = None,
        background: bool = True,
    ) -> None:
        self.cache_dir = Path(work_root) / ".genre"
        self.model_dir = model_dir or style_model.default_model_dir()
        self.runner = runner or source.default_runner
        self.fetch_text = fetch_text or source.default_fetch_text
        self.fetch_bytes = fetch_bytes or default_fetch_bytes
        self.lookups = lookups or Lookups()
        self._infer_factory = infer_factory or (lambda d: style_model.StyleModel(d).activations)
        self._model_fetch = model_fetch
        self._infer: Callable[[np.ndarray], np.ndarray] | None = None
        self._infer_lock = threading.Lock()
        self._checks: dict[str, Check] = {}
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(2)
        self._background = background

    # ---------------------------------------------------------------- public API
    def start(self, text: str, pick: str | None = None) -> Check:
        query = source.classify(text)  # raises SourceError for nonsense input, before any work
        if pick is not None:
            picked = source.classify(pick)
            if picked.kind != "link":
                raise source.SourceError("Pick one of the listed results.")
        check = Check(id=secrets.token_hex(8), input=query.text, pick=pick)
        cached = self._cached(self._input_key(query.text, pick))
        if cached is not None:
            self._finish_cached(check, cached)
        with self._lock:
            self._checks[check.id] = check
            while len(self._checks) > MAX_CHECKS_KEPT:
                self._checks.pop(next(iter(self._checks)))
        if check.status == "running":
            if self._background:
                threading.Thread(target=self._run, args=(check, query), daemon=True).start()
            else:
                self._run(check, query)
        return check

    def get(self, check_id: str) -> Check | None:
        with self._lock:
            return self._checks.get(check_id)

    def model_ready(self) -> bool:
        return style_model.is_ready(self.model_dir)

    # ---------------------------------------------------------------- the work
    def _run(self, check: Check, query: source.Query) -> None:
        with self._slots:
            try:
                asyncio.run(self._check(check, query))
            except source.SourceError as exc:
                self._fail(check, str(exc))
            except style_model.ModelError as exc:
                self._fail(check, f"The genre model could not be used: {exc}.")
            except Exception:  # noqa: BLE001 - an unexpected fault must still end the check
                self._fail(check, "Something went wrong during the check. Try again.")

    def _fail(self, check: Check, message: str) -> None:
        for key, state in check.steps.items():
            if state == "active":
                check.steps[key] = "failed"
        check.error = message
        check.message = message
        check.status = "failed"
        check.finished = _now()

    async def _check(self, check: Check, query: source.Query) -> None:
        timings: dict[str, float] = {}
        t0 = time.perf_counter()
        model_task = None
        if not style_model.is_ready(self.model_dir):
            check.steps["model"] = "active"
            check.download = {"done": 0, "total": style_model.DOWNLOAD_BYTES}
            model_task = asyncio.create_task(asyncio.to_thread(self._download, check))
        check.steps["find"] = "active"
        check.message = "Finding the track"
        resolved, lookup_artist, lookup_title, candidates = await self._find(check, query)
        check.candidates = [c.to_dict() for c in candidates[:8]]
        check.track = resolved.public()
        timings["find"] = round(time.perf_counter() - t0, 2)
        track_key = self._track_key(resolved.url)
        cached = self._cached(track_key)
        if cached is not None:
            if model_task is not None:
                model_task.cancel()
                check.steps.pop("model", None)
            cached = dict(cached, candidates=check.candidates, input=check.input)
            self._store(self._input_key(check.input, check.pick), cached)
            self._finish_cached(check, cached)
            return
        check.steps["find"] = "done"

        check.steps["listen"] = "active"
        check.steps["lookup"] = "active"
        check.message = "Listening and looking up"
        t1 = time.perf_counter()
        listen_task = asyncio.create_task(self._listen(check, resolved, model_task))
        lookup_task = asyncio.create_task(self._lookup(check, lookup_artist, lookup_title))
        art_task = asyncio.create_task(self._artwork(resolved.artwork))
        probabilities, segments, starts, listen_seconds, key_task = await listen_task
        discogs, lastfm = await lookup_task
        art = await art_task
        if art:
            check.track = dict(check.track, art=art)
        timings["listen"] = listen_seconds
        timings["listen_and_lookup"] = round(time.perf_counter() - t1, 2)

        fused = fusion.fuse(probabilities, discogs, lastfm)
        t2 = time.perf_counter()
        hint = _tempo_hint(fused, probabilities)
        tempo = tempo_key.estimate_tempo(segments, hint)
        key, second_opinion = await key_task
        timings["bpm_key"] = round(time.perf_counter() - t2, 2)
        timings["total"] = round(time.perf_counter() - t0, 2)
        result = {
            "version": RESULT_VERSION,
            "input": check.input,
            "track": check.track,
            "candidates": check.candidates,
            "lookup_names": {"artist": lookup_artist, "title": lookup_title},
            "genres": fused.ranking,
            "model_ranking": fused.model_ranking,
            "parent": fused.parent,
            "parent_share": fused.parent_share,
            "agreement": fused.agreement,
            "summary": fused.summary,
            "weights": fused.weights,
            "sources": {"discogs": discogs.to_dict(), "lastfm": lastfm.to_dict()},
            "lookups_enabled": Lookups.enabled(),
            "bpm": None if tempo is None else {"value": tempo.bpm, "confidence": tempo.confidence},
            "key": None
            if key is None
            else {
                "name": key.name,
                "tonic": key.tonic,
                "mode": key.mode,
                "camelot": key.camelot,
                "confidence": key.confidence,
                "alternative": None
                if second_opinion is None or second_opinion.name == key.name
                else {"name": second_opinion.name, "camelot": second_opinion.camelot},
            },
            "segments": [{"start": s, "seconds": source.SEGMENT_SECONDS} for s in starts],
            "timings": timings,
            "analysed_at": int(_now()),
        }
        self._store(track_key, result)
        self._store(self._input_key(check.input, check.pick), result)
        check.result = dict(result, cached=False)
        check.steps["done"] = "done"
        check.message = "Done"
        check.status = "done"
        check.finished = _now()

    async def _find(
        self, check: Check, query: source.Query
    ) -> tuple[source.Resolved, str, str, list[source.Candidate]]:
        if check.pick:
            previous = self._cached(self._input_key(check.input, None)) or {}
            names = previous.get("lookup_names") or {}
            resolved = await source.resolve(check.pick, self.runner)
            candidates = [source.Candidate(**c) for c in previous.get("candidates") or []]
            if not candidates and query.kind == "search":
                candidates = await source.search(query.text, self.runner)
            artist = names.get("artist") or resolved.artist
            title = names.get("title") or resolved.track
            if query.kind == "link":
                artist, title = resolved.artist, resolved.track
            return resolved, artist, title, candidates
        if query.kind == "link":
            resolved = await source.resolve(query.text, self.runner, query.platform)
            return resolved, resolved.artist, resolved.track, []
        expected = None
        spotify = None
        if query.kind == "spotify":
            spotify = await source.spotify_track(query.text, self.fetch_text)
            artist, title, expected = spotify.artist, spotify.title, spotify.duration
            search_text = f"{artist} - {title}"
            check.message = f"Found on Spotify: {artist} - {title}. Finding a playable copy"
        else:
            search_text = query.text
            artist, title = source.split_artist_title(query.text)
        candidates = await source.search(search_text, self.runner, expected_duration=expected)
        if not candidates:
            raise source.SourceError(
                "Nothing matched on SoundCloud or YouTube. Check the spelling."
            )
        check.candidates = [c.to_dict() for c in candidates[:8]]
        last_error: source.SourceError | None = None
        for candidate in candidates[:MAX_TRIES]:
            try:
                resolved = await source.resolve(candidate.url, self.runner, candidate.platform)
            except source.SourceError as exc:
                last_error = exc
                continue
            if not artist:
                artist, title = resolved.artist, resolved.track
            if spotify is not None and spotify.artwork:
                resolved.artwork = spotify.artwork
            return resolved, artist, title, candidates
        raise source.SourceError(
            "Found matches, but none could be played"
            + (f" ({str(last_error).rstrip('.')})." if last_error else ".")
        )

    async def _listen(
        self, check: Check, resolved: source.Resolved, model_task: asyncio.Task[None] | None
    ) -> tuple[np.ndarray, list[np.ndarray], list[float], float, asyncio.Future[Any]]:
        starts = source.segment_starts(resolved.duration)
        segments = await source.fetch_segments(resolved, self.runner, starts=starts)
        # The key needs only the audio, so it is measured while the model listens.
        key_task = asyncio.ensure_future(asyncio.to_thread(tempo_key.estimate_keys, segments))
        if model_task is not None:
            check.message = "Waiting for the genre model to finish downloading"
            await model_task
            check.steps["model"] = "done"
        t = time.perf_counter()
        infer = await asyncio.to_thread(self._inference)
        probabilities = await asyncio.to_thread(style_model.predict, segments, infer)
        check.steps["listen"] = "done"
        return probabilities, segments, starts, round(time.perf_counter() - t, 2), key_task

    async def _lookup(
        self, check: Check, artist: str, title: str
    ) -> tuple[SourceEvidence, SourceEvidence]:
        discogs, lastfm = await asyncio.gather(
            self.lookups.discogs(artist, title), self.lookups.lastfm(artist, title)
        )
        enabled = [e for e in (discogs, lastfm) if e.status != "off"]
        check.steps["lookup"] = "done" if enabled else "skipped"
        return discogs, lastfm

    async def _artwork(self, url: str | None) -> str | None:
        """Keep a copy of the cover image, so the page (whose policy loads images only from
        itself) can show it, and a saved answer still shows it offline.  Returns its id."""

        if not url or not artwork_allowed(url):
            return None
        art_id = hashlib.sha256(url.encode()).hexdigest()[:24]
        if any(self.cache_dir.glob(f"art-{art_id}.*")):
            return art_id
        try:
            body, content_type = await self.fetch_bytes(url)
        except Exception:  # noqa: BLE001 - a missing cover never stops a check
            return None
        extension = ART_TYPES.get(content_type.split(";")[0].strip().lower())
        if extension is None or not body or len(body) > ART_LIMIT:
            return None
        with contextlib.suppress(OSError):
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"art-{art_id}{extension}").write_bytes(body)
            return art_id
        return None

    def artwork_path(self, art_id: str) -> Path | None:
        if not re.fullmatch(r"[0-9a-f]{24}", art_id or ""):
            return None
        for path in self.cache_dir.glob(f"art-{art_id}.*"):
            if path.suffix in ART_TYPES.values():
                return path
        return None

    def recent(self, limit: int = 8) -> list[dict[str, Any]]:
        """The latest distinct answers (newest first) for the page's "Recent" list."""

        try:
            files = sorted(
                self.cache_dir.glob("input-*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )
        except OSError:
            return []
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for path in files:
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            track = data.get("track") or {}
            genres = data.get("genres") or []
            url = str(track.get("url") or "")
            if not genres or url in seen or data.get("version") != RESULT_VERSION:
                continue
            seen.add(url)
            items.append(
                {
                    "input": data.get("input") or "",
                    "title": track.get("title") or "",
                    "genre": genres[0].get("name") or "",
                    "bpm": (data.get("bpm") or {}).get("value"),
                }
            )
            if len(items) >= limit:
                break
        return items

    def _download(self, check: Check) -> None:
        def progress(done: int, total: int) -> None:
            check.download = {"done": done, "total": total}

        style_model.ensure_model(self.model_dir, progress, fetch=self._model_fetch)

    def _inference(self) -> Callable[[np.ndarray], np.ndarray]:
        with self._infer_lock:
            if self._infer is None:
                self._infer = self._infer_factory(self.model_dir)
            return self._infer

    # ---------------------------------------------------------------- cache
    @staticmethod
    def _input_key(text: str, pick: str | None) -> str:
        normal = " ".join(text.casefold().split())
        return "input-" + hashlib.sha256(f"{normal}\n{pick or ''}".encode()).hexdigest()[:32]

    @staticmethod
    def _track_key(url: str) -> str:
        return "track-" + hashlib.sha256(url.strip().encode()).hexdigest()[:32]

    def _cached(self, key: str) -> dict[str, Any] | None:
        path = self.cache_dir / f"{key}.json"
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("version") != RESULT_VERSION:
            return None
        return data

    def _store(self, key: str, result: dict[str, Any]) -> None:
        with contextlib.suppress(OSError):
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self.cache_dir / f"{key}.json"
            temporary = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
            temporary.write_text(json.dumps(result, ensure_ascii=False), "utf-8")
            os.replace(temporary, path)

    def _finish_cached(self, check: Check, cached: dict[str, Any]) -> None:
        check.track = cached.get("track")
        check.candidates = cached.get("candidates") or []
        check.result = dict(cached, cached=True)
        for key, _ in STEPS:
            if key != "model":
                check.steps[key] = "done"
        check.message = "Done (saved answer)"
        check.status = "done"
        check.finished = _now()


ART_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
ART_LIMIT = 2_000_000
_ART_HOSTS = ("sndcdn.com", "ytimg.com", "scdn.co", "ggpht.com", "googleusercontent.com")


def artwork_allowed(url: str) -> bool:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    return parts.scheme == "https" and any(
        host == allowed or host.endswith("." + allowed) for allowed in _ART_HOSTS
    )


async def default_fetch_bytes(url: str) -> tuple[bytes, str]:
    import httpx

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content[: ART_LIMIT + 1], response.headers.get("content-type", "")


def _tempo_hint(fused: fusion.Fused, probabilities: np.ndarray) -> str | None:
    """The best-placed style with a distinctive tempo band, from the fused and heard rankings."""

    banded = set(tempo_key._STYLE_TEMPO)
    for item in fused.ranking[:2]:
        if item["label"] in banded:
            return item["label"]
    labels = vocab.labels()
    top = int(np.argmax(probabilities))
    return labels[top] if labels[top] in banded else None
