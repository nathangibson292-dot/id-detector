from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from id_detector.benchmark.corpus import _real_analyse_command
from id_detector.calibrate.certify import (
    CorpusNotFrozen,
    DuplicateTestVersion,
    _population_prediction_count,
    registered_targets,
    run_certify,
)
from id_detector.calibrate.features import build_features, work_index
from id_detector.calibrate.model import (
    CalibrationApplier,
    CalibrationExample,
    _pav,
    fit_calibration,
    isotonic_score_e4,
    load_calibration,
)
from id_detector.calibrate.reconstruct import feature_inputs_from_record
from id_detector.calibrate.validate import _split_sets
from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    CalibrationFeatures,
    CalibrationModelRecord,
    ObservationRecord,
    RawLabel,
    compose_natural_key,
    make_id,
)
from id_detector.fuse.episodes import build_episodes, competing_candidate_count
from id_detector.fuse.identity import build_identity_graph
from id_detector.io import atomic_write_json

MEDIA_KEY = "7" * 64


# --------------------------------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------------------------------
def _observation(index: int, *, provider: str, provider_ids: dict[str, str]) -> ObservationRecord:
    start = index * 12_000
    label = RawLabel(artist="Artist", title="Title", album=None, label=None, release_date=None)
    transform = {"type": "none", "rate_e4": 10_000, "semitones": 0}
    natural = {
        "query_id": f"{index + 1:040x}",
        "mix_span_ms": [start, start + 12_000],
        "raw_label_hash": sha256(
            json.dumps(label.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest(),
        "native_index": 0,
        "transform": transform,
    }
    return ObservationRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(MEDIA_KEY, "observation", compose_natural_key("observation", natural)),
        generation=0,
        query_id=f"{index + 1:040x}",
        provider=provider,
        capability="clip_recognizer",
        status="match",
        is_final=True,
        mix_span_ms=(start, start + 12_000),
        support_ms=(start, start + 12_000),
        transform=transform,
        logical_trial_id=f"{100 + index:040x}",
        raw_label=label,
        provider_ids=provider_ids,
        native={"matches": [{"offset_ms": start, "frequencyskew_e6": 0, "timeskew_e6": 0}]},
        anchor={
            "mix_anchor_ms": start,
            "ref_anchor_ms": start,
            "uncertainty_ms": 10,
            "reliable": True,
            "method": "fixture",
            "bias_applied_ms": 0,
        },
        score_raw=None,
        quality=None,
        raw_response_ref=f"fixture/{index}.json",
        source_ids=[f"query:{index + 1:040x}"],
    )


def test_features_are_deterministic_and_integer() -> None:
    from id_detector.calibrate.features import EpisodeFeatureInputs

    votes = (
        _observation(0, provider="shazam", provider_ids={"isrc": "I1"}),
        _observation(1, provider="audd", provider_ids={"isrc": "I1"}),
    )
    inputs = EpisodeFeatureInputs(
        episode_id="a" * 40,
        candidate_id="b" * 40,
        votes=votes,
        has_global_alignment=True,
        span_ms=24_000,
        support_total_ms=24_000,
        recording_supported=True,
        version_ids_count=1,
    )
    first = build_features(inputs)
    second = build_features(inputs)
    assert first == second
    assert first.n_logical_trials == 2
    assert first.n_providers == 2
    # shazam + audd are not both commercial, so the corroborating engine counts at full weight.
    assert first.engine_agreement_e4 == 10_000
    dumped = first.model_dump(mode="json")
    assert all(not isinstance(value, float) for value in dumped.values())


# --------------------------------------------------------------------------------------------------
# Isotonic calibrator
# --------------------------------------------------------------------------------------------------
def test_pav_is_monotone_non_decreasing() -> None:
    points = [(0, 0), (10, 1), (5, 0), (20, 1), (15, 0), (25, 1)]
    bins = _pav(points)
    values = [item.calibrated_e4 for item in bins]
    assert values == sorted(values)
    # Evaluation is a right-continuous step function.
    assert isotonic_score_e4(bins, -1) == 0
    assert isotonic_score_e4(bins, 100) == max(values)


def _feature(
    t_ind_e4: int, span: int, has_global: bool, *, recording: bool = True, competing: bool = False
) -> CalibrationFeatures:
    return CalibrationFeatures(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        episode_id="a" * 40,
        candidate_id="b" * 40,
        t_ind_e4=t_ind_e4,
        n_logical_trials=max(1, t_ind_e4 // 10_000),
        n_selected_observations=max(1, t_ind_e4 // 10_000),
        span_ms=span,
        support_total_ms=span,
        n_alignment_segments=1 if has_global else 0,
        max_residual_ms=200,
        n_alignment_events=0,
        has_global_alignment=has_global,
        n_providers=1,
        engine_agreement_e4=0,
        transform_consistency_e4=10_000,
        n_score_raw=0,
        median_score_raw=None,
        n_provenance_groups=0,
        hint_vote_e4=0,
        competing=competing,
        n_competing_candidates=1 if competing else 0,
        identity_conflicts=0,
        contested=False,
        recording_supported=recording,
        version_ids_count=1 if recording else 0,
        claim="performed",
        heuristic_work_tier="likely",
        heuristic_version_tier="likely",
        heuristic_boundary_tier="possible",
    )


def _examples() -> list[CalibrationExample]:
    examples: list[CalibrationExample] = []
    for _ in range(12):
        examples.append(
            CalibrationExample(
                _feature(50_000, 60_000, True),
                True,
                True,
                True,
                True,
                3_000,
                5_000,
                (1_000, 1_400),
                (4_200, 4_600),
            )
        )
    for _ in range(8):
        examples.append(
            CalibrationExample(
                _feature(10_000, 15_000, False, recording=False),
                False,
                False,
                False,
                False,
                3_000,
                5_000,
                None,
                None,
            )
        )
    return examples


def _fit() -> CalibrationModelRecord:
    return fit_calibration(
        _examples(),
        profile="controlled-machinery",
        version_number=1,
        corpus_version="controlled-synth-1",
        config_hash="a" * 64,
        population="controlled -- test",
        split_seed=1,
        calibration_set_ids=["s1", "s2"],
        test_version="controlled-machinery",
    )


def test_fit_and_apply_calibrates_scores_tiers_and_pis() -> None:
    model = _fit()
    CalibrationModelRecord.model_validate(model.model_dump(mode="json"))
    assert all(entry.status == "provisional" for entry in model.certification)
    applier = CalibrationApplier(model)
    strong = applier.apply(
        _feature(50_000, 60_000, True), start_proved_ms=3_000, end_proved_ms=5_000
    )
    weak = applier.apply(
        _feature(10_000, 15_000, False, recording=False), start_proved_ms=3_000, end_proved_ms=5_000
    )
    assert strong.tiers.work == "verified"
    assert strong.scores.work >= weak.scores.work
    # PI centre is the interval midpoint (the scorer requires this for calibrated episodes).
    assert strong.best_start_ms == (strong.start_pi.lo + strong.start_pi.hi) // 2
    assert strong.best_end_ms == (strong.end_pi.lo + strong.end_pi.hi) // 2
    assert strong.start_pi.calibrated is True
    assert strong.start_pi.coverage_target == 9_000


def test_apply_caps_version_and_boundary_structurally() -> None:
    applier = CalibrationApplier(_fit())
    # Strong evidence but no recording corroboration -> version stays unclear.
    no_recording = applier.apply(
        _feature(50_000, 60_000, True, recording=False), start_proved_ms=3_000, end_proved_ms=5_000
    )
    assert no_recording.tiers.version == "unclear"
    # No global alignment -> boundary stays unclear regardless of the calibrated score.
    no_global = applier.apply(
        _feature(50_000, 60_000, False), start_proved_ms=3_000, end_proved_ms=5_000
    )
    assert no_global.tiers.boundary == "unclear"


# --------------------------------------------------------------------------------------------------
# analyse wiring: heuristic by default, calibrated when a model is present
# --------------------------------------------------------------------------------------------------
def _corroborated_observations() -> list[ObservationRecord]:
    observations: list[ObservationRecord] = []
    for index in range(4):
        observations.append(
            _observation(index, provider="shazam", provider_ids={"isrc": "I1", "shazam": "s1"})
        )
        observations.append(
            _observation(index, provider="audd", provider_ids={"isrc": "I1", "shazam": "s1"})
        )
    # Give the paired providers distinct trial ids so both are independent votes.
    fixed: list[ObservationRecord] = []
    for position, observation in enumerate(observations):
        fixed.append(observation.model_copy(update={"logical_trial_id": f"{200 + position:040x}"}))
    return fixed


def test_build_episodes_is_heuristic_by_default() -> None:
    observations = _corroborated_observations()
    identity = build_identity_graph(MEDIA_KEY, observations)
    episodes, _ = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=60_000,
        observations=observations,
        windows=[],
        identity=identity,
    )
    assert episodes.episodes
    for episode in episodes.episodes:
        assert episode.score_kind == "heuristic"
        assert episode.start_pi is None and episode.end_pi is None
    assert {entry.test_version for entry in episodes.certification.per} == {"not-run"}


def test_build_episodes_uses_calibrator_when_present() -> None:
    observations = _corroborated_observations()
    identity = build_identity_graph(MEDIA_KEY, observations)
    applier = CalibrationApplier(_fit())
    episodes, _ = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=60_000,
        observations=observations,
        windows=[],
        identity=identity,
        calibrator=applier,
    )
    assert episodes.episodes
    for episode in episodes.episodes:
        assert episode.score_kind == "calibrated"
        assert episode.start_pi is not None and episode.end_pi is not None
        assert episode.best_start_ms == (episode.start_pi.lo + episode.start_pi.hi) // 2
        # The badge is recomputed from the calibrated tiers.
        assert episode.badge in {"unclear", "possible", "likely", "verified"}
    # The certification block is stamped from the (provisional) model.
    assert {entry.status for entry in episodes.certification.per} == {"provisional"}
    assert {entry.test_version for entry in episodes.certification.per} == {"controlled-machinery"}


# --------------------------------------------------------------------------------------------------
# Certification command refusal paths
# --------------------------------------------------------------------------------------------------
def _write_manifest(project_root: Path, corpus: str, *, frozen: bool) -> None:
    atomic_write_json(
        project_root / "data" / "corpus" / corpus / "corpus-version.json",
        {
            "schema_version": SCHEMA_VERSION,
            "generated_by": GENERATED_BY,
            "corpus_version": corpus,
            "frozen": frozen,
            "sets": [],
        },
    )


def test_certify_refuses_unfrozen_corpus(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "draftcorpus", frozen=False)
    with pytest.raises(CorpusNotFrozen):
        __import__("asyncio").run(
            run_certify(
                corpus_version="draftcorpus",
                profile="free",
                test_version="v1",
                project_root=tmp_path,
                work_root=tmp_path / "work",
            )
        )


def test_certify_refuses_repeated_test_version(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "frozencorpus", frozen=True)
    registry = (
        tmp_path / "data" / "local" / "certification" / "frozencorpus" / "free" / "registry.json"
    )
    atomic_write_json(registry, {"test_versions": ["v1"]})
    with pytest.raises(DuplicateTestVersion):
        __import__("asyncio").run(
            run_certify(
                corpus_version="frozencorpus",
                profile="free",
                test_version="v1",
                project_root=tmp_path,
                work_root=tmp_path / "work",
            )
        )


def test_registered_targets_cover_every_dimension_and_tier() -> None:
    targets = registered_targets("free")
    assert len(targets) == 15
    assert {target.tier for target in targets} == {"possible", "likely", "verified"}
    assert {target.target_e4 for target in targets} == {7_000, 9_000, 9_900}


def test_load_calibration_absent_returns_none(tmp_path: Path) -> None:
    assert load_calibration(tmp_path, "free") is None


def test_work_index_is_monotone_in_evidence() -> None:
    low = work_index(_feature(20_000, 20_000, False))
    high = work_index(_feature(60_000, 80_000, True))
    assert high > low


# --------------------------------------------------------------------------------------------------
# [P1] Features recompute identically at fit vs analyse time (recording_supported / competition)
# --------------------------------------------------------------------------------------------------
class _CapturingCalibrator:
    """Wraps a real applier and records the exact feature inputs the fuser passes per episode."""

    def __init__(self, inner: CalibrationApplier) -> None:
        self.inner = inner
        self.model = inner.model  # the fuser stamps the certification block from here
        self.captured: dict[str, dict[str, object]] = {}

    def apply_episode(self, **kwargs: object) -> object:
        self.captured[str(kwargs["episode_id"])] = dict(kwargs)
        return self.inner.apply_episode(**kwargs)  # type: ignore[arg-type]


def _single_engine_isrc_observations() -> list[ObservationRecord]:
    """One candidate, a single ISRC recording node, asserted by a single engine/source."""

    observations = [
        _observation(index, provider="shazam", provider_ids={"isrc": "I1"}) for index in range(4)
    ]
    return [
        observation.model_copy(update={"logical_trial_id": f"{300 + position:040x}"})
        for position, observation in enumerate(observations)
    ]


def test_recording_supported_and_competition_match_fit_and_analyse() -> None:
    observations = _single_engine_isrc_observations()
    identity = build_identity_graph(MEDIA_KEY, observations)
    capture = _CapturingCalibrator(CalibrationApplier(_fit()))
    episodes, _ = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=60_000,
        observations=observations,
        windows=[],
        identity=identity,
        calibrator=capture,
    )
    assert episodes.episodes
    observations_by_id = {observation.id: observation for observation in observations}
    for episode in episodes.episodes:
        reconstructed = feature_inputs_from_record(
            episode,
            identities=identity.record,
            observations_by_id=observations_by_id,
            hints_by_id={},
            all_episodes=list(episodes.episodes),
            duration_ms=60_000,
        )
        analyse = capture.captured[episode.id]
        # A single-engine single-ISRC candidate is NOT recording-corroborated: both places agree it
        # is False -- the old reconstruct rule (>= 1 recording node) wrongly returned True here.
        assert reconstructed.recording_supported is False
        assert analyse["recording_supported"] is False
        # And both compute the same competitor count from the same selected supports.
        assert reconstructed.n_competing_candidates == analyse["n_competing_candidates"]


