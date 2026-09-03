"""Pure semantic helpers backing the Stage 0 executable vectors."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any, Literal

OUTPUT_MS = 12_000
SAMPLE_RATE = 16_000
MAX_UNRESOLVED_MS = 120_000
GAP_MIN_MS = 45_000

Interval = tuple[int, int]


@dataclass(frozen=True)
class TransformSpec:
    transform_type: str
    rate_e4: int
    semitones: int
    original_span_ms: int
    output_ms: int
    a_num: int
    a_den: int
    b_samples: int
    uncertainty_ms: int

    def map_output_sample(self, sample: int) -> Fraction:
        return Fraction(self.a_num * sample, self.a_den) + self.b_samples


def _round_fraction(numerator: int, denominator: int) -> int:
    quotient, remainder = divmod(numerator, denominator)
    return quotient + int(remainder * 2 >= denominator)


def transform_spec(
    transform_type: str, *, rate_e4: int = 10_000, semitones: int = 0
) -> TransformSpec:
    """Build the exact 12-second transform contract from revision 5."""

    if transform_type not in {"none", "resample", "tempo", "pitch"}:
        raise ValueError(f"unknown transform type: {transform_type}")
    if rate_e4 <= 0:
        raise ValueError("rate_e4 must be positive")
    if transform_type == "pitch":
        derived_rate = round(10_000 * (2 ** (semitones / 12)))
        if rate_e4 not in {10_000, derived_rate}:
            raise ValueError("pitch rate_e4 does not agree with semitones")
        return TransformSpec(
            transform_type, derived_rate, semitones, OUTPUT_MS, OUTPUT_MS, 1, 1, 0, 100
        )
    if transform_type == "none":
        if rate_e4 != 10_000 or semitones != 0:
            raise ValueError("none transform must have rate_e4=10000 and semitones=0")
        return TransformSpec(transform_type, rate_e4, 0, OUTPUT_MS, OUTPUT_MS, 1, 1, 0, 0)
    if semitones != 0:
        raise ValueError("rate transforms must have semitones=0")
    return TransformSpec(
        transform_type,
        rate_e4,
        0,
        _round_fraction(OUTPUT_MS * 10_000, rate_e4),
        OUTPUT_MS,
        10_000,
        rate_e4,
        0,
        0 if transform_type == "resample" else 100,
    )


def proved_bounds(supports: Sequence[Interval]) -> tuple[int, int, None, None]:
    """Return only the two facts proved by positive fingerprint support."""

    if not supports:
        raise ValueError("at least one final-match support is required")
    return min(end for _, end in supports), max(start for start, _ in supports), None, None


def normalise_intervals(intervals: Iterable[Interval], limit: int) -> list[Interval]:
    clipped = sorted((max(0, start), min(limit, end)) for start, end in intervals if end > start)
    merged: list[Interval] = []
    for start, end in clipped:
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract_intervals(
    base: Iterable[Interval], removed: Iterable[Interval], limit: int
) -> list[Interval]:
    result = normalise_intervals(base, limit)
    for cut_start, cut_end in normalise_intervals(removed, limit):
        next_result: list[Interval] = []
        for start, end in result:
            if cut_end <= start or cut_start >= end:
                next_result.append((start, end))
                continue
            if start < cut_start:
                next_result.append((start, cut_start))
            if cut_end < end:
                next_result.append((cut_end, end))
        result = next_result
    return result


def interval_length(intervals: Iterable[Interval], limit: int) -> int:
    return sum(end - start for start, end in normalise_intervals(intervals, limit))


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def partition_durations(
    media_duration_ms: int,
    episodes: Sequence[Any],
    scanned_ms: Sequence[Interval],
) -> tuple[dict[str, int], dict[str, list[Interval]]]:
    """Build a precedence-based, overlap-safe partition of the full media duration."""

    if media_duration_ms < 0:
        raise ValueError("media duration cannot be negative")

    clear_support: list[Interval] = []
    unclear_support: list[Interval] = []
    episode_hulls: list[tuple[Any, int, int]] = []
    predicted: list[Interval] = []
    for episode in episodes:
        supports = list(_field(episode, "evidence_support_ms", []))
        if not supports:
            continue
        supports = normalise_intervals(supports, media_duration_ms)
        if not supports:
            continue
        badge = _field(episode, "badge")
        if badge in {"possible", "likely", "verified"}:
            clear_support.extend(supports)
        else:
            unclear_support.extend(supports)
        episode_hulls.append((episode, supports[0][0], supports[-1][1]))

        start_pi = _field(episode, "start_pi")
        end_pi = _field(episode, "end_pi")
        if start_pi and end_pi and _field(start_pi, "calibrated") and _field(end_pi, "calibrated"):
            predicted.append((_field(start_pi, "lo"), _field(end_pi, "hi")))

    clear_support = normalise_intervals(clear_support, media_duration_ms)
    unclear_support = normalise_intervals(unclear_support, media_duration_ms)
    predicted = subtract_intervals(predicted, [*clear_support, *unclear_support], media_duration_ms)

    all_evidence = normalise_intervals([*clear_support, *unclear_support], media_duration_ms)
    unresolved: list[Interval] = []
    for episode, first, last in episode_hulls:
        if _field(episode, "start_no_earlier_than_ms") is None:
            prior_ends = [end for _, end in all_evidence if end <= first]
            unresolved.append((max(first - MAX_UNRESOLVED_MS, max(prior_ends, default=0)), first))
        if _field(episode, "end_no_later_than_ms") is None:
            next_starts = [start for start, _ in all_evidence if start >= last]
            unresolved.append(
                (last, min(last + MAX_UNRESOLVED_MS, min(next_starts, default=media_duration_ms)))
            )
    unresolved = subtract_intervals(
        unresolved, [*clear_support, *unclear_support, *predicted], media_duration_ms
    )

    occupied = normalise_intervals(
        [*clear_support, *predicted, *unresolved, *unclear_support], media_duration_ms
    )
    no_evidence = subtract_intervals(scanned_ms, occupied, media_duration_ms)
    assigned = normalise_intervals([*occupied, *no_evidence], media_duration_ms)
    unscanned = subtract_intervals([(0, media_duration_ms)], assigned, media_duration_ms)

    interval_parts = {
        "evidence_supported_ms": clear_support,
        "predicted_episode_ms": predicted,
        "unresolved_boundary_ms": unresolved,
        "unclear_ms": subtract_intervals(unclear_support, clear_support, media_duration_ms),
        "no_evidence_ms": no_evidence,
        "unscanned_ms": unscanned,
    }
    duration_parts = {
        name: interval_length(intervals, media_duration_ms)
        for name, intervals in interval_parts.items()
    }
    if sum(duration_parts.values()) != media_duration_ms:
        raise AssertionError("duration partition does not cover the media exactly once")
    return duration_parts, interval_parts


def gap_intervals(no_evidence_ms: Sequence[Interval]) -> list[Interval]:
    """Return maximal scanned/no-evidence regions large enough to be labelled gaps."""

    if not no_evidence_ms:
        return []
    limit = max(end for _, end in no_evidence_ms)
    return [
        item
        for item in normalise_intervals(no_evidence_ms, limit)
        if item[1] - item[0] >= GAP_MIN_MS
    ]


def _offset_ms(value: Any) -> int:
    return int((Decimal(str(value)) * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def aggregate_shazam_anchor(
    matches: Sequence[Mapping[str, Any]],
    *,
    support_start_ms: int,
    adapter_bias_ms: int,
    adapter_bias_uncertainty_ms: int,
    adapter_measured: bool,
) -> dict[str, int | bool | str] | None:
    """Cluster all Shazam offsets and apply the immutable adapter bias."""

    offsets = sorted(_offset_ms(match["offset"]) for match in matches if "offset" in match)
    if not offsets:
        return None
    candidates: list[list[int]] = []
    for index, start in enumerate(offsets):
        candidates.append([offset for offset in offsets[index:] if offset - start <= 1500])
    cluster = max(candidates, key=lambda values: (len(values), -values[0]))
    if len(cluster) * 2 < len(offsets):
        return None
    middle = len(cluster) // 2
    if len(cluster) % 2:
        median = cluster[middle]
    else:
        median = _round_fraction(cluster[middle - 1] + cluster[middle], 2)
    dispersion = max(abs(offset - median) for offset in cluster)
    return {
        "mix_anchor_ms": support_start_ms,
        "ref_anchor_ms": median - adapter_bias_ms,
        "uncertainty_ms": max(dispersion, adapter_bias_uncertainty_ms),
        "reliable": adapter_measured,
        "method": "shazam_offset_cluster_median",
        "bias_applied_ms": adapter_bias_ms,
    }


RECORDING_NAMESPACES = {
    "isrc",
    "mb_recording",
    "shazam",
    "deezer",
    "apple",
    "spotify",
    "acr",
    "audd",
    "beatport",
    "soundcloud",
}


@dataclass(frozen=True)
class IdentityMergeResult:
    components: tuple[tuple[str, ...], ...]
    contested: tuple[tuple[str, ...], ...]
    refused: tuple[tuple[str, str], ...]


def text_equality_relation() -> Literal["same_work"]:
    """Normalised artist/title equality is never recording equivalence."""

    return "same_work"


def merge_recording_identities(
    nodes: Mapping[str, str], assertions: Sequence[Mapping[str, Any]]
) -> IdentityMergeResult:
    """Apply corroboration, conflict veto, and late-conflict contesting to recording unions."""

    parent = {node: node for node in nodes}
    contested_roots: set[str] = set()
    conflicts_seen: list[tuple[str, str]] = []
    refused: list[tuple[str, str]] = []

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def members(root: str) -> set[str]:
        return {node for node in parent if find(node) == root}

    corroboration: dict[tuple[str, str], set[str]] = {}
    privileged: set[tuple[str, str]] = set()
    for assertion in assertions:
        if assertion["relation"] != "same_recording":
            continue
        pair = tuple(sorted((assertion["a"], assertion["b"])))
        corroboration.setdefault(pair, set()).add(str(assertion["independent_of"]))
        source_kind = str(assertion.get("source", {}).get("kind", ""))
        if source_kind in {"aligned_held_reference", "audited_truth"}:
            privileged.add(pair)

    for assertion in assertions:
        a, b = str(assertion["a"]), str(assertion["b"])
        relation = assertion["relation"]
        if a not in parent or b not in parent:
            raise ValueError("assertion references an unknown identity node")
        if relation == "conflicts":
            if find(a) == find(b):
                contested_roots.add(find(a))
            else:
                conflicts_seen.append((a, b))
            continue
        if relation != "same_recording":
            continue
        pair = tuple(sorted((a, b)))
        has_recording_id = nodes[a] in RECORDING_NAMESPACES or nodes[b] in RECORDING_NAMESPACES
        eligible = (
            has_recording_id and len(corroboration.get(pair, set())) >= 2
        ) or pair in privileged
        if not eligible or find(a) == find(b):
            continue
        left, right = members(find(a)), members(find(b))
        vetoed = any(
            (x in left and y in right) or (x in right and y in left) for x, y in conflicts_seen
        )
        if vetoed:
            refused.append(pair)
            continue
        root_a, root_b = find(a), find(b)
        parent[root_b] = root_a
        if root_b in contested_roots:
            contested_roots.add(root_a)
        contested_roots.discard(root_b)

    grouped: dict[str, list[str]] = {}
    for node in parent:
        grouped.setdefault(find(node), []).append(node)
    components = tuple(sorted(tuple(sorted(group)) for group in grouped.values()))
    contested = tuple(
        component
        for component in components
        if any(find(node) in contested_roots for node in component)
    )
    return IdentityMergeResult(components, contested, tuple(sorted(set(refused))))
