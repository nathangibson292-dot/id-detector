"""The live paid file-scanner stage (:mod:`id_detector.scan`).

These exercise the whole store/cache/persist/parse chain with the *real* provider adapter over an
``httpx.MockTransport`` — so everything but the actual network call to AudD/ACRCloud is covered,
and no credentials or third-party upload ever happen.
"""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path

import httpx

from id_detector.fuse.scanners import validate_scanner_observations
from id_detector.io import sha256_file
from id_detector.providers.audd import AudDAdapter, AudDCredentials
from id_detector.providers.base import AppConfig
from id_detector.scan import PaidScanResult, run_paid_scanners

ROOT = Path(__file__).resolve().parents[1]


def _owned_wav(path: Path, seconds: int = 60) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 16_000 * seconds)


def _prepared(tmp_path: Path) -> dict[str, object]:
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True)
    asset = media_dir / "ingest" / "original.wav"
    asset.parent.mkdir(parents=True)
    _owned_wav(asset)
    source = media_dir / "ingest" / "source.json"
    source.write_text("{}", encoding="utf-8")
    return {
        "media_key": sha256_file(asset),
        "media_dir": media_dir,
        "asset_path": asset,
        "asset_sha256": sha256_file(asset),
        "asset_kind": "original",
        "duration_ms": 60_000,
        "source_path": source,
    }


def _audd_adapter(fixture: str) -> AudDAdapter:
    response = json.loads((ROOT / "tests/fixtures/audd" / fixture).read_text(encoding="utf-8"))

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    return AudDAdapter(
        AudDCredentials("fixture"),
        AppConfig(True),
        True,
        transport=httpx.MockTransport(handler),
    )


def test_no_paid_engine_enabled_is_a_pure_no_op(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    result = asyncio.run(
        run_paid_scanners(
            **prepared,
            app_config=AppConfig(True),
            enabled_engines=("shazam",),  # only the free engine
            cli_confirmation=True,
        )
    )
    assert result == PaidScanResult()
    assert not result.ran
    # Nothing was written under the media dir's invocation tree.
    assert not (prepared["media_dir"] / "recognise" / "invocations").exists()


def test_enabled_but_unconsented_is_skipped_with_a_reason_not_an_error(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    logged: list[str] = []
    result = asyncio.run(
        run_paid_scanners(
            **prepared,
            app_config=AppConfig(False),  # allow_third_party_upload = False
            enabled_engines=("audd",),
            cli_confirmation=False,
            log=logged.append,
        )
    )
    assert not result.ran
    assert result.observations == ()
    assert [name for name, _ in result.skipped] == ["audd"]
    reason = result.skipped[0][1]
    assert "allow_third_party_upload" in reason and "permission" in reason
    assert any("audd skipped" in line for line in logged)


def test_audd_scan_yields_fusable_positioned_observations(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    result = asyncio.run(
        run_paid_scanners(
            **prepared,
            app_config=AppConfig(True),
            enabled_engines=("audd",),
            cli_confirmation=True,
            adapters={"audd": _audd_adapter("enterprise-authored-match.json")},
        )
    )
    assert result.engines_run == ("audd",)
    assert result.observations, "a matching scan must yield at least one observation"
    assert any(item.status == "match" for item in result.observations)
    # Every observation is a file_scanner record the fuser will accept (transform null, known
    # anchor conversion) — the same contract the generation loop relies on.
    for item in result.observations:
        assert item.capability == "file_scanner"
        assert item.transform is None
    validate_scanner_observations(result.observations)
    # The observation artefact was persisted under a distinct live- invocation directory.
    assert len(result.observation_paths) == 1
    path = result.observation_paths[0]
    assert path.is_file() and "live-audd-v1" in path.as_posix()


def test_a_second_run_reconstructs_from_cache_without_the_adapter(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)

    def go(adapters: dict[str, object] | None) -> PaidScanResult:
        return asyncio.run(
            run_paid_scanners(
                **prepared,
                app_config=AppConfig(True),
                enabled_engines=("audd",),
                cli_confirmation=True,
                adapters=adapters,
            )
        )

    first = go({"audd": _audd_adapter("enterprise-authored-match.json")})
    # No adapter the second time: a valid cache must reconstruct the same observations, proving the
    # scan is not re-billed on repeat analyses of the same mix.
    second = go(None)
    assert second.engines_run == ("audd",)
    assert [item.model_dump() for item in second.observations] == [
        item.model_dump() for item in first.observations
    ]


def test_analyse_rejects_an_unknown_engine_before_running() -> None:
    from typer.testing import CliRunner

    from id_detector.cli import app

    result = CliRunner().invoke(
        app, ["analyse", "https://soundcloud.com/example/mix", "--engine", "bogus"]
    )
    assert result.exit_code == 2
    assert "unknown --engine" in result.output
