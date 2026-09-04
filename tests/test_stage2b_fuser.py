from __future__ import annotations

import json
from pathlib import Path

import pytest

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    IdentityAssertion,
    ObservationRecord,
    RawLabel,
    compose_natural_key,
    make_id,
)
from id_detector.fuse.alignment import point_from_observation
from id_detector.fuse.episodes import _badge, build_episodes
from id_detector.fuse.identity import build_identity_graph

MEDIA_KEY = "4" * 64


@pytest.mark.parametrize(
    ("work", "version", "expected"),
    [
        ("unclear", "unclear", "unclear"),
        ("possible", "unclear", "possible"),
        ("likely", "unclear", "likely"),
        ("verified", "likely", "likely"),
        ("verified", "verified", "verified"),
    ],
)
def test_revision_5_1_badge_rule(work: str, version: str, expected: str) -> None:
    assert _badge(work, version) == expected


def _observation(
    index: int,
    *,
    provider: str,
    provider_ids: dict[str, str],
    artist: str = "Artist",
    title: str = "Title",
    transform: dict[str, int | str] | None = None,
) -> ObservationRecord:
    start = index * 9_000
    transform = transform or {"type": "none", "rate_e4": 10_000, "semitones": 0}
    label = RawLabel(artist=artist, title=title, album=None, label=None, release_date=None)
    natural = {
        "query_id": f"{index + 1:040x}",
        "mix_span_ms": [start, start + 12_000],
        "raw_label_hash": __import__("hashlib")
        .sha256(json.dumps(label.model_dump(mode="json"), sort_keys=True).encode())
        .hexdigest(),
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


def _conflict(a: str, b: str, identifier: str) -> IdentityAssertion:
    return IdentityAssertion(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=identifier,
        a=a,
        b=b,
        relation="conflicts",
        source={"kind": "catalogue", "record_id": "conflict-source"},
        independent_of="catalogue:conflict",
        confidence=10_000,
    )


def test_identity_graph_uses_text_only_for_work_and_two_sources_for_recording() -> None:
    observations = [
        _observation(0, provider="shazam", provider_ids={"shazam": "s1", "isrc": "I1"}),
        _observation(1, provider="audd", provider_ids={"shazam": "s1", "isrc": "I1"}),
    ]
    built = build_identity_graph(MEDIA_KEY, observations)
    candidate = next(item for item in built.record.candidates if "isrc:I1" in item.member_nodes)
    assert set(candidate.member_nodes) == {"isrc:I1", "shazam:s1"}
    assert candidate.canonical_id in built.recording_supported
    assert "text:artist|title" not in candidate.member_nodes
    assert all(
        assertion.relation == "same_work"
        for assertion in built.record.assertions
        if assertion.a.startswith("text:") or assertion.b.startswith("text:")
    )


def test_identity_conflict_veto_and_late_contested_marking() -> None:
    observations = [
        _observation(0, provider="shazam", provider_ids={"shazam": "s1", "isrc": "I1"}),
        _observation(1, provider="audd", provider_ids={"shazam": "s1", "isrc": "I1"}),
    ]
    early = build_identity_graph(
        MEDIA_KEY,
        observations,
        extra_assertions=[_conflict("isrc:I1", "shazam:s1", "0" * 40)],
    )
    assert not any(
        set(candidate.member_nodes) == {"isrc:I1", "shazam:s1"}
        for candidate in early.record.candidates
    )
    reordered = build_identity_graph(
        MEDIA_KEY,
        observations,
        extra_assertions=[_conflict("isrc:I1", "shazam:s1", "f" * 40)],
    )
    assert [candidate.member_nodes for candidate in reordered.record.candidates] == [
        candidate.member_nodes for candidate in early.record.candidates
    ]
    late = build_identity_graph(
        MEDIA_KEY,
        observations,
        extra_assertions=[_conflict("isrc:I1", "shazam:s1", "f" * 40)],
        prior_recording_components=(("isrc:I1", "shazam:s1"),),
    )
    merged = next(
        candidate
        for candidate in late.record.candidates
        if set(candidate.member_nodes) == {"isrc:I1", "shazam:s1"}
    )
    assert merged.contested
    assert merged.canonical_id not in late.recording_supported


def test_same_recording_id_from_two_providers_is_corroborated() -> None:
    observations = [
        _observation(0, provider="shazam", provider_ids={"isrc": "I1"}),
        _observation(1, provider="audd", provider_ids={"isrc": "I1"}),
    ]
    built = build_identity_graph(MEDIA_KEY, observations)
    candidate = next(item for item in built.record.candidates if item.member_nodes == ["isrc:I1"])
    assert candidate.canonical_id in built.recording_supported


def test_native_time_skew_sets_the_initial_alignment_rate() -> None:
    observation = _observation(0, provider="shazam", provider_ids={"shazam": "s1"})
    observation = observation.model_copy(
        update={
            "native": {
                "matches": [{"offset_ms": 0, "frequencyskew_e6": 44_000, "timeskew_e6": 44_000}]
            }
        }
    )
    point = point_from_observation(observation, "a" * 40)
    assert point is not None
    assert point.rate_e4 == 10_440


def test_vetoed_candidates_may_record_conflicts_without_being_contested() -> None:
    observations = [
        _observation(0, provider="shazam", provider_ids={"shazam": "s1", "isrc": "I1"}),
        _observation(1, provider="audd", provider_ids={"shazam": "s1", "isrc": "I1"}),
    ]
    built = build_identity_graph(
        MEDIA_KEY,
        observations,
        extra_assertions=[_conflict("isrc:I1", "shazam:s1", "1" * 40)],
    )
    assert all(candidate.conflicts for candidate in built.record.candidates)
    assert all(not candidate.contested for candidate in built.record.candidates)


def test_identity_graph_is_byte_deterministic_across_input_order() -> None:
    observations = [
        _observation(0, provider="shazam", provider_ids={"shazam": "s1"}),
        _observation(1, provider="shazam", provider_ids={"shazam": "s1"}),
    ]
    left = build_identity_graph(MEDIA_KEY, observations).record.model_dump_json()
    right = build_identity_graph(MEDIA_KEY, list(reversed(observations))).record.model_dump_json()
    assert left == right


def test_episode_proofs_use_only_the_selected_observation_of_each_logical_trial() -> None:
    selected = _observation(1, provider="shazam", provider_ids={"shazam": "s1"})
    rejected = selected.model_copy(
        update={
            "id": "e" * 40,
            "mix_span_ms": (0, 10_000),
            "support_ms": (0, 10_000),
            "transform": {"type": "resample", "rate_e4": 10_800, "semitones": 0},
            "native": {
                "matches": [{"offset_ms": 9_000, "frequencyskew_e6": 900, "timeskew_e6": 900}]
            },
        }
    )
    other = _observation(2, provider="shazam", provider_ids={"shazam": "s2"}, title="Other")
    observations = [selected, rejected, other]
    identity = build_identity_graph(MEDIA_KEY, observations)
    episodes, _ = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=40_000,
        observations=observations,
        windows=[],
        identity=identity,
    )
    candidate_id = identity.observation_candidates[selected.id]
    episode = next(item for item in episodes.episodes if item.candidate_id == candidate_id)
    other_episode = next(item for item in episodes.episodes if item.candidate_id != candidate_id)
    # The rejected sibling's support (0, 10_000) must not move either proved bound or the
    # evidence union: rev 5.2 reads both only from the per-trial selected observation.
    assert episode.start_no_later_than_ms == selected.support_ms[1] == 21_000
    assert episode.end_no_earlier_than_ms == selected.support_ms[0] == 9_000
    assert episode.evidence_support_ms == [(9_000, 21_000)]
    assert episode.evidence == [selected.id]
    assert episode.rejected_evidence == [rejected.id]
    assert "hypothesis_rejected" in episode.flags
    assert "hypothesis_rejected" not in other_episode.flags
    assert other_episode.rejected_evidence == []


