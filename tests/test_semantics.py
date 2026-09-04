from __future__ import annotations

import io
import json
import logging
from fractions import Fraction
from pathlib import Path

import pytest

from id_detector.contracts import (
    clip_cache_key,
    compose_natural_key,
    derive_media_key,
    derive_source_key,
    file_scan_cache_key,
    local_index_cache_key,
    make_id,
    sort_records,
)
from id_detector.hints import is_track_question, parse_hint_timestamp
from id_detector.io import (
    SecretRedactionFilter,
    atomic_write_bytes,
    canonical_json_bytes,
    verify_completion_sidecar,
    write_completion_sidecar,
)
from id_detector.semantics import (
    aggregate_shazam_anchor,
    gap_intervals,
    merge_recording_identities,
    partition_durations,
    proved_bounds,
    text_equality_relation,
    transform_spec,
)


@pytest.mark.parametrize("kind", ["resample", "tempo"])
@pytest.mark.parametrize("rate_e4", [9200, 9600, 10400, 10800])
def test_rate_transform_algebra(kind: str, rate_e4: int) -> None:
    spec = transform_spec(kind, rate_e4=rate_e4)
    assert spec.original_span_ms == round(12_000 / (rate_e4 / 10_000))
    assert spec.output_ms == 12_000
    assert spec.map_output_sample(0) == 0
    last_output_sample = 12 * 16_000 - 1
    assert spec.map_output_sample(last_output_sample) == Fraction(
        10_000 * last_output_sample, rate_e4
    )


@pytest.mark.parametrize("semitones", [-2, -1, 1, 2])
def test_pitch_transform_algebra(semitones: int) -> None:
    spec = transform_spec("pitch", semitones=semitones)
    assert spec.original_span_ms == 12_000
    assert spec.output_ms == 12_000
    assert spec.map_output_sample(0) == 0
    assert spec.map_output_sample(12 * 16_000 - 1) == 12 * 16_000 - 1
    assert spec.rate_e4 == round(10_000 * (2 ** (semitones / 12)))


def test_one_sided_bounds_leave_censored_sides_null() -> None:
    assert proved_bounds([(18_000, 30_000), (27_000, 39_000), (90_000, 102_000)]) == (
        30_000,
        90_000,
        None,
        None,
    )


def test_duration_partition_is_exact_overlap_safe_and_keeps_tails_out_of_gaps() -> None:
    episodes = [
        {
            "badge": "possible",
            "evidence_support_ms": [(100_000, 180_000), (140_000, 200_000)],
            "start_no_earlier_than_ms": None,
            "end_no_later_than_ms": None,
            "start_pi": None,
            "end_pi": None,
        },
        {
            "badge": "possible",
            "evidence_support_ms": [(250_000, 300_000)],
            "start_no_earlier_than_ms": None,
            "end_no_later_than_ms": None,
            "start_pi": None,
            "end_pi": None,
        },
    ]
    durations, intervals = partition_durations(600_000, episodes, [(0, 600_000)])
    assert durations["evidence_supported_ms"] == 150_000
    assert durations["predicted_episode_ms"] == 0
    assert sum(durations.values()) == 600_000
    assert all(end - start <= 120_000 for start, end in intervals["unresolved_boundary_ms"])
    gaps = gap_intervals(intervals["no_evidence_ms"])
    for gap_start, gap_end in gaps:
        assert all(
            gap_end <= unresolved_start or gap_start >= unresolved_end
            for unresolved_start, unresolved_end in intervals["unresolved_boundary_ms"]
        )


def test_duration_partition_counts_overlapping_episodes_once() -> None:
    base = {
        "badge": "possible",
        "start_no_earlier_than_ms": 0,
        "end_no_later_than_ms": 200_000,
        "start_pi": None,
        "end_pi": None,
    }
    episodes = [
        {**base, "evidence_support_ms": [(10_000, 100_000)]},
        {**base, "evidence_support_ms": [(50_000, 150_000)]},
    ]
    durations, _ = partition_durations(200_000, episodes, [(0, 200_000)])
    assert durations["evidence_supported_ms"] == 140_000


def test_shazam_anchor_clustering_bias_and_reliability() -> None:
    matches = [{"offset": value} for value in ("10.0", "10.4", "10.8", "30.0")]
    anchor = aggregate_shazam_anchor(
        matches,
        support_start_ms=18_000,
        adapter_bias_ms=250,
        adapter_bias_uncertainty_ms=500,
        adapter_measured=True,
    )
    assert anchor == {
        "mix_anchor_ms": 18_000,
        "ref_anchor_ms": 10_150,
        "uncertainty_ms": 500,
        "reliable": True,
        "method": "shazam_offset_cluster_median",
        "bias_applied_ms": 250,
    }
    unmeasured = aggregate_shazam_anchor(
        matches,
        support_start_ms=18_000,
        adapter_bias_ms=250,
        adapter_bias_uncertainty_ms=500,
        adapter_measured=False,
    )
    assert unmeasured is not None and unmeasured["reliable"] is False
    assert (
        aggregate_shazam_anchor(
            [{"offset": 1}, {"offset": 4}, {"offset": 7}],
            support_start_ms=0,
            adapter_bias_ms=0,
            adapter_bias_uncertainty_ms=0,
            adapter_measured=True,
        )
        is None
    )


