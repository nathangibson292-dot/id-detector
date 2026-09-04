"""Stage 4b controlled transform-policy and schedule benchmark."""

from __future__ import annotations

import wave
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from id_detector.benchmark.corpus import _controlled_audio, _label_for_candidate, _source_offset
from id_detector.benchmark.scorer import (
    PredictionDocument,
    ScoreState,
    ScoringConfigSnapshot,
    paired_non_inferiority,
    score_corpus_detailed,
    truth_is_frozen_verified,
    work_key,
)
from id_detector.contracts import BenchmarkCost, GroundTruthRecord, TruthWork
from id_detector.fuse.episodes import build_episodes
from id_detector.fuse.identity import build_identity_graph
from id_detector.io import (
    atomic_write_json,
    canonical_json_bytes,
    native_path,
    read_text,
    sha256_file,
)
from id_detector.local_fixture import (
    DECOY_RESIDUAL_RATE_E4,
    recognise_fixture_windows_in_memory,
)
from id_detector.providers.base import DEFAULT_HOP_MS, DEFAULT_PHASE_MS, DEFAULT_WINDOW_MS
from id_detector.recognise import load_provider_configs
from id_detector.semantics import RECORDING_NAMESPACES
from id_detector.windows import (
    DEFAULT_TRANSFORM_GRID,
    TransformGrid,
    WindowSchedule,
    plan_fixture_windows,
    schedule_options,
)

BOOTSTRAP_SEED = 20_260_904
FIXTURE_TOLERANCE_E4 = 300

# (name, numerator, denominator) read from the raw per-set counts. Ratios are never averaged and
# a set whose denominator is zero contributes nothing rather than a spurious 0.
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


@dataclass(frozen=True)
class PolicyRun:
    policy: str
    report: Any
    states: dict[str, ScoreState]
    request_count: int
    logical_trial_count: int
    match_count: int
    false_match_count: int


@dataclass(frozen=True)
class TransformScheduleResult:
    path: Path
    selected_schedule: WindowSchedule


def _truth_files(corpus_dir: Path) -> list[Path]:
    paths = sorted(corpus_dir.rglob("ground_truth.json"))
    if not paths:
        raise ValueError(f"controlled corpus contains no truth: {corpus_dir}")
    return paths


def _prediction_set(
    truth: GroundTruthRecord,
    identities: Any,
    episodes: Any,
) -> dict[str, Any]:
    candidates = {item.canonical_id: item for item in identities.record.candidates}
    scored = []
    for episode in episodes.episodes:
        artist, title = _label_for_candidate(identities.record, episode.candidate_id)
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
    return {"set_id": truth.set_id, "identities": identities.record, "episodes": scored}


def _config(
    truths: list[GroundTruthRecord], schedule: WindowSchedule, policy: str, grid: TransformGrid
) -> ScoringConfigSnapshot:
    run_config = {
        "set_ids": sorted(truth.set_id for truth in truths),
        "providers": [
            {
                "provider": "local_fixture",
                "provider_config_version": "local_fixture-stage4b-rate-tolerance-v1",
                "scan_policy": "validated-source-rate-tolerance",
                "tolerance_e4": FIXTURE_TOLERANCE_E4,
            }
        ],
        "window_schedule": {
            "window_ms": schedule.window_ms,
            "hop_ms": schedule.hop_ms,
            "phase_ms": schedule.phase_ms,
            "end_anchored_tail": True,
            "short_input": True,
        },
        "transforms": {
            "policy": policy,
            "rate_e4": list(grid.rates_e4),
            "semitones": list(grid.semitones),
        },
        "fuser": {
            "policy_version": "stage4b-one-hypothesis-per-logical-trial-v1",
            "generation": 0,
            "badge_rule": "rev5.1",
        },
        "source_validation": {"frozen_manifest": True, "media_sha256": True},
    }
    return ScoringConfigSnapshot(
        schema_version="1.0.0",
        config_version="stage4b-transforms-schedule-v1",
        profile=f"stage4b-{policy}",
        bootstrap_seed=BOOTSTRAP_SEED,
        certification_targets=[],
        run_config=run_config,
    )


