"""Non-authoritative version corroboration from enrichment recording ids.

Enrichment may *attach* a recording-specific id (a Deezer/Apple provider id, an MBID, an ISRC) found
for an already-identified episode, but must never rewrite that episode's identity. The only thing
these ids can do is raise ``version_status`` — and only when the plan's corroboration rule is met:
a ``same_recording`` union needs a recording-specific id asserted by **≥ 2 independent sources**, or
an aligned held reference / audited truth. We fold the ids in as ``source.kind = "enrich"``
assertions and re-run the very same Stage-0 merge + support test used at fusion time, so a single
enrichment provider can never, on its own, corroborate a version.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from id_detector.contracts import IdentitiesRecord
from id_detector.fuse.identity import candidate_recording_supported
from id_detector.semantics import RECORDING_NAMESPACES, merge_recording_identities

# One recording id an enrichment source asserted for a given candidate.
EnrichRecordingId = tuple[str, str, str]  # (source, namespace, value)


def _enrich_assertions(
    identities: IdentitiesRecord,
    enrich_ids_by_candidate: Mapping[str, Iterable[EnrichRecordingId]],
) -> tuple[dict[str, str], list[dict[str, object]]]:
    nodes = {node.id: node.ns for node in identities.nodes}
    extra: list[dict[str, object]] = []
    candidates = {item.canonical_id: item for item in identities.candidates}
    for canonical_id, ids in enrich_ids_by_candidate.items():
        candidate = candidates.get(canonical_id)
        if candidate is None:
            continue
        existing_recording = [
            node for node in candidate.member_nodes if node.split(":", 1)[0] in RECORDING_NAMESPACES
        ]
        anchors = existing_recording or candidate.member_nodes[:1]
        for source, namespace, value in ids:
            if namespace not in RECORDING_NAMESPACES or not value:
                continue
            node_id = f"{namespace}:{value}"
            nodes.setdefault(node_id, namespace)
            for anchor in anchors:
                if anchor == node_id:
                    continue
                a, b = sorted((anchor, node_id))
                extra.append(
                    {
                        "a": a,
                        "b": b,
                        "relation": "same_recording",
                        "source": {
                            "kind": "enrich",
                            "record_id": f"enrich:{source}:{canonical_id}",
                        },
                        "independent_of": f"enrich:{source}",
                        "confidence": 7_000,
                    }
                )
    return nodes, extra


def _enrich_producing_sources(
    enrich_ids_by_candidate: Mapping[str, Iterable[EnrichRecordingId]],
) -> dict[str, set[str]]:
    """Per recording node, the set of enrichment providers that *produced* it.

    This is the enrichment analogue of fusion's per-node producer set: a node is corroborated by the
    single-node rule only when ≥ 2 independent providers emit the *same* recording id (e.g. two
    catalogues returning one ISRC), never when one provider links a candidate to several ids.
    """

    producing: dict[str, set[str]] = {}
    for ids in enrich_ids_by_candidate.values():
        for source, namespace, value in ids:
            if namespace in RECORDING_NAMESPACES and value:
                producing.setdefault(f"{namespace}:{value}", set()).add(f"enrich:{source}")
    return producing


def augmented_version_support(
    identities: IdentitiesRecord,
    enrich_ids_by_candidate: Mapping[str, Iterable[EnrichRecordingId]],
) -> dict[str, bool]:
    """Per candidate: is its recording/version *supported* once enrichment ids are folded in?

    Runs the Stage-0 corroboration merge over the union of the persisted assertions and the enrich
    assertions.  A candidate is supported only when the corroboration rule is met — so it can only
    become supported when a base pair gains a second independent source (a union) or two enrichment
    providers emit the same recording id.  Base verification is never inspected here (and never
    downgraded): the caller keeps the episode's original version status whenever this returns False.
    """

    base_assertions = [item.model_dump(mode="json") for item in identities.assertions]
    nodes, extra = _enrich_assertions(identities, enrich_ids_by_candidate)
    merged = merge_recording_identities(nodes, base_assertions + extra)
    component_by_node = {node: component for component in merged.components for node in component}
    contested_components = {tuple(item) for item in merged.contested}
    producing = _enrich_producing_sources(enrich_ids_by_candidate)

    support: dict[str, bool] = {}
    for candidate in identities.candidates:
        anchor = candidate.member_nodes[0] if candidate.member_nodes else None
        component = component_by_node.get(anchor, tuple(candidate.member_nodes))
        contested = candidate.contested or component in contested_components
        support[candidate.canonical_id] = candidate_recording_supported(
            contested=contested,
            member_nodes=component,
            recording_node_sources=producing,
        )
    return support


def enriched_version_status(original: str, *, contested: bool, supported: bool) -> str:
    """Fold corroboration into a version status without ever downgrading a real identity signal."""

    if contested or original == "contested":
        return "contested"
    if supported:
        return "verified"
    return original
