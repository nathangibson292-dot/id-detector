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
) -> ObservationRecord:
    start = index * 9_000
    label = RawLabel(artist=artist, title=title, album=None, label=None, release_date=None)
    natural = {
        "query_id": f"{index + 1:040x}",
        "mix_span_ms": [start, start + 12_000],
        "raw_label_hash": __import__("hashlib")
        .sha256(json.dumps(label.model_dump(mode="json"), sort_keys=True).encode())
        .hexdigest(),
        "native_index": 0,
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
        transform={"type": "none", "rate_e4": 10_000, "semitones": 0},
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


def test_episode_proofs_and_evidence_use_all_final_hypotheses() -> None:
    selected = _observation(1, provider="shazam", provider_ids={"shazam": "s1"})
    rejected = selected.model_copy(
        update={
            "id": "e" * 40,
            "mix_span_ms": (0, 10_000),
            "support_ms": (0, 10_000),
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
    assert episode.start_no_later_than_ms == 10_000
    assert episode.end_no_earlier_than_ms == 9_000
    assert episode.evidence_support_ms == [(0, 21_000)]
    assert set(episode.evidence) == {selected.id, rejected.id}
    assert "hypothesis_rejected" in episode.flags
    assert "hypothesis_rejected" not in other_episode.flags


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