def _run_policy(
    *,
    corpus_dir: Path,
    truths: list[GroundTruthRecord],
    project_root: Path,
    work_root: Path,
    schedule: WindowSchedule,
    policy: str,
    grid: TransformGrid,
) -> PolicyRun:
    prediction_sets: list[dict[str, Any]] = []
    requests = 0
    trials: set[tuple[str, str]] = set()
    matches = 0
    false_matches = 0
    for truth in truths:
        windows = plan_fixture_windows(
            media_key=truth.source.media_key,
            duration_ms=truth.source.duration_ms,
            schedule=schedule,
            transform_policy=policy,  # type: ignore[arg-type]
            transform_grid=grid,
        )
        requests += len(windows)
        trials.update((truth.set_id, window.logical_trial_id) for window in windows)
        observations = recognise_fixture_windows_in_memory(
            media_key=truth.source.media_key,
            truth=truth,
            windows=windows,
            source_offset_ms=_source_offset(project_root, truth.set_id),
            rate_tolerance_e4=FIXTURE_TOLERANCE_E4,
        )
        matched = [item for item in observations if item.status == "match"]
        matches += len(matched)
        # A false match is a returned identity that names no work in this set's truth. The test
        # is on the label, not on bookkeeping, so any wrong identity the recogniser can produce
        # (today: the catalogue's rate-edit decoy) is counted.
        truth_works = {work_key(episode.work) for episode in truth.episodes}
        false_matches += sum(
            work_key(
                TruthWork(artist=item.raw_label.artist or "", title=item.raw_label.title or "")
            )
            not in truth_works
            for item in matched
        )
        identity = build_identity_graph(truth.source.media_key, observations)
        episodes, _ = build_episodes(
            media_key=truth.source.media_key,
            duration_ms=truth.source.duration_ms,
            observations=observations,
            windows=windows,
            identity=identity,
            generation=0,
            profile=f"stage4b-{policy}",
        )
        prediction_sets.append(_prediction_set(truth, identity, episodes))

    config = _config(truths, schedule, policy, grid)
    config_hash = sha256(canonical_json_bytes(config)).hexdigest()
    document = PredictionDocument(
        corpus_version=truths[0].corpus_version,
        profile=f"stage4b-{policy}",
        config_hash=config_hash,
        config_snapshot=config,
        sets=sorted(prediction_sets, key=lambda item: item["set_id"]),
        engines=[],
        cost=BenchmarkCost(
            requests=requests,
            physical_attempts=0,
            billable_seconds=0,
            usd_e2=0,
            wall_ms=0,
        ),
        unverified_seed_comparison=False,
    )
    label = f"w{schedule.window_ms}-h{schedule.hop_ms}-p{schedule.phase_ms}-{policy}"
    prediction_path = work_root / f"predictions-{label}.json"
    atomic_write_json(prediction_path, document)
    report, scores = score_corpus_detailed(corpus_dir, prediction_path)
    return PolicyRun(
        policy=policy,
        report=report,
        states={score.truth.set_id: score.state for score in scores},
        request_count=requests,
        logical_trial_count=len(trials),
        match_count=matches,
        false_match_count=false_matches,
    )


def _metric_pairs(run: PolicyRun, numerator: Any, denominator: Any) -> dict[str, tuple[int, int]]:
    """Return raw ``(numerator, denominator)`` counts per set, dropping empty denominators."""

    pairs: dict[str, tuple[int, int]] = {}
    for set_id, state in run.states.items():
        total = int(denominator(state))
        if total > 0:
            pairs[set_id] = (int(numerator(state)), total)
    return pairs


