"""Identity graph v0: work equality, corroborated recordings, and conflict vetoes."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    HintRecord,
    IdentitiesRecord,
    IdentityAssertion,
    IdentityCandidate,
    IdentityNode,
    IdentityWork,
    ObservationRecord,
    compose_natural_key,
    make_id,
)
from id_detector.io import atomic_write_json, write_completion_sidecar
from id_detector.semantics import RECORDING_NAMESPACES, merge_recording_identities

_ALLOWED_NAMESPACES = RECORDING_NAMESPACES | {"mb_work", "mb_release", "text"}
_NS_ALIASES = {
    "apple_id": "apple",
    "apple_music": "apple",
    "deezer_id": "deezer",
    "isrc_id": "isrc",
    "musicbrainz_recording": "mb_recording",
    "musicbrainz_work": "mb_work",
    "spotify_id": "spotify",
}


def normalise_text(value: str | None) -> str:
    """Normalise display text without erasing version-significant words."""

    if not value:
        return ""
    normalised = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"\s+", " ", normalised)


def work_text_key(observation: ObservationRecord) -> str | None:
    artist = normalise_text(observation.raw_label.artist)
    title = normalise_text(observation.raw_label.title)
    return f"{artist}|{title}" if artist and title else None


def display_label(observation: ObservationRecord) -> str:
    artist = observation.raw_label.artist or "Unknown artist"
    title = observation.raw_label.title or "Unknown title"
    return f"{artist} - {title}"


def _provider_nodes(observation: ObservationRecord) -> list[tuple[str, str]]:
    nodes: list[tuple[str, str]] = []
    for raw_ns, raw_value in sorted(observation.provider_ids.items()):
        ns = _NS_ALIASES.get(raw_ns.casefold(), raw_ns.casefold())
        if ns not in _ALLOWED_NAMESPACES or isinstance(raw_value, (dict, list, bool)):
            continue
        value = str(raw_value).strip()
        if value:
            nodes.append((ns, value))
    return nodes


def _assertion(
    media_key: str,
    *,
    a: str,
    b: str,
    relation: str,
    source_kind: str,
    source_record_id: str,
    independent_of: str,
    confidence: int,
) -> IdentityAssertion:
    left, right = sorted((a, b))
    values = {
        "a": left,
        "b": right,
        "relation": relation,
        "source": {"record_id": source_record_id},
    }
    return IdentityAssertion(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(
            media_key,
            "identity_assertion",
            compose_natural_key("identity_assertion", values),
        ),
        a=left,
        b=right,
        relation=relation,
        source={"kind": source_kind, "record_id": source_record_id},
        independent_of=independent_of,
        confidence=confidence,
    )


@dataclass(frozen=True)
class IdentityBuildResult:
    record: IdentitiesRecord
    observation_candidates: dict[str, str]
    hint_candidates: dict[str, str]
    hint_work_ids: dict[str, str]
    candidate_labels: dict[str, tuple[str, str]]
    recording_supported: frozenset[str]


class _UnionFind:
    def __init__(self, values: list[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        keep, discard = sorted((left_root, right_root))
        self.parent[discard] = keep


def build_identity_graph(
    media_key: str,
    observations: list[ObservationRecord] | tuple[ObservationRecord, ...],
    *,
    hints: list[HintRecord] | tuple[HintRecord, ...] = (),
    extra_assertions: list[IdentityAssertion] | tuple[IdentityAssertion, ...] = (),
    prior_recording_components: tuple[tuple[str, ...], ...] = (),
) -> IdentityBuildResult:
    """Resolve final matches into deterministic work and recording components.

    The Stage 0 ``merge_recording_identities`` helper remains the sole implementation of
    corroboration, privileged-source handling, conflict veto, and late-conflict contesting.
    """

    final_matches = sorted(
        (item for item in observations if item.is_final and item.status == "match"),
        key=lambda item: item.id,
    )
    node_labels: dict[str, str] = {}
    assertion_by_id: dict[str, IdentityAssertion] = {item.id: item for item in extra_assertions}
    observation_nodes: dict[str, list[str]] = {}
    observation_text: dict[str, str] = {}
    recording_node_sources: dict[str, set[str]] = {}
    hint_text: dict[str, str] = {}

    for observation in final_matches:
        label = display_label(observation)
        key = work_text_key(observation)
        text_node = f"text:{key}" if key is not None else None
        if text_node is not None:
            node_labels[text_node] = label
            observation_text[observation.id] = text_node
        provider_nodes = []
        for ns, value in _provider_nodes(observation):
            node_id = f"{ns}:{value}"
            node_labels.setdefault(node_id, label)
            provider_nodes.append(node_id)
            if ns in RECORDING_NAMESPACES:
                recording_node_sources.setdefault(node_id, set()).add(
                    f"provider:{observation.provider}"
                )
        observation_nodes[observation.id] = provider_nodes

        source_kind = (
            "aligned_held_reference"
            if observation.provider == "local_fixture"
            else "provider_observation"
        )
        independent = f"provider:{observation.provider}"
        if text_node is not None:
            for provider_node in provider_nodes:
                item = _assertion(
                    media_key,
                    a=text_node,
                    b=provider_node,
                    relation="same_work",
                    source_kind=source_kind,
                    source_record_id=observation.id,
                    independent_of=independent,
                    confidence=10_000 if observation.provider == "local_fixture" else 8_000,
                )
                assertion_by_id[item.id] = item
        recording_nodes = [
            node for node in provider_nodes if node.split(":", 1)[0] in RECORDING_NAMESPACES
        ]
        for index, left in enumerate(recording_nodes):
            for right in recording_nodes[index + 1 :]:
                item = _assertion(
                    media_key,
                    a=left,
                    b=right,
                    relation="same_recording",
                    source_kind=source_kind,
                    source_record_id=observation.id,
                    independent_of=independent,
                    confidence=10_000 if observation.provider == "local_fixture" else 9_000,
                )
                assertion_by_id[item.id] = item

    for hint in sorted(hints, key=lambda item: item.id):
        if (
            hint.mirror_status != "verified"
            or hint.flags.id_unknown
            or not hint.artist
            or not hint.title
        ):
            continue
        artist = normalise_text(hint.artist)
        title = normalise_text(hint.title)
        if not artist or not title:
            continue
        text_node = f"text:{artist}|{title}"
        node_labels.setdefault(text_node, f"{hint.artist} - {hint.title}")
        hint_text[hint.id] = text_node

    assertions = sorted(assertion_by_id.values(), key=lambda item: item.id)
    # Reuse the Stage 0 helper; this call is intentionally not duplicated below.
    merged = merge_recording_identities(
        {node: node.split(":", 1)[0] for node in node_labels},
        [item.model_dump(mode="json") for item in assertions],
        prior_components=prior_recording_components,
    )
    recording_component_by_node = {
        node: component for component in merged.components for node in component
    }

    work_union = _UnionFind(list(node_labels))
    for assertion in assertions:
        if assertion.relation in {"same_work", "same_recording"}:
            work_union.union(assertion.a, assertion.b)
    work_groups: dict[str, list[str]] = {}
    for node in node_labels:
        work_groups.setdefault(work_union.find(node), []).append(node)

    works: list[IdentityWork] = []
    work_id_by_node: dict[str, str] = {}
    for members in sorted(sorted(group) for group in work_groups.values()):
        text_keys = [node.removeprefix("text:") for node in members if node.startswith("text:")]
        normalised_key = min(text_keys) if text_keys else min(members)
        work_id = make_id(
            media_key,
            "identity_work",
            compose_natural_key("identity_work", {"normalised_artist_title": normalised_key}),
        )
        works.append(
            IdentityWork(
                schema_version=SCHEMA_VERSION,
                generated_by=GENERATED_BY,
                work_id=work_id,
                member_nodes=members,
            )
        )
        work_id_by_node.update({node: work_id for node in members})

    provider_linked_text = {
        observation_text[observation_id]
        for observation_id, nodes in observation_nodes.items()
        if nodes and observation_id in observation_text
    }
    candidate_components = [
        component
        for component in merged.components
        if any(node.split(":", 1)[0] in RECORDING_NAMESPACES for node in component)
        or any(node.startswith("text:") and node not in provider_linked_text for node in component)
    ]
    contested_components = {tuple(item) for item in merged.contested}
    conflict_nodes: dict[tuple[str, ...], set[str]] = {
        tuple(component): set() for component in candidate_components
    }
    for assertion in assertions:
        if assertion.relation != "conflicts":
            continue
        for component in candidate_components:
            if assertion.a in component or assertion.b in component:
                conflict_nodes[tuple(component)].update((assertion.a, assertion.b))

    preliminary: list[tuple[tuple[str, ...], str, str, bool, list[str]]] = []
    for component in sorted(candidate_components):
        work_id = work_id_by_node[component[0]]
        canonical_id = make_id(
            media_key,
            "identity_candidate",
            compose_natural_key("identity_candidate", {"member_nodes": list(component)}),
        )
        conflicts = sorted(conflict_nodes[tuple(component)])
        preliminary.append(
            (component, canonical_id, work_id, component in contested_components, conflicts)
        )
    by_work: dict[str, list[str]] = {}
    for _, canonical_id, work_id, _, _ in preliminary:
        by_work.setdefault(work_id, []).append(canonical_id)
    candidates = [
        IdentityCandidate(
            schema_version=SCHEMA_VERSION,
            generated_by=GENERATED_BY,
            canonical_id=canonical_id,
            work_id=work_id,
            member_nodes=list(component),
            alternatives=sorted(item for item in by_work[work_id] if item != canonical_id),
            contested=contested,
            conflicts=conflicts,
        )
        for component, canonical_id, work_id, contested, conflicts in preliminary
    ]
    candidate_by_node = {
        node: candidate.canonical_id for candidate in candidates for node in candidate.member_nodes
    }
    observation_candidates: dict[str, str] = {}
    hint_candidates: dict[str, str] = {}
    hint_work_ids: dict[str, str] = {}
    candidate_labels: dict[str, tuple[str, str]] = {}
    for observation in final_matches:
        provider_nodes = observation_nodes[observation.id]
        preferred = next(
            (node for node in provider_nodes if node.startswith(f"{observation.provider}:")),
            provider_nodes[0] if provider_nodes else observation_text.get(observation.id),
        )
        if preferred is None:
            continue
        component = recording_component_by_node.get(preferred, (preferred,))
        candidate_id = candidate_by_node[component[0]]
        observation_candidates[observation.id] = candidate_id
        candidate_labels.setdefault(
            candidate_id,
            (
                observation.raw_label.artist or "Unknown artist",
                observation.raw_label.title or "Unknown title",
            ),
        )
    candidates_by_work = {
        work_id: sorted(
            candidate.canonical_id for candidate in candidates if candidate.work_id == work_id
        )
        for work_id in {candidate.work_id for candidate in candidates}
    }
    hint_by_id = {hint.id: hint for hint in hints}
    for hint_id, text_node in sorted(hint_text.items()):
        work_id = work_id_by_node[text_node]
        hint_work_ids[hint_id] = work_id
        work_candidates = candidates_by_work.get(work_id, [])
        if work_candidates:
            hint_candidates[hint_id] = work_candidates[0]
            hint = hint_by_id[hint_id]
            candidate_labels.setdefault(
                work_candidates[0], (hint.artist or "Unknown artist", hint.title or "Unknown title")
            )

    recording_supported = frozenset(
        candidate.canonical_id
        for candidate in candidates
        if not candidate.contested
        and (
            len(
                [
                    node
                    for node in candidate.member_nodes
                    if node.split(":", 1)[0] in RECORDING_NAMESPACES
                ]
            )
            >= 2
            or any(
                len(recording_node_sources.get(node, set())) >= 2 for node in candidate.member_nodes
            )
        )
    )
    record = IdentitiesRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        nodes=[
            IdentityNode(
                schema_version=SCHEMA_VERSION,
                generated_by=GENERATED_BY,
                id=node,
                ns=node.split(":", 1)[0],
                label=node_labels[node],
            )
            for node in sorted(node_labels)
        ],
        assertions=assertions,
        works=sorted(works, key=lambda item: item.work_id),
        candidates=sorted(candidates, key=lambda item: item.canonical_id),
    )
    return IdentityBuildResult(
        record=record,
        observation_candidates=observation_candidates,
        hint_candidates=hint_candidates,
        hint_work_ids=hint_work_ids,
        candidate_labels=candidate_labels,
        recording_supported=recording_supported,
    )


def write_identity_graph(
    media_dir: Path,
    generation: int,
    build: IdentityBuildResult,
    *,
    observations_path: Path,
    hints_path: Path | None = None,
) -> Path:
    path = media_dir / "fuse" / f"identities.gen{generation}.json"
    atomic_write_json(path, build.record)
    upstream = {observations_path.relative_to(media_dir).as_posix(): observations_path}
    if hints_path is not None:
        upstream[hints_path.relative_to(media_dir).as_posix()] = hints_path
    write_completion_sidecar(path, upstream)
    return path
