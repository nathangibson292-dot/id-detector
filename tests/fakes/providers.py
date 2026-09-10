"""Scripted offline fakes matching the production provider boundaries.

A script section may carry ``"latency_ms"`` so several fake requests are genuinely in flight at
once (the concurrency and cancellation gates); ``FakeAudD`` also reports the peak number of
concurrent calls it saw.  :func:`no_backoff` is the retry sleeper tests inject so the recipe's
1/2/4 s backoff costs nothing but still yields to the event loop.

A script's ``"windows"`` table is keyed by the window's position in the mix (``"3"`` = the fourth
window): a clip is ranked among the ``.wav`` files of its directory, which are named
``<start_ms:010d>-<variant>.wav`` and all written before the first request, so the mapping does
not depend on the order a worker pool happens to dispatch in (Shazam leases are ordered by
creation time then id, which is not stable run to run; that stability is what the Local Free
golden relies on).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from shazamio.interfaces.client import HTTPClientInterface

from id_detector.io import native_path
from id_detector.providers.base import (
    AmbiguousProviderOutcome,
    ProviderProtocolError,
    ProviderUnavailable,
)
from id_detector.shazam import ShazamHTTPError

OUTCOMES = frozenset(
    {
        "match",
        "no_match",
        "http_401",
        "http_403",
        "quota_error",
        "http_429",
        "http_503",
        "http_500",
        "timeout_pre",
        "timeout_post",
        "malformed",
    }
)
_BILLABLE = frozenset({"match", "no_match", "http_500", "timeout_post", "malformed"})


def _load_script(value: Path | Mapping[str, Any]) -> Mapping[str, Any]:
    loaded = json.loads(value.read_text(encoding="utf-8")) if isinstance(value, Path) else value
    if not isinstance(loaded, Mapping):
        raise ValueError("fake-provider script root must be an object")
    return loaded


class _ScriptedProvider:
    def __init__(self, provider: str, script: Path | Mapping[str, Any]) -> None:
        root = _load_script(script)
        section = root.get(provider, {})
        if not isinstance(section, Mapping):
            raise ValueError(f"fake-provider {provider} section must be an object")
        default = section.get("default", "no_match")
        windows = section.get("windows", {})
        if not isinstance(default, str) or default not in OUTCOMES:
            raise ValueError(f"unsupported {provider} default outcome: {default!r}")
        if not isinstance(windows, Mapping):
            raise ValueError(f"fake-provider {provider}.windows must be an object")
        latency_ms = section.get("latency_ms", 0)
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0:
            raise ValueError(f"fake-provider {provider}.latency_ms must be a non-negative integer")
        self.provider = provider
        self.default = default
        self.latency_s = latency_ms / 1000
        # Optional ``"labels": {"<index>": "D"}`` — the tone letter a window's match names instead
        # of the position-derived default (A/B/C by index), so a script can make one provider
        # find a track the other never reported (the secondary's reserve confirmations).
        labels = section.get("labels", {})
        if not isinstance(labels, Mapping):
            raise ValueError(f"fake-provider {provider}.labels must be an object")
        self.labels: dict[int, str] = {}
        for key, letter in labels.items():
            if not isinstance(letter, str) or letter not in _LETTER_TONES:
                raise ValueError(f"unsupported {provider} label for window {key!r}: {letter!r}")
            self.labels[int(key)] = letter
        self.windows: dict[int, tuple[str, ...]] = {}
        for key, value in windows.items():
            try:
                index = int(key)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"fake-provider window index must be an integer: {key!r}") from exc
            sequence = (value,) if isinstance(value, str) else value
            if not isinstance(sequence, Sequence) or isinstance(sequence, (str, bytes)):
                raise ValueError(f"fake-provider window {index} outcome must be a string or list")
            outcomes = tuple(str(item) for item in sequence)
            if not outcomes or any(outcome not in OUTCOMES for outcome in outcomes):
                raise ValueError(f"unsupported {provider} window {index} outcome")
            self.windows[index] = outcomes
        self._path_indices: dict[str, int] = {}
        self._first_seen = 0
        self._outcome_counts: dict[int, int] = {}
        self.attempts: list[dict[str, object]] = []

    def index_for_path(self, path: Path | None) -> int:
        """The window ordinal a script's ``windows`` table refers to (see the module note).

        A clip that is not one of a directory's ``.wav`` files (a synthetic path in a unit test,
        or no path at all) keeps the historical first-seen numbering.
        """

        key = str(path.resolve()) if path is not None else f"request:{self._first_seen}"
        if key not in self._path_indices:
            ordinal = _directory_ordinal(path) if path is not None else None
            if ordinal is None:
                ordinal = self._first_seen
                self._first_seen += 1
            self._path_indices[key] = ordinal
        return self._path_indices[key]

    def next_outcome(self, index: int) -> str:
        sequence = self.windows.get(index, (self.default,))
        offset = self._outcome_counts.get(index, 0)
        self._outcome_counts[index] = offset + 1
        outcome = sequence[min(offset, len(sequence) - 1)]
        self.attempts.append({"provider": self.provider, "window": index, "outcome": outcome})
        return outcome


def _directory_ordinal(path: Path) -> int | None:
    """``path``'s rank among the ``.wav`` files beside it (window order), or ``None``."""

    try:
        names = sorted(
            name for name in os.listdir(native_path(path.parent)) if name.endswith(".wav")
        )
        return names.index(path.name)
    except (OSError, ValueError):
        return None


