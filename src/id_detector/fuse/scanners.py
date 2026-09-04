"""File-scanner observations entering the same fuser as clip observations.

The plan's rules for this path are short and exact:

* ``logical_trial_id = sha1(provider ‖ chunk_index)`` and ``transform = null``;
* engine agreement is **discounted by the initial 0.5 prior for a second commercial engine**
  (implemented in ``fuse.episodes._independent_trials_e4``);
* **no cascading** — every enabled engine runs over the whole set, and a match from one engine
  never suppresses another engine's query;
* provider-native timelines are aligned through each provider's *documented* anchor conversion,
  which the Stage 3 adapters apply when they build the observation.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from hashlib import sha1

from id_detector.contracts import ObservationRecord, sort_records
from id_detector.fuse.episodes import COMMERCIAL_PROVIDERS, discounted_providers

#: Documented anchor conversions, recorded so fusion can report how a timeline was aligned.
ANCHOR_CONVERSIONS: dict[str, str] = {
    "audd_chunk_offset_to_song_timecode": (
        "mix = chunk offset; reference = song timecode; evidence span delimited by "
        "start_offset/end_offset inside the chunk"
    ),
    "acrcloud_sample_begin_to_db_begin": (
        "mix = offset * 1000 + sample_begin_time_offset_ms; reference = db_begin_time_offset_ms"
    ),
    "acrcloud_offset_to_play_offset_fallback": (
        "mix = offset * 1000; reference = play_offset_ms; explicitly unreliable"
    ),
    "local_fixture_content_hash": "controlled fixture; mix = first matched sample",
}


def scanner_logical_trial_id(provider: str, chunk_index: int) -> str:
    """``sha1(provider ‖ chunk_index)`` exactly as the plan's observation contract spells it."""

    return sha1(f"{provider}|{chunk_index}".encode(), usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class EngineIndependence:
    providers: tuple[str, ...]
    commercial: tuple[str, ...]
    discounted: tuple[str, ...]
    prior_e4: int = 5_000

    def as_dict(self) -> dict[str, object]:
        return {
            "providers": list(self.providers),
            "commercial_providers": list(self.commercial),
            "discounted_providers": list(self.discounted),
            "second_commercial_engine_prior_e4": self.prior_e4,
            "rule": (
                "a trial owned by a second commercial engine contributes 0.5 independent trials "
                "until dependence is measured"
            ),
        }


def engine_independence(
    observations: Sequence[ObservationRecord] | Iterable[ObservationRecord],
) -> EngineIndependence:
    items = list(observations)
    providers = tuple(sorted({item.provider for item in items}))
    commercial = tuple(name for name in providers if name in COMMERCIAL_PROVIDERS)
    return EngineIndependence(
        providers=providers,
        commercial=commercial,
        discounted=tuple(sorted(discounted_providers(items))),
    )


def validate_scanner_observations(
    observations: Sequence[ObservationRecord] | Iterable[ObservationRecord],
) -> None:
    """Reject scanner evidence that would break the plan's fusion contract."""

    for item in observations:
        if item.capability != "file_scanner":
            continue
        if item.transform is not None:
            raise ValueError(f"scanner observation {item.id} must carry transform null")
        if item.anchor is not None and item.anchor.method not in ANCHOR_CONVERSIONS:
            raise ValueError(f"unknown scanner anchor conversion: {item.anchor.method}")


def merge_engine_observations(
    *groups: Sequence[ObservationRecord] | Iterable[ObservationRecord],
) -> tuple[ObservationRecord, ...]:
    """Deterministically merge every enabled engine's observations for one media key.

    No engine is suppressed by another: the merge is a union, and duplicate observation ids (the
    same engine's evidence supplied twice) collapse to one record rather than double-counting.
    """

    merged: dict[str, ObservationRecord] = {}
    for group in groups:
        for item in group:
            merged.setdefault(item.id, item)
    validate_scanner_observations(merged.values())
    return tuple(sort_records(list(merged.values())))