def test_shazam_anchor_accepts_exactly_half_of_offsets_in_largest_cluster() -> None:
    anchor = aggregate_shazam_anchor(
        [{"offset": 10}, {"offset": "10.5"}, {"offset": 20}, {"offset": 30}],
        support_start_ms=0,
        adapter_bias_ms=0,
        adapter_bias_uncertainty_ms=0,
        adapter_measured=True,
    )
    assert anchor is not None
    assert anchor["ref_anchor_ms"] == 10_250


def _assertion(
    a: str,
    b: str,
    relation: str,
    independent: str,
    source_kind: str = "provider_observation",
) -> dict:
    return {
        "a": a,
        "b": b,
        "relation": relation,
        "independent_of": independent,
        "source": {"kind": source_kind, "record_id": independent},
    }


def test_identity_merge_requires_two_sources_and_text_is_work_only() -> None:
    nodes = {"isrc:a": "isrc", "shazam:a": "shazam", "text:a": "text"}
    one_source = merge_recording_identities(
        nodes, [_assertion("isrc:a", "shazam:a", "same_recording", "source-1")]
    )
    assert ("isrc:a", "shazam:a") not in one_source.components
    corroborated = merge_recording_identities(
        nodes,
        [
            _assertion("isrc:a", "shazam:a", "same_recording", "source-1"),
            _assertion("isrc:a", "shazam:a", "same_recording", "source-2"),
        ],
    )
    assert ("isrc:a", "shazam:a") in corroborated.components
    assert text_equality_relation() == "same_work"


def test_adversarial_same_label_versions_never_imply_same_recording() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "fixtures"
        / "identities"
        / "adversarial_versions.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert len({case["label"] for case in fixture["cases"]}) == 1
    assert len({case["qualifier"] for case in fixture["cases"]}) == 7
    assert fixture["expected_text_relation"] == text_equality_relation()
    assert fixture["expected_version_tier_without_recording_ids"] == "unclear"


def test_identity_conflict_veto_and_late_conflict_contesting() -> None:
    nodes = {"isrc:a": "isrc", "shazam:a": "shazam"}
    recording = [
        _assertion("isrc:a", "shazam:a", "same_recording", "source-1"),
        _assertion("isrc:a", "shazam:a", "same_recording", "source-2"),
    ]
    conflict = _assertion("isrc:a", "shazam:a", "conflicts", "audit")
    vetoed = merge_recording_identities(nodes, [conflict, *recording])
    assert vetoed.components == (("isrc:a",), ("shazam:a",))
    assert vetoed.refused == (("isrc:a", "shazam:a"),)
    contested = merge_recording_identities(nodes, [*recording, conflict])
    assert contested.components == (("isrc:a",), ("shazam:a",))
    assert not contested.contested
    contested = merge_recording_identities(
        nodes,
        [conflict, *recording],
        prior_components=(("isrc:a", "shazam:a"),),
    )
    assert contested.components == (("isrc:a", "shazam:a"),)
    assert contested.contested == (("isrc:a", "shazam:a"),)


@pytest.mark.parametrize("source_kind", ["aligned_held_reference", "audited_truth"])
def test_privileged_identity_sources_merge_without_recording_ids_and_honor_conflicts(
    source_kind: str,
) -> None:
    nodes = {"text:a": "text", "mb_work:a": "mb_work"}
    privileged = _assertion("text:a", "mb_work:a", "same_recording", source_kind, source_kind)
    conflict = _assertion("text:a", "mb_work:a", "conflicts", "audit")

    merged = merge_recording_identities(nodes, [privileged])
    assert merged.components == (("mb_work:a", "text:a"),)
    vetoed = merge_recording_identities(nodes, [conflict, privileged])
    assert vetoed.components == (("mb_work:a",), ("text:a",))
    assert vetoed.refused == (("mb_work:a", "text:a"),)
    contested = merge_recording_identities(nodes, [privileged, conflict])
    assert contested.components == (("mb_work:a",), ("text:a",))
    assert not contested.contested
    contested = merge_recording_identities(
        nodes,
        [conflict, privileged],
        prior_components=(("mb_work:a", "text:a"),),
    )
    assert contested.components == (("mb_work:a", "text:a"),)
    assert contested.contested == (("mb_work:a", "text:a"),)


def test_deterministic_ids_are_stable_and_natural_key_sensitive() -> None:
    media_key = "a" * 64
    first = make_id(media_key, "gap", "[1000,2000]")
    assert first == make_id(media_key, "gap", "[1000,2000]")
    assert first != make_id(media_key, "gap", "[1000,2001]")


