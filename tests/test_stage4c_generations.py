from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import id_detector.recognise as recognise_module
from id_detector.contracts import EpisodesFile, RescanRequestRecord, WindowRecord
from id_detector.io import (
    completion_sidecar_path,
    path_is_file,
    read_bytes,
    read_text,
    sha256_file,
)
from id_detector.jobs import AsyncJobStore
from id_detector.orchestrate import (
    STOP_BUDGET_EXHAUSTED,
    STOP_NO_REQUESTS,
    run_generation_loop,
)
from id_detector.providers.base import AppConfig
from id_detector.recognise import load_provider_config, recognise_generation
from id_detector.shazam import ShazamAdapter, TokenBucket
from id_detector.windows import generate_windows_async
from tests.test_stage1_windows import _decoded

ROOT = Path(__file__).parent
# The generation loop is the unit under test here, not the transform algebra (Stage 4b covers
# that with real FFmpeg vectors), so rescans use the ``none`` sibling only and stay fast.
NO_TRANSFORMS = AppConfig(transforms_policy="off")


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((ROOT / "fixtures" / "shazam" / name).read_text(encoding="utf-8"))


class _Harness:
    """A real ingest-free pipeline: real windows, real job store, one fake Shazam server."""

    def __init__(self, tmp_path: Path, duration_ms: int = 30_000) -> None:
        self.decoded, _, self.media_dir = _decoded(tmp_path, duration_ms)
        self.project_root = tmp_path
        self.media_key = "a" * 64
        self.calls = 0
        self.transport_error_on: set[int] = set()
        config, _ = load_provider_config(tmp_path)

        async def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            if self.calls in self.transport_error_on:
                raise httpx.ConnectError("injected transport failure", request=request)
            return httpx.Response(200, json=_fixture("response-match.json"))

        self.adapter = ShazamAdapter(
            config,
            limiter=TokenBucket(rate_per_minute=1_000_000),
            transport=httpx.MockTransport(handler),
        )

    async def recognise(self, *, windows: Any, generation: int) -> Any:
        return await recognise_generation(
            media_key=self.media_key,
            media_dir=self.media_dir,
            windows=windows,
            project_root=self.project_root,
            run_id="stage4c",
            generation=generation,
            max_requests=5_000,
            adapter=self.adapter,
        )

    async def loop(self, windows: Any, recognised: Any, **kwargs: Any) -> Any:
        return await run_generation_loop(
            media_key=self.media_key,
            media_dir=self.media_dir,
            decoded=self.decoded,
            windows=windows,
            observations=recognised.observations,
            observations_path=recognised.observations_path,
            recognise=self.recognise,
            app_config=kwargs.pop("app_config", NO_TRANSFORMS),
            request_budget=kwargs.pop("request_budget", 5_000),
            **kwargs,
        )


async def _run(
    tmp_path: Path,
    *,
    duration_ms: int = 30_000,
    max_generations: int = 3,
    request_budget: int = 5_000,
    app_config: AppConfig = NO_TRANSFORMS,
) -> tuple[_Harness, Any]:
    harness = _Harness(tmp_path, duration_ms)
    windows = await generate_windows_async(harness.decoded, harness.media_dir)
    recognised = await harness.recognise(windows=windows, generation=0)
    result = await harness.loop(
        windows,
        recognised,
        max_generations=max_generations,
        request_budget=request_budget,
        app_config=app_config,
        gen0_requests=recognised.requests,
        gen0_physical_attempts=recognised.physical_attempts,
    )
    return harness, result


def test_short_synthetic_run_converges_in_two_generations(tmp_path: Path) -> None:
    _, result = asyncio.run(_run(tmp_path, duration_ms=14_000))
    assert result.stop_reason == STOP_NO_REQUESTS
    assert result.final_generation == 1
    assert [item.generation for item in result.generations] == [0, 1]
    assert result.generations[0].emitted_requests > 0
    assert result.generations[0].accepted_requests > 0
    assert result.generations[1].emitted_requests == 0


