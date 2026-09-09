"""The orchestrator-owned generation loop (plan rev 5.2, "Pipeline and iteration").

``fuse`` never writes windows or queries: it emits ``fuse/rescan_plan.gen<N>.jsonl``.  This module
turns those requests into ``windows/windows.gen<N+1>.jsonl`` and
``recognise/queries.gen<N+1>.jsonl``, has recognition append ``observations.gen<N+1>.jsonl``, then
re-fuses the **union of every generation** into ``fuse/episodes.gen<N+1>.json``.  It stops on the
first of: no requests, ``max_generations`` (default 3), or an exhausted budget.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
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
    """Spectral-novelty change points, the rescan ``novelty`` trigger's input.

    This reads the whole PCM and runs a full log-mel/flux pass (~0.7 GB and seconds per hour of
    mix), so a caller computes it once per run and only when rescans can use it — see
    :func:`run_generation_loop` (review M2).
    """

    if not enabled:
        return ()
    events = novelty_change_points(decoded.pcm_path, duration_ms=decoded.record.pcm.duration_ms)
    return tuple(item.at_ms for item in events)


def scanned_windows(
    windows: Iterable[WindowRecord], observations: Iterable[Any]
) -> list[WindowRecord]:
    """The windows some engine actually answered for, in the given order (review M10).

    A window is scanned when a resolved clip observation (``match`` or ``no_match``, from any
    provider) names it in ``source_ids`` as ``window:<id>``; an ``error`` observation means no
    engine answered.  Under the Deep recipe only the windows AudD swept (every other one at
    density 2) plus the ones the Shazam secondary probed count, so the fuser's coverage figures
    and gap evidence describe the pass that really happened instead of a free-engine sweep of the
    whole mix.
    """

    answered = {
        source_id.removeprefix("window:")
        for observation in observations
        if getattr(observation, "status", "error") != "error"
        for source_id in getattr(observation, "source_ids", ())
        if source_id.startswith("window:")
    }
    return [window for window in windows if window.id in answered]


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
    extra_observations: tuple[ObservationRecord, ...] | list[ObservationRecord] = (),
    extra_observation_paths: tuple[Path, ...] | list[Path] = (),
    hints: tuple[HintRecord, ...] | list[HintRecord] = (),
    hints_path: Path | None = None,
    profile: str = "free",
    max_generations: int = DEFAULT_MAX_GENERATIONS,
    request_budget: int = DEFAULT_REQUEST_BUDGET,
    novelty_enabled: bool = True,
    novelty_change_points_ms: Sequence[int] | None = None,
    scanned_windows: Sequence[WindowRecord] | None = None,
    gen0_requests: int = 0,
    gen0_physical_attempts: int = 0,
    calibrator: object | None = None,
) -> OrchestrationResult:
    """Run generation 0's fusion and every budgeted rescan generation after it.

    ``novelty_change_points_ms`` lets a caller that fuses more than once (the Deep re-fuse) pass
    the points it computed a single time; when absent they are computed here, and only if
    ``max_generations`` allows a rescan to consume them (review M2).  ``scanned_windows`` is the
    subset of ``windows.records`` an engine actually answered for (:func:`scanned_windows`);
    it defaults to every window, the free-recipe truth.
    """

    duration_ms = decoded.record.pcm.duration_ms
    if novelty_change_points_ms is None:
        novelty_change_points_ms = compute_novelty_change_points(
            decoded, enabled=novelty_enabled and max_generations > 0
        )
    novelty_points = tuple(novelty_change_points_ms)
    transforms = rescan_transform_grid(app_config)

    all_windows: list[WindowRecord] = list(
        windows.records if scanned_windows is None else scanned_windows
    )
    # Paid whole-file scanner observations (AudD/ACRCloud) join generation 0 as a static set: they
    # are never re-run per rescan generation, so they simply persist through the loop and fuse
    # alongside every generation's clip observations.  Empty on the free path.
    all_observations: list[ObservationRecord] = list(observations) + list(extra_observations)
    window_paths: list[Path] = [windows.record_path]
    observation_paths: list[Path] = [observations_path, *extra_observation_paths]
    prior_keys: set[str] = set()
    # The request budget is spent per window *generated*, not per window an engine answered:
    # restricting the fused set to the answered ones (review M10) must not quietly widen the
    # rescan allowance ``request_budget`` (``--max-requests``) bounds.  Under a Deep density-2
    # sweep half the windows go unanswered by design, which would otherwise hand the loop half a
    # mix's worth of extra Shazam requests.
    spent_windows = len(windows.records)
    budget = max(request_budget, len(windows.records))

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
