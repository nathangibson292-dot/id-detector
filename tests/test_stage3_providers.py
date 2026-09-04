from __future__ import annotations

import asyncio
import builtins
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha1
from pathlib import Path
from typing import Any

import httpx
import pytest

from id_detector.contracts import SENSITIVE_FIELD_NAMES
from id_detector.io import native_path
from id_detector.jobs import AsyncJobStore
from id_detector.providers import ProviderUnavailable
from id_detector.providers.acrcloud import (
    MAX_UPLOAD_BYTES,
    ACRCloudAdapter,
    ACRCloudCredentials,
    has_required_probe_fields,
    poll_delay_seconds,
)
from id_detector.providers.acrcloud import (
    billable_seconds as acr_billable_seconds,
)
from id_detector.providers.acrcloud import build_query as build_acr_query
from id_detector.providers.acrcloud import cost_usd_e2 as acr_cost_usd_e2
from id_detector.providers.acrcloud import execute_job as execute_acr_job
from id_detector.providers.acrcloud import parse_response as parse_acr_response
from id_detector.providers.audd import (
    AudDAdapter,
    AudDCredentials,
    AudDScanPolicy,
    billable_units,
)
from id_detector.providers.audd import build_query as build_audd_query
from id_detector.providers.audd import cost_usd_e2 as audd_cost_usd_e2
from id_detector.providers.audd import execute_job as execute_audd_job
from id_detector.providers.audd import parse_response as parse_audd_response
from id_detector.providers.base import (
    AmbiguousProviderOutcome,
    AppConfig,
    ProviderProtocolError,
    UploadPermissionError,
)
from id_detector.providers.panako import CAPABILITY, PanakoConfig, PanakoProvider, doctor_detail

ROOT = Path(__file__).resolve().parents[1]
MEDIA_KEY = "a" * 64
ASSET_HASH = "b" * 64


def _fixture(relative: str) -> dict[str, Any]:
    return json.loads((ROOT / "tests" / "fixtures" / relative).read_text(encoding="utf-8"))


def test_audd_authored_fixture_request_shape_and_anchor_conversion(tmp_path: Path) -> None:
    response = _fixture("audd/enterprise-authored-match.json")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen["body"] = body
        seen["content_type"] = request.headers["content-type"]
        return httpx.Response(200, json=response)

    path = tmp_path / "owned fixture.wav"
    path.write_bytes(b"fixture audio")
    attempts = 0

    async def run() -> dict[str, Any]:
        nonlocal attempts

        async def on_attempt() -> None:
            nonlocal attempts
            attempts += 1

        adapter = AudDAdapter(
            AudDCredentials("fixture-token"),
            AppConfig(allow_third_party_upload=True),
            True,
            transport=httpx.MockTransport(handler),
        )
        return await adapter.scan_file(
            path,
            policy=AudDScanPolicy(limit=5, skip=2, every=1, accurate_offsets=True),
            on_attempt=on_attempt,
        )

    assert asyncio.run(run()) == response
    assert attempts == 1
    body = seen["body"]
    assert seen["content_type"].startswith("multipart/form-data;")
    for expected in (
        b'name="file"',
        b'name="limit"',
        b"\r\n\r\n5\r\n",
        b'name="skip"',
        b"\r\n\r\n2\r\n",
        b'name="every"',
        b'name="accurate_offsets"',
    ):
        assert expected in body

    query = build_audd_query(
        media_key=MEDIA_KEY,
        asset_kind="original",
        asset_sha256=ASSET_HASH,
        scan_policy="fixture",
    )
    observations = parse_audd_response(
        response,
        query=query,
        media_key=MEDIA_KEY,
        duration_ms=60_000,
        raw_response_ref="recognise/raw/fixture.json",
    )
    assert len(observations) == 2
    match = next(item for item in observations if item.status == "match")
    assert match.mix_span_ms == (25_000, 35_000)
    assert match.support_ms == (24_000, 36_000)
    assert match.anchor is not None
    assert (match.anchor.mix_anchor_ms, match.anchor.ref_anchor_ms) == (24_000, 97_000)
    assert match.logical_trial_id == sha1(b"audd|1", usedforsecurity=False).hexdigest()
    assert match.transform is None
    assert match.score_raw == 97
    assert match.native["offset"] == "00:24"
    assert match.native["timecode"] == "01:37"
    assert match.native["start_offset"] == 1_000
    assert match.native["end_offset"] == 11_000