def test_synthetic_run_converges_and_publishes_every_generation_artifact(tmp_path: Path) -> None:
    harness, result = asyncio.run(_run(tmp_path))

    assert result.stop_reason == STOP_NO_REQUESTS
    assert 1 <= result.final_generation <= 3
    assert result.final_generation + 1 == len(result.generations)

    media_dir = harness.media_dir
    for record in result.generations:
        generation = record.generation
        assert record.windows_path.name == f"windows.gen{generation}.jsonl"
        assert record.observations_path.name == f"observations.gen{generation}.jsonl"
        assert record.episodes_path == media_dir / "fuse" / f"episodes.gen{generation}.json"
        assert record.rescan_path == media_dir / "fuse" / f"rescan_plan.gen{generation}.jsonl"
        assert path_is_file(record.windows_path)
        assert path_is_file(record.observations_path)
        assert path_is_file(record.episodes_path)
        assert path_is_file(record.rescan_path)

    later = [
        WindowRecord.model_validate_json(line)
        for record in result.generations[1:]
        for line in read_text(record.windows_path).splitlines()
        if line.strip()
    ]
    assert later
    assert all(item.reason == "rescan" and item.rescan_request_id for item in later)
    assert all(item.generation >= 1 for item in later)
    # A rescan never repeats a geometry an earlier generation already scanned.
    gen0_shapes = {
        (item.start_ms, item.output_ms)
        for line in read_text(result.generations[0].windows_path).splitlines()
        if line.strip()
        for item in [WindowRecord.model_validate_json(line)]
    }
    assert not gen0_shapes & {(item.start_ms, item.output_ms) for item in later}

    final = result.generations[-1]
    sidecar = json.loads(read_text(completion_sidecar_path(final.episodes_path)))
    for record in result.generations:
        for path in (record.windows_path, record.observations_path):
            key = path.relative_to(media_dir).as_posix()
            assert sidecar["upstream"][key] == sha256_file(path)

    final_bytes = read_bytes(final.episodes_path)
    assert read_bytes(media_dir / "fuse" / "episodes.json") == final_bytes
    assert EpisodesFile.model_validate_json(final_bytes).generation == final.generation
    assert read_text(final.rescan_path).strip() == ""


def test_rescans_lower_the_proved_start_bound(tmp_path: Path) -> None:
    harness, result = asyncio.run(_run(tmp_path))
    first = EpisodesFile.model_validate_json(read_bytes(result.generations[0].episodes_path))
    last = EpisodesFile.model_validate_json(read_bytes(result.generations[-1].episodes_path))
    assert first.episodes and last.episodes
    assert min(item.best_start_ms for item in last.episodes) < min(
        item.best_start_ms for item in first.episodes
    )
    assert harness.calls > 0


def test_rescan_request_ids_are_deterministic_and_generation_scoped(tmp_path: Path) -> None:
    _, first = asyncio.run(_run(tmp_path / "a"))
    _, second = asyncio.run(_run(tmp_path / "b"))
    first_plan = read_text(first.generations[0].rescan_path)
    assert first_plan == read_text(second.generations[0].rescan_path)
    requests = [
        RescanRequestRecord.model_validate_json(line)
        for line in first_plan.splitlines()
        if line.strip()
    ]
    assert requests
    assert len({item.id for item in requests}) == len(requests)
    assert all(item.generation == 0 for item in requests)
    assert all(item.input_hashes for item in requests)
    assert {item.trigger for item in requests} <= {
        "gap",
        "contested",
        "edge",
        "long_episode",
        "novelty",
        "hint_cluster",
        "question_cluster",
    }


def test_two_identical_runs_produce_byte_identical_final_episodes(tmp_path: Path) -> None:
    harness_a, first = asyncio.run(_run(tmp_path / "one"))
    harness_b, second = asyncio.run(_run(tmp_path / "two"))
    assert read_bytes(harness_a.media_dir / "fuse" / "episodes.json") == read_bytes(
        harness_b.media_dir / "fuse" / "episodes.json"
    )
    assert first.final_generation == second.final_generation
    assert first.stop_reason == second.stop_reason
    for left, right in zip(first.generations, second.generations, strict=True):
        assert read_bytes(left.windows_path) == read_bytes(right.windows_path)
        assert read_bytes(left.episodes_path) == read_bytes(right.episodes_path)


