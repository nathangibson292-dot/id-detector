from __future__ import annotations

import asyncio
import copy
import json
import os
import wave
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import id_detector.benchmark.shortlist as shortlist_module
import id_detector.cli as cli_module
from id_detector.benchmark.shortlist import EngineRun, ShortlistResult
from id_detector.contracts import BenchmarkCost, GroundTruthRecord, ShortlistReportRecord
from id_detector.providers.audd import AudDAdapter, AudDCredentials
from id_detector.providers.base import AppConfig, ProviderProtocolError
from id_detector.recognise import (
    NO_MATCH_MAX_AGE_SECONDS,
    POSITIVE_MAX_AGE_SECONDS,
    cache_valid,
)

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "data" / "corpus" / "controlled-synth-1" / "shortlist.json"


def test_committed_shortlist_is_honest_complete_and_no_cascade() -> None:
    report = ShortlistReportRecord.model_validate_json(REPORT.read_text(encoding="utf-8"))
    engines = {item.provider: item for item in report.engines}
    assert set(engines) == {"local_fixture", "shazam", "audd", "acrcloud", "panako"}
    assert engines["local_fixture"].set_count == engines["shazam"].set_count == 25
    assert engines["local_fixture"].metrics is not None
    assert engines["local_fixture"].metrics.identification_work.recall_e4 == 10_000
    assert engines["shazam"].status == "evaluated"
    assert engines["shazam"].cost.physical_attempts == engines["shazam"].observation_count
    assert engines["shazam"].cost.physical_attempts > 0
    assert engines["audd"].status == "not_evaluated (no credentials)"
    assert engines["acrcloud"].status == "not_evaluated (no credentials)"
    assert engines["audd"].metrics is engines["acrcloud"].metrics is None
    assert "pending owner's JDK decision" in engines["panako"].status
    assert report.reference_pool_status == "excluded_from_v1_pending_owner_jdk_decision"
    pair = report.pairwise_agreement[0]
    assert {pair.provider_a, pair.provider_b} == {"local_fixture", "shazam"}
    assert pair.n_sets == 25
    assert report.union_coverage_e4 == report.oracle_coverage_e4 == 10_000


