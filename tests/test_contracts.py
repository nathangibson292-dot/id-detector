from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from id_detector.contracts import (
    SCHEMA_MODELS,
    ProviderConfigRecord,
    QueryRecord,
    SourceRecord,
    WindowRecord,
    clip_cache_key,
    compose_natural_key,
    derive_source_key,
    file_scan_cache_key,
    make_id,
    schema_for,
)
from id_detector.io import canonical_json_bytes
from id_detector.semantics import (
    gap_intervals,
    merge_recording_identities,
    normalise_intervals,
    partition_durations,
    proved_bounds,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
SCHEMAS = ROOT / "docs" / "schemas"
MEDIA_KEY = "a" * 64


def _golden(name: str) -> dict:
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


def _contains_float(value: object) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_contains_float(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_float(item) for item in value)
    return False


@pytest.mark.parametrize("name", sorted(SCHEMA_MODELS))
def test_golden_validates_against_schema_and_model(name: str) -> None:
    instance = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
    schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(instance)
    parsed = SCHEMA_MODELS[name].model_validate(instance)
    assert parsed.model_dump(mode="json", by_alias=True, exclude_none=False) == instance
    assert not _contains_float(instance)


@pytest.mark.parametrize("name,model", sorted(SCHEMA_MODELS.items()))
def test_checked_in_schema_is_current(name: str, model: type) -> None:
    expected = json.loads(canonical_json_bytes(schema_for(model)))
    actual = json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
    assert actual == expected


def _assert_nullable_properties_are_required(schema: object) -> None:
    if isinstance(schema, dict):
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        for name, field_schema in properties.items():
            alternatives = field_schema.get("anyOf", []) if isinstance(field_schema, dict) else []
            if any(item.get("type") == "null" for item in alternatives):
                assert name in required, f"nullable field {name!r} may be omitted"
        for value in schema.values():
            _assert_nullable_properties_are_required(value)
    elif isinstance(schema, list):
        for value in schema:
            _assert_nullable_properties_are_required(value)


def test_all_nullable_contract_fields_require_explicit_nulls() -> None:
    for model in SCHEMA_MODELS.values():
        _assert_nullable_properties_are_required(schema_for(model))


def test_nested_float_is_rejected_by_model_and_schema() -> None:
    source = _golden("source")
    source["config_snapshot"]["bad_rate"] = 1.25
    with pytest.raises(ValidationError, match="floating-point"):
        SourceRecord.model_validate(source)
    schema = schema_for(SourceRecord)
    assert list(Draft202012Validator(schema).iter_errors(source))


def test_golden_ids_keys_and_cross_record_references_are_deterministic() -> None:
    source = _golden("source")
    window = _golden("window")
    query = _golden("query")
    observation = _golden("observation")
    hint = _golden("hint")
    identities = _golden("identities")
    episode = _golden("episode")
    gaps = _golden("episodes")["gaps"]
    rescan = _golden("rescan_request")
    raw_index = _golden("raw_index_entry")
    provider_config = _golden("provider_config")

    assert source["source_key"] == derive_source_key(source["canonical_url"])
    assert window["id"] == make_id(MEDIA_KEY, "window", compose_natural_key("window", window))
    assert window["logical_trial_id"] == window["id"]
    assert query["target"] == {"window_id": window["id"]}
    assert query["id"] == make_id(MEDIA_KEY, "query", compose_natural_key("query", query))
    assert query["cache_key"] == clip_cache_key(
        window["wav_sha256"], query["provider"], query["provider_config_version"]
    )
    assert query["provider_config_version"] == provider_config["version"]

    raw_label_hash = sha256(canonical_json_bytes(observation["raw_label"])).hexdigest()
    observation_key = {
        **observation,
        "raw_label_hash": raw_label_hash,
        "native_index": 0,
    }
    assert observation["id"] == make_id(
        MEDIA_KEY,
        "observation",
        compose_natural_key("observation", observation_key),
    )
    assert observation["query_id"] == query["id"]
    assert observation["logical_trial_id"] == window["id"]
    assert hint["id"] == make_id(
        MEDIA_KEY,
        "hint",
        compose_natural_key("hint", {**hint, "source_record_id": "fixture-comment-001"}),
    )
    for node in identities["nodes"]:
        assert node["id"].startswith(f"{node['ns']}:")
    for assertion in identities["assertions"]:
        assert assertion["id"] == make_id(
            MEDIA_KEY,
            "identity_assertion",
            compose_natural_key("identity_assertion", assertion),
        )
    work = identities["works"][0]
    assert work["work_id"] == make_id(
        MEDIA_KEY,
        "identity_work",
        compose_natural_key(
            "identity_work", {"normalised_artist_title": "example artist|signal path"}
        ),
    )
    candidate = identities["candidates"][0]
    assert candidate["canonical_id"] == make_id(
        MEDIA_KEY,
        "identity_candidate",
        compose_natural_key("identity_candidate", candidate),
    )
    assert episode["candidate_id"] == candidate["canonical_id"]
    assert episode["id"] == make_id(
        MEDIA_KEY,
        "episode",
        compose_natural_key(
            "episode",
            {**episode, "first_support_start_ms": episode["evidence_support_ms"][0][0]},
        ),
    )
    for gap in gaps:
        assert gap["id"] == make_id(MEDIA_KEY, "gap", compose_natural_key("gap", gap))
    assert rescan["id"] == make_id(
        MEDIA_KEY,
        "rescan_request",
        compose_natural_key("rescan_request", rescan),
    )
    assert raw_index["cache_key"] == query["cache_key"]
    assert raw_index["id"] == make_id(
        MEDIA_KEY,
        "raw_index_entry",
        compose_natural_key("raw_index_entry", raw_index),
    )
    assert provider_config["id"] == make_id(
        MEDIA_KEY,
        "provider_config",
        compose_natural_key("provider_config", provider_config),
    )


def test_golden_identity_bounds_evidence_union_and_duration_partition_are_coherent() -> None:
    identities = _golden("identities")
    episodes_file = _golden("episodes")
    episode = episodes_file["episodes"][0]
    media_duration_ms = _golden("pcm")["pcm"]["duration_ms"]

    assert identities["nodes"][0] == _golden("identity_node")
    assert identities["assertions"][0] == _golden("identity_assertion")
    assert identities["works"][0] == _golden("identity_work")
    assert identities["candidates"][0] == _golden("identity_candidate")
    assert episode == _golden("episode")
    assert episodes_file["gaps"][0] == _golden("gap")
    assert episodes_file["durations"] == _golden("durations")

    merge = merge_recording_identities(
        {node["id"]: node["ns"] for node in identities["nodes"]},
        identities["assertions"],
    )
    candidate_members = tuple(identities["candidates"][0]["member_nodes"])
    assert candidate_members in merge.components
    assert len(identities["assertions"]) == 2

    supports = [tuple(span) for span in episode["evidence_support_ms"]]
    start_by, end_after, censored_start, censored_end = proved_bounds(supports)
    assert start_by == episode["start_no_later_than_ms"]
    assert end_after == episode["end_no_earlier_than_ms"]
    assert censored_start == episode["start_no_earlier_than_ms"]
    assert censored_end == episode["end_no_later_than_ms"]
    assert normalise_intervals(supports, media_duration_ms) == supports

    durations, intervals = partition_durations(
        media_duration_ms, episodes_file["episodes"], [(0, media_duration_ms)]
    )
    expected_durations = {
        key: value
        for key, value in episodes_file["durations"].items()
        if key not in {"schema_version", "generated_by"}
    }
    assert durations == expected_durations
    assert sum(durations.values()) == media_duration_ms
    assert gap_intervals(intervals["no_evidence_ms"]) == [
        (gap["start_ms"], gap["end_ms"]) for gap in episodes_file["gaps"]
    ]


@pytest.mark.parametrize("bad_value", ["12000", True])
def test_contract_integer_fields_are_strict(bad_value: object) -> None:
    window = _golden("window")
    window["output_ms"] = bad_value
    with pytest.raises(ValidationError, match="Input should be a valid integer"):
        WindowRecord.model_validate(window)


def test_window_model_validates_transform_span_map_and_mapped_samples() -> None:
    window = _golden("window")
    window.update(
        {
            "support_ms": [18_000, 29_111],
            "transform": {"type": "resample", "rate_e4": 10_800, "semitones": 0},
            "sample_map": {
                "a_num": 10_000,
                "a_den": 10_800,
                "b_samples": 0,
                "uncertainty_ms": 0,
            },
        }
    )
    parsed = WindowRecord.model_validate(window)
    first = Fraction(parsed.sample_map.a_num * 0, parsed.sample_map.a_den)
    last_sample = parsed.output_ms * 16 - 1
    last = (
        Fraction(parsed.sample_map.a_num * last_sample, parsed.sample_map.a_den)
        + parsed.sample_map.b_samples
    )
    assert first == 0
    assert last == Fraction(10_000 * last_sample, 10_800)

    pitch = deepcopy(window)
    pitch.update(
        {
            "support_ms": [18_000, 30_000],
            "transform": {
                "type": "pitch",
                "rate_e4": round(10_000 * (2 ** (2 / 12))),
                "semitones": 2,
            },
            "sample_map": {
                "a_num": 1,
                "a_den": 1,
                "b_samples": 0,
                "uncertainty_ms": 100,
            },
        }
    )
    parsed_pitch = WindowRecord.model_validate(pitch)
    assert parsed_pitch.sample_map.a_num == parsed_pitch.sample_map.a_den == 1
    assert parsed_pitch.support_ms == (18_000, 30_000)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"support_ms": [30_000, 18_000]}, "ordered span"),
        (
            {
                "transform": {"type": "resample", "rate_e4": 10_800, "semitones": 0},
                "sample_map": {
                    "a_num": 1,
                    "a_den": 1,
                    "b_samples": 0,
                    "uncertainty_ms": 0,
                },
            },
            "support_ms span",
        ),
        (
            {
                "support_ms": [18_000, 29_111],
                "transform": {"type": "resample", "rate_e4": 10_800, "semitones": 0},
            },
            "sample_map",
        ),
        (
            {"transform": {"type": "none", "rate_e4": 10_800, "semitones": 0}},
            "none transform",
        ),
    ],
)
def test_window_model_rejects_contradictory_records(change: dict, message: str) -> None:
    window = _golden("window")
    window.update(change)
    with pytest.raises(ValidationError, match=message):
        WindowRecord.model_validate(window)


