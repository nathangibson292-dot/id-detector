from __future__ import annotations

import asyncio
import json
import math
import os
import time
import wave
from pathlib import Path

from id_detector.calibration import (
    estimate_latency_distribution,
    offset_error_from_first_fingerprinted_sample,
)
from id_detector.contracts import ProviderConfigRecord, QueryRecord, WindowRecord
from id_detector.io import path_is_file, read_bytes, read_text
from id_detector.jobs import AsyncJobStore
from id_detector.recognise import (
    MAX_RETRIES,
    _cache_valid,
    load_provider_config,
    recognise_generation_zero,
)
from id_detector.shazam import CircuitBreaker, ShazamAdapter, TokenBucket, response_to_observation
from id_detector.windows import generate_windows_async
from tests.test_stage1_windows import _decoded

ROOT = Path(__file__).parent


def test_partial_overlap_bias_is_relative_to_first_fingerprinted_sample() -> None:
    assert (
        offset_error_from_first_fingerprinted_sample(
            offset_ms=38_934, leading_silence_ms=1_000, position_ms=40_000
        )
        == -66
    )


def test_latency_estimator_uses_position_rates_and_censored_failures() -> None:
    cases = [
        {
            "position_ms": position,
            "material_ms": duration,
            "track_key": "expected" if duration >= threshold else None,
            "offset_ms": position if duration >= threshold else None,
        }
        for position, threshold in ((10_000, 4_000), (40_000, 6_000), (70_000, 9_000))
        for duration in (4_000, 6_000, 9_000)
    ]
    l_min, curve = estimate_latency_distribution(cases, "expected")
    assert [item["success_fraction_e4"] for item in curve] == [3333, 6667, 10_000]
    assert l_min == {"p50": 6_000, "p90": 9_000, "p95": 9_000}
    assert sum(item["n_trials"] - item["n_successes"] for item in curve) == 3


def test_latency_estimator_rejects_a_right_censored_quantile() -> None:
    cases = [
        {
            "position_ms": position,
            "material_ms": duration,
            "track_key": "expected" if position == 10_000 and duration == 8_000 else None,
            "offset_ms": 1 if position == 10_000 and duration == 8_000 else None,
        }
        for position in (10_000, 40_000)
        for duration in (6_000, 8_000)
    ]
    with __import__("pytest").raises(RuntimeError, match="right-censored"):
        estimate_latency_distribution(cases, "expected")


def _fixture(name: str) -> dict[str, object]:
    return json.loads((ROOT / "fixtures" / "shazam" / name).read_text(encoding="utf-8"))


def _golden(name: str) -> dict[str, object]:
    return json.loads((ROOT / "golden" / f"{name}.json").read_text(encoding="utf-8"))


def test_adapter_selects_latest_measured_v3_config() -> None:
    config, name = load_provider_config(ROOT.parent)
    assert name == config.version == "shazam-v3.json"
    assert config.measured
    assert config.config["latency_estimator"] == "position-first-success-ecdf-v1"


def test_recorded_responses_map_full_matches_and_apply_measured_bias() -> None:
    window = WindowRecord.model_validate(_golden("window"))
    query = QueryRecord.model_validate(_golden("query"))
    config_data = _golden("provider_config")
    config_data.update(
        {
            "measured": True,
            "adapter_bias_ms": 250,
            "adapter_bias_uncertainty_ms": 80,
            "L_min_ms": {"p95": 6000},
            "source_ids": ["insertion-suite:fixture"],
        }
    )
    config = ProviderConfigRecord.model_validate(config_data)
    observation = response_to_observation(
        _fixture("response-match.json"),
        query,
        window,
        config,
        "recognise/raw/fixture.json",
        "a" * 64,
    )
    assert observation.status == "match"
    assert len(observation.native["matches"]) == 2
    assert observation.native["matches"][0]["offset_ms"] == 45_250
    assert observation.native["matches"][0]["frequencyskew_e6"] == 120
    assert observation.anchor and observation.anchor.reliable
    assert observation.anchor.ref_anchor_ms == 45_075
    assert observation.anchor.bias_applied_ms == 250

    unmeasured = ProviderConfigRecord.model_validate(_golden("provider_config"))
    provisional = response_to_observation(
        _fixture("response-match.json"),
        query,
        window,
        unmeasured,
        "recognise/raw/unmeasured.json",
        "a" * 64,
    )
    assert provisional.anchor and not provisional.anchor.reliable

    no_match = response_to_observation(
        _fixture("response-no-match.json"),
        query,
        window,
        config,
        "recognise/raw/no-match.json",
        "a" * 64,
    )
    assert no_match.status == "no_match"
    assert no_match.anchor is None


