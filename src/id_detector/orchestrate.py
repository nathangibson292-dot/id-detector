"""The orchestrator-owned generation loop (plan rev 5.2, "Pipeline and iteration").

``fuse`` never writes windows or queries: it emits ``fuse/rescan_plan.gen<N>.jsonl``.  This module
turns those requests into ``windows/windows.gen<N+1>.jsonl`` and
``recognise/queries.gen<N+1>.jsonl``, has recognition append ``observations.gen<N+1>.jsonl``, then
re-fuses the **union of every generation** into ``fuse/episodes.gen<N+1>.json``.  It stops on the
first of: no requests, ``max_generations`` (default 3), or an exhausted budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from id_detector.contracts import (
    HintRecord,
    ObservationRecord,
    RescanRequestRecord,
    Transform,
    WindowRecord,
)
from id_detector.decode import DecodeResult
from id_detector.fuse.episodes import FusionResult, fuse_generation, region_request_key
from id_detector.novelty import novelty_change_points
from id_detector.providers.base import AppConfig
from id_detector.rescan import DEFAULT_MAX_GENERATIONS, BudgetedPlan, plan_within_budget
from id_detector.windows import (
    TransformGrid,
    WindowsResult,
    generate_rescan_windows_async,
)

DEFAULT_REQUEST_BUDGET = 2_000
STOP_NO_REQUESTS = "no_requests"
STOP_MAX_GENERATIONS = "max_generations"
STOP_BUDGET_EXHAUSTED = "budget_exhausted"
STOP_NO_NEW_WINDOWS = "no_new_windows"


class RecogniseGeneration(Protocol):
    async def __call__(
        self, *, windows: WindowsResult, generation: int
    ) -> Any:  # pragma: no cover - structural type
        ...


@dataclass(frozen=True)
class GenerationRecord:
    generation: int
    windows_path: Path
    observations_path: Path
    episodes_path: Path
    rescan_path: Path
    window_count: int
    observation_count: int
    emitted_requests: int
    accepted_requests: int
    deferred_requests: int
    requests: int
    physical_attempts: int


@dataclass(frozen=True)
class OrchestrationResult:
    fusion: FusionResult
    generations: tuple[GenerationRecord, ...]
    stop_reason: str
    novelty_change_points_ms: tuple[int, ...]

    @property
    def final_generation(self) -> int:
        return self.generations[-1].generation

    @property
    def requests(self) -> int:
        return sum(item.requests for item in self.generations)

    @property
    def physical_attempts(self) -> int:
        return sum(item.physical_attempts for item in self.generations)


def window_shapes(
    windows: list[WindowRecord] | tuple[WindowRecord, ...],
) -> frozenset[tuple[int, int]]:
    return frozenset((item.start_ms, item.output_ms) for item in windows)


def request_keys(
    requests: list[RescanRequestRecord] | tuple[RescanRequestRecord, ...],
) -> frozenset[str]:
    return frozenset(
        region_request_key(item.trigger, item.start_ms, item.end_ms, item.policy)
        for item in requests
    )


def rescan_transform_grid(config: AppConfig) -> list[Transform]:
    """The hypotheses a rescan may use, per the Stage 4b ``transforms.policy`` decision."""

    if config.transforms_policy == "off":
        return [Transform(type="none", rate_e4=10_000, semitones=0)]
    return list(
        TransformGrid(
            rates_e4=config.transform_rates_e4, semitones=config.transform_semitones
        ).hypotheses()
    )


def compute_novelty_change_points(
    decoded: DecodeResult, *, enabled: bool = True
) -> tuple[int, ...]:
    if not enabled:
        return ()
    events = novelty_change_points(decoded.pcm_path, duration_ms=decoded.record.pcm.duration_ms)
    return tuple(item.at_ms for item in events)


async def run_generation_loop(
    *,
    media_key: str,
    media_dir: Path,
    decoded: DecodeResult,
    windows: WindowsResult,
    observations: tuple[ObservationRecord, ...] | list[ObservationRecord],
    observations_path: Path,
    recognise: RecogniseGeneration,
    app_config: AppConfig,
    hints: tuple[HintRecord, ...] | list[HintRecord] = (),
    hints_path: Path | None = None,
    profile: str = "free",
    max_generations: int = DEFAULT_MAX_GENERATIONS,
    request_budget: int = DEFAULT_REQUEST_BUDGET,
    novelty_enabled: bool = True,
    gen0_requests: int = 0,
    gen0_physical_attempts: int = 0,
    calibrator: object | None = None,
) -> OrchestrationResult:
    """Run generation 0's fusion and every budgeted rescan generation after it."""

    duration_ms = decoded.record.pcm.duration_ms
    novelty_points = compute_novelty_change_points(decoded, enabled=novelty_enabled)
    transforms = rescan_transform_grid(app_config)

    all_windows: list[WindowRecord] = list(windows.records)
    all_observations: list[ObservationRecord] = list(observations)
    window_paths: list[Path] = [windows.record_path]
    observation_paths: list[Path] = [observations_path]
    prior_keys: set[str] = set()
    spent_windows = len(all_windows)
    budget = max(request_budget, len(all_windows))

    fusion = fuse_generation(
        media_key=media_key,
        media_dir=media_dir,
        duration_ms=duration_ms,
        observations=all_observations,
        observation_paths=observation_paths,
        windows=all_windows,
        window_paths=window_paths,
        pcm_path=decoded.record_path,
        generation=0,
        hints=hints,
        hints_path=hints_path,
        profile=profile,
        rescan_transforms=transforms,
        novelty_change_points_ms=novelty_points,
        scanned_window_shapes=window_shapes(all_windows),
        config=app_config,
        calibrator=calibrator,
    )
    generations = [
        GenerationRecord(
            generation=0,
            windows_path=windows.record_path,
            observations_path=observations_path,
            episodes_path=fusion.generation_path,
            rescan_path=fusion.rescan_path,
            window_count=len(windows.records),
            observation_count=len(observations),
            emitted_requests=len(fusion.requests),
            accepted_requests=0,
            deferred_requests=0,
            requests=gen0_requests,
            physical_attempts=gen0_physical_attempts,
        )
    ]

    stop_reason = STOP_NO_REQUESTS
    generation = 0
    while True:
        pending: tuple[RescanRequestRecord, ...] = fusion.requests
        if not pending:
            stop_reason = STOP_NO_REQUESTS
            break
        if generation + 1 > max_generations:
            stop_reason = STOP_MAX_GENERATIONS
            break
        remaining = max(0, budget - spent_windows)
        plan: BudgetedPlan = plan_within_budget(
            pending, duration_ms=duration_ms, budget_windows=remaining
        )
        generations[-1] = _with_plan(generations[-1], plan)
        if not plan.accepted:
            stop_reason = STOP_BUDGET_EXHAUSTED
            break

        generation += 1
        prior_keys |= request_keys(plan.accepted)
        rescan_windows = await generate_rescan_windows_async(
            decoded,
            media_dir,
            generation=generation,
            requests=plan.accepted,
            transform_policy=app_config.transforms_policy,
            existing_shapes=window_shapes(all_windows),
            upstream={
                fusion.rescan_path.relative_to(media_dir).as_posix(): fusion.rescan_path,
            },
        )
        if not rescan_windows.records:
            stop_reason = STOP_NO_NEW_WINDOWS
            break

        recognised = await recognise(windows=rescan_windows, generation=generation)
        all_windows.extend(rescan_windows.records)
        all_observations.extend(recognised.observations)
        window_paths.append(rescan_windows.record_path)
        observation_paths.append(recognised.observations_path)
        spent_windows += len(rescan_windows.records)

        fusion = fuse_generation(
            media_key=media_key,
            media_dir=media_dir,
            duration_ms=duration_ms,
            observations=all_observations,
            observation_paths=observation_paths,
            windows=all_windows,
            window_paths=window_paths,
            pcm_path=decoded.record_path,
            generation=generation,
            hints=hints,
            hints_path=hints_path,
            profile=profile,
            rescan_transforms=transforms,
            novelty_change_points_ms=novelty_points,
            prior_request_keys=frozenset(prior_keys),
            scanned_window_shapes=window_shapes(all_windows),
            config=app_config,
            calibrator=calibrator,
        )
        generations.append(
            GenerationRecord(
                generation=generation,
                windows_path=rescan_windows.record_path,
                observations_path=recognised.observations_path,
                episodes_path=fusion.generation_path,
                rescan_path=fusion.rescan_path,
                window_count=len(rescan_windows.records),
                observation_count=len(recognised.observations),
                emitted_requests=len(fusion.requests),
                accepted_requests=0,
                deferred_requests=0,
                requests=getattr(recognised, "requests", 0),
                physical_attempts=getattr(recognised, "physical_attempts", 0),
            )
        )

    return OrchestrationResult(
        fusion=fusion,
        generations=tuple(generations),
        stop_reason=stop_reason,
        novelty_change_points_ms=novelty_points,
    )


def _with_plan(record: GenerationRecord, plan: BudgetedPlan) -> GenerationRecord:
    return GenerationRecord(
        generation=record.generation,
        windows_path=record.windows_path,
        observations_path=record.observations_path,
        episodes_path=record.episodes_path,
        rescan_path=record.rescan_path,
        window_count=record.window_count,
        observation_count=record.observation_count,
        emitted_requests=record.emitted_requests,
        accepted_requests=len(plan.accepted),
        deferred_requests=len(plan.deferred),
        requests=record.requests,
        physical_attempts=record.physical_attempts,
    )
