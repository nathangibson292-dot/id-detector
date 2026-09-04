"""Stage 4c rescan policies: per-trigger schedules, priority order, and budget awareness.

The plan gives two separate instructions about rescan geometry and both are honoured here:

* the ``[rescan]`` config table (rev 5.2, from the Stage 4b benchmark) is the **base** policy —
  12,000 ms windows at a 5,000 ms hop, phase 0 — used wherever the evidence being repaired is a
  whole-episode question (``contested``, ``long_episode``) and as the fallback for any trigger
  without a specific policy;
* "*Policies use shorter windows (6–8 s) and shifted phases*" for the triggers that need to move
  a **boundary**, i.e. ``edge``/``gap``/``novelty``.  A shorter window is the only thing that can
  lower ``start_no_later_than_ms = min support_ms[1]``: at a fixed 12 s window the proved start
  bound cannot fall below the first window's end however dense the hop is.

Window lengths never exceed the configured rescan window, so a deployment that shortens the base
policy shortens every derived policy with it.
"""

from __future__ import annotations

from dataclasses import dataclass

from id_detector.contracts import RescanPolicy, RescanRequestRecord, Transform
from id_detector.providers.base import (
    DEFAULT_MAX_GENERATIONS,
    DEFAULT_RESCAN_HOP_MS,
    DEFAULT_RESCAN_PHASE_MS,
    DEFAULT_RESCAN_WINDOW_MS,
    AppConfig,
)

# The generation limit lives with the rest of the ``[rescan]`` table but is re-exported here so
# the orchestrator imports every rescan-policy constant from one module.
__all__ = [
    "DEFAULT_MAX_GENERATIONS",
    "TRIGGER_PRIORITY",
    "BudgetedPlan",
    "RescanWindow",
    "base_policy",
    "estimate_request_windows",
    "plan_within_budget",
    "policy_for_trigger",
    "priority_for_trigger",
    "request_sort_key",
    "schedule_rescan_windows",
    "trigger_geometry",
]

#: Highest priority first. The order is the plan's "budgets ordered by priority".
TRIGGER_PRIORITY: dict[str, int] = {
    "gap": 100,
    "question_cluster": 95,
    "contested": 90,
    "hint_cluster": 80,
    "edge": 70,
    "novelty": 65,
    "long_episode": 60,
}

#: ``(window_ms, hop_ms, phase_ms)`` per trigger. ``None`` means "use the configured base policy".
_TRIGGER_GEOMETRY: dict[str, tuple[int, int, int] | None] = {
    "gap": (8_000, 4_000, 2_000),
    "question_cluster": (8_000, 4_000, 2_000),
    "contested": None,
    "hint_cluster": (8_000, 4_000, 2_000),
    "edge": (6_000, 3_000, 0),
    "novelty": (6_000, 3_000, 0),
    "long_episode": None,
}


@dataclass(frozen=True)
class RescanWindow:
    """One planned rescan window: an original-timebase start and an output length."""

    start_ms: int
    output_ms: int


def base_policy(config: AppConfig | None = None) -> tuple[int, int, int]:
    if config is None:
        return (DEFAULT_RESCAN_WINDOW_MS, DEFAULT_RESCAN_HOP_MS, DEFAULT_RESCAN_PHASE_MS)
    return (config.rescan_window_ms, config.rescan_hop_ms, config.rescan_phase_ms)


def trigger_geometry(trigger: str, config: AppConfig | None = None) -> tuple[int, int, int]:
    """Return ``(window_ms, hop_ms, phase_ms)`` for one trigger, clamped by the base policy."""

    base_window, base_hop, base_phase = base_policy(config)
    geometry = _TRIGGER_GEOMETRY.get(trigger)
    if geometry is None:
        return base_window, base_hop, base_phase
    window_ms = min(geometry[0], base_window)
    hop_ms = min(geometry[1], max(1, window_ms))
    phase_ms = geometry[2] % hop_ms
    return window_ms, hop_ms, phase_ms


def policy_for_trigger(
    trigger: str,
    *,
    transforms: list[Transform] | tuple[Transform, ...] | None = None,
    config: AppConfig | None = None,
) -> RescanPolicy:
    window_ms, hop_ms, phase_ms = trigger_geometry(trigger, config)
    return RescanPolicy(
        window_ms=window_ms,
        hop_ms=hop_ms,
        phase_ms=phase_ms,
        transforms=list(transforms)
        if transforms
        else [Transform(type="none", rate_e4=10_000, semitones=0)],
    )


def priority_for_trigger(trigger: str) -> int:
    return TRIGGER_PRIORITY.get(trigger, 50)


def schedule_rescan_windows(
    *, start_ms: int, end_ms: int, policy: RescanPolicy, duration_ms: int
) -> tuple[RescanWindow, ...]:
    """Place windows inside one rescan region, anchored at the region start and end.

    Anchoring at the region start rather than at the media origin is what shifts the rescan grid
    off the generation-0 phase: a request that begins at 37,000 ms produces starts the 9,000 ms
    schedule never visits.
    """

    region_start = max(0, min(start_ms, duration_ms))
    region_end = max(region_start, min(end_ms, duration_ms))
    span = region_end - region_start
    if span <= 0:
        return ()
    window_ms = min(policy.window_ms, span)
    if window_ms <= 0:
        return ()
    hop_ms = max(1, policy.hop_ms)
    starts: list[int] = []
    cursor = region_start + (policy.phase_ms % hop_ms)
    while cursor + window_ms <= region_end:
        starts.append(cursor)
        cursor += hop_ms
    tail = region_end - window_ms
    if tail >= region_start and tail not in starts:
        starts.append(tail)
    if not starts:
        starts.append(region_start)
    return tuple(RescanWindow(start, window_ms) for start in sorted(set(starts)))


def request_sort_key(request: RescanRequestRecord) -> tuple[int, int, str]:
    """Highest priority first, then earliest region, then the deterministic request id."""

    return (-request.priority, request.start_ms, request.id)


def estimate_request_windows(
    request: RescanRequestRecord, *, duration_ms: int, hypotheses: int
) -> int:
    windows = schedule_rescan_windows(
        start_ms=request.start_ms,
        end_ms=request.end_ms,
        policy=request.policy,
        duration_ms=duration_ms,
    )
    return len(windows) * max(1, hypotheses)


@dataclass(frozen=True)
class BudgetedPlan:
    accepted: tuple[RescanRequestRecord, ...]
    deferred: tuple[RescanRequestRecord, ...]
    planned_windows: int
    budget_windows: int

    @property
    def exhausted(self) -> bool:
        return bool(self.deferred)


def plan_within_budget(
    requests: list[RescanRequestRecord] | tuple[RescanRequestRecord, ...],
    *,
    duration_ms: int,
    budget_windows: int,
) -> BudgetedPlan:
    """Accept requests in priority order until the remaining window budget is spent."""

    accepted: list[RescanRequestRecord] = []
    deferred: list[RescanRequestRecord] = []
    used = 0
    for request in sorted(requests, key=request_sort_key):
        hypotheses = max(1, len(request.policy.transforms))
        cost = estimate_request_windows(request, duration_ms=duration_ms, hypotheses=hypotheses)
        if cost <= 0:
            continue
        if used + cost > budget_windows:
            deferred.append(request)
            continue
        accepted.append(request)
        used += cost
    return BudgetedPlan(
        accepted=tuple(sorted(accepted, key=request_sort_key)),
        deferred=tuple(sorted(deferred, key=request_sort_key)),
        planned_windows=used,
        budget_windows=budget_windows,
    )