def test_audd_url_request_uses_enterprise_scanner_fields() -> None:
    response = _fixture("audd/enterprise-authored-no-match.json")
    seen_body = b""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_body
        seen_body = request.read()
        return httpx.Response(200, json=response)

    async def run() -> dict[str, Any]:
        adapter = AudDAdapter(
            AudDCredentials("fixture-token"),
            AppConfig(allow_third_party_upload=True),
            True,
            transport=httpx.MockTransport(handler),
        )

        async def on_attempt() -> None:
            return None

        return await adapter.scan_url(
            "https://fixture.invalid/owned.wav",
            policy=AudDScanPolicy(limit=4, skip=1, every=2, accurate_offsets=False),
            on_attempt=on_attempt,
        )

    assert asyncio.run(run()) == response
    assert b"url=https%3A%2F%2Ffixture.invalid%2Fowned.wav" in seen_body
    for field in (b"limit=4", b"skip=1", b"every=2", b"accurate_offsets=false"):
        assert field in seen_body


@pytest.mark.parametrize(
    "config,confirmation",
    [(AppConfig(False), True), (AppConfig(True), False), (AppConfig(False), False)],
)
def test_audd_refuses_upload_before_network(
    tmp_path: Path, config: AppConfig, confirmation: bool
) -> None:
    path = tmp_path / "audio.wav"
    path.write_bytes(b"fixture")
    attempts = 0

    async def run() -> None:
        nonlocal attempts

        async def on_attempt() -> None:
            nonlocal attempts
            attempts += 1

        adapter = AudDAdapter(AudDCredentials("fixture"), config, confirmation)
        with pytest.raises(UploadPermissionError, match="third-party upload refused"):
            await adapter.scan_file(path, policy=AudDScanPolicy(limit=1), on_attempt=on_attempt)

    asyncio.run(run())
    assert attempts == 0


def test_audd_lost_response_is_outcome_unknown_and_keeps_integer_reservation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audio.wav"
    path.write_bytes(b"fixture")
    query = build_audd_query(
        media_key=MEDIA_KEY,
        asset_kind="original",
        asset_sha256=ASSET_HASH,
        scan_policy="fixture",
    )

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("fixture disconnect")

    async def run() -> tuple[str, int, int, dict[str, int]]:
        async with AsyncJobStore(tmp_path / "jobs.sqlite") as store:
            units = billable_units(3_600_000)
            cents = audd_cost_usd_e2(units)
            await store.ensure_budget(MEDIA_KEY, "audd", max_requests=units, max_usd=cents)
            await store.ensure_job(MEDIA_KEY, query.id, "audd")
            job = await store.lease_next("owner", provider="audd")
            assert job is not None
            adapter = AudDAdapter(
                AudDCredentials("fixture"),
                AppConfig(True),
                True,
                transport=httpx.MockTransport(handler),
            )
            with pytest.raises(AmbiguousProviderOutcome, match="no reconciliation"):
                await execute_audd_job(
                    store=store,
                    job=job,
                    owner="owner",
                    adapter=adapter,
                    query=query,
                    media_key=MEDIA_KEY,
                    duration_ms=3_600_000,
                    asset_path=path,
                    raw_path=tmp_path / "raw.json",
                    raw_response_ref="raw.json",
                )
            final = await store.get_job(job.id)
            budget = await store.budget(MEDIA_KEY, "audd")
            assert final is not None and budget is not None
            return final.state, final.physical_attempts, final.reserved_usd, budget

    state, attempts, reserved, budget = asyncio.run(run())
    assert (state, attempts, reserved) == ("outcome_unknown", 1, 150)
    assert budget["reserved_requests"] == 300
    assert budget["reserved_usd"] == 150
    assert budget["used_requests"] == 0


