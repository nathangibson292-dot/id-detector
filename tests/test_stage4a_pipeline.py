from __future__ import annotations

import asyncio
import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from id_detector.benchmark.hints import (
    _evidence_coverage,
    _raw_segment_precision_counts,
    run_hint_gate,
)
from id_detector.benchmark.scorer import SegmentCount, paired_non_inferiority
from id_detector.cli import app
from id_detector.contracts import HintRecord, SourceRecord
from id_detector.hints.connectors.base import CircuitBreaker, RetryableConnectorError
from id_detector.hints.pipeline import _execute, _shared_breaker, run_hints
from id_detector.ingest import ingest
from id_detector.io import atomic_write_json, read_bytes, sha256_file
from id_detector.jobs import AsyncJobStore, ProcessLock
from id_detector.process import run_process


def _source() -> SourceRecord:
    return SourceRecord.model_validate_json(
        Path("tests/golden/source.json").read_text(encoding="utf-8")
    )


def test_manual_pipeline_is_cached_deterministic_and_job_backed(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = _source()
        media_dir = tmp_path / "work" / source.source_key / source.media_key
        source_path = media_dir / "ingest" / "source.json"
        atomic_write_json(source_path, source)
        manual = tmp_path / "tracklist.txt"
        manual.write_text(
            "00:00 Artist One - Title One\n05:00 Artist Two - Title Two\n",
            encoding="utf-8",
        )
        first = await run_hints(
            source=source,
            duration_ms=600_000,
            media_dir=media_dir,
            source_path=source_path,
            project_root=tmp_path,
            manual_tracklist=manual,
        )
        first_hints = read_bytes(first.hints_path)
        first_status = read_bytes(first.status_path)
        second = await run_hints(
            source=source,
            duration_ms=600_000,
            media_dir=media_dir,
            source_path=source_path,
            project_root=tmp_path,
            manual_tracklist=manual,
        )
        assert read_bytes(second.hints_path) == first_hints
        assert read_bytes(second.status_path) == first_status
        parsed = [
            HintRecord.model_validate_json(line)
            for line in first_hints.decode("utf-8").splitlines()
        ]
        assert len(parsed) == 2
        assert all(item.connector == "manual_tracklist" for item in parsed)
        sidecar = json.loads(first.hints_path.with_suffix(".done.json").read_text(encoding="utf-8"))
        assert sidecar["sha256"] == sha256_file(first.hints_path)
        assert set(sidecar["upstream"]) >= {"ingest/source.json"}
        async with AsyncJobStore(media_dir / "jobs.sqlite") as store:
            jobs = await store.list_connector_jobs(source.media_key)
            assert len(jobs) == 1
            assert jobs[0].connector == "manual_tracklist" and jobs[0].state == "succeeded"
        assert list((tmp_path / "data/local/hints/manual_tracklist").rglob("result.json"))

        manual.write_text(
            "00:00 Artist One - Edited Title\n05:00 Artist Two - Title Two\n",
            encoding="utf-8",
        )
        third = await run_hints(
            source=source,
            duration_ms=600_000,
            media_dir=media_dir,
            source_path=source_path,
            project_root=tmp_path,
            manual_tracklist=manual,
        )
        edited = [
            HintRecord.model_validate_json(line)
            for line in read_bytes(third.hints_path).decode("utf-8").splitlines()
        ]
        assert {item.title for item in edited} == {"Edited Title", "Title Two"}
        assert [item.id for item in edited] == sorted(item.id for item in edited)
        async with AsyncJobStore(media_dir / "jobs.sqlite") as store:
            jobs = await store.list_connector_jobs(source.media_key)
            assert len(jobs) == 2
            assert len({job.id for job in jobs}) == 2

    asyncio.run(scenario())


def test_formal_gate_refuses_missing_owner_verified_dev2_before_network(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pending owner-verified frozen dev-2 truth"):
        asyncio.run(
            run_hint_gate(
                corpus_version="dev-2",
                out_path=tmp_path / "gate.json",
                project_root=tmp_path,
                work_root=tmp_path / "work",
            )
        )


def test_manual_mirror_confirmation_is_imported_released_and_audited(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = _source()
        media_dir = tmp_path / "work" / source.source_key / source.media_key
        source_path = media_dir / "ingest" / "source.json"
        atomic_write_json(source_path, source)
        html = Path("tests/fixtures/hints/pointer-1001-authored.html").read_text(encoding="utf-8")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html))
        ) as client:
            result = await run_hints(
                source=source,
                duration_ms=600_000,
                media_dir=media_dir,
                source_path=source_path,
                project_root=tmp_path,
                confirmed_mirrors=("https://1001.tl/manual-fixture",),
                http=client,
            )
        imported = [hint for hint in result.hints if hint.connector == "1001tl"]
        assert len(imported) == 2
        assert all(hint.mirror_status == "verified" for hint in imported)
        status = json.loads(result.status_path.read_text(encoding="utf-8"))
        assert status["mirror_releases"] == [
            {
                "hints_released": 2,
                "method": "manual",
                "url": "https://1001.tl/manual-fixture",
            }
        ]
        assert status["mirror_confirmations"] == [
            {"matched_import": True, "url": "https://1001.tl/manual-fixture"}
        ]

    asyncio.run(scenario())