def _paired_deltas(off: PolicyRun, global_run: PolicyRun, *, seed: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for index, (name, numerator, denominator) in enumerate(_PAIRED_METRICS):
        baseline = _metric_pairs(off, numerator, denominator)
        challenger = _metric_pairs(global_run, numerator, denominator)
        shared = sorted(set(baseline) & set(challenger))
        if not shared:
            result[name] = {
                "delta_e4": None,
                "lower_bound_e4": None,
                "margin_e4": 0,
                "pass": False,
                "n_sets": 0,
                "note": "no set has a non-zero denominator under both policies",
            }
            continue
        result[name] = paired_non_inferiority(
            baseline,
            challenger,
            seed=seed + index,
            margin_e4=0,
        )
        result[name]["n_sets_excluded_zero_denominator"] = len(
            set(off.states) | set(global_run.states)
        ) - len(shared)
    return result


def _run_summary(run: PolicyRun) -> dict[str, Any]:
    metrics = run.report.overall
    return {
        "logical_trials": run.logical_trial_count,
        "requests": run.request_count,
        "hypotheses_per_trial_e4": (
            run.request_count * 10_000 // run.logical_trial_count if run.logical_trial_count else 0
        ),
        "matched_hypotheses": run.match_count,
        "false_matched_hypotheses": run.false_match_count,
        "hypothesis_false_match_rate_e4": (
            run.false_match_count * 10_000 // run.match_count if run.match_count else 0
        ),
        "episode_false_discovery_rate_e4": metrics.false_discovery_rate_e4,
        "work_precision_e4": metrics.identification_work.precision_e4,
        "work_recall_e4": metrics.identification_work.recall_e4,
        "segment_precision_e4": metrics.segment_micro.precision_e4,
        "segment_recall_e4": metrics.segment_micro.recall_e4,
    }


def _accuracy(row: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        row["global"]["work_recall_e4"],
        row["global"]["segment_recall_e4"],
        row["global"]["work_precision_e4"],
        row["global"]["segment_precision_e4"],
    )


def _best_schedule(rows: list[dict[str, Any]], *, window_ms: int | None) -> WindowSchedule:
    """Rank schedules: coverage-complete at the *active* L_min, then accuracy, then cost.

    rev 5.2: only the active measured ``L_min`` gates completeness. Superseded measurements are
    reported as columns and must not eliminate a schedule. ``window_ms`` optionally pins the
    window length so that only the hop and phase vary.
    """

    pool = [row for row in rows if window_ms is None or row["window_ms"] == window_ms]
    robust = [row for row in pool if row["coverage"]["complete_at_active_l"]] or pool
    best_accuracy = max(_accuracy(row) for row in robust)
    eligible = [row for row in robust if _accuracy(row) == best_accuracy]
    chosen = min(
        eligible,
        key=lambda row: (
            row["global"]["requests"],
            row["phase_ms"] != 0,
            -row["window_ms"],
            row["hop_ms"],
        ),
    )
    return WindowSchedule(
        window_ms=chosen["window_ms"],
        hop_ms=chosen["hop_ms"],
        phase_ms=chosen["phase_ms"],
    )


def run_transform_schedule_benchmark(
    *,
    corpus_version: str,
    out_path: Path,
    project_root: Path,
    work_root: Path,
) -> TransformScheduleResult:
    corpus_dir = project_root / "data" / "corpus" / corpus_version
    truths = [
        GroundTruthRecord.model_validate_json(read_text(path)) for path in _truth_files(corpus_dir)
    ]
    if any(truth.corpus_version != corpus_version for truth in truths):
        raise ValueError("truth corpus_version differs from requested corpus")
    if not truth_is_frozen_verified(corpus_dir, truths):
        raise ValueError("Stage 4b benchmark requires a frozen controlled corpus")
    for truth in truths:
        audio = _controlled_audio(project_root, truth.set_id)
        if audio is None or sha256_file(audio) != truth.source.media_key:
            raise ValueError(f"validated controlled audio is missing for {truth.set_id}")
        with wave.open(native_path(audio), "rb") as handle:
            duration_ms = handle.getnframes() * 1_000 // handle.getframerate()
        if abs(duration_ms - truth.source.duration_ms) > 1:
            raise ValueError(f"controlled audio duration differs from truth for {truth.set_id}")

    provider_configs = load_provider_configs(project_root)
    active_config = provider_configs[-1]
    superseded = [
        {
            "config": item.version,
            "l_min_ms": item.l_min_ms,
        }
        for item in provider_configs[:-1]
    ]
    active_l_ms = int((active_config.l_min_ms or {}).get("p50", 0))
    reported_l_ms = sorted(
        {active_l_ms} | {int((item["l_min_ms"] or {}).get("p50", 0)) for item in superseded} - {0}
    )

    grid = DEFAULT_TRANSFORM_GRID
    rows: list[dict[str, Any]] = []
    for index, schedule in enumerate(schedule_options()):
        off = _run_policy(
            corpus_dir=corpus_dir,
            truths=truths,
            project_root=project_root,
            work_root=work_root,
            schedule=schedule,
            policy="off",
            grid=grid,
        )
        global_run = _run_policy(
            corpus_dir=corpus_dir,
            truths=truths,
            project_root=project_root,
            work_root=work_root,
            schedule=schedule,
            policy="global",
            grid=grid,
        )
        rows.append(
            {
                "window_ms": schedule.window_ms,
                "hop_ms": schedule.hop_ms,
                "phase_ms": schedule.phase_ms,
                "coverage": {
                    "active_l_ms": active_l_ms,
                    "complete_at_active_l": schedule.coverage_complete(active_l_ms),
                    "reported": [
                        {
                            "l_ms": value,
                            "complete": schedule.coverage_complete(value),
                            "gates_selection": value == active_l_ms,
                        }
                        for value in reported_l_ms
                    ],
                },
                "off": _run_summary(off),
                "global": _run_summary(global_run),
                "global_minus_off": _paired_deltas(
                    off, global_run, seed=BOOTSTRAP_SEED + index * 10
                ),
            }
        )

    # The rescan policy varies only the hop and phase: keeping the production window length fixed
    # is what makes a rescan's support intervals — and therefore T_ind — comparable with the
    # generation-0 evidence it is meant to extend. The unconstrained ranking is reported too, so
    # the shorter-window option is visible rather than silently discarded.
    rescan = _best_schedule(rows, window_ms=DEFAULT_WINDOW_MS)
    unconstrained = _best_schedule(rows, window_ms=None)
    payload = {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "benchmark": "stage-4b-transforms-schedule",
        "corpus_version": corpus_version,
        "bootstrap": {
            "method": "paired-one-sided-95-percent-set-cluster",
            "replicates": 2_000,
            "seed": BOOTSTRAP_SEED,
            "statistic": "difference of pooled raw-count ratios over resampled sets",
            "zero_denominator_sets": "excluded per metric, never scored as 0",
        },
        "recogniser": {
            "provider": "local_fixture",
            "behavior": "match-only-after-undo-within-temporal-and-frequency-tolerance",
            "tolerance_e4": FIXTURE_TOLERANCE_E4,
            "decoy": {
                "identity": "one rate-edit bootleg release next to the controlled truth",
                "residual_rate_e4": DECOY_RESIDUAL_RATE_E4,
                "rule": (
                    "a hypothesis whose residual temporal and frequency rate both land within "
                    "tolerance of the decoy ratio returns the decoy identity instead of the "
                    "true recording"
                ),
                "caveat": (
                    "the resulting false-match counts measure this fixture catalogue only; they "
                    "are not an estimate of open-world false-match risk"
                ),
            },
        },
        "provider_measurements": {
            "active_config": active_config.version,
            "active_l_min_ms": active_config.l_min_ms,
            "active_l_ms_used_as_gate": active_l_ms,
            "superseded_configs": superseded,
            "coverage_reported_for_l_ms": reported_l_ms,
            "gate_rule": (
                "rev 5.2: only the active measured L_min gates coverage-completeness; superseded "
                "configurations are loaded from provider_configs/ and reported as columns only"
            ),
        },
        "grid": {
            "rate_e4": list(grid.rates_e4),
            "types": ["resample", "tempo"],
            "semitones": list(grid.semitones),
            "hypotheses_with_none": len(grid.hypotheses()),
            "provenance": (
                "fixed by the plan; the controlled corpus is rendered at exactly these factors, "
                "so this benchmark measures the grid's cost and benefit and cannot select it"
            ),
        },
        "schedules": rows,
        "selected_defaults": {
            "generation_zero_schedule": {
                "window_ms": DEFAULT_WINDOW_MS,
                "hop_ms": DEFAULT_HOP_MS,
                "phase_ms": DEFAULT_PHASE_MS,
            },
            "rescan_policy": {
                "window_ms": rescan.window_ms,
                "hop_ms": rescan.hop_ms,
                "phase_ms": rescan.phase_ms,
            },
            "best_schedule_any_window_length": {
                "window_ms": unconstrained.window_ms,
                "hop_ms": unconstrained.hop_ms,
                "phase_ms": unconstrained.phase_ms,
            },
            "transforms_policy": "rescan_only",
            "grid": {
                "rate_e4": list(grid.rates_e4),
                "types": ["resample", "tempo"],
                "semitones": list(grid.semitones),
            },
            "decision": (
                "generation 0 keeps the plan's 12 s / 9 s schedule, which is coverage-complete at "
                "the active measured L_min and matches the hop the provisional tier thresholds "
                "were calibrated against; the rescan policy is the best coverage-complete "
                "schedule at the production window length, so that only hop and phase change and "
                "rescan supports stay comparable with generation-0 supports; the best schedule "
                "over all window lengths is reported separately; the transform grid is fixed by "
                "the plan and is retained for rescans because global raises controlled transform "
                "recall but multiplies hypotheses and their opportunities to match a wrong "
                "catalogue entry"
            ),
        },
    }
    atomic_write_json(out_path, payload)
    return TransformScheduleResult(path=out_path, selected_schedule=rescan)