def test_competing_candidate_count_returns_the_actual_count() -> None:
    # Two other candidates each cover >= 50% of the subject's support: the shared helper returns 2,
    # not a clamped 0/1 -- the property the fuser and the reconstruction now share.
    supports = [(0, 20_000)]
    all_supports = {
        "subject": [(0, 20_000)],
        "rivalA": [(0, 20_000)],
        "rivalB": [(5_000, 25_000)],
        "faraway": [(90_000, 100_000)],
    }
    assert competing_candidate_count(supports, "subject", all_supports, 120_000) == 2


# --------------------------------------------------------------------------------------------------
# [P1] certify wires the selected --profile into the real-mix analyse path
# --------------------------------------------------------------------------------------------------
def test_real_set_analyse_command_carries_the_profile() -> None:
    command = _real_analyse_command(
        "https://example/mix",
        work_root=Path("work"),
        max_requests=2_000,
        include_hints=False,
        profile="free",
    )
    assert command[:5] == [
        sys.executable,
        "-m",
        "id_detector.cli",
        "analyse",
        "https://example/mix",
    ]
    assert "--profile" in command
    assert command[command.index("--profile") + 1] == "free"
    assert "--no-hints" in command
    # Without a profile the flag is absent (real-set certification must therefore pass one).
    profileless = _real_analyse_command(
        "https://example/mix",
        work_root=Path("work"),
        max_requests=2_000,
        include_hints=True,
        profile=None,
    )
    assert "--profile" not in profileless


