"""Reply, correction, copy, and provenance resolution for parsed hints."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha1

from id_detector.contracts import HintRecord, HintRelation, compose_natural_key, make_id
from id_detector.hints.parse import HintInput, parse_text_units

_MENTION = re.compile(r"^\s*@([\w.-]+)\s*:?")
_CORRELATED_CONNECTORS = {"mixesdb", "1001tl"}


def _normalise(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"\([^)]*(?:remix|edit|bootleg|vip|dub|mix)[^)]*\)", "", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _identity_key(hint: HintRecord) -> tuple[str, str] | None:
    artist, title = _normalise(hint.artist), _normalise(hint.title)
    return (artist, title) if artist and title and not hint.flags.id_unknown else None


def _time_distance(left: HintRecord, right: HintRecord) -> int | None:
    if left.position_range_ms is None or right.position_range_ms is None:
        return None
    if min(left.position_range_ms[1], right.position_range_ms[1]) >= max(
        left.position_range_ms[0], right.position_range_ms[0]
    ):
        return 0
    return min(
        abs(left.position_range_ms[0] - right.position_range_ms[1]),
        abs(right.position_range_ms[0] - left.position_range_ms[1]),
    )


@dataclass(frozen=True)
class _SourceBinding:
    source: HintInput
    unit_index: int


def _source_bindings(
    media_key: str, duration_ms: int, inputs: list[HintInput] | tuple[HintInput, ...]
) -> dict[str, _SourceBinding]:
    bindings: dict[str, _SourceBinding] = {}
    for source in inputs:
        units = parse_text_units(
            source.text,
            media_duration_ms=duration_ms,
            comment_timestamp_ms=source.position_ms,
            comment_position_kind=source.position_kind,
            structured_tracklist=source.structured_tracklist,
            enforce_block_acceptance=True,
        )
        for index in range(len(units)):
            source_id = f"{source.source_record_id}:{index}"
            hint_id = make_id(
                media_key,
                "hint",
                compose_natural_key(
                    "hint", {"connector": source.connector, "source_record_id": source_id}
                ),
            )
            bindings[hint_id] = _SourceBinding(source, index)
    return bindings


class _UnionFind:
    def __init__(self, values: list[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: str, right: str) -> None:
        roots = sorted((self.find(left), self.find(right)))
        if roots[0] != roots[1]:
            self.parent[roots[1]] = roots[0]


def _copy_confidence(left: HintRecord, right: HintRecord) -> int:
    left_key, right_key = _identity_key(left), _identity_key(right)
    if left_key == right_key:
        return 9_000
    assert left_key is not None and right_key is not None
    artist = SequenceMatcher(None, left_key[0], right_key[0]).ratio()
    title = SequenceMatcher(None, left_key[1], right_key[1]).ratio()
    return 7_500 if min(artist, title) >= 0.88 else 0


def apply_relations(
    media_key: str,
    duration_ms: int,
    hints: list[HintRecord] | tuple[HintRecord, ...],
    inputs: list[HintInput] | tuple[HintInput, ...],
) -> list[HintRecord]:
    """Resolve bounded relations and collapse correlated text into provenance groups."""

    bindings = _source_bindings(media_key, duration_ms, inputs)
    ordered = sorted(
        hints,
        key=lambda item: (
            bindings[item.id].source.position_ms
            if item.id in bindings and bindings[item.id].source.position_ms is not None
            else duration_ms + 1,
            item.id,
        ),
    )
    by_source: dict[tuple[str, str], list[HintRecord]] = {}
    for hint in ordered:
        binding = bindings.get(hint.id)
        if binding is not None:
            by_source.setdefault(
                (binding.source.connector, binding.source.source_record_id), []
            ).append(hint)

    updated: dict[str, HintRecord] = {hint.id: hint for hint in hints}
    earlier_by_permalink: dict[str, list[HintRecord]] = {}
    for hint in ordered:
        binding = bindings.get(hint.id)
        if binding is None:
            continue
        source = binding.source
        relations = list(updated[hint.id].relations)
        parent: HintRecord | None = None
        relation_confidence = 0

        if source.parent_source_id:
            candidates = by_source.get((source.connector, source.parent_source_id), [])
            if hint.position_range_ms is None:
                compatible = candidates if len(candidates) == 1 else []
            else:
                compatible = [
                    candidate
                    for candidate in candidates
                    if _time_distance(hint, candidate) is not None
                    and (_time_distance(hint, candidate) or 0) <= 60_000
                ]
            if compatible:
                parent = min(
                    compatible,
                    key=lambda item: (_time_distance(hint, item) or 0, item.id),
                )
                relation_confidence = 9_500
        elif source.connector == "sc_comments":
            mention = _MENTION.match(source.text)
            if mention:
                candidates = [
                    candidate
                    for candidate in earlier_by_permalink.get(mention.group(1).casefold(), [])
                    if (_time_distance(hint, candidate) is not None)
                    and (_time_distance(hint, candidate) or 0) <= 60_000
                ]
                if len(candidates) == 1:
                    parent = candidates[0]
                    relation_confidence = 8_000

        if parent is not None:
            relations.append(
                HintRelation(
                    type="corrects" if hint.kind == "correction" else "replies_to",
                    hint_id=parent.id,
                    confidence=relation_confidence,
                )
            )
            changes: dict[str, object] = {"relations": relations}
            if hint.kind == "correction":
                changes["parse_confidence"] = hint.parse_confidence * relation_confidence // 10_000
                changes["position_range_ms"] = parent.position_range_ms
                changes["position_kind"] = parent.position_kind
                changes["temporal_precision_ms"] = parent.temporal_precision_ms
            updated[hint.id] = hint.model_copy(update=changes)

        if source.author_permalink:
            earlier_by_permalink.setdefault(source.author_permalink.casefold(), []).append(hint)

    result = list(updated.values())
    union = _UnionFind([hint.id for hint in result])
    copy_pairs: dict[tuple[str, str], int] = {}
    for index, left in enumerate(result):
        if _identity_key(left) is None:
            continue
        for right in result[index + 1 :]:
            if left.connector == right.connector or _identity_key(right) is None:
                continue
            confidence = _copy_confidence(left, right)
            if confidence == 0:
                continue
            distance = _time_distance(left, right)
            correlated_family = (
                left.connector in _CORRELATED_CONNECTORS
                or right.connector in _CORRELATED_CONNECTORS
                or left.mirror_of is not None
                or right.mirror_of is not None
            )
            if (distance is not None and distance <= 60_000) or correlated_family:
                union.union(left.id, right.id)
                copy_pairs[tuple(sorted((left.id, right.id)))] = confidence

    groups: dict[str, list[str]] = {}
    for hint in result:
        groups.setdefault(union.find(hint.id), []).append(hint.id)
    final: list[HintRecord] = []
    by_id = {hint.id: hint for hint in result}
    for members in groups.values():
        members.sort()
        group_id = sha1("|".join(members).encode("utf-8"), usedforsecurity=False).hexdigest()
        representative = members[0]
        for hint_id in members:
            hint = by_id[hint_id]
            relations = list(hint.relations)
            if hint_id != representative:
                confidence = copy_pairs.get(tuple(sorted((representative, hint_id))), 7_500)
                relations.append(
                    HintRelation(type="copies", hint_id=representative, confidence=confidence)
                )
            final.append(
                hint.model_copy(
                    update={
                        "relations": sorted(
                            relations, key=lambda item: (item.type, item.hint_id, item.confidence)
                        ),
                        "provenance_group": group_id,
                    }
                )
            )
    return sorted(
        final,
        key=lambda item: (
            item.position_range_ms[0] if item.position_range_ms is not None else duration_ms,
            item.id,
        ),
    )
