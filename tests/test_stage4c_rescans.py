from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    RescanPolicy,
    RescanRequestRecord,
    Transform,
    compose_natural_key,
    make_id,
)
from id_detector.novelty import (
    flux_change_points,
    log_mel_frames,
    spectral_flux,
)
from id_detector.providers.base import (
    DEFAULT_RESCAN_HOP_MS,
    DEFAULT_RESCAN_PHASE_MS,
    DEFAULT_RESCAN_WINDOW_MS,
    AppConfig,
)
from id_detector.rescan import (
    TRIGGER_PRIORITY,
    plan_within_budget,
    policy_for_trigger,
    request_sort_key,
    schedule_rescan_windows,
    trigger_geometry,
)

MEDIA_KEY = "b" * 64


def _request(trigger: str, start: int, end: int, *, transforms: int = 1) -> RescanRequestRecord:
    policy = policy_for_trigger(
        trigger,
        transforms=[Transform(type="none", rate_e4=10_000, semitones=0)] * transforms,
    )
    natural = {
        "generation": 0,
        "trigger": trigger,
        "start_ms": start,
        "end_ms": end,
        "policy": policy.model_dump(mode="json"),
    }
    return RescanRequestRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(MEDIA_KEY, "rescan_request", compose_natural_key("rescan_request", natural)),
        generation=0,
        trigger=trigger,
        start_ms=start,
        end_ms=end,
        policy=policy,
        priority=TRIGGER_PRIORITY[trigger],
        input_hashes={},
    )


def test_every_plan_trigger_has_a_policy_and_a_priority() -> None:
    triggers = {
        "gap",
        "contested",
        "edge",
        "long_episode",
        "novelty",
        "hint_cluster",
        "question_cluster",
    }
    assert set(TRIGGER_PRIORITY) == triggers
    for trigger in triggers:
        policy = policy_for_trigger(trigger)
        assert isinstance(policy, RescanPolicy)
        assert 0 < policy.window_ms <= DEFAULT_RESCAN_WINDOW_MS
        assert 0 < policy.hop_ms <= policy.window_ms
        assert 0 <= policy.phase_ms < policy.hop_ms
        assert policy.transforms


def test_boundary_triggers_use_shorter_windows_and_whole_episode_triggers_use_the_config() -> None:
    # The plan: "policies use shorter windows (6-8 s) and shifted phases" for edges and gaps.
    assert trigger_geometry("edge")[0] == 6_000
    assert trigger_geometry("novelty")[0] == 6_000
    assert trigger_geometry("gap")[0] == 8_000
    assert trigger_geometry("gap")[2] != 0
    # The rev 5.2 ``[rescan]`` config table is the base policy for whole-episode questions.
    assert trigger_geometry("contested") == (
        DEFAULT_RESCAN_WINDOW_MS,
        DEFAULT_RESCAN_HOP_MS,
        DEFAULT_RESCAN_PHASE_MS,
    )
    assert trigger_geometry("long_episode") == (12_000, 5_000, 0)


def test_example_config_carries_the_stage_4c_generation_limit() -> None:
    from id_detector.providers.base import DEFAULT_MAX_GENERATIONS

    example = AppConfig.load(Path(__file__).resolve().parents[1] / "id-detector.example.toml")
    assert example.rescan_max_generations == DEFAULT_MAX_GENERATIONS == 3
    assert (example.rescan_window_ms, example.rescan_hop_ms, example.rescan_phase_ms) == (
        12_000,
        5_000,
        0,
    )


def test_configured_rescan_table_clamps_every_derived_policy() -> None:
    config = AppConfig(rescan_window_ms=5_000, rescan_hop_ms=2_500, rescan_phase_ms=0)
    for trigger in TRIGGER_PRIORITY:
        window_ms, hop_ms, phase_ms = trigger_geometry(trigger, config)
        assert window_ms <= 5_000
        assert 0 < hop_ms <= window_ms
        assert 0 <= phase_ms < hop_ms


def test_priority_order_is_gap_question_contested_hint_edge_novelty_long() -> None:
    order = sorted(TRIGGER_PRIORITY, key=lambda name: -TRIGGER_PRIORITY[name])
    assert order == [
        "gap",
        "question_cluster",
        "contested",
        "hint_cluster",
        "edge",
        "novelty",
        "long_episode",
    ]
    requests = [_request("edge", 0, 40_000), _request("gap", 100_000, 200_000)]
    assert [item.trigger for item in sorted(requests, key=request_sort_key)] == ["gap", "edge"]


