"""Identity graph v0: work equality, corroborated recordings, and conflict vetoes."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
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


def candidate_recording_supported(
    *,
    contested: bool,
    member_nodes: Iterable[str],
    recording_node_sources: Mapping[str, set[str]],
) -> bool:
    """Authoritative recording-support test, shared by fusion and calibration reconstruction.

    A candidate's recording (version) identity is *supported* when it is not contested and either
    at least two of its member nodes are recording-specific ids, or a single recording node is
    asserted by at least two independent sources.  Computed identically at analyse time (the
    identity graph) and at fit time (``calibrate.reconstruct``) so the version feature can never
    differ between the two.
    """

    if contested:
        return False
    nodes = list(member_nodes)
    recording_nodes = [node for node in nodes if node.split(":", 1)[0] in RECORDING_NAMESPACES]
    if len(recording_nodes) >= 2:
        return True
    return any(len(recording_node_sources.get(node, ())) >= 2 for node in nodes)


def recording_node_sources_from_observations(
    observations: Iterable[ObservationRecord],
) -> dict[str, set[str]]:
    """Recompute, from persisted observations, the independent sources for each recording node.

    Mirrors the analyse-time construction inside :func:`build_identity_graph` (final matches only,
    provider node ids, one ``provider:<name>`` source per node) so that
    :func:`candidate_recording_supported` sees the same evidence at fit time as at analyse time.
    """

    sources: dict[str, set[str]] = {}
    for observation in observations:
        if not (observation.is_final and observation.status == "match"):
            continue
        for ns, value in _provider_nodes(observation):
            if ns in RECORDING_NAMESPACES:
                sources.setdefault(f"{ns}:{value}", set()).add(f"provider:{observation.provider}")
    return sources


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


def _within_one_edit(x: str, y: str) -> bool:
    """True if x and y are equal or one insertion/deletion/substitution apart (Levenshtein ≤ 1)."""

    if x == y:
        return True
    lx, ly = len(x), len(y)
    if abs(lx - ly) > 1:
        return False
    if lx == ly:  # a single substitution
        return sum(cx != cy for cx, cy in zip(x, y, strict=True)) == 1
    if lx > ly:  # make x the shorter one; y is x with one extra char
        x, y = y, x
    i = j = 0
    edited = False
    while i < len(x) and j < len(y):
        if x[i] == y[j]:
            i += 1
            j += 1
        elif edited:
            return False
        else:
            edited = True
            j += 1
    return True


def _word_sets_corroborate(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether a hint word set and an audio word set name the same work (order-independent).

    Exact rule: one set fully contains the other (≥2 words), so collaborators/extra words never
    block a genuine ID.  Tolerant rule: everything matches except a single long title token that is
    only a near-spelling apart (e.g. "clubgrls" vs "clubgirls") — that is a crowd ID of the SAME
    track, not a different one, so it should corroborate the audio match rather than duplicate it.
    """

    if min(len(a), len(b)) < 2:
        return False
    if a <= b or b <= a:
        return True
    a_only = a - b
    b_only = b - a
    if len(a_only) == 1 and len(b_only) == 1 and (a & b):
        (x,) = tuple(a_only)
        (y,) = tuple(b_only)
        if len(x) >= 4 and len(y) >= 4 and _within_one_edit(x, y):
            return True
    return False


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

    # Casual "ID" answers in comments are frequently written "Title - Artist", the reverse of the
    # provider label order ("Artist - Title").  That leaves the answer's text node (e.g.
    # ``text:senses|bakey``) in an identity component of its own, unable to corroborate the audio
    # match (``text:bakey|senses``).  Union a hint text node with an audio text node whenever their
    # normalised token SETS are equal regardless of order, so the answer joins the right work.
    audio_text_nodes = {node for node in observation_text.values() if node}

    def _words(node: str) -> frozenset[str]:
        return frozenset(re.findall(r"[a-z0-9]+", node.removeprefix("text:")))

    # Match on the WORD set (not the two fields) so order AND extra collaborators don't block it:
    # the answer "breaka breaka - bushbaby" must still corroborate the audio "Bushbaby & Eloq -
    # Breaka Breaka".  Require one word set to fully contain the other (with ≥2 words) so a genuine
    # ID lines up while an unrelated answer, whose words are not a subset, never does.
    audio_words = [(node, _words(node)) for node in sorted(audio_text_nodes)]
    for hint_id, text_node in sorted(hint_text.items()):
        if text_node in audio_text_nodes:
            continue
        hint_words = _words(text_node)
        for audio_node, words in audio_words:
            if _word_sets_corroborate(hint_words, words):
                item = _assertion(
                    media_key,
                    a=text_node,
                    b=audio_node,
                    relation="same_work",
                    source_kind="hint_text_match",
                    source_record_id=hint_id,
                    independent_of="hint:comment_answer",
                    confidence=7_000,
                )
                assertion_by_id[item.id] = item

    # Two crowd IDs of the SAME track — "Entasia - Satalite" and "Entasia - Satalite (unreleased)",
    # "…Pump It" and "…Pump It (Club Royalty 3)" — otherwise land in separate works and the track is
    # listed twice.  Union hint text nodes with each OTHER on the same conservative word-set rule
    # (subset with >=2 words, or a near-spelled long token), so a track named more than once in the
    # comments is one identity.  Audio-matched hints are excluded — they already joined the audio
    # work above, and a distinct node only ever merges the pair, never a wider chain.
    unmatched = sorted(
        (hint_id, node) for hint_id, node in hint_text.items() if node not in audio_text_nodes
    )
    for index, (hint_a, node_a) in enumerate(unmatched):
        words_a = _words(node_a)
        for _hint_b, node_b in unmatched[index + 1 :]:
            if node_a == node_b or not _word_sets_corroborate(words_a, _words(node_b)):
                continue
            item = _assertion(
                media_key,
                a=node_a,
                b=node_b,
                relation="same_work",
                source_kind="hint_text_match",
                source_record_id=hint_a,
                independent_of="hint:comment_answer",
                confidence=6_000,
            )
            assertion_by_id[item.id] = item

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
        candidate_id = candidate_by_node.get(component[0])
        if candidate_id is None:
            # A provider match with no recording id (e.g. an AudD clip whose only field is a song
            # link) contributes just a text node.  When that title is provider-linked — a Shazam
            # recording shares it — the pure-text component was excluded from the candidate set, so
            # attach the observation to that title's work candidate instead of failing.  This is how
            # a paid clip corroborates (and can promote) a Shazam track it agrees with.
            work_id = work_id_by_node.get(preferred)
            work_candidates = sorted(by_work.get(work_id, [])) if work_id else []
            if not work_candidates:
                continue
            candidate_id = work_candidates[0]
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
        if candidate_recording_supported(
            contested=candidate.contested,
            member_nodes=candidate.member_nodes,
            recording_node_sources=recording_node_sources,
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