def test_query_target_union_accepts_both_branches_and_checks_capability() -> None:
    clip = _golden("query")
    QueryRecord.model_validate(clip)
    Draft202012Validator(schema_for(QueryRecord)).validate(clip)

    asset_sha256 = _golden("pcm")["pcm"]["sha256"]
    scanner = deepcopy(clip)
    scanner.update(
        {
            "provider": "audd",
            "capability": "file_scanner",
            "target": {"asset": "pcm", "asset_sha256": asset_sha256},
            "provider_config_version": "audd-unmeasured.json",
            "scan_policy": "every-12",
        }
    )
    scanner["cache_key"] = file_scan_cache_key(
        "pcm", asset_sha256, "audd", "audd-unmeasured.json", "every-12"
    )
    scanner["id"] = make_id(MEDIA_KEY, "query", compose_natural_key("query", scanner))
    QueryRecord.model_validate(scanner)
    Draft202012Validator(schema_for(QueryRecord)).validate(scanner)

    old_nullable_shape = deepcopy(clip)
    old_nullable_shape["target"].update({"asset": None, "asset_sha256": None})
    with pytest.raises(ValidationError):
        QueryRecord.model_validate(old_nullable_shape)
    mismatch = deepcopy(clip)
    mismatch["capability"] = "file_scanner"
    with pytest.raises(ValidationError, match="asset target"):
        QueryRecord.model_validate(mismatch)
    reverse_mismatch = deepcopy(scanner)
    reverse_mismatch["capability"] = "local_index_query"
    with pytest.raises(ValidationError, match="window target"):
        QueryRecord.model_validate(reverse_mismatch)


