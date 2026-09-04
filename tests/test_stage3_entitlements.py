from __future__ import annotations

import asyncio
import math
import wave
from array import array
from pathlib import Path

import pytest

from id_detector.providers.acrcloud import (
    MAX_UPLOAD_BYTES,
    ACRCloudAdapter,
    ACRCloudCredentials,
)
from id_detector.providers.acrcloud import (
    cost_usd_e2 as acrcloud_cost_usd_e2,
)
from id_detector.providers.audd import (
    AudDAdapter,
    AudDCredentials,
    AudDScanPolicy,
)
from id_detector.providers.audd import (
    cost_usd_e2 as audd_cost_usd_e2,
)
from id_detector.providers.base import AppConfig, ProviderUnavailable


def _probe_wav(path: Path) -> None:
    samples = array("h", (round(8_000 * math.sin(index / 13)) for index in range(16_000)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(samples.tobytes())


@pytest.mark.live
def test_audd_entitlement_smoke_skips_clearly_without_credentials(tmp_path: Path) -> None:
    try:
        credentials = AudDCredentials.from_env()
    except ProviderUnavailable as exc:
        pytest.skip(f"AudD not evaluated (no credentials): {exc}")
    probe = tmp_path / "owned-entitlement-probe.wav"
    _probe_wav(probe)
    adapter = AudDAdapter(credentials, AppConfig(True), True)
    attempts = 0

    async def run() -> dict:
        nonlocal attempts

        async def on_attempt() -> None:
            nonlocal attempts
            attempts += 1

        return await adapter.scan_file(probe, policy=AudDScanPolicy(limit=1), on_attempt=on_attempt)

    response = asyncio.run(run())
    assert attempts == 1
    assert response.get("status") == "success"
    assert isinstance(response.get("result"), list)
    assert audd_cost_usd_e2(1) == 1


@pytest.mark.live
def test_acrcloud_entitlement_smoke_skips_clearly_without_credentials(
    tmp_path: Path,
) -> None:
    try:
        credentials = ACRCloudCredentials.from_env()
    except ProviderUnavailable as exc:
        pytest.skip(f"ACRCloud not evaluated (no credentials): {exc}")
    probe = tmp_path / "owned-entitlement-probe.wav"
    _probe_wav(probe)
    assert probe.stat().st_size < MAX_UPLOAD_BYTES
    adapter = ACRCloudAdapter(credentials, AppConfig(True), True)
    attempts = 0

    async def run() -> tuple[dict, str, dict]:
        nonlocal attempts

        async def on_attempt() -> None:
            nonlocal attempts
            attempts += 1

        container = await adapter.container(on_attempt)
        remote = await adapter.submit("entitlement-probe-owned-audio", probe, on_attempt)
        response = await adapter.poll(remote, on_attempt)
        return container, remote, response

    container, remote, response = asyncio.run(run())
    assert attempts >= 3
    assert str(container.get("data", {}).get("id")) == credentials.container_id
    assert remote
    assert response.get("data")
    assert acrcloud_cost_usd_e2(1) == 1