def test_budget_exhaustion_stops_the_loop_before_any_rescan(tmp_path: Path) -> None:
    _, result = asyncio.run(_run(tmp_path, request_budget=1))
    assert result.stop_reason == STOP_BUDGET_EXHAUSTED
    assert result.final_generation == 0
    assert result.generations[0].emitted_requests > 0
    assert result.generations[0].accepted_requests == 0
    assert result.generations[0].deferred_requests == result.generations[0].emitted_requests


def test_max_generations_zero_disables_rescans(tmp_path: Path) -> None:
    _, result = asyncio.run(_run(tmp_path, max_generations=0))
    assert result.stop_reason == "max_generations"
    assert result.final_generation == 0


def _submission_audit(jobs: list[Any]) -> tuple[dict[str, int], dict[str, int]]:
    submitted: dict[str, int] = {}
    attempts: dict[str, int] = {}
    for job in jobs:
        if not job.result_path:
            continue
        content = Path(job.result_path).stem
        attempts[content] = attempts.get(content, 0) + job.physical_attempts
        if job.submitted_at is not None:
            submitted[content] = submitted.get(content, 0) + 1
    return submitted, attempts


@pytest.mark.parametrize(
    "injection",
    [
        "transport_error_before_acknowledgement",
        "crash_after_acknowledgement",
        "crash_between_generations",
    ],
)
def test_failure_injection_never_double_submits_across_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, injection: str
) -> None:
    async def scenario() -> tuple[dict[str, int], dict[str, int], bytes]:
        harness = _Harness(tmp_path, 30_000)
        windows = await generate_windows_async(harness.decoded, harness.media_dir)
        recognised = await harness.recognise(windows=windows, generation=0)
        gen0_calls = harness.calls

        if injection == "transport_error_before_acknowledgement":
            harness.transport_error_on = {gen0_calls + 2}
            await harness.loop(windows, recognised)
        elif injection == "crash_after_acknowledgement":
            original = recognise_module._write_immutable_json
            state = {"seen": 0}

            def failing(path: Path, value: Any) -> None:
                if path.parent.name == "raw":
                    state["seen"] += 1
                    if state["seen"] == gen0_calls + 2:
                        raise RuntimeError("injected crash after the provider acknowledged")
                original(path, value)

            monkeypatch.setattr(recognise_module, "_write_immutable_json", failing)
            with pytest.raises(RuntimeError):
                await harness.loop(windows, recognised)
            monkeypatch.undo()
        else:
            calls = {"n": 0}
            original_recognise = harness.recognise

            async def crashing(*, windows: Any, generation: int) -> Any:
                result = await original_recognise(windows=windows, generation=generation)
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("injected crash between generations")
                return result

            harness.recognise = crashing  # type: ignore[method-assign]
            with pytest.raises(RuntimeError):
                await harness.loop(windows, recognised)
            harness.recognise = original_recognise  # type: ignore[method-assign]

        # Recovery: the whole loop runs again on the same work directory.
        result = await harness.loop(windows, recognised)
        async with AsyncJobStore(harness.media_dir / "jobs.sqlite") as store:
            jobs = await store.list_jobs()
        submitted, attempts = _submission_audit(jobs)
        return submitted, attempts, read_bytes(result.fusion.final_path)

    submitted, attempts, episodes = asyncio.run(scenario())
    assert submitted, "the run must have submitted something"
    assert max(submitted.values()) == 1, "no content may be submitted twice across generations"
    assert all(count >= 1 for count in attempts.values())
    assert EpisodesFile.model_validate_json(episodes).episodes

    if injection != "crash_after_acknowledgement":
        # A retried transport failure and a crash between generations both recover exactly.
        _, clean_result = asyncio.run(_run(tmp_path / "clean"))
        assert read_bytes(clean_result.fusion.final_path) == episodes