def test_audd_budget_reconciles_returned_chunks_in_integer_cents(tmp_path: Path) -> None:
    response = _fixture("audd/enterprise-authored-match.json")
    path = tmp_path / "audio.wav"
    path.write_bytes(b"fixture")
    query = build_audd_query(
        media_key=MEDIA_KEY,
        asset_kind="original",
        asset_sha256=ASSET_HASH,
        scan_policy="fixture",
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    async def run() -> tuple[Any, dict[str, int]]:
        async with AsyncJobStore(tmp_path / "jobs.sqlite") as store:
            await store.ensure_budget(MEDIA_KEY, "audd", max_requests=5, max_usd=3)
            await store.ensure_job(MEDIA_KEY, query.id, "audd")
            job = await store.lease_next("owner", provider="audd")
            assert job is not None
            result = await execute_audd_job(
                store=store,
                job=job,
                owner="owner",
                adapter=AudDAdapter(
                    AudDCredentials("fixture"),
                    AppConfig(True),
                    True,
                    transport=httpx.MockTransport(handler),
                ),
                query=query,
                media_key=MEDIA_KEY,
                duration_ms=60_000,
                asset_path=path,
                raw_path=tmp_path / "raw.json",
                raw_response_ref="raw.json",
            )
            budget = await store.budget(MEDIA_KEY, "audd")
            assert budget is not None
            return result, budget

    result, budget = asyncio.run(run())
    assert (result.billable_units, result.usd_e2) == (2, 1)
    assert budget["reserved_requests"] == budget["reserved_usd"] == 0
    assert (budget["used_requests"], budget["used_usd"]) == (2, 1)


def test_audd_reservation_is_idempotent_after_crash_before_submission(tmp_path: Path) -> None:
    response = _fixture("audd/enterprise-authored-match.json")
    path = tmp_path / "audio.wav"
    path.write_bytes(b"fixture")
    query = build_audd_query(
        media_key=MEDIA_KEY,
        asset_kind="original",
        asset_sha256=ASSET_HASH,
        scan_policy="reservation-crash",
    )

    async def reserve_then_crash() -> None:
        async with AsyncJobStore(tmp_path / "jobs.sqlite", lease_seconds=0) as store:
            await store.ensure_budget(MEDIA_KEY, "audd", max_requests=5, max_usd=3)
            await store.ensure_job(MEDIA_KEY, query.id, "audd")
            job = await store.lease_next("crashed", provider="audd")
            assert job is not None
            await store.reserve_billing(job.id, units=5, usd=3)

    async def recover() -> tuple[int, int, int]:
        async with AsyncJobStore(tmp_path / "jobs.sqlite", lease_seconds=0) as store:
            job = await store.lease_next("recovered", provider="audd")
            assert job is not None
            await execute_audd_job(
                store=store,
                job=job,
                owner="recovered",
                adapter=AudDAdapter(
                    AudDCredentials("fixture"),
                    AppConfig(True),
                    True,
                    transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
                ),
                query=query,
                media_key=MEDIA_KEY,
                duration_ms=60_000,
                asset_path=path,
                raw_path=tmp_path / "raw.json",
                raw_response_ref="raw.json",
            )
            final = await store.get_job(job.id)
            budget = await store.budget(MEDIA_KEY, "audd")
            assert final is not None and budget is not None
            return (
                budget["reserved_requests"],
                budget["used_requests"],
                final.physical_attempts,
            )

    asyncio.run(reserve_then_crash())
    assert asyncio.run(recover()) == (0, 2, 1)


def test_audd_acknowledged_retry_preserves_unknown_charge_and_funds_retry(
    tmp_path: Path,
) -> None:
    response = _fixture("audd/enterprise-authored-match.json")
    path = tmp_path / "audio.wav"
    path.write_bytes(b"fixture")
    query = build_audd_query(
        media_key=MEDIA_KEY,
        asset_kind="original",
        asset_sha256=ASSET_HASH,
        scan_policy="manual-retry",
    )
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadError("lost first response")
        return httpx.Response(200, json=response)

    async def run() -> tuple[str, int, int, int, int, int]:
        async with AsyncJobStore(tmp_path / "jobs.sqlite") as store:
            await store.ensure_budget(MEDIA_KEY, "audd", max_requests=5, max_usd=3)
            original = await store.ensure_job(MEDIA_KEY, query.id, "audd")
            adapter = AudDAdapter(
                AudDCredentials("fixture"),
                AppConfig(True),
                True,
                transport=httpx.MockTransport(handler),
            )
            first = await store.lease_next("first", provider="audd")
            assert first is not None
            with pytest.raises(AmbiguousProviderOutcome):
                await execute_audd_job(
                    store=store,
                    job=first,
                    owner="first",
                    adapter=adapter,
                    query=query,
                    media_key=MEDIA_KEY,
                    duration_ms=60_000,
                    asset_path=path,
                    raw_path=tmp_path / "raw.json",
                    raw_response_ref="raw.json",
                )
            acknowledged = await store.acknowledge_retry(original.id)
            assert (acknowledged.actual_units, acknowledged.actual_usd) == (5, 3)
            second = await store.lease_next("second", provider="audd")
            assert second is not None
            await execute_audd_job(
                store=store,
                job=second,
                owner="second",
                adapter=adapter,
                query=query,
                media_key=MEDIA_KEY,
                duration_ms=60_000,
                asset_path=path,
                raw_path=tmp_path / "raw.json",
                raw_response_ref="raw.json",
            )
            final = await store.get_job(original.id)
            budget = await store.budget(MEDIA_KEY, "audd")
            assert final is not None and budget is not None
            return (
                final.state,
                final.actual_units,
                final.actual_usd,
                budget["max_requests"],
                budget["used_requests"],
                budget["used_usd"],
            )

    assert asyncio.run(run()) == ("succeeded", 7, 4, 10, 7, 4)
    assert calls == 2


def test_scanner_executor_heartbeats_during_long_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("id_detector.providers.audd.HEARTBEAT_SECONDS", 0.01)
    path = tmp_path / "audio.wav"
    path.write_bytes(b"fixture")
    query = build_audd_query(
        media_key=MEDIA_KEY,
        asset_kind="original",
        asset_sha256=ASSET_HASH,
        scan_policy="heartbeat",
    )

    async def run() -> tuple[str | None, str | None]:
        started = asyncio.Event()
        release = asyncio.Event()

        @dataclass
        class SlowAdapter:
            app_config: AppConfig = AppConfig(True)
            cli_confirmation: bool = True

            async def scan_file(self, _: Path, *, policy: Any, on_attempt: Any) -> dict[str, Any]:
                del policy
                await on_attempt()
                started.set()
                await release.wait()
                return _fixture("audd/enterprise-authored-no-match.json")

        async with AsyncJobStore(tmp_path / "jobs.sqlite") as store:
            await store.ensure_budget(MEDIA_KEY, "audd", max_requests=2, max_usd=1)
            await store.ensure_job(MEDIA_KEY, query.id, "audd")
            job = await store.lease_next("owner", provider="audd")
            assert job is not None
            initial = job.heartbeat_at
            execution = asyncio.create_task(
                execute_audd_job(
                    store=store,
                    job=job,
                    owner="owner",
                    adapter=SlowAdapter(),  # type: ignore[arg-type]
                    query=query,
                    media_key=MEDIA_KEY,
                    duration_ms=24_000,
                    asset_path=path,
                    raw_path=tmp_path / "raw.json",
                    raw_response_ref="raw.json",
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.sleep(0.05)
            current = await store.get_job(job.id)
            assert current is not None
            release.set()
            await execution
            return initial, current.heartbeat_at

    initial, heartbeat = asyncio.run(run())
    assert initial is not None and heartbeat is not None
    assert heartbeat > initial


def test_acrcloud_authored_fixture_anchor_ids_custom_bucket_and_required_fields() -> None:
    response = _fixture("acrcloud/filescan-authored-ready.json")
    query = build_acr_query(
        media_key=MEDIA_KEY,
        asset_kind="original",
        asset_sha256=ASSET_HASH,
        scan_policy="fixture",
    )
    observations = parse_acr_response(
        response,
        query=query,
        media_key=MEDIA_KEY,
        duration_ms=60_000,
        raw_response_ref="recognise/raw/fixture.json",
    )
    assert len(observations) == 2
    music = next(item for item in observations if item.native["result_type"] == "music")
    custom = next(item for item in observations if item.native["result_type"] == "custom_files")
    assert music.mix_span_ms == (25_000, 33_000)
    assert music.anchor is not None
    assert (music.anchor.mix_anchor_ms, music.anchor.ref_anchor_ms) == (25_000, 41_000)
    assert music.anchor.reliable
    assert music.provider_ids == {
        "acr": "fixture-music-acr",
        "isrc": "FIXTUREISRC2",
        "deezer": "fixture-deezer",
        "mb_recording": "fixture-recording",
        "spotify": "fixture-spotify",
    }
    assert custom.provider_ids["audio_id"] == "fixture-audio"
    assert custom.raw_label.title == "Own Bucket Insertion"
    assert music.transform is custom.transform is None
    assert has_required_probe_fields(response)


@pytest.mark.parametrize(
    "config,confirmation",
    [(AppConfig(False), True), (AppConfig(True), False)],
)
def test_acrcloud_refuses_upload_before_network(
    tmp_path: Path, config: AppConfig, confirmation: bool
) -> None:
    path = tmp_path / "audio.wav"
    path.write_bytes(b"fixture")
    attempts = 0

    async def run() -> None:
        nonlocal attempts

        async def on_attempt() -> None:
            nonlocal attempts
            attempts += 1

        adapter = ACRCloudAdapter(
            ACRCloudCredentials("fixture.invalid", "key", "secret", "container"),
            config,
            confirmation,
        )
        with pytest.raises(UploadPermissionError):
            await adapter.submit("c" * 64, path, on_attempt)

    asyncio.run(run())
    assert attempts == 0


@dataclass
class _DurableFakeACR:
    response: dict[str, Any]
    fail_upload_once: bool = False
    fail_poll_once: bool = False
    app_config: AppConfig = AppConfig(True)
    cli_confirmation: bool = True
    submissions: int = 0
    accepted_remote: str | None = None

    async def reconcile(self, _: str, on_attempt: Any) -> str | None:
        await on_attempt()
        return self.accepted_remote

    async def submit(self, _: str, __: Path, on_attempt: Any) -> str:
        await on_attempt()
        self.submissions += 1
        self.accepted_remote = "remote-fixture"
        if self.fail_upload_once:
            self.fail_upload_once = False
            raise AmbiguousProviderOutcome("upload response lost")
        return self.accepted_remote

    async def poll(
        self, _: str, on_attempt: Any, *, submitted_at: datetime | None = None
    ) -> dict[str, Any]:
        assert submitted_at is not None
        await on_attempt()
        if self.fail_poll_once:
            self.fail_poll_once = False
            raise AmbiguousProviderOutcome("poll response lost")
        return self.response


@pytest.mark.parametrize(
    "failure_point",
    [
        "before_network",
        "during_upload",
        "after_acceptance",
        "after_remote_id_persistence",
        "during_polling",
    ],
)
def test_acrcloud_five_failure_points_recover_with_exactly_one_submission(
    tmp_path: Path, failure_point: str
) -> None:
    path = tmp_path / "audio.wav"
    path.write_bytes(b"fixture")
    response = _fixture("acrcloud/filescan-authored-ready.json")
    query = build_acr_query(
        media_key=MEDIA_KEY,
        asset_kind="original",
        asset_sha256=ASSET_HASH,
        scan_policy="fixture",
    )
    adapter = _DurableFakeACR(
        response,
        fail_upload_once=failure_point == "during_upload",
        fail_poll_once=failure_point == "during_polling",
    )
    hook_fired = False

    def hook(point: str) -> None:
        nonlocal hook_fired
        if point == failure_point and point != "during_upload" and not hook_fired:
            hook_fired = True
            raise RuntimeError(f"injected {point}")

    async def first_attempt() -> None:
        async with AsyncJobStore(tmp_path / "jobs.sqlite", lease_seconds=0) as store:
            units = acr_billable_seconds(60_000)
            await store.ensure_budget(
                MEDIA_KEY,
                "acrcloud",
                max_requests=units,
                max_usd=acr_cost_usd_e2(units),
            )
            await store.ensure_job(MEDIA_KEY, query.id, "acrcloud")
            job = await store.lease_next("owner-one", provider="acrcloud")
            assert job is not None
            with pytest.raises((RuntimeError, AmbiguousProviderOutcome)):
                await execute_acr_job(
                    store=store,
                    job=job,
                    owner="owner-one",
                    adapter=adapter,  # type: ignore[arg-type]
                    query=query,
                    media_key=MEDIA_KEY,
                    duration_ms=60_000,
                    asset_path=path,
                    raw_path=tmp_path / "raw.json",
                    raw_response_ref="raw.json",
                    failure_hook=hook,
                )

    raw_path = tmp_path / "raw" / f"{query.cache_key}.json"

    async def recover() -> tuple[str, int, int, dict[str, int]]:
        async with AsyncJobStore(tmp_path / "jobs.sqlite", lease_seconds=0) as store:
            current = await store.get_job_by_query(query.id)
            assert current is not None
            if current.state == "outcome_unknown":
                await store.acknowledge_retry(current.id)
            job = await store.lease_next("owner-two", provider="acrcloud")
            assert job is not None
            await execute_acr_job(
                store=store,
                job=job,
                owner="owner-two",
                adapter=adapter,  # type: ignore[arg-type]
                query=query,
                media_key=MEDIA_KEY,
                duration_ms=60_000,
                asset_path=path,
                raw_path=raw_path,
                raw_response_ref=f"raw/{query.cache_key}.json",
            )
            final = await store.get_job(job.id)
            budget = await store.budget(MEDIA_KEY, "acrcloud")
            assert final is not None and budget is not None
            return final.state, final.physical_attempts, final.actual_units, budget

    asyncio.run(first_attempt())
    state, physical_attempts, actual_units, budget = asyncio.run(recover())
    assert adapter.submissions == 1
    assert state == "succeeded"
    assert raw_path.name == f"{query.cache_key}.json"
    assert raw_path.is_file()
    expected_attempts = {
        "before_network": 3,
        "during_upload": 4,
        "after_acceptance": 4,
        "after_remote_id_persistence": 3,
        "during_polling": 4,
    }[failure_point]
    assert physical_attempts == expected_attempts
    assert budget["reserved_requests"] == budget["reserved_usd"] == 0
    expected_exposure = (
        120
        if failure_point
        in {
            "before_network",
            "during_upload",
            "after_acceptance",
        }
        else 60
    )
    assert actual_units == budget["used_requests"] == expected_exposure
    assert budget["used_usd"] == (6 if expected_exposure == 120 else 3)


def test_acrcloud_http_reconciliation_adopts_existing_name_without_upload() -> None:
    cache_key = "c" * 64
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "remote-existing", "name": cache_key, "state": 0},
                    {"id": "remote-other", "name": "other", "state": 0},
                ]
            },
        )

    async def run() -> str | None:
        adapter = ACRCloudAdapter(
            ACRCloudCredentials("fixture.invalid", "key", "secret", "container"),
            AppConfig(True),
            True,
            transport=httpx.MockTransport(handler),
        )

        async def attempt() -> None:
            return None

        return await adapter.reconcile(cache_key, attempt)

    assert asyncio.run(run()) == "remote-existing"
    assert requests == [("GET", "/api/fs-containers/container/files")]