def test_provider_measurement_state_is_consistent() -> None:
    config = _golden("provider_config")
    assert ProviderConfigRecord.model_validate(config).measured is False
    claimed = deepcopy(config)
    claimed["measured"] = True
    with pytest.raises(ValidationError, match="requires all measurement outputs"):
        ProviderConfigRecord.model_validate(claimed)
    assert list(Draft202012Validator(schema_for(ProviderConfigRecord)).iter_errors(claimed))
    leaked_outputs = deepcopy(config)
    leaked_outputs.update(
        {"adapter_bias_ms": 250, "adapter_bias_uncertainty_ms": 500, "L_min_ms": {"p50": 6500}}
    )
    with pytest.raises(ValidationError, match="must not claim"):
        ProviderConfigRecord.model_validate(leaked_outputs)
    assert list(Draft202012Validator(schema_for(ProviderConfigRecord)).iter_errors(leaked_outputs))
    measured = deepcopy(leaked_outputs)
    measured.update({"measured": True, "source_ids": ["insertion-suite-fixture"]})
    assert ProviderConfigRecord.model_validate(measured).measured is True
    Draft202012Validator(schema_for(ProviderConfigRecord)).validate(measured)


def test_jobs_ddl_has_exact_plan_columns() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript((SCHEMAS / "jobs.sql").read_text(encoding="utf-8"))
    expected = {
        "jobs": [
            "id",
            "media_key",
            "query_id",
            "provider",
            "state",
            "lease_owner",
            "lease_expires_at",
            "heartbeat_at",
            "attempts",
            "physical_attempts",
            "next_retry_at",
            "submission_started_at",
            "submitted_at",
            "remote_ref",
            "reserved_units",
            "reserved_usd",
            "actual_units",
            "actual_usd",
            "result_path",
            "error",
            "created_at",
            "updated_at",
        ],
        "budgets": [
            "media_key",
            "provider",
            "max_requests",
            "max_usd",
            "reserved_requests",
            "reserved_usd",
            "used_requests",
            "used_usd",
        ],
        "connector_jobs": [
            "id",
            "media_key",
            "connector",
            "target_url",
            "cursor",
            "page",
            "page_cap",
            "item_cap",
            "items_fetched",
            "state",
            "lease_owner",
            "lease_expires_at",
            "heartbeat_at",
            "attempts",
            "next_retry_at",
            "result_path",
            "truncated",
            "error",
            "created_at",
            "updated_at",
        ],
    }
    for table, columns in expected.items():
        actual = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        assert actual == columns
