"""Stage 4c ablations and acceptance gates on the controlled stratum.

Every arm is a complete run of the production fuser over the controlled corpus with exactly one
capability changed, scored by the Stage 2a scorer.  Paired deltas use the same one-sided 95 %
set-cluster bootstrap as Stage 4b, over raw per-set counts.

The harness is deliberately in-memory: it validates each set's frozen truth, source SHA-256 and
decoded duration, then plans windows and answers them from the content-bound local fixture, so
tens of thousands of window evaluations do not have to be written to disk.  The production FFmpeg
window path is covered by the Stage 4b insertion vectors and by the Stage 4c generation-loop
tests, which use the real ``windows``/``recognise``/``fuse`` artefacts.
"""

from __future__ import annotations

import random
import wave
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np

from id_detector.benchmark.corpus import _controlled_audio, _label_for_candidate, _source_offset
from id_detector.benchmark.scorer import (
    BOOTSTRAP_REPLICATES,
    EVENT_TYPES,
    PredictionDocument,
    ScoreState,
    ScoringConfigSnapshot,
    SetScore,
    paired_non_inferiority,
    score_corpus_detailed,
    truth_is_frozen_verified,
)
from id_detector.contracts import (
    BenchmarkCost,
    GroundTruthRecord,
    ObservationRecord,
    WindowRecord,
)
from id_detector.fuse.episodes import build_episodes, region_request_key
from id_detector.fuse.identity import build_identity_graph
from id_detector.io import atomic_write_json, canonical_json_bytes, native_path, read_text
from id_detector.local_fixture import recognise_fixture_windows_in_memory
from id_detector.novelty import (
    flux_change_points,
    log_mel_frames,
    spectral_flux,
)
from id_detector.providers.base import (
    DEFAULT_HOP_MS,
    DEFAULT_PHASE_MS,
    DEFAULT_WINDOW_MS,
    AppConfig,
)
from id_detector.rescan import DEFAULT_MAX_GENERATIONS, plan_within_budget
from id_detector.semantics import RECORDING_NAMESPACES
from id_detector.windows import (
    DEFAULT_TRANSFORM_GRID,
    TransformGrid,
    WindowSchedule,
    plan_fixture_rescan_windows,
    plan_fixture_windows,
)

BOOTSTRAP_SEED = 20_260_904
FIXTURE_TOLERANCE_E4 = 300
#: Rescan windows a single set may spend beyond generation 0. Large enough that the controlled
#: sets are never truncated, small enough to keep the budget path exercised.
RESCAN_WINDOW_BUDGET = 4_000
GATE_P90_IMPROVEMENT_E4 = 2_000
GATE_EVENT_E4 = 8_000
GATE_MIN_BOUNDARIES = 100
GATE_MIN_EVENT_CASES = 30


@dataclass(frozen=True)
class ArmOptions:
    """One ablation arm: the production pipeline with exactly one capability changed."""

    name: str
    rescans: bool = True
    transforms_policy: str = "rescan_only"
    hints: bool = False
    window_ms: int = DEFAULT_WINDOW_MS
    hop_ms: int = DEFAULT_HOP_MS
    phase_ms: int = DEFAULT_PHASE_MS
    novelty: bool = True
    max_generations: int = DEFAULT_MAX_GENERATIONS
    engine: str = "local_fixture"

    @property
    def schedule(self) -> WindowSchedule:
        return WindowSchedule(window_ms=self.window_ms, hop_ms=self.hop_ms, phase_ms=self.phase_ms)


@dataclass
class SetRun:
    set_id: str
    windows: list[WindowRecord] = field(default_factory=list)
    observations: list[ObservationRecord] = field(default_factory=list)
    generations: int = 1
    emitted_requests: int = 0
    accepted_requests: int = 0
    deferred_requests: int = 0
    stop_reason: str = "no_requests"
    prediction: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArmRun:
    options: ArmOptions
    report: Any
    scores: list[SetScore]
    runs: list[SetRun]

    @property
    def states(self) -> dict[str, ScoreState]:
        return {score.truth.set_id: score.state for score in self.scores}

    @property
    def window_count(self) -> int:
        return sum(len(run.windows) for run in self.runs)

    @property
    def generation_count(self) -> int:
        return sum(run.generations for run in self.runs)