def test_acrcloud_upload_name_bearer_auth_and_capped_poll_backoff(tmp_path: Path) -> None:
    cache_key = "c" * 64
    path = tmp_path / "owned.wav"
    path.write_bytes(b"fixture")
    processing = _fixture("acrcloud/filescan-authored-processing.json")
    ready = _fixture("acrcloud/filescan-authored-ready.json")
    poll_responses = [processing] * 5 + [ready]
    seen_upload = b""
    seen_headers: httpx.Headers | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_headers, seen_upload
        if request.method == "POST":
            seen_headers = request.headers
            seen_upload = request.read()
            return httpx.Response(200, json={"data": {"id": "remote-fixture"}})
        return httpx.Response(200, json=poll_responses.pop(0))

    delays: list[float] = []
    attempts = 0

    async def run() -> tuple[str, dict[str, Any]]:
        nonlocal attempts

        async def on_attempt() -> None:
            nonlocal attempts
            attempts += 1

        async def sleep(delay: float) -> None:
            delays.append(delay)

        adapter = ACRCloudAdapter(
            ACRCloudCredentials("fixture.invalid", "fixture-bearer", "fixture-secret", "box"),
            AppConfig(True),
            True,
            transport=httpx.MockTransport(handler),
            sleep=sleep,
        )
        remote_ref = await adapter.submit(cache_key, path, on_attempt)
        return remote_ref, await adapter.poll(remote_ref, on_attempt)

    remote_ref, result = asyncio.run(run())
    assert remote_ref == "remote-fixture"
    assert result == ready
    assert attempts == 7
    assert delays == [30, 60, 120, 240, 300]
    assert seen_headers is not None
    assert seen_headers["authorization"] == "Bearer fixture-bearer"
    assert "fixture-secret" not in str(seen_headers)
    assert cache_key.encode() in seen_upload
    assert b'name="name"' in seen_upload
    assert b'name="file"' in seen_upload