_LETTER_TONES = {"A": 440, "B": 554, "C": 659, "D": 880, "E": 1046, "F": 1318, "G": 1568}


def _tone_label(index: int, letter: str | None = None) -> tuple[str, str, int]:
    if letter is None:
        letter = "A" if index < 2 else "B" if index < 5 else "C"
    frequency = _LETTER_TONES[letter]
    return f"Fixture Artist {letter}", f"Tone {frequency}", frequency


async def _notify_attempt(callback: Callable[[], Awaitable[None]] | object) -> None:
    if not callable(callback):
        return
    result = callback()
    if inspect.isawaitable(result):
        await result


async def no_backoff(_seconds: float) -> None:
    """Retry sleeper for tests: the recipe's backoff schedule costs no wall time but yields."""

    await asyncio.sleep(0)


class FakeAudD:
    """Scripted ``AudDAdapter.recognize_clip`` replacement."""

    def __init__(self, script: Path | Mapping[str, Any], *, latency_s: float | None = None) -> None:
        self.script = _ScriptedProvider("audd", script)
        self.latency_s = self.script.latency_s if latency_s is None else latency_s
        self.calls = 0
        self.billed_units = 0
        self.paths: list[Path] = []
        self.in_flight = 0
        self.max_in_flight = 0

    @property
    def attempts(self) -> list[dict[str, object]]:
        return self.script.attempts

    async def recognize_clip(
        self,
        path: Path,
        on_attempt: Callable[[], Awaitable[None]],
    ) -> dict[str, Any]:
        index = self.script.index_for_path(path)
        # Production invokes this callback immediately before network I/O. A refused admission
        # therefore must not appear as a provider call or scripted attempt in the fake either.
        await _notify_attempt(on_attempt)
        self.paths.append(path)
        outcome = self.script.next_outcome(index)
        self.calls += 1
        self.billed_units += int(outcome in _BILLABLE)
        if self.latency_s:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await asyncio.sleep(self.latency_s)
            finally:
                self.in_flight -= 1
        if outcome == "timeout_pre":
            raise ProviderUnavailable("AudD timed out before receiving a response")
        if outcome == "timeout_post":
            raise AmbiguousProviderOutcome("AudD response was lost after dispatch")
        if outcome.startswith("http_"):
            status = int(outcome.removeprefix("http_"))
            if status in {401, 403}:
                # AudD commonly expresses authentication failures in a JSON error body.
                return {
                    "status": "error",
                    "error": {"error_code": status, "error_message": "authentication failed"},
                }
            raise ProviderProtocolError(f"AudD HTTP {status}")
        if outcome == "quota_error":
            return {
                "status": "error",
                "error": {"error_code": 402, "error_message": "fixture quota exhausted"},
            }
        if outcome == "malformed":
            return {"status": "success", "result": {}}
        if outcome == "no_match":
            return {"status": "success", "result": None}
        artist, title, frequency = _tone_label(index, self.script.labels.get(index))
        return {
            "status": "success",
            "result": {
                "artist": artist,
                "title": title,
                "timecode": index * 9,
                "song_link": f"fixture:tone:{frequency}",
            },
        }


class FakeShazamHTTP(HTTPClientInterface):
    """Scripted Shazam HTTP boundary; signature generation remains production code."""

    def __init__(self, script: Path | Mapping[str, Any]) -> None:
        self.script = _ScriptedProvider("shazam", script)
        self._path: ContextVar[Path | None] = ContextVar("fake_shazam_path", default=None)
        self.requests = 0

    @property
    def attempts(self) -> list[dict[str, object]]:
        return self.script.attempts

    def prepare_path(self, path: Path) -> None:
        self._path.set(path)

    async def request(
        self,
        method: str,
        url: str,
        *args: object,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del method, url, args, kwargs
        index = self.script.index_for_path(self._path.get())
        outcome = self.script.next_outcome(index)
        self.requests += 1
        if self.script.latency_s:
            await asyncio.sleep(self.script.latency_s)
        if outcome.startswith("http_"):
            status = int(outcome.removeprefix("http_"))
            raise ShazamHTTPError(status, f"scripted Shazam HTTP {status}")
        if outcome == "quota_error":
            raise ShazamHTTPError(402, "scripted Shazam quota error")
        if outcome in {"timeout_pre", "timeout_post"}:
            raise ShazamHTTPError(0, f"scripted Shazam {outcome}")
        if outcome == "malformed":
            raise ShazamHTTPError(200, "scripted malformed Shazam response")
        if outcome == "no_match":
            return {"matches": [], "track": None}
        artist, title, frequency = _tone_label(index, self.script.labels.get(index))
        return {
            "matches": [
                {
                    "offset": index * 9,
                    "frequencyskew": 0,
                    "timeskew": 0,
                }
            ],
            "track": {
                "key": f"fixture-{frequency}",
                "subtitle": artist,
                "title": title,
                "sections": [],
            },
        }


def load_fake_providers(
    path: Path,
    names: Sequence[str],
) -> tuple[FakeAudD | None, FakeShazamHTTP | None]:
    """Load one script once and construct only the requested provider boundaries."""

    script = _load_script(path)
    audd = FakeAudD(script) if "audd" in names else None
    shazam = FakeShazamHTTP(script) if "shazam" in names else None
    return audd, shazam