def test_rejected_minority_candidate_never_proves_the_winner_bound() -> None:
    winner = _observation(1, provider="shazam", provider_ids={"shazam": "winner"})
    same_candidate = winner.model_copy(
        update={
            "id": "a" * 40,
            "transform": {"type": "tempo", "rate_e4": 9_600, "semitones": 0},
            "native": {
                "matches": [{"offset_ms": 9_000, "frequencyskew_e6": 700, "timeskew_e6": 700}]
            },
        }
    )
    minority = _observation(
        2,
        provider="shazam",
        provider_ids={"shazam": "minority"},
        title="Another track entirely",
        transform={"type": "resample", "rate_e4": 10_800, "semitones": 0},
    ).model_copy(
        update={
            "id": "c" * 40,
            "logical_trial_id": winner.logical_trial_id,
            "mix_span_ms": (9_000, 20_111),
            "support_ms": (9_000, 20_111),
            "anchor": winner.anchor,
        }
    )
    identity = build_identity_graph(MEDIA_KEY, [winner, same_candidate, minority])
    episodes, _ = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=30_000,
        observations=[winner, same_candidate, minority],
        windows=[],
        identity=identity,
    )
    episode = next(iter(episodes.episodes))
    assert episode.candidate_id == identity.observation_candidates[winner.id]
    assert episode.start_no_later_than_ms == 21_000
    assert episode.evidence == [winner.id]
    assert sorted(episode.rejected_evidence) == sorted([same_candidate.id, minority.id])