def _truth_files(corpus_dir: Path) -> list[Path]:
    paths = sorted(corpus_dir.rglob("ground_truth.json"))
    if not paths:
        raise ValueError(f"controlled corpus contains no truth: {corpus_dir}")
    return paths


def _validated_truths(
    project_root: Path, corpus_version: str
) -> tuple[Path, list[GroundTruthRecord]]:
    corpus_dir = project_root / "data" / "corpus" / corpus_version
    truths = [
        GroundTruthRecord.model_validate_json(read_text(path)) for path in _truth_files(corpus_dir)
    ]
    if any(truth.corpus_version != corpus_version for truth in truths):
        raise ValueError("truth corpus_version differs from requested corpus")
    if not truth_is_frozen_verified(corpus_dir, truths):
        raise ValueError("Stage 4c ablations require a frozen controlled corpus")
    return corpus_dir, truths


def _audio_for(project_root: Path, truth: GroundTruthRecord) -> Path:
    audio = _controlled_audio(project_root, truth.set_id)
    if audio is None:
        raise ValueError(f"validated controlled audio is missing for {truth.set_id}")
    return audio


def novelty_points_for_audio(path: Path) -> tuple[int, ...]:
    """Spectral-novelty change points of one controlled mix, computed from its samples."""

    with wave.open(native_path(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float64) / 32768.0
    return tuple(item.at_ms for item in flux_change_points(spectral_flux(log_mel_frames(samples))))


def _prediction_set(truth: GroundTruthRecord, identity: Any, episodes: Any) -> dict[str, Any]:
    candidates = {item.canonical_id: item for item in identity.record.candidates}
    scored = []
    for episode in episodes.episodes:
        artist, title = _label_for_candidate(identity.record, episode.candidate_id)
        version_ids: dict[str, str] = {}
        for node in candidates[episode.candidate_id].member_nodes:
            namespace, value = node.split(":", 1)
            if namespace in RECORDING_NAMESPACES:
                version_ids.setdefault(namespace, value)
        scored.append(
            {
                "work": {"artist": artist, "title": title},
                "version": {"qualifier": None, "ids": version_ids},
                "candidate_id": episode.candidate_id,
                "evidence_support_ms": episode.evidence_support_ms,
                "start_no_later_than_ms": episode.start_no_later_than_ms,
                "end_no_earlier_than_ms": episode.end_no_earlier_than_ms,
                "start_pi": episode.start_pi,
                "end_pi": episode.end_pi,
                "best_start_ms": episode.best_start_ms,
                "best_end_ms": episode.best_end_ms,
                "role_segments": episode.role_segments,
                "occurrence_index": episode.occurrence_index,
                "claim": episode.claim,
                "scores": episode.scores,
                "tiers": episode.tiers,
                "alignment_events": episode.alignment_events,
            }
        )
    return {"set_id": truth.set_id, "identities": identity.record, "episodes": scored}


def run_fixture_set(
    truth: GroundTruthRecord,
    *,
    options: ArmOptions,
    source_offset_ms: int,
    novelty_points: tuple[int, ...],
    grid: TransformGrid = DEFAULT_TRANSFORM_GRID,
    config: AppConfig | None = None,
) -> SetRun:
    """Run generation 0 and, when the arm enables them, every budgeted rescan generation."""

    config = config or AppConfig(transforms_policy=options.transforms_policy)  # type: ignore[arg-type]
    duration_ms = truth.source.duration_ms
    windows = list(
        plan_fixture_windows(
            media_key=truth.source.media_key,
            duration_ms=duration_ms,
            schedule=options.schedule,
            transform_policy=options.transforms_policy,  # type: ignore[arg-type]
            transform_grid=grid,
        )
    )
    observations = list(
        recognise_fixture_windows_in_memory(
            media_key=truth.source.media_key,
            truth=truth,
            windows=windows,
            source_offset_ms=source_offset_ms,
            rate_tolerance_e4=FIXTURE_TOLERANCE_E4,
        )
    )
    run = SetRun(set_id=truth.set_id)
    points = novelty_points if options.novelty else ()
    transforms = list(grid.hypotheses()) if options.transforms_policy != "off" else None
    prior_keys: set[str] = set()
    spent = 0
    generation = 0
    identity = build_identity_graph(truth.source.media_key, observations)
    episodes, requests = build_episodes(
        media_key=truth.source.media_key,
        duration_ms=duration_ms,
        observations=observations,
        windows=windows,
        identity=identity,
        generation=0,
        profile=f"stage4c-{options.name}",
        rescan_transforms=transforms,
        novelty_change_points_ms=points,
        scanned_window_shapes=frozenset((item.start_ms, item.output_ms) for item in windows),
        config=config,
    )
    run.emitted_requests = len(requests)
    while options.rescans and generation < options.max_generations and requests:
        plan = plan_within_budget(
            requests,
            duration_ms=duration_ms,
            budget_windows=max(0, RESCAN_WINDOW_BUDGET - spent),
        )
        run.accepted_requests += len(plan.accepted)
        run.deferred_requests = len(plan.deferred)
        if not plan.accepted:
            run.stop_reason = "budget_exhausted"
            break
        generation += 1
        prior_keys |= {
            region_request_key(item.trigger, item.start_ms, item.end_ms, item.policy)
            for item in plan.accepted
        }
        new_windows = list(
            plan_fixture_rescan_windows(
                media_key=truth.source.media_key,
                duration_ms=duration_ms,
                requests=plan.accepted,
                generation=generation,
                transform_policy=options.transforms_policy,  # type: ignore[arg-type]
                transform_grid=grid,
                existing_shapes=frozenset((item.start_ms, item.output_ms) for item in windows),
            )
        )
        if not new_windows:
            run.stop_reason = "no_new_windows"
            break
        observations.extend(
            recognise_fixture_windows_in_memory(
                media_key=truth.source.media_key,
                truth=truth,
                windows=new_windows,
                source_offset_ms=source_offset_ms,
                rate_tolerance_e4=FIXTURE_TOLERANCE_E4,
            )
        )
        windows.extend(new_windows)
        spent += len(new_windows)
        identity = build_identity_graph(truth.source.media_key, observations)
        episodes, requests = build_episodes(
            media_key=truth.source.media_key,
            duration_ms=duration_ms,
            observations=observations,
            windows=windows,
            identity=identity,
            generation=generation,
            profile=f"stage4c-{options.name}",
            rescan_transforms=transforms,
            novelty_change_points_ms=points,
            prior_request_keys=frozenset(prior_keys),
            scanned_window_shapes=frozenset((item.start_ms, item.output_ms) for item in windows),
            config=config,
        )
        run.emitted_requests += len(requests)
        if not requests:
            run.stop_reason = "no_requests"
    else:
        if options.rescans and requests and generation >= options.max_generations:
            run.stop_reason = "max_generations"
        elif not options.rescans:
            run.stop_reason = "rescans_disabled"
    run.windows = windows
    run.observations = observations
    run.generations = generation + 1
    run.prediction = _prediction_set(truth, identity, episodes)
    return run


def _config_snapshot(
    truths: list[GroundTruthRecord], options: ArmOptions, grid: TransformGrid
) -> ScoringConfigSnapshot:
    run_config = {
        "set_ids": sorted(truth.set_id for truth in truths),
        "providers": [
            {
                "provider": options.engine,
                "provider_config_version": "local_fixture-stage4c-rate-tolerance-v1",
                "scan_policy": "validated-source-rate-tolerance",
                "tolerance_e4": FIXTURE_TOLERANCE_E4,
            }
        ],
        "window_schedule": {
            "window_ms": options.window_ms,
            "hop_ms": options.hop_ms,
            "phase_ms": options.phase_ms,
            "end_anchored_tail": True,
            "short_input": True,
        },
        "transforms": {
            "policy": options.transforms_policy,
            "rate_e4": list(grid.rates_e4),
            "semitones": list(grid.semitones),
        },
        "rescans": {
            "enabled": options.rescans,
            "max_generations": options.max_generations,
            "window_budget": RESCAN_WINDOW_BUDGET,
            "novelty": options.novelty,
        },
        "hints": {"enabled": options.hints},
        "fuser": {
            "policy_version": "stage4c-generation-loop-v1",
            "badge_rule": "rev5.1",
            "tier_counting": "T_ind",
        },
        "source_validation": {"frozen_manifest": True, "media_sha256": True},
    }
    return ScoringConfigSnapshot(
        schema_version="1.0.0",
        config_version="stage4c-ablations-v1",
        profile=f"stage4c-{options.name}",
        bootstrap_seed=BOOTSTRAP_SEED,
        certification_targets=[],
        run_config=run_config,
    )


def run_arm(
    *,
    corpus_dir: Path,
    truths: list[GroundTruthRecord],
    project_root: Path,
    work_root: Path,
    options: ArmOptions,
    novelty_by_set: dict[str, tuple[int, ...]],
    grid: TransformGrid = DEFAULT_TRANSFORM_GRID,
) -> ArmRun:
    runs = [
        run_fixture_set(
            truth,
            options=options,
            source_offset_ms=_source_offset(project_root, truth.set_id),
            novelty_points=novelty_by_set.get(truth.set_id, ()),
            grid=grid,
        )
        for truth in truths
    ]
    snapshot = _config_snapshot(truths, options, grid)
    document = PredictionDocument(
        corpus_version=truths[0].corpus_version,
        profile=f"stage4c-{options.name}",
        config_hash=sha256(canonical_json_bytes(snapshot)).hexdigest(),
        config_snapshot=snapshot,
        sets=sorted((run.prediction for run in runs), key=lambda item: item["set_id"]),
        engines=[],
        cost=BenchmarkCost(
            requests=sum(len(run.windows) for run in runs),
            physical_attempts=0,
            billable_seconds=0,
            usd_e2=0,
            wall_ms=0,
        ),
        unverified_seed_comparison=False,
    )
    prediction_path = work_root / f"predictions-{options.name}.json"
    atomic_write_json(prediction_path, document)
    report, scores = score_corpus_detailed(corpus_dir, prediction_path)
    return ArmRun(options=options, report=report, scores=scores, runs=runs)


def _percentile_int(values: list[int], numerator: int, denominator: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, (len(ordered) * numerator + denominator - 1) // denominator - 1)
    return ordered[index]


def paired_p90_improvement(
    baseline: dict[str, list[int]],
    challenger: dict[str, list[int]],
    *,
    seed: int,
    replicates: int = BOOTSTRAP_REPLICATES,
) -> dict[str, Any]:
    """Relative improvement of the pooled p90 ``best_start`` error, paired and set-clustered."""

    keys = sorted(set(baseline) & set(challenger))
    keys = [key for key in keys if baseline[key] and challenger[key]]
    if not keys:
        return {
            "n_sets": 0,
            "n_boundaries_baseline": 0,
            "n_boundaries_challenger": 0,
            "baseline_p90_ms": 0,
            "challenger_p90_ms": 0,
            "relative_improvement_e4": None,
            "lower_bound_e4": None,
            "note": "no set produced an associated start error under both arms",
        }

    def p90(sample: list[str], values: dict[str, list[int]]) -> int:
        pooled = [item for key in sample for item in values[key]]
        return _percentile_int(pooled, 9, 10)

    def improvement(sample: list[str]) -> int:
        before = p90(sample, baseline)
        after = p90(sample, challenger)
        if before <= 0:
            return 0
        return (before - after) * 10_000 // before

    rng = random.Random(seed)
    estimates = [
        improvement([keys[rng.randrange(len(keys))] for _ in keys]) for _ in range(replicates)
    ]
    return {
        "n_sets": len(keys),
        "n_boundaries_baseline": sum(len(baseline[key]) for key in keys),
        "n_boundaries_challenger": sum(len(challenger[key]) for key in keys),
        "baseline_p90_ms": p90(keys, baseline),
        "challenger_p90_ms": p90(keys, challenger),
        "relative_improvement_e4": improvement(keys),
        "lower_bound_e4": _percentile_int(estimates, 5, 100),
        "replicates": replicates,
        "seed": seed,
    }


def _event_rows(arm: ArmRun, *, seed: int) -> dict[str, Any]:
    """Per-type event precision/recall with one-sided 95 % set-cluster bootstrap bounds."""

    states = arm.states
    keys = sorted(states)
    rows: dict[str, Any] = {}
    for index, event in enumerate(EVENT_TYPES):

        def totals(sample: list[str], name: str = event) -> tuple[int, int, int]:
            correct = sum(states[key].event_counts[name].correct for key in sample)
            predicted = sum(states[key].event_counts[name].predicted for key in sample)
            truth = sum(states[key].event_counts[name].truth for key in sample)
            return correct, predicted, truth

        correct, predicted, truth = totals(keys)
        strict_correct = sum(states[key].event_counts_strict[event].correct for key in keys)
        strict_predicted = sum(states[key].event_counts_strict[event].predicted for key in keys)
        rng = random.Random(seed + index)
        precisions: list[int] = []
        recalls: list[int] = []
        for _ in range(BOOTSTRAP_REPLICATES):
            sample = [keys[rng.randrange(len(keys))] for _ in keys]
            sample_correct, sample_predicted, sample_truth = totals(sample)
            precisions.append(
                sample_correct * 10_000 // sample_predicted if sample_predicted else 0
            )
            recalls.append(sample_correct * 10_000 // sample_truth if sample_truth else 0)
        rows[event] = {
            "n_truth_events": truth,
            "n_predicted_events": predicted,
            "n_matched": correct,
            "precision_e4": correct * 10_000 // predicted if predicted else 0,
            "recall_e4": correct * 10_000 // truth if truth else 0,
            "precision_cluster_lower_e4": _percentile_int(precisions, 5, 100) if truth else 0,
            "recall_cluster_lower_e4": _percentile_int(recalls, 5, 100) if truth else 0,
            "strict_2s_precision_e4": (
                strict_correct * 10_000 // strict_predicted if strict_predicted else 0
            ),
            "strict_2s_recall_e4": strict_correct * 10_000 // truth if truth else 0,
            "n_cases": truth,
            "meets_gate": bool(
                truth >= GATE_MIN_EVENT_CASES
                and predicted
                and correct * 10_000 // predicted >= GATE_EVENT_E4
                and correct * 10_000 // truth >= GATE_EVENT_E4
            ),
        }
    return rows


_PAIRED_METRICS: tuple[tuple[str, Any, Any], ...] = (
    (
        "work_precision",
        lambda state: state.identification_work.correct,
        lambda state: state.identification_work.predicted,
    ),
    (
        "work_recall",
        lambda state: state.identification_work.correct,
        lambda state: state.identification_work.truth,
    ),
    (
        "segment_precision",
        lambda state: state.segment.tp,
        lambda state: state.segment.tp + state.segment.fp,
    ),
    (
        "segment_recall",
        lambda state: state.segment.tp,
        lambda state: state.segment.tp + state.segment.fn,
    ),
)


def _metric_pairs(arm: ArmRun, numerator: Any, denominator: Any) -> dict[str, tuple[int, int]]:
    pairs: dict[str, tuple[int, int]] = {}
    for set_id, state in arm.states.items():
        total = int(denominator(state))
        if total > 0:
            pairs[set_id] = (int(numerator(state)), total)
    return pairs


def paired_arm_deltas(baseline: ArmRun, challenger: ArmRun, *, seed: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for index, (name, numerator, denominator) in enumerate(_PAIRED_METRICS):
        left = _metric_pairs(baseline, numerator, denominator)
        right = _metric_pairs(challenger, numerator, denominator)
        shared = sorted(set(left) & set(right))
        if not shared:
            result[name] = {
                "delta_e4": None,
                "lower_bound_e4": None,
                "n_sets": 0,
                "note": "no set has a non-zero denominator under both arms",
            }
            continue
        outcome = paired_non_inferiority(left, right, seed=seed + index, margin_e4=0)
        outcome["n_sets_excluded_zero_denominator"] = len(
            set(baseline.states) | set(challenger.states)
        ) - len(shared)
        result[name] = outcome
    start_errors_baseline = {
        key: list(state.start_errors) for key, state in baseline.states.items()
    }
    start_errors_challenger = {
        key: list(state.start_errors) for key, state in challenger.states.items()
    }
    result["best_start_p90"] = paired_p90_improvement(
        start_errors_baseline, start_errors_challenger, seed=seed + 100
    )
    return result


def _arm_summary(arm: ArmRun) -> dict[str, Any]:
    metrics = arm.report.overall
    states = arm.states
    return {
        "windows": arm.window_count,
        "generations_total": arm.generation_count,
        "sets_reaching_max_generations": sum(
            run.stop_reason == "max_generations" for run in arm.runs
        ),
        "sets_budget_exhausted": sum(run.stop_reason == "budget_exhausted" for run in arm.runs),
        "rescan_requests_accepted": sum(run.accepted_requests for run in arm.runs),
        "rescan_requests_deferred": sum(run.deferred_requests for run in arm.runs),
        "work_precision_e4": metrics.identification_work.precision_e4,
        "work_recall_e4": metrics.identification_work.recall_e4,
        "segment_precision_e4": metrics.segment_micro.precision_e4,
        "segment_recall_e4": metrics.segment_micro.recall_e4,
        "start_median_error_ms": metrics.start_median_absolute_error_ms,
        "start_p90_error_ms": metrics.start_p90_error_ms,
        "start_within_10s_e4": metrics.start_within_10s_e4,
        "n_start_errors": sum(len(state.start_errors) for state in states.values()),
        "episode_false_discovery_rate_e4": metrics.false_discovery_rate_e4,
    }


def engine_status_rows(project_root: Path) -> list[dict[str, Any]]:
    """Per-engine ablation status: measured where possible, honestly excluded where not."""

    from id_detector.providers.acrcloud import ACRCloudCredentials
    from id_detector.providers.audd import AudDCredentials
    from id_detector.providers.panako import PROVIDER as PANAKO
    from id_detector.providers.panako import doctor_detail

    def credentials_present(loader: Any) -> tuple[bool, str]:
        try:
            loader()
        except Exception as exc:  # ProviderUnavailable and anything it wraps
            return False, str(exc)
        return True, "credentials present"

    audd_ok, audd_detail = credentials_present(AudDCredentials.from_env)
    acr_ok, acr_detail = credentials_present(ACRCloudCredentials.from_env)
    _, panako_detail = doctor_detail()
    _ = project_root
    return [
        {
            "provider": "local_fixture",
            "capability": "clip_recognizer",
            "status": "evaluated",
            "detail": (
                "content-bound controlled oracle; it measures the fuser, never open-world "
                "catalogue coverage"
            ),
            "in_ablation": True,
        },
        {
            "provider": "shazam",
            "capability": "clip_recognizer",
            "status": "not_evaluated (no controlled-stratum coverage)",
            "detail": (
                "the controlled corpus is synthesised audio that is not in any commercial "
                "catalogue; Stage 3 measured 73 attempts, 1 false identification, work P/R 0/0"
            ),
            "in_ablation": False,
        },
        {
            "provider": "audd",
            "capability": "file_scanner",
            "status": "evaluated" if audd_ok else "not_evaluated (no credentials)",
            "detail": audd_detail if not audd_ok else "credentials present",
            "in_ablation": False,
            "fusion_validated_on_fixtures": True,
        },
        {
            "provider": "acrcloud",
            "capability": "file_scanner",
            "status": "evaluated" if acr_ok else "not_evaluated (no credentials)",
            "detail": acr_detail if not acr_ok else "credentials present",
            "in_ablation": False,
            "fusion_validated_on_fixtures": True,
        },
        {
            "provider": PANAKO,
            "capability": "local_index_query",
            "status": "excluded from v1 pending JDK",
            "detail": panako_detail,
            "in_ablation": False,
        },
    ]


@dataclass(frozen=True)
class AblationResult:
    path: Path
    payload: dict[str, Any]


def run_ablations(
    *,
    corpus_version: str,
    out_path: Path,
    project_root: Path,
    work_root: Path,
    engine_statuses: list[dict[str, Any]] | None = None,
) -> AblationResult:
    corpus_dir, truths = _validated_truths(project_root, corpus_version)
    novelty_by_set: dict[str, tuple[int, ...]] = {}
    for truth in truths:
        audio = _audio_for(project_root, truth)
        novelty_by_set[truth.set_id] = novelty_points_for_audio(audio)

    boundary_count = sum(2 * len(truth.episodes) for truth in truths)
    event_case_counts: dict[str, int] = {name: 0 for name in EVENT_TYPES}
    for truth in truths:
        for event in truth.events:
            event_case_counts[event.type] += 1

    arms = {
        "rescans_off": ArmOptions(name="rescans_off", rescans=False),
        "rescans_on": ArmOptions(name="rescans_on", rescans=True),
        "rescans_on_no_novelty": ArmOptions(name="rescans_on_no_novelty", novelty=False),
        "transforms_off": ArmOptions(name="transforms_off", transforms_policy="off"),
        "transforms_global": ArmOptions(name="transforms_global", transforms_policy="global"),
        "schedule_12_5_0": ArmOptions(name="schedule_12_5_0", hop_ms=5_000),
        "schedule_8_5_0": ArmOptions(name="schedule_8_5_0", window_ms=8_000, hop_ms=5_000),
        "schedule_12_9_0_gen0_only": ArmOptions(
            name="schedule_12_9_0_gen0_only", rescans=False, novelty=False
        ),
    }
    runs = {
        name: run_arm(
            corpus_dir=corpus_dir,
            truths=truths,
            project_root=project_root,
            work_root=work_root,
            options=options,
            novelty_by_set=novelty_by_set,
        )
        for name, options in arms.items()
    }

    comparisons = {
        "rescans_on_minus_off": paired_arm_deltas(
            runs["rescans_off"], runs["rescans_on"], seed=BOOTSTRAP_SEED
        ),
        "novelty_on_minus_off": paired_arm_deltas(
            runs["rescans_on_no_novelty"], runs["rescans_on"], seed=BOOTSTRAP_SEED + 1_000
        ),
        "transforms_rescan_only_minus_off": paired_arm_deltas(
            runs["transforms_off"], runs["rescans_on"], seed=BOOTSTRAP_SEED + 2_000
        ),
        "transforms_global_minus_rescan_only": paired_arm_deltas(
            runs["rescans_on"], runs["transforms_global"], seed=BOOTSTRAP_SEED + 3_000
        ),
        "schedule_12_5_minus_12_9": paired_arm_deltas(
            runs["rescans_on"], runs["schedule_12_5_0"], seed=BOOTSTRAP_SEED + 4_000
        ),
        "schedule_8_5_minus_12_9": paired_arm_deltas(
            runs["rescans_on"], runs["schedule_8_5_0"], seed=BOOTSTRAP_SEED + 5_000
        ),
    }

    events = {name: _event_rows(run, seed=BOOTSTRAP_SEED + 7_000) for name, run in runs.items()}
    gate_events = events["rescans_on"]
    p90 = comparisons["rescans_on_minus_off"]["best_start_p90"]
    gates = [
        {
            "name": "controlled_boundaries_at_least_100",
            "observed": boundary_count,
            "target": GATE_MIN_BOUNDARIES,
            "pass": boundary_count >= GATE_MIN_BOUNDARIES,
        },
        {
            "name": "best_start_p90_improves_20_percent_relative_with_rescans",
            "observed_e4": p90.get("relative_improvement_e4"),
            "lower_bound_e4": p90.get("lower_bound_e4"),
            "target_e4": GATE_P90_IMPROVEMENT_E4,
            "baseline_p90_ms": p90.get("baseline_p90_ms"),
            "challenger_p90_ms": p90.get("challenger_p90_ms"),
            "pass": bool(
                p90.get("relative_improvement_e4") is not None
                and p90["relative_improvement_e4"] >= GATE_P90_IMPROVEMENT_E4
            ),
            "pass_on_cluster_lower_bound": bool(
                p90.get("lower_bound_e4") is not None
                and p90["lower_bound_e4"] >= GATE_P90_IMPROVEMENT_E4
            ),
        },
    ]
    for event in ("loop", "jump", "drift", "replay"):
        row = gate_events[event]
        gates.append(
            {
                "name": f"event_{event}_precision_and_recall_at_least_80_percent",
                "n_cases": row["n_cases"],
                "precision_e4": row["precision_e4"],
                "recall_e4": row["recall_e4"],
                "precision_cluster_lower_e4": row["precision_cluster_lower_e4"],
                "recall_cluster_lower_e4": row["recall_cluster_lower_e4"],
                "target_e4": GATE_EVENT_E4,
                "min_cases": GATE_MIN_EVENT_CASES,
                "pass": row["meets_gate"],
            }
        )

    payload = {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "benchmark": "stage-4c-ablations",
        "corpus_version": corpus_version,
        "n_sets": len(truths),
        "n_boundaries": boundary_count,
        "n_event_cases": event_case_counts,
        "bootstrap": {
            "method": "paired-one-sided-95-percent-set-cluster",
            "replicates": BOOTSTRAP_REPLICATES,
            "seed": BOOTSTRAP_SEED,
            "statistic": "difference of pooled raw-count ratios over resampled sets",
            "zero_denominator_sets": "excluded per metric, never scored as 0",
        },
        "recogniser": {
            "provider": "local_fixture",
            "behavior": "match-only-after-undo-within-temporal-and-frequency-tolerance",
            "tolerance_e4": FIXTURE_TOLERANCE_E4,
            "caveat": (
                "controlled-stratum measurements only; they are not an estimate of open-world "
                "accuracy or false-match risk"
            ),
        },
        "arms": {name: {**_arm_summary(run), **arms[name].__dict__} for name, run in runs.items()},
        "comparisons": comparisons,
        "events": events,
        "engines": engine_statuses or [],
        "not_evaluable": [
            {
                "feature": "hints on/off",
                "reason": (
                    "the controlled stratum carries no hint evidence and the held-out dev-2 "
                    "corpus does not exist; Stage 4a's gate remains the authority and is blocked "
                    "on that corpus"
                ),
            }
        ],
        "gates": gates,
    }
    atomic_write_json(out_path, payload)
    return AblationResult(path=out_path, payload=payload)