def test_provider_http_error_bodies_never_expose_supported_secret_fields(tmp_path: Path) -> None:
    secret_body = {name: f"fixture-secret-{name}" for name in SENSITIVE_FIELD_NAMES}
    path = tmp_path / "owned.wav"
    path.write_bytes(b"fixture")

    async def run() -> tuple[str, str]:
        async def attempt() -> None:
            return None

        audd = AudDAdapter(
            AudDCredentials("fixture-token"),
            AppConfig(True),
            True,
            transport=httpx.MockTransport(lambda _: httpx.Response(403, json=secret_body)),
        )
        with pytest.raises(ProviderProtocolError) as audd_error:
            await audd.scan_file(path, policy=AudDScanPolicy(limit=1), on_attempt=attempt)
        acr = ACRCloudAdapter(
            ACRCloudCredentials("fixture.invalid", "fixture-key", "fixture-secret", "box"),
            AppConfig(True),
            True,
            transport=httpx.MockTransport(lambda _: httpx.Response(403, json=secret_body)),
        )
        with pytest.raises(ProviderProtocolError) as acr_error:
            await acr.container(attempt)
        return str(audd_error.value), str(acr_error.value)

    messages = asyncio.run(run())
    assert messages == ("AudD HTTP 403", "ACRCloud HTTP 403")
    for value in secret_body.values():
        assert all(value not in message for message in messages)