def test_shortlist_cli_contract_uses_requested_corpus_and_output(
    tmp_path: Path, monkeypatch
) -> None:
    committed = ShortlistReportRecord.model_validate_json(REPORT.read_text(encoding="utf-8"))
    captured: dict[str, object] = {}

    async def fake_run_shortlist(**kwargs: object) -> ShortlistResult:
        captured.update(kwargs)
        return ShortlistResult(committed, ())

    monkeypatch.setattr(cli_module, "run_shortlist", fake_run_shortlist)
    destination = tmp_path / "shortlist.json"
    result = CliRunner().invoke(
        cli_module.app,
        [
            "benchmark",
            "shortlist",
            "--corpus",
            "controlled-synth-1",
            "--out",
            str(destination),
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["corpus_version"] == "controlled-synth-1"
    assert captured["out_path"] == destination
    assert captured["cli_confirmation"] is False
    assert captured["refresh"] is False
    assert "audd=not_evaluated (no credentials)" in result.output


def test_shortlist_cli_passes_refresh_and_redacts_failures(tmp_path: Path, monkeypatch) -> None:
    supported_values = [
        "secret-client",
        "secret-oauth",
        "secret-api-key",
        "secret-api-token",
        "secret-access-key",
        "secret-access-secret",
        "secret-client-secret",
        "secret-authorization",
        "secret-cookie",
    ]
    message = " ".join(
        f"{name}={value}"
        for name, value in zip(
            (
                "client_id",
                "oauth_token",
                "api_key",
                "api_token",
                "access_key",
                "access_secret",
                "client_secret",
                "authorization",
                "cookie",
            ),
            supported_values,
            strict=True,
        )
    )

    async def failing_shortlist(**kwargs: object) -> ShortlistResult:
        assert kwargs["refresh"] is True
        raise ValueError(message)

    monkeypatch.setattr(cli_module, "run_shortlist", failing_shortlist)
    result = CliRunner().invoke(
        cli_module.app,
        [
            "benchmark",
            "shortlist",
            "--corpus",
            "controlled-synth-1",
            "--out",
            str(tmp_path / "out.json"),
            "--refresh",
        ],
    )
    assert result.exit_code == 1
    assert "[REDACTED]" in result.output
    assert all(value not in result.output for value in supported_values)


def test_shortlist_executes_every_eligible_engine_without_cascade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed = ShortlistReportRecord.model_validate_json(REPORT.read_text(encoding="utf-8"))
    metric = next(item.metrics for item in committed.engines if item.metrics is not None)
    assert metric is not None
    calls: list[str] = []

    def runner(name: str):
        async def run(**kwargs: object) -> EngineRun:
            calls.append(name)
            truths = kwargs["truths"]
            predictions = tuple(
                {"set_id": truth.set_id, "identities": {}, "episodes": []}  # type: ignore[attr-defined]
                for truth in truths  # type: ignore[union-attr]
            )
            return EngineRun(
                name,
                f"{name}-v1.json",
                "clip_recognizer" if name in {"local_fixture", "shazam"} else "file_scanner",
                predictions,
                metric,
                BenchmarkCost(
                    requests=0,
                    physical_attempts=0,
                    billable_seconds=0,
                    usd_e2=0,
                    wall_ms=0,
                ),
                0,
                0,
            )

        return run

    for name in (
        "AUDD_API_TOKEN",
        "ACRCLOUD_HOST",
        "ACRCLOUD_ACCESS_KEY",
        "ACRCLOUD_ACCESS_SECRET",
        "ACRCLOUD_CONTAINER_ID",
    ):
        monkeypatch.setenv(name, "fixture-credential")
    result = asyncio.run(
        shortlist_module.run_shortlist(
            corpus_version="controlled-synth-1",
            out_path=tmp_path / "shortlist.json",
            project_root=ROOT,
            work_root=tmp_path / "work",
            app_config=AppConfig(True),
            cli_confirmation=True,
            engine_runners={
                name: runner(name) for name in ("local_fixture", "shazam", "audd", "acrcloud")
            },
        )
    )
    assert calls == ["local_fixture", "shazam", "audd", "acrcloud"]
    assert {item.provider for item in result.report.engines} == {
        "local_fixture",
        "shazam",
        "audd",
        "acrcloud",
        "panako",
    }


def test_union_coverage_associates_repeated_occurrences_one_to_one() -> None:
    payload = json.loads(
        (
            ROOT / "data/corpus/controlled-synth-1/controlled-001-length-3s/ground_truth.json"
        ).read_text(encoding="utf-8")
    )
    first = copy.deepcopy(payload["episodes"][0])
    second = copy.deepcopy(first)
    first.update(
        start_ms_range=[1_000, 1_200],
        end_ms_range=[9_800, 10_000],
        occurrence_index=0,
        role_segments=[{"from_ms": 1_100, "to_ms": 9_900, "role": "dominant"}],
    )
    second.update(
        start_ms_range=[100_000, 100_200],
        end_ms_range=[109_800, 110_000],
        occurrence_index=1,
        role_segments=[{"from_ms": 100_100, "to_ms": 109_900, "role": "dominant"}],
    )
    payload["episodes"] = [first, second]
    payload["source"]["duration_ms"] = 120_000
    truth = GroundTruthRecord.model_validate(payload)
    committed = ShortlistReportRecord.model_validate_json(REPORT.read_text(encoding="utf-8"))
    metric = next(item.metrics for item in committed.engines if item.metrics is not None)
    assert metric is not None
    run = EngineRun(
        "fixture",
        "fixture-v1.json",
        "file_scanner",
        (
            {
                "set_id": truth.set_id,
                "episodes": [
                    {
                        "work": first["work"],
                        "occurrence_index": 0,
                        "evidence_support_ms": [[2_000, 8_000]],
                    }
                ],
            },
        ),
        metric,
        BenchmarkCost(
            requests=0,
            physical_attempts=0,
            billable_seconds=0,
            usd_e2=0,
            wall_ms=0,
        ),
        1,
        1,
    )
    assert shortlist_module._union_coverage([truth], [run]) == 5_000


def test_scanner_cache_ttl_boundaries_and_errors_never_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw.json"
    raw.write_text("{}", encoding="utf-8")
    now = 2_000_000_000
    monkeypatch.setattr("id_detector.recognise.time.time", lambda: now)
    os.utime(raw, (now - POSITIVE_MAX_AGE_SECONDS, now - POSITIVE_MAX_AGE_SECONDS))
    assert cache_valid(raw, "succeeded")
    os.utime(raw, (now - POSITIVE_MAX_AGE_SECONDS - 1, now - POSITIVE_MAX_AGE_SECONDS - 1))
    assert not cache_valid(raw, "succeeded")
    os.utime(raw, (now - NO_MATCH_MAX_AGE_SECONDS, now - NO_MATCH_MAX_AGE_SECONDS))
    assert cache_valid(raw, "no_match")
    assert not cache_valid(raw, "permanent_failure")


def _scanner_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 16_000 * 60)


@pytest.mark.parametrize(
    "fixture_name",
    ["enterprise-authored-match.json", "enterprise-authored-no-match.json"],
)
def test_scanner_rerun_reconstructs_positive_and_no_match_from_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_name: str
) -> None:
    audio = tmp_path / "owned.wav"
    _scanner_wav(audio)
    truth = GroundTruthRecord.model_validate_json(
        (
            ROOT / "data/corpus/controlled-synth-1/controlled-001-length-3s/ground_truth.json"
        ).read_text(encoding="utf-8")
    )
    monkeypatch.setattr(shortlist_module, "_controlled_audio", lambda *_: audio)
    monkeypatch.setattr(shortlist_module, "_validate_source_media", lambda *_, **__: None)
    response = json.loads((ROOT / "tests/fixtures/audd" / fixture_name).read_text(encoding="utf-8"))
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response)

    adapter = AudDAdapter(
        AudDCredentials("fixture"),
        AppConfig(True),
        True,
        transport=httpx.MockTransport(handler),
    )

    async def run_once(*, refresh: bool = False, use_adapter: bool = True):
        return await shortlist_module._run_scanner_set(
            truth=truth,
            project_root=ROOT,
            work_root=tmp_path / "work",
            provider="audd",
            app_config=AppConfig(use_adapter),
            cli_confirmation=use_adapter,
            refresh=refresh,
            adapter_override=adapter if use_adapter else None,
        )

    first = asyncio.run(run_once())
    monkeypatch.delenv("AUDD_API_TOKEN", raising=False)
    second = asyncio.run(run_once(use_adapter=False))
    assert calls == 1
    assert (first[1].requests, first[1].cache_hits) == (1, 0)
    assert (second[1].requests, second[1].physical_attempts, second[1].cache_hits) == (0, 0, 1)
    assert second[1].raw_index[0].path.endswith(f"/{second[1].queries[0].cache_key}.json")
    assert [item.model_dump() for item in second[1].observations] == [
        item.model_dump() for item in first[1].observations
    ]
    refreshed = asyncio.run(run_once(refresh=True))
    assert calls == 2
    assert (refreshed[1].requests, refreshed[1].cache_hits) == (1, 0)


