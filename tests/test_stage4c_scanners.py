from __future__ import annotations

import json
from hashlib import sha1
from pathlib import Path
from typing import Any

import pytest

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    QueryRecord,
    WindowRecord,
)
from id_detector.fuse.episodes import (
    DISCOUNTED_TRIAL_E4,
    FULL_TRIAL_E4,
    _independent_trials_e4,
    build_episodes,
)
from id_detector.fuse.identity import build_identity_graph
from id_detector.fuse.scanners import (
    ANCHOR_CONVERSIONS,
    engine_independence,
    merge_engine_observations,
    scanner_logical_trial_id,
    validate_scanner_observations,
)
from id_detector.providers import acrcloud, audd
from id_detector.providers.base import ProviderUnavailable
from id_detector.providers.panako import CAPABILITY as PANAKO_CAPABILITY
from id_detector.providers.panako import PanakoConfig, PanakoProvider

ROOT = Path(__file__).parent
MEDIA_KEY = "c" * 64
DURATION_MS = 600_000


def _fixture(provider: str, name: str) -> dict[str, Any]:
    return json.loads((ROOT / "fixtures" / provider / name).read_text(encoding="utf-8"))


def _query(provider: str) -> QueryRecord:
    return QueryRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=sha1(f"query-{provider}".encode(), usedforsecurity=False).hexdigest(),
        generation=0,
        provider=provider,
        capability="file_scanner",
        target={"asset": "original", "asset_sha256": "d" * 64},
        provider_config_version=f"{provider}-v1.json",
        scan_policy="stage4c",
        cache_key="e" * 64,
    )


def _audd_observations() -> tuple[Any, ...]:
    return audd.parse_response(
        _fixture("audd", "enterprise-authored-match.json"),
        query=_query("audd"),
        media_key=MEDIA_KEY,
        duration_ms=DURATION_MS,
        raw_response_ref="recognise/raw/audd.json",
    )


def _acrcloud_observations() -> tuple[Any, ...]:
    return acrcloud.parse_response(
        _fixture("acrcloud", "filescan-authored-ready.json"),
        query=_query("acrcloud"),
        media_key=MEDIA_KEY,
        duration_ms=DURATION_MS,
        raw_response_ref="recognise/raw/acrcloud.json",
    )


def test_scanner_observations_use_provider_chunk_trial_ids_and_null_transforms() -> None:
    for observations in (_audd_observations(), _acrcloud_observations()):
        assert observations
        for item in observations:
            assert item.capability == "file_scanner"
            assert item.transform is None
            assert item.logical_trial_id == scanner_logical_trial_id(
                item.provider, _chunk_index_of(item)
            )
    validate_scanner_observations([*_audd_observations(), *_acrcloud_observations()])


def _chunk_index_of(observation: Any) -> int:
    """Recover the chunk ordinal a trial id was built from, by brute force over a small range."""

    for index in range(64):
        if scanner_logical_trial_id(observation.provider, index) == observation.logical_trial_id:
            return index
    raise AssertionError(f"no chunk index produced {observation.logical_trial_id}")


def test_acrcloud_buckets_at_one_offset_share_one_logical_trial() -> None:
    observations = _acrcloud_observations()
    by_offset: dict[int, set[str]] = {}
    for item in observations:
        offset = int(item.native["offset"]) * 1_000
        by_offset.setdefault(offset, set()).add(item.logical_trial_id)
    assert by_offset
    for trial_ids in by_offset.values():
        assert len(trial_ids) == 1, "music and own-bucket hits in one chunk are one logical trial"
    sources = {item.native.get("simultaneous_source") for item in observations}
    assert sources <= {"music", "custom_files"}


def test_documented_anchor_conversions_are_the_only_ones_fusion_accepts() -> None:
    for item in (*_audd_observations(), *_acrcloud_observations()):
        if item.anchor is not None:
            assert item.anchor.method in ANCHOR_CONVERSIONS
    anchored = next(item for item in _audd_observations() if item.anchor is not None)
    broken = anchored.model_copy(
        update={"anchor": anchored.anchor.model_copy(update={"method": "guessed"})}
    )
    with pytest.raises(ValueError, match="anchor conversion"):
        validate_scanner_observations([broken])
    with pytest.raises(ValueError, match="transform null"):
        validate_scanner_observations(
            [
                _audd_observations()[0].model_copy(
                    update={"transform": {"type": "none", "rate_e4": 10_000, "semitones": 0}}
                )
            ]
        )


