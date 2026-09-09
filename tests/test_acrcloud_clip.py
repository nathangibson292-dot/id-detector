"""ACRCloud's real-time identify (clip) path — the gate-free paid design, mirroring AudD's.

Covers the identify response -> observation parse and the HMAC-signed POST over a MockTransport;
no network, no credentials, no trial quota is used.
"""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path

import httpx

from id_detector.contracts import QueryRecord, WindowRecord
from id_detector.providers.acrcloud import (
    ACRCloudClipAdapter,
    ACRCloudCredentials,
    acrcloud_clip_response_to_observation,
)

ROOT = Path(__file__).resolve().parent


def _golden(name: str) -> dict[str, object]:
    return json.loads((ROOT / "golden" / f"{name}.json").read_text(encoding="utf-8"))


def _window_and_query() -> tuple[WindowRecord, QueryRecord]:
    return WindowRecord.model_validate(_golden("window")), QueryRecord.model_validate(
        _golden("query")
    )


def test_a_music_match_becomes_a_positioned_clip_observation() -> None:
    window, query = _window_and_query()
    response = {
        "status": {"code": 0, "msg": "Success"},
        "metadata": {
            "music": [
                {
                    "title": "Run It",
                    "artists": [{"name": "MPH"}],
                    "album": {"name": "Run It"},
                    "label": "Self",
                    "release_date": "2023-01-01",
                    "external_ids": {"isrc": "GBABC1234567"},
                    "external_metadata": {"spotify": {"track": {"id": "abc"}}},
                    "score": 100,
                }
            ]
        },
    }
    obs = acrcloud_clip_response_to_observation(
        response, query=query, window=window, media_key="a" * 64, raw_response_ref="r.json"
    )
    assert obs.provider == "acrcloud" and obs.capability == "clip_recognizer"
    assert obs.status == "match"
    assert obs.raw_label.artist == "MPH" and obs.raw_label.title == "Run It"
    assert obs.provider_ids["isrc"] == "GBABC1234567"
    assert "acrcloud_spotify" in obs.provider_ids  # platform link feeds the "where to get it" chips
    assert obs.mix_span_ms == window.support_ms and obs.transform == window.transform


def test_multiple_artists_join_with_a_comma() -> None:
    window, query = _window_and_query()
    response = {
        "status": {"code": 0},
        "metadata": {"music": [{"title": "Contact", "artists": [{"name": "A"}, {"name": "B"}]}]},
    }
    obs = acrcloud_clip_response_to_observation(
        response, query=query, window=window, media_key="a" * 64, raw_response_ref="r.json"
    )
    assert obs.raw_label.artist == "A, B"


def test_no_result_code_1001_is_a_no_match() -> None:
    window, query = _window_and_query()
    obs = acrcloud_clip_response_to_observation(
        {"status": {"code": 1001, "msg": "No result"}, "metadata": {}},
        query=query,
        window=window,
        media_key="a" * 64,
        raw_response_ref="r.json",
    )
    assert obs.status == "no_match"
    assert obs.raw_label.artist is None and obs.raw_label.title is None


def test_recognize_clip_posts_a_signed_request_to_the_identify_endpoint(tmp_path: Path) -> None:
    clip = tmp_path / "clip.wav"
    with wave.open(str(clip), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 16_000)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"status": {"code": 1001}, "metadata": {}})

    adapter = ACRCloudClipAdapter(
        ACRCloudCredentials(
            host="identify-eu-west-1.acrcloud.com",
            access_key="key",
            access_secret="secret",
            container_id="000000",
        ),
        transport=httpx.MockTransport(handler),
    )

    async def _noop() -> None:
        return None

    asyncio.run(adapter.recognize_clip(clip, on_attempt=_noop))
    assert seen["url"] == "https://identify-eu-west-1.acrcloud.com/v1/identify"
    body = bytes(seen["body"]).decode("latin-1")  # type: ignore[arg-type]
    # The signed, gate-free form fields are present (the secret itself is never sent).
    assert "signature" in body and "access_key" in body and "data_type" in body
    assert "secret" not in body