def test_audd_known_http_failure_is_terminal_not_outcome_unknown(tmp_path: Path) -> None:
    path = tmp_path / "audio.wav"
    path.write_bytes(b"fixture")
    query = build_audd_query(
        media_key=MEDIA_KEY,
        asset_kind="original",
        asset_sha256=ASSET_HASH,
        scan_policy="known-http-failure",
    )

    async def run() -> tuple[str, int, int]:
        async with AsyncJobStore(tmp_path / "jobs.sqlite") as store:
            await store.ensure_budget(MEDIA_KEY, "audd", max_requests=5, max_usd=3)
            await store.ensure_job(MEDIA_KEY, query.id, "audd")
            job = await store.lease_next("owner", provider="audd")
            assert job is not None
            with pytest.raises(ProviderProtocolError, match="AudD HTTP 422"):
                await execute_audd_job(
                    store=store,
                    job=job,
                    owner="owner",
                    adapter=AudDAdapter(
                        AudDCredentials("fixture"),
                        AppConfig(True),
                        True,
                        transport=httpx.MockTransport(
                            lambda _: httpx.Response(422, text="api_token=must-not-appear")
                        ),
                    ),
                    query=query,
                    media_key=MEDIA_KEY,
                    duration_ms=60_000,
                    asset_path=path,
                    raw_path=tmp_path / "raw.json",
                    raw_response_ref="raw.json",
                )
            final = await store.get_job(job.id)
            budget = await store.budget(MEDIA_KEY, "audd")
            assert final is not None and budget is not None
            return final.state, budget["reserved_requests"], budget["used_requests"]

    assert asyncio.run(run()) == ("permanent_failure", 0, 5)