def test_hint_gate_coverage_uses_union_of_badge_eligible_evidence(tmp_path: Path) -> None:
    predictions = tmp_path / "predictions.json"
    atomic_write_json(
        predictions,
        {
            "sets": [
                {
                    "set_id": "set-a",
                    "episodes": [
                        {"tiers": {"work": "possible"}, "evidence_support_ms": [[0, 40]]},
                        {"tiers": {"work": "likely"}, "evidence_support_ms": [[20, 60]]},
                        {"tiers": {"work": "unclear"}, "evidence_support_ms": [[60, 100]]},
                    ],
                }
            ]
        },
    )
    assert _evidence_coverage(predictions, {"set-a": 100}) == {"set-a": (60, 100)}


def test_precision_gate_uses_raw_segment_counts_for_unequal_sets() -> None:
    scores = [
        SimpleNamespace(
            truth=SimpleNamespace(set_id="large"),
            state=SimpleNamespace(segment=SegmentCount(tp=98_000, fp=2_000)),
        ),
        *[
            SimpleNamespace(
                truth=SimpleNamespace(set_id=f"small-{index:02d}"),
                state=SimpleNamespace(segment=SegmentCount(tp=100, fp=0)),
            )
            for index in range(19)
        ],
    ]
    challenger = _raw_segment_precision_counts(scores)  # type: ignore[arg-type]
    baseline = {"large": (100_000, 100_000)} | {
        f"small-{index:02d}": (99, 100) for index in range(19)
    }
    raw = paired_non_inferiority(
        baseline, challenger, seed=20_260_905, margin_e4=100, replicates=2_000
    )
    rounded_baseline = {"large": (10_000, 10_000)} | {
        f"small-{index:02d}": (9_900, 10_000) for index in range(19)
    }
    rounded_challenger = {"large": (9_800, 10_000)} | {
        f"small-{index:02d}": (10_000, 10_000) for index in range(19)
    }
    rounded = paired_non_inferiority(
        rounded_baseline,
        rounded_challenger,
        seed=20_260_905,
        margin_e4=100,
        replicates=2_000,
    )
    assert raw["delta_e4"] < -100 and raw["pass"] is False
    assert rounded["delta_e4"] > 0 and rounded["pass"] is True