def test_cache_ttls_and_refresh_boundary(tmp_path: Path) -> None:
    raw = tmp_path / "raw.json"
    raw.write_text("{}", encoding="utf-8")
    assert _cache_valid(raw, "succeeded")
    assert _cache_valid(raw, "no_match")
    assert not _cache_valid(raw, "permanent_failure")
    old = time.time() - 181 * 24 * 60 * 60
    os.utime(raw, (old, old))
    assert not _cache_valid(raw, "succeeded")


def test_raw_unicode_payload_is_utf8_serializable() -> None:
    payload = {"artist": "İstanbul – 音楽", "title": "Café"}
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert json.loads(encoded.decode("utf-8")) == payload


def test_recognition_artifacts_cache_rerun_and_refresh(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, ...]:
        decoded, _, media_dir = _decoded(tmp_path, 21_000)
        windows = await generate_windows_async(decoded, media_dir)
        config, _ = load_provider_config(tmp_path)
        calls = 0

        async def handler(request: object) -> object:
            nonlocal calls
            calls += 1
            return __import__("httpx").Response(200, json=_fixture("response-match.json"))

        adapter = ShazamAdapter(
            config,
            limiter=TokenBucket(rate_per_minute=1_000_000),
            transport=__import__("httpx").MockTransport(handler),
        )
        first = await recognise_generation_zero(
            media_key="a" * 64,
            media_dir=media_dir,
            windows=windows,
            project_root=tmp_path,
            run_id="first",
            max_requests=10,
            adapter=adapter,
        )
        first_calls = calls
        first_query_bytes = read_bytes(first.queries_path)
        first_raw_path = next(first.queries_path.parent.joinpath("raw").glob("*.json"))
        first_raw_bytes = read_bytes(first_raw_path)
        # A historical provider-config generation may share jobs.sqlite. Its attempts must not
        # leak into or be leased by the active generation.
        async with AsyncJobStore(media_dir / "jobs.sqlite") as store:
            await store.ensure_budget("a" * 64, "shazam", max_requests=10)
            historical = await store.ensure_job("a" * 64, "c" * 40, "shazam")
        second = await recognise_generation_zero(
            media_key="a" * 64,
            media_dir=media_dir,
            windows=windows,
            project_root=tmp_path,
            run_id="second",
            max_requests=10,
            adapter=adapter,
        )
        async with AsyncJobStore(media_dir / "jobs.sqlite") as store:
            historical_after = await store.get_job(historical.id)
            assert historical_after and historical_after.state == "pending"
        second_calls = calls
        refreshed = await recognise_generation_zero(
            media_key="a" * 64,
            media_dir=media_dir,
            windows=windows,
            project_root=tmp_path,
            run_id="refresh",
            refresh=True,
            max_requests=10,
            adapter=adapter,
        )
        assert path_is_file(first.observations_path)
        assert path_is_file(first.raw_index_path)
        assert len(json.loads(read_text(first.raw_index_path))) == 2
        assert (
            len(
                {
                    first.queries_path.parent,
                    second.queries_path.parent,
                    refreshed.queries_path.parent,
                }
            )
            == 3
        )
        assert read_bytes(first.queries_path) == first_query_bytes
        assert read_bytes(first_raw_path) == first_raw_bytes
        assert first.observations[0].raw_response_ref != refreshed.observations[0].raw_response_ref
        return (
            first.physical_attempts,
            first.requests,
            second.cache_hits,
            second.requests,
            refreshed.physical_attempts,
            refreshed.requests,
            second_calls - first_calls,
        )

    assert asyncio.run(scenario()) == (2, 2, 2, 0, 2, 2, 0)