@pytest.mark.parametrize("failure", ["http", "malformed", "state--2", "state--3"])
def test_acrcloud_known_failures_are_terminal(tmp_path: Path, failure: str) -> None:
    path = tmp_path / "audio.wav"
    path.write_bytes(b"fixture")
    query = build_acr_query(
        media_key=MEDIA_KEY,
        asset_kind="original",
        asset_sha256=ASSET_HASH,
        scan_policy=f"known-{failure}",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "http":
            return httpx.Response(503, text="access_secret=must-not-appear")
        if request.method == "GET" and request.url.path.endswith("/files"):
            if failure == "malformed":
                return httpx.Response(200, json={"data": {"not": "a list"}})
            return httpx.Response(200, json={"data": []})
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"id": "remote-known-failure"}})
        return httpx.Response(
            200,
            json={"data": {"state": int(failure.removeprefix("state-")), "results": {}}},
        )

    async def run() -> tuple[str, int, int]:
        async with AsyncJobStore(tmp_path / "jobs.sqlite") as store:
            await store.ensure_budget(MEDIA_KEY, "acrcloud", max_requests=60, max_usd=3)
            await store.ensure_job(MEDIA_KEY, query.id, "acrcloud")
            job = await store.lease_next("owner", provider="acrcloud")
            assert job is not None
            with pytest.raises(ProviderProtocolError):
                await execute_acr_job(
                    store=store,
                    job=job,
                    owner="owner",
                    adapter=ACRCloudAdapter(
                        ACRCloudCredentials("fixture.invalid", "key", "secret", "box"),
                        AppConfig(True),
                        True,
                        transport=httpx.MockTransport(handler),
                    ),
                    query=query,
                    media_key=MEDIA_KEY,
                    duration_ms=60_000,
                    asset_path=path,
                    raw_path=tmp_path / "raw.json",
                    raw_response_ref="raw.json",
                )
            final = await store.get_job(job.id)
            budget = await store.budget(MEDIA_KEY, "acrcloud")
            assert final is not None and budget is not None
            return final.state, budget["reserved_requests"], budget["used_requests"]

    assert asyncio.run(run()) == ("permanent_failure", 0, 60)


def test_acrcloud_upload_size_boundary_is_checked_before_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audio.wav"
    path.write_bytes(b"fixture")
    attempts = 0

    async def attempt() -> None:
        nonlocal attempts
        attempts += 1

    adapter = ACRCloudAdapter(
        ACRCloudCredentials("fixture.invalid", "key", "secret", "box"),
        AppConfig(True),
        True,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": {"id": "remote-boundary"}})
        ),
    )
    monkeypatch.setattr("id_detector.providers.acrcloud.path_size", lambda _: MAX_UPLOAD_BYTES - 1)
    assert asyncio.run(adapter.submit("c" * 64, path, attempt)) == "remote-boundary"
    monkeypatch.setattr("id_detector.providers.acrcloud.path_size", lambda _: MAX_UPLOAD_BYTES)
    with pytest.raises(ValueError, match="smaller than 500 MB"):
        asyncio.run(adapter.submit("c" * 64, path, attempt))
    assert attempts == 1


def test_native_path_preflight_accepts_windows_long_paths(tmp_path: Path) -> None:
    long_dir = tmp_path
    while len(str(long_dir / "owned.wav")) <= 280:
        long_dir /= "long-path-segment"
    os.makedirs(native_path(long_dir), exist_ok=True)
    path = long_dir / "owned.wav"
    with open(native_path(path), "wb") as handle:
        handle.write(b"fixture")
    attempts = 0

    async def run() -> None:
        nonlocal attempts

        async def attempt() -> None:
            nonlocal attempts
            attempts += 1

        audd = AudDAdapter(
            AudDCredentials("fixture"),
            AppConfig(True),
            True,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json=_fixture("audd/enterprise-authored-no-match.json")
                )
            ),
        )
        await audd.scan_file(path, policy=AudDScanPolicy(limit=2), on_attempt=attempt)
        acr = ACRCloudAdapter(
            ACRCloudCredentials("fixture.invalid", "key", "secret", "box"),
            AppConfig(True),
            True,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"data": {"id": "remote-long-path"}})
            ),
        )
        assert await acr.submit("c" * 64, path, attempt) == "remote-long-path"

    asyncio.run(run())
    assert attempts == 2