def test_rescan_windows_are_anchored_at_the_region_and_end_anchored() -> None:
    policy = policy_for_trigger("edge")
    windows = schedule_rescan_windows(
        start_ms=37_000, end_ms=61_000, policy=policy, duration_ms=120_000
    )
    starts = [item.start_ms for item in windows]
    assert starts[0] == 37_000
    assert starts[-1] + windows[-1].output_ms == 61_000
    assert all(item.output_ms == 6_000 for item in windows)
    assert starts == sorted(set(starts))
    # A region shorter than the policy window still yields exactly one clipped window.
    short = schedule_rescan_windows(start_ms=0, end_ms=4_000, policy=policy, duration_ms=120_000)
    assert [(item.start_ms, item.output_ms) for item in short] == [(0, 4_000)]
    # Regions are clipped to the media.
    clipped = schedule_rescan_windows(
        start_ms=118_000, end_ms=130_000, policy=policy, duration_ms=120_000
    )
    assert all(item.start_ms + item.output_ms <= 120_000 for item in clipped)
    assert schedule_rescan_windows(start_ms=5, end_ms=5, policy=policy, duration_ms=10) == ()


def test_budget_accepts_by_priority_and_defers_the_rest() -> None:
    requests = [
        _request("edge", 0, 60_000),
        _request("gap", 60_000, 120_000),
        _request("long_episode", 0, 120_000),
    ]
    generous = plan_within_budget(requests, duration_ms=120_000, budget_windows=10_000)
    assert len(generous.accepted) == 3
    assert not generous.exhausted

    tight = plan_within_budget(requests, duration_ms=120_000, budget_windows=16)
    assert [item.trigger for item in tight.accepted] == ["gap"]
    assert {item.trigger for item in tight.deferred} == {"edge", "long_episode"}
    assert tight.exhausted
    assert tight.planned_windows <= 16

    starved = plan_within_budget(requests, duration_ms=120_000, budget_windows=0)
    assert starved.accepted == ()
    assert len(starved.deferred) == 3


def test_budget_counts_every_transform_hypothesis_of_a_rescan_window() -> None:
    single = _request("edge", 0, 60_000, transforms=1)
    grid = _request("edge", 0, 60_000, transforms=13)
    one = plan_within_budget([single], duration_ms=60_000, budget_windows=10_000)
    many = plan_within_budget([grid], duration_ms=60_000, budget_windows=10_000)
    assert many.planned_windows == one.planned_windows * 13


def _tone(seconds: float, hz: float, sample_rate: int = 16_000) -> np.ndarray:
    time = np.arange(int(seconds * sample_rate)) / sample_rate
    return 0.2 * np.sin(2 * np.pi * hz * time)


def test_spectral_novelty_finds_a_known_change_point_and_ignores_a_steady_tone() -> None:
    steady = _tone(20, 440)
    assert flux_change_points(spectral_flux(log_mel_frames(steady))) == ()

    changed = np.concatenate([_tone(10, 440), _tone(10, 3_000)])
    events = flux_change_points(spectral_flux(log_mel_frames(changed)))
    assert len(events) == 1
    assert abs(events[0].at_ms - 10_000) <= 200
    assert events[0].z_e4 > 30_000


def test_novelty_threshold_and_separation_are_configurable() -> None:
    signal = np.concatenate([_tone(5, 440), _tone(5, 3_000), _tone(5, 440), _tone(5, 3_000)])
    flux = spectral_flux(log_mel_frames(signal))
    dense = flux_change_points(flux, min_separation_ms=1_000)
    sparse = flux_change_points(flux, min_separation_ms=20_000)
    assert len(dense) >= len(sparse)
    # The threshold is a real gate: raising it past the strongest observed peak silences it.
    peak = max(item.z_e4 for item in dense)
    assert flux_change_points(flux, z_threshold_e4=peak + 1) == ()


@pytest.mark.parametrize("trigger", sorted(TRIGGER_PRIORITY))
def test_policies_are_stable_and_serialise_as_integers(trigger: str) -> None:
    payload = policy_for_trigger(trigger).model_dump(mode="json")
    assert set(payload) == {"window_ms", "hop_ms", "phase_ms", "transforms"}
    assert all(isinstance(payload[key], int) for key in ("window_ms", "hop_ms", "phase_ms"))