def test_record_ordering_uses_start_then_id_or_id_alone() -> None:
    timed = [
        {"id": "b", "start_ms": 20},
        {"id": "c", "start_ms": 10},
        {"id": "a", "start_ms": 20},
    ]
    assert [item["id"] for item in sort_records(timed)] == ["c", "a", "b"]
    assert [item["id"] for item in sort_records([{"id": "z"}, {"id": "a"}])] == ["a", "z"]


def test_source_and_media_keys_hash_the_exact_contract_inputs() -> None:
    assert derive_source_key("source-ref:canonical") == (
        "737bbe10977cbb5a4903c9354b9af6efda63213c3fbc3533e9eff2cd572efd7c"
    )
    assert derive_media_key(b"original bytes") == (
        "52c3935626c104b2cbc9031291a1c4d56614c38f52072a361d658a58a9c48698"
    )


def test_canonical_json_is_sorted_compact_utf8_and_rejects_floats() -> None:
    assert canonical_json_bytes({"z": None, "accent": "café", "a": 1}) == (
        b'{"a":1,"accent":"caf\xc3\xa9","z":null}'
    )
    with pytest.raises(ValueError, match="floating-point"):
        canonical_json_bytes({"bad": [1, 1.5]})
    with pytest.raises(ValueError, match="secret field"):
        canonical_json_bytes({"api_key": "must-not-be-written"})


def test_sidecar_rejects_a_stale_upstream_hash(tmp_path: Path) -> None:
    artifact = tmp_path / "episodes.json"
    upstream = tmp_path / "observations.jsonl"
    atomic_write_bytes(artifact, b"{}")
    atomic_write_bytes(upstream, b"first")
    write_completion_sidecar(artifact, {"recognise/observations.jsonl": upstream})
    assert verify_completion_sidecar(artifact, {"recognise/observations.jsonl": upstream}).valid
    atomic_write_bytes(upstream, b"changed")
    result = verify_completion_sidecar(artifact, {"recognise/observations.jsonl": upstream})
    assert not result.valid
    assert "upstream hash differs: recognise/observations.jsonl" in result.errors


def test_log_filter_redacts_all_named_secret_fields() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactionFilter())
    logger = logging.getLogger("id-detector-test-redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.info(
        "client_id=%s oauth_token=%s api_key=%s Authorization: Bearer %s Cookie=%s",
        "client-secret",
        "oauth-secret",
        "api-secret",
        "auth-secret",
        "cookie-secret",
    )
    output = stream.getvalue()
    for secret in ("client-secret", "oauth-secret", "api-secret", "auth-secret", "cookie-secret"):
        assert secret not in output
    assert output.count("[REDACTED]") == 5


def test_cache_keys_include_every_capability_specific_component() -> None:
    wav = "a" * 64
    asset = "b" * 64
    assert clip_cache_key(wav, "shazam", "shazam-v1.json") != clip_cache_key(
        wav, "shazam", "shazam-v2.json"
    )
    original = file_scan_cache_key("original", asset, "audd", "audd-v1.json", "every-12")
    assert original != file_scan_cache_key("pcm", asset, "audd", "audd-v1.json", "every-12")
    assert original != file_scan_cache_key("original", "c" * 64, "audd", "audd-v1.json", "every-12")
    assert local_index_cache_key(wav, "pool-a", "v1") != local_index_cache_key(wav, "pool-a", "v2")


def test_rescan_request_natural_key_contains_generation_trigger_span_and_policy() -> None:
    base = {
        "generation": 1,
        "trigger": "gap",
        "start_ms": 1000,
        "end_ms": 9000,
        "policy": {"window_ms": 8000, "hop_ms": 4000, "phase_ms": 0, "transforms": []},
    }
    first = compose_natural_key("rescan_request", base)
    assert first == compose_natural_key("rescan_request", dict(base))
    assert first != compose_natural_key("rescan_request", {**base, "end_ms": 9001})
    assert first != compose_natural_key(
        "rescan_request", {**base, "policy": {**base["policy"], "phase_ms": 1}}
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("75:30", 4_530_000),
        ("1:72", None),
        ("1:05:00", 3_900_000),
        ("track at 1.02?", 62_000),
        ("ratio 1.02", None),
        ("2:60:00", None),
        ("1:02:03:04", None),
        ("prefix :1:02 suffix", None),
        ("prefix 1:02: suffix", None),
    ],
)
def test_hint_timestamp_component_rules(text: str, expected: int | None) -> None:
    assert parse_hint_timestamp(text, media_duration_ms=8_000_000) == expected


def test_hint_timestamp_respects_media_duration_and_question_negatives() -> None:
    assert parse_hint_timestamp("75:30", media_duration_ms=4_000_000) is None
    assert is_track_question("Anyone know this track?")
    assert not is_track_question("What mixer is this?")
    assert not is_track_question("How good is this?")
    assert not is_track_question("Anyone?")
    assert not is_track_question("What's this?")
    assert not is_track_question("Can I see your ID?")
    assert is_track_question("track ID")