@pytest.mark.parametrize("provider", ["audd", "acrcloud"])
def test_file_open_failure_does_not_count_physical_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    path = tmp_path / "locked.wav"
    path.write_bytes(b"fixture")
    actual_open = builtins.open

    def blocked_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fspath(file) == native_path(path):
            raise PermissionError("fixture sharing violation")
        return actual_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", blocked_open)
    attempts = 0

    async def run() -> None:
        nonlocal attempts

        async def attempt() -> None:
            nonlocal attempts
            attempts += 1

        if provider == "audd":
            adapter = AudDAdapter(AudDCredentials("fixture"), AppConfig(True), True)
            await adapter.scan_file(path, policy=AudDScanPolicy(limit=1), on_attempt=attempt)
        else:
            adapter = ACRCloudAdapter(
                ACRCloudCredentials("fixture.invalid", "key", "secret", "box"),
                AppConfig(True),
                True,
            )
            await adapter.submit("c" * 64, path, attempt)

    with pytest.raises(PermissionError, match="sharing violation"):
        asyncio.run(run())
    assert attempts == 0


def test_acrcloud_persistent_deadline_terminalizes_without_another_poll(tmp_path: Path) -> None:
    path = tmp_path / "audio.wav"
    path.write_bytes(b"fixture")
    database = tmp_path / "jobs.sqlite"
    query = build_acr_query(
        media_key=MEDIA_KEY,
        asset_kind="original",
        asset_sha256=ASSET_HASH,
        scan_policy="persistent-deadline",
    )
    old = datetime(2026, 1, 1, tzinfo=UTC)
    old_text = old.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    async def setup() -> str:
        async with AsyncJobStore(database) as store:
            await store.ensure_budget(MEDIA_KEY, "acrcloud", max_requests=60, max_usd=3)
            original = await store.ensure_job(MEDIA_KEY, query.id, "acrcloud")
            job = await store.lease_next("first", provider="acrcloud")
            assert job is not None
            await store.reserve_billing(job.id, units=60, usd=3)
            await store.submission_started(job.id, "first")
            await store.submitted(job.id, "remote-persisted")
            await store.release_owner("first")
            return original.id

    job_id = asyncio.run(setup())
    with sqlite3.connect(native_path(database)) as connection:
        connection.execute("UPDATE jobs SET submitted_at=? WHERE id=?", (old_text, job_id))
        connection.commit()
    network_attempts = 0

    def unexpected_network(_: httpx.Request) -> httpx.Response:
        nonlocal network_attempts
        network_attempts += 1
        return httpx.Response(500)

    async def recover() -> tuple[str, str | None, int, int]:
        async with AsyncJobStore(database) as store:
            job = await store.lease_next("second", provider="acrcloud")
            assert job is not None
            adapter = ACRCloudAdapter(
                ACRCloudCredentials("fixture.invalid", "key", "secret", "box"),
                AppConfig(True),
                True,
                transport=httpx.MockTransport(unexpected_network),
                now=lambda: old + timedelta(hours=48, seconds=1),
            )
            with pytest.raises(TimeoutError, match="48 hours"):
                await execute_acr_job(
                    store=store,
                    job=job,
                    owner="second",
                    adapter=adapter,
                    query=query,
                    media_key=MEDIA_KEY,
                    duration_ms=60_000,
                    asset_path=path,
                    raw_path=tmp_path / "raw.json",
                    raw_response_ref="raw.json",
                )
            final = await store.get_job(job.id)
            budget = await store.budget(MEDIA_KEY, "acrcloud")
            assert final is not None and budget is not None
            return final.state, final.submitted_at, final.physical_attempts, budget["used_requests"]

    assert asyncio.run(recover()) == ("permanent_failure", old_text, 0, 60)
    assert network_attempts == 0
    assert poll_delay_seconds(450) == 300


def test_panako_is_declared_disabled_and_every_operation_is_clear(tmp_path: Path) -> None:
    provider = PanakoProvider(PanakoConfig(index_path=tmp_path / "index"))
    assert CAPABILITY.capability == "local_index_query"
    assert not CAPABILITY.available
    for method in (provider.create_index, provider.query, provider.recognise, provider.close):
        with pytest.raises(ProviderUnavailable, match="^JDK not found$"):
            method()
    status, detail = doctor_detail()
    assert status == "WARN"
    assert "Panako" in detail