def test_duplicate_wavs_submit_once_and_fan_out_timed_observations(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int, int, list[tuple[int, int]], int]:
        decoded, _, media_dir = _decoded(tmp_path, 21_000)
        decoded.pcm_path.write_bytes(bytes(21_000 * 16 * 2))
        windows = await generate_windows_async(decoded, media_dir)
        config, _ = load_provider_config(tmp_path)
        calls = 0

        async def handler(request: object) -> object:
            nonlocal calls
            calls += 1
            return __import__("httpx").Response(200, json=_fixture("response-match.json"))

        result = await recognise_generation_zero(
            media_key="a" * 64,
            media_dir=media_dir,
            windows=windows,
            project_root=tmp_path,
            run_id="duplicate-wavs",
            max_requests=10,
            adapter=ShazamAdapter(
                config,
                limiter=TokenBucket(rate_per_minute=1_000_000),
                transport=__import__("httpx").MockTransport(handler),
            ),
        )
        return (
            calls,
            result.physical_attempts,
            len(result.queries),
            # Observations carry no ``start_ms`` and are therefore ordered by id; the property
            # under test is that one cached response fans out to both duplicate windows.
            sorted(item.mix_span_ms for item in result.observations),
            len(result.raw_index),
        )

    assert asyncio.run(scenario()) == (1, 1, 1, [(0, 12_000), (9_000, 21_000)], 1)


def _tone(path: Path) -> None:
    frames = bytearray()
    for index in range(16_000 * 12):
        value = round(10_000 * math.sin(2 * math.pi * (300 + index // 8000 * 23) * index / 16_000))
        frames.extend(int(value).to_bytes(2, "little", signed=True))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(frames)


def test_job_executor_owns_fake_server_retry(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setattr("id_detector.recognise.retry_delay", lambda *_: 0)  # type: ignore[attr-defined]

    async def scenario() -> tuple[int, int, int, str, int]:
        request_count = 0
        response_body = json.dumps(_fixture("response-no-match.json")).encode()

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal request_count
            headers = await reader.readuntil(b"\r\n\r\n")
            content_length = 0
            for line in headers.decode("latin-1").splitlines():
                if line.casefold().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1])
            if content_length:
                await reader.readexactly(content_length)
            request_count += 1
            status = b"500 Internal Server Error" if request_count == 1 else b"200 OK"
            body = b'{"error":"retry"}' if request_count == 1 else response_body
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        decoded, _, media_dir = _decoded(tmp_path, 12_000)
        windows = await generate_windows_async(decoded, media_dir)
        config, _ = load_provider_config(tmp_path)
        adapter = ShazamAdapter(
            config,
            limiter=TokenBucket(rate_per_minute=1_000_000),
            url_override=f"http://127.0.0.1:{port}/recognise",
        )
        try:
            result = await recognise_generation_zero(
                media_key="a" * 64,
                media_dir=media_dir,
                windows=windows,
                project_root=tmp_path,
                run_id="fake-server-retry",
                max_requests=5,
                adapter=adapter,
            )
            async with AsyncJobStore(media_dir / "jobs.sqlite") as store:
                final = await store.get_job_by_query(result.queries[0].id)
                assert final
            return (
                result.physical_attempts,
                request_count,
                final.physical_attempts,
                final.state,
                final.attempts,
            )
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario()) == (2, 2, 2, "no_match", 1)


def test_job_executor_enforces_retry_limit(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setattr("id_detector.recognise.retry_delay", lambda *_: 0)  # type: ignore[attr-defined]

    async def scenario() -> tuple[int, int, str, int]:
        decoded, _, media_dir = _decoded(tmp_path, 12_000)
        windows = await generate_windows_async(decoded, media_dir)
        config, _ = load_provider_config(tmp_path)
        calls = 0

        async def handler(request: object) -> object:
            nonlocal calls
            calls += 1
            return __import__("httpx").Response(500, json={"error": "retry"})

        result = await recognise_generation_zero(
            media_key="a" * 64,
            media_dir=media_dir,
            windows=windows,
            project_root=tmp_path,
            run_id="retry-limit",
            max_requests=MAX_RETRIES + 2,
            adapter=ShazamAdapter(
                config,
                limiter=TokenBucket(rate_per_minute=1_000_000),
                breaker=CircuitBreaker(open_seconds=0),
                transport=__import__("httpx").MockTransport(handler),
            ),
        )
        async with AsyncJobStore(media_dir / "jobs.sqlite") as store:
            final = await store.get_job_by_query(result.queries[0].id)
            assert final
        return calls, final.physical_attempts, final.state, final.attempts

    assert asyncio.run(scenario()) == (MAX_RETRIES + 1, MAX_RETRIES + 1, "permanent_failure", 1)