def test_connector_cache_key_covers_input_config_and_caps(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with AsyncJobStore(tmp_path / "jobs.sqlite") as store:
            kwargs = {
                "page_cap": 1,
                "item_cap": 10,
                "input_content_sha256": "1" * 64,
                "configuration_sha256": "2" * 64,
            }
            media_key = _source().media_key
            original = await store.ensure_connector_job(media_key, "manual", "target", **kwargs)
            same = await store.ensure_connector_job(media_key, "manual", "target", **kwargs)
            changed_input = await store.ensure_connector_job(
                media_key,
                "manual",
                "target",
                **(kwargs | {"input_content_sha256": "3" * 64}),
            )
            changed_config = await store.ensure_connector_job(
                media_key,
                "manual",
                "target",
                **(kwargs | {"configuration_sha256": "4" * 64}),
            )
            changed_cap = await store.ensure_connector_job(
                media_key, "manual", "target", **(kwargs | {"item_cap": 11})
            )
            assert original.id == same.id
            assert len({original.id, changed_input.id, changed_config.id, changed_cap.id}) == 4

    asyncio.run(scenario())


def test_breakers_are_shared_per_connector_and_host() -> None:
    breakers: dict[tuple[str, str], CircuitBreaker] = {}
    first = _shared_breaker(breakers, "pointer_import", "https://1001.tl/one")
    second = _shared_breaker(breakers, "pointer_import", "https://1001.tl/two")
    other_host = _shared_breaker(breakers, "pointer_import", "https://www.mixesdb.com/one")
    assert first is second
    assert first is not other_host


def test_retryable_connector_failure_is_recorded_for_later_retry(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = _source()
        async with (
            AsyncJobStore(tmp_path / "jobs.sqlite") as store,
            httpx.AsyncClient() as client,
        ):

            def transient(_context: object) -> object:
                raise RetryableConnectorError("temporary")

            _, status, _ = await _execute(
                store=store,
                source=source,
                duration_ms=600_000,
                media_dir=tmp_path,
                cache_root=tmp_path / "cache",
                client=client,
                owner="owner",
                connector="pointer_import",
                target_url="https://1001.tl/retry",
                page_cap=1,
                item_cap=1,
                refresh=False,
                callback=transient,
                input_content_sha256="1" * 64,
                configuration_sha256="2" * 64,
                breaker=CircuitBreaker(),
            )
            assert status["state"] == "retryable_failure"
            jobs = await store.list_connector_jobs(source.media_key)
            assert len(jobs) == 1 and jobs[0].state == "retryable_failure"

    asyncio.run(scenario())


def test_cli_exposes_required_stage4a_options() -> None:
    runner = CliRunner()
    analyse_help = runner.invoke(app, ["analyse", "--help"])
    assert analyse_help.exit_code == 0
    assert "--tracklist" in analyse_help.stdout and "--no-hints" in analyse_help.stdout
    hints_help = runner.invoke(app, ["hints", "--help"])
    assert hints_help.exit_code == 0
    assert "Fetch and parse hints" in hints_help.stdout
    assert "--confirm-mirror" in hints_help.stdout
    gate_help = runner.invoke(app, ["benchmark", "hints", "--help"])
    assert gate_help.exit_code == 0
    assert "--corpus" in gate_help.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Windows workspace-lock acceptance test")
def test_standalone_hints_holds_source_and_media_locks_across_processes(tmp_path: Path) -> None:
    source = tmp_path / "fixture.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(bytes(16_000 * 2))
    work_root = tmp_path / "work"
    source_arg = str(source)
    source_lock = ProcessLock(
        work_root.resolve()
        / ".locks"
        / f"{__import__('hashlib').sha256(source_arg.encode('utf-8')).hexdigest()}.lock"
    )
    source_lock.acquire()
    try:
        blocked = asyncio.run(
            run_process(
                [
                    sys.executable,
                    "-m",
                    "id_detector.cli",
                    "hints",
                    source_arg,
                    "--work-root",
                    str(work_root),
                ],
                timeout=20,
                check=False,
            )
        )
    finally:
        source_lock.release()
    assert blocked.returncode == 1
    assert "workspace is already active" in blocked.stderr

    ingested = asyncio.run(ingest(source_arg, work_root))
    media_lock = ProcessLock(ingested.media_dir / ".media.lock")
    media_lock.acquire()
    try:
        blocked = asyncio.run(
            run_process(
                [
                    sys.executable,
                    "-m",
                    "id_detector.cli",
                    "hints",
                    source_arg,
                    "--work-root",
                    str(work_root),
                ],
                timeout=20,
                check=False,
            )
        )
    finally:
        media_lock.release()
    assert blocked.returncode == 1
    assert "workspace is already active" in blocked.stderr
