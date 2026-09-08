"""AudD's clip (main-endpoint) recognition path — the corrected, gate-free paid design.

Covers the clip->observation parse and the adapter's HTTP call over a MockTransport; no network,
no credentials, no trial quota is used.
"""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path

import httpx

from id_detector.contracts import QueryRecord, WindowRecord
from id_detector.providers.audd import (
    MAIN_ENDPOINT,
    AudDAdapter,
    AudDCredentials,
    clip_response_to_observation,
)
from id_detector.providers.base import AppConfig

ROOT = Path(__file__).resolve().parent


def _golden(name: str) -> dict[str, object]:
    return json.loads((ROOT / "golden" / f"{name}.json").read_text(encoding="utf-8"))


def _window_and_query() -> tuple[WindowRecord, QueryRecord]:
    return WindowRecord.model_validate(_golden("window")), QueryRecord.model_validate(
        _golden("query")
    )


def test_a_clip_match_becomes_a_positioned_clip_observation() -> None:
    window, query = _window_and_query()
    response = {
        "status": "success",
        "result": {
            "artist": "Effy",
            "title": "CLUBGRLS",
            "album": "Single",
            "label": "Self",
            "release_date": "2024-01-01",
            "isrc": "GB1234567890",
            "song_link": "https://lis.tn/CLUBGRLS",
        },
    }
    obs = clip_response_to_observation(
        response,
        query=query,
        window=window,
        media_key="a" * 64,
        raw_response_ref="recognise/x.json",
    )
    assert obs.provider == "audd" and obs.capability == "clip_recognizer"
    assert obs.status == "match"
    assert obs.raw_label.artist == "Effy" and obs.raw_label.title == "CLUBGRLS"
    assert obs.provider_ids["isrc"] == "GB1234567890"
    # Positioned on the window it recognised, so it competes with a Shazam clip observation.
    assert obs.mix_span_ms == window.support_ms and obs.support_ms == window.support_ms
    assert obs.transform == window.transform


def test_a_clip_with_no_result_is_a_no_match() -> None:
    window, query = _window_and_query()
    obs = clip_response_to_observation(
        {"status": "success", "result": None},
        query=query,
        window=window,
        media_key="a" * 64,
        raw_response_ref="recognise/x.json",
    )
    assert obs.status == "no_match"
    assert obs.raw_label.artist is None and obs.raw_label.title is None


def test_recognize_clip_posts_to_the_main_endpoint_with_no_consent_gate(tmp_path: Path) -> None:
    clip = tmp_path / "clip.wav"
    with wave.open(str(clip), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 16_000)
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200, json={"status": "success", "result": {"artist": "A", "title": "B"}}
        )

    # AppConfig(False) = no upload consent — the clip path must NOT require it (unlike whole-file).
    adapter = AudDAdapter(
        AudDCredentials("token"), AppConfig(False), False, transport=httpx.MockTransport(handler)
    )

    async def _noop() -> None:
        return None

    response = asyncio.run(adapter.recognize_clip(clip, on_attempt=_noop))
    assert seen["url"] == MAIN_ENDPOINT
    assert response["result"]["artist"] == "A"