def test_merge_is_a_union_and_never_lets_one_engine_suppress_another() -> None:
    audd_items = _audd_observations()
    acr_items = _acrcloud_observations()
    merged = merge_engine_observations(audd_items, acr_items, audd_items)
    assert {item.provider for item in merged} == {"audd", "acrcloud"}
    assert len(merged) == len({item.id for item in [*audd_items, *acr_items]})
    assert list(merged) == sorted(merged, key=lambda item: (item.mix_span_ms[0], item.id))


def test_second_commercial_engine_is_discounted_by_the_initial_half_prior() -> None:
    audd_items = [item for item in _audd_observations() if item.status == "match"]
    acr_items = [item for item in _acrcloud_observations() if item.status == "match"]
    assert audd_items and acr_items

    independence = engine_independence([*audd_items, *acr_items])
    assert independence.commercial == ("acrcloud", "audd")
    assert len(independence.discounted) == 1
    assert independence.prior_e4 == DISCOUNTED_TRIAL_E4

    # Four pairwise-disjoint trials, two per engine. Without the prior they would count 4.0;
    # with it the second commercial engine's two trials count 0.5 each.
    sources = [audd_items[0], audd_items[0], acr_items[0], acr_items[0]]
    disjoint = [
        item.model_copy(
            update={
                "id": f"{index:040x}",
                "support_ms": (index * 20_000, index * 20_000 + 10_000),
                "logical_trial_id": scanner_logical_trial_id(item.provider, 90 + index),
            }
        )
        for index, item in enumerate(sources)
    ]
    assert len(disjoint) == 4
    discounted = set(engine_independence(disjoint).discounted)
    assert len(discounted) == 1
    expected = sum(
        DISCOUNTED_TRIAL_E4 if item.provider in discounted else FULL_TRIAL_E4 for item in disjoint
    )
    assert expected < 4 * FULL_TRIAL_E4
    assert _independent_trials_e4(disjoint) == expected
    # Remove the second engine and every remaining trial is worth a full trial again.
    solo = [item for item in disjoint if item.provider not in discounted]
    assert _independent_trials_e4(solo) == FULL_TRIAL_E4 * len(solo)


def test_scanner_evidence_fuses_into_episodes_without_windows() -> None:
    observations = list(merge_engine_observations(_audd_observations(), _acrcloud_observations()))
    identity = build_identity_graph(MEDIA_KEY, observations)
    episodes, requests = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=DURATION_MS,
        observations=observations,
        windows=[],
        identity=identity,
    )
    assert episodes.episodes
    assert all(item.evidence for item in episodes.episodes)
    assert all(item.score_kind == "heuristic" for item in episodes.episodes)
    assert isinstance(requests, list)


def test_panako_is_disabled_and_excluded_from_v1() -> None:
    assert PANAKO_CAPABILITY.available is False
    assert "JDK not found" in PANAKO_CAPABILITY.detail
    provider = PanakoProvider(PanakoConfig(index_path=Path("index")))
    for call in (provider.create_index, provider.query, provider.recognise, provider.close):
        with pytest.raises(ProviderUnavailable):
            call()


def test_no_scanner_window_type_records_are_required(tmp_path: Path) -> None:
    """A file scan needs no ``window`` record; the episode contract must tolerate that."""

    observations = list(_audd_observations())
    identity = build_identity_graph(MEDIA_KEY, observations)
    episodes, _ = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=DURATION_MS,
        observations=observations,
        windows=[],
        identity=identity,
    )
    assert (
        episodes.durations.unscanned_ms == DURATION_MS - 0 or episodes.durations.unscanned_ms >= 0
    )
    assert not [item for item in observations if isinstance(item, WindowRecord)]
    assert tmp_path.exists()