def test_conflicting_variants_belong_to_majority_episode_and_only_best_variant_votes() -> None:
    best = _observation(1, provider="shazam", provider_ids={"shazam": "majority"})
    same_candidate = best.model_copy(
        update={
            "id": "b" * 40,
            "native": {
                "matches": [{"offset_ms": 9_000, "frequencyskew_e6": 400, "timeskew_e6": 500}]
            },
        }
    )
    minority = _observation(
        2,
        provider="shazam",
        provider_ids={"shazam": "minority"},
        title="Conflicting hypothesis",
    ).model_copy(
        update={
            "id": "c" * 40,
            "logical_trial_id": best.logical_trial_id,
            "mix_span_ms": best.mix_span_ms,
            "support_ms": best.support_ms,
            "anchor": best.anchor,
        }
    )
    identity = build_identity_graph(MEDIA_KEY, [best, same_candidate, minority])
    episodes, _ = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=30_000,
        observations=[best, same_candidate, minority],
        windows=[],
        identity=identity,
    )
    assert len(episodes.episodes) == 1
    episode = episodes.episodes[0]
    assert episode.candidate_id == identity.observation_candidates[best.id]
    assert episode.evidence == [best.id]
    assert set(episode.rejected_evidence) == {same_candidate.id, minority.id}
    assert episode.scores.work == 2_500
    assert "hypothesis_rejected" in episode.flags


def _trial_observation(index: int, start_ms: int, *, window_ms: int = 12_000) -> ObservationRecord:
    base = _observation(index, provider="shazam", provider_ids={"shazam": "s1"})
    return base.model_copy(
        update={
            "id": f"{index + 1:040x}",
            "mix_span_ms": (start_ms, start_ms + window_ms),
            "support_ms": (start_ms, start_ms + window_ms),
            "anchor": base.anchor.model_copy(
                update={"mix_anchor_ms": start_ms, "ref_anchor_ms": start_ms}
            )
            if base.anchor is not None
            else None,
            "native": {
                "matches": [{"offset_ms": start_ms, "frequencyskew_e6": 0, "timeskew_e6": 0}]
            },
        }
    )


def _work_tier(starts: list[int], duration_ms: int = 200_000) -> str:
    observations = [_trial_observation(index, start) for index, start in enumerate(starts)]
    identity = build_identity_graph(MEDIA_KEY, observations)
    episodes, _ = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=duration_ms,
        observations=observations,
        windows=[],
        identity=identity,
    )
    return episodes.episodes[0].tiers.work


def test_dense_overlapping_hops_do_not_inflate_the_work_tier() -> None:
    # Four 12 s windows on a 9 s hop are pairwise non-overlapping only in pairs: T_ind = 3.
    sparse = [0, 12_000, 24_000, 36_000, 48_000]
    assert _work_tier(sparse) == "likely"
    # Halving the hop doubles the number of trials over the same 48 s of audio without adding a
    # single disjoint support, so rev 5.2's T_ind — and hence the tier — must not move.
    dense = [value for start in sparse for value in (start, start + 6_000) if value <= 48_000]
    assert len(dense) > len(sparse)
    assert _work_tier(dense) == "likely"
    # Three heavily overlapping windows cover < 40 s of span and yield T_ind = 1, not 3.
    assert _work_tier([0, 1_000, 2_000]) == "unclear"


def test_long_episode_emits_one_deterministic_rescan_request() -> None:
    observations = [
        _observation(0, provider="shazam", provider_ids={"shazam": "s1"}),
        _observation(81, provider="shazam", provider_ids={"shazam": "s1"}),
    ]
    identity = build_identity_graph(MEDIA_KEY, observations)
    episode_file, first = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=750_000,
        observations=observations,
        windows=[],
        identity=identity,
    )
    _, second = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=750_000,
        observations=list(reversed(observations)),
        windows=[],
        identity=identity,
    )
    first_long = [item for item in first if item.trigger == "long_episode"]
    second_long = [item for item in second if item.trigger == "long_episode"]
    assert [(item.id, item.start_ms, item.end_ms) for item in first_long] == [
        (item.id, item.start_ms, item.end_ms) for item in second_long
    ]
    assert len(first_long) == 1
    assert (first_long[0].start_ms, first_long[0].end_ms) == (0, 741_000)
    assert episode_file.episodes[0].tiers.work == "possible"
    assert episode_file.episodes[0].tiers.version == "unclear"
    assert episode_file.episodes[0].badge == "possible"
    assert episode_file.episodes[0].version_status == "unverified"


@pytest.fixture
def golden_paths() -> tuple[Path, Path]:
    root = Path(__file__).parent / "golden"
    return root / "episodes.json", root / "identities.json"