def test_scanner_error_is_not_cached_and_next_run_attempts_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "owned.wav"
    _scanner_wav(audio)
    truth = GroundTruthRecord.model_validate_json(
        (
            ROOT / "data/corpus/controlled-synth-1/controlled-001-length-3s/ground_truth.json"
        ).read_text(encoding="utf-8")
    )
    monkeypatch.setattr(shortlist_module, "_controlled_audio", lambda *_: audio)
    monkeypatch.setattr(shortlist_module, "_validate_source_media", lambda *_, **__: None)
    no_match = json.loads(
        (ROOT / "tests/fixtures/audd/enterprise-authored-no-match.json").read_text(encoding="utf-8")
    )
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="api_token=must-not-leak")
        return httpx.Response(200, json=no_match)

    adapter = AudDAdapter(
        AudDCredentials("fixture"),
        AppConfig(True),
        True,
        transport=httpx.MockTransport(handler),
    )

    async def run_once():
        return await shortlist_module._run_scanner_set(
            truth=truth,
            project_root=ROOT,
            work_root=tmp_path / "work",
            provider="audd",
            app_config=AppConfig(True),
            cli_confirmation=True,
            adapter_override=adapter,
        )

    with pytest.raises(ProviderProtocolError, match="AudD HTTP 503"):
        asyncio.run(run_once())
    recovered = asyncio.run(run_once())
    assert calls == 2
    assert (recovered[1].requests, recovered[1].cache_hits) == (1, 0)