# --------------------------------------------------------------------------------------------------
# [P2] Deterministic seed-independent split; honest certify prediction count
# --------------------------------------------------------------------------------------------------
def test_split_sets_is_deterministic_stratified_and_seedless() -> None:
    import inspect

    ids = ["s5", "s1", "s3", "s2", "s4"]
    calibration, test = _split_sets(ids)
    # Sorted alternation: index 0,2,4 -> calibration; 1,3 -> test.
    assert calibration == ["s1", "s3", "s5"]
    assert test == ["s2", "s4"]
    # The dead `seed` parameter is gone, so provenance can no longer imply a seeded split.
    assert list(inspect.signature(_split_sets).parameters) == ["set_ids"]


def test_population_prediction_count_is_per_episode_not_tier_sum() -> None:
    from types import SimpleNamespace

    truths = [
        SimpleNamespace(set_id="real-1", split="test", stratum="catalogue-covered"),
        SimpleNamespace(set_id="real-2", split="test", stratum="hard-id"),
        SimpleNamespace(set_id="ctrl-1", split="test", stratum="controlled"),
        SimpleNamespace(set_id="real-dev", split="calibration", stratum="catalogue-covered"),
    ]
    prediction_sets = [
        {"set_id": "real-1", "episodes": [{}, {}, {}]},
        {"set_id": "real-2", "episodes": [{}, {}]},
        {"set_id": "ctrl-1", "episodes": [{}, {}, {}, {}]},
        {"set_id": "real-dev", "episodes": [{}]},
    ]
    # Only the two real-mix *test* sets count: 3 + 2 = 5 predicted episodes. The controlled set and
    # the calibration-split set are excluded; there is no cross-dimension/tier double-counting.
    assert _population_prediction_count(truths, prediction_sets) == 5
