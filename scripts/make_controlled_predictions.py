"""Create truth-derived predictions solely for Stage 2a scorer plumbing checks."""

from __future__ import annotations

import argparse
import re
from hashlib import sha1, sha256
from pathlib import Path

from id_detector.benchmark.scorer import load_truth_directory, work_key
from id_detector.io import atomic_write_json, canonical_json_bytes

_EVENT = re.compile(r"event:(jump|loop|reset|drift)@(\d+)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("truth", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--profile", default="controlled-plumbing")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    truths = load_truth_directory(args.truth)
    versions = {truth.corpus_version for truth in truths}
    if len(versions) != 1:
        raise ValueError("truth corpus_version values differ")
    sets = []
    for truth in truths:
        predictions = []
        nodes: dict[str, dict] = {}
        works: dict[str, dict] = {}
        candidates: dict[str, dict] = {}
        for episode in truth.episodes:
            start = sum(episode.start_ms_range) // 2
            end = sum(episode.end_ms_range) // 2
            support_start = min(max(start, (start + end) // 2 - 1_000), max(start, end - 1))
            support_end = min(end, support_start + 2_000)
            if support_end <= support_start:
                support_end = support_start + 1
            events = [
                {"type": event, "at_ms": int(at_ms)}
                for event, at_ms in _EVENT.findall(episode.note or "")
            ]
            text_node = f"text:{work_key(episode.work)}"
            nodes[text_node] = {
                "schema_version": "1.0.0",
                "generated_by": "id-detector/0.1.0",
                "id": text_node,
                "ns": "text",
                "label": f"{episode.work.artist} - {episode.work.title}",
            }
            recording_nodes: list[str] = []
            for namespace, value in sorted(episode.version.ids.items()):
                node_id = f"{namespace}:{value}"
                nodes[node_id] = {
                    "schema_version": "1.0.0",
                    "generated_by": "id-detector/0.1.0",
                    "id": node_id,
                    "ns": namespace,
                    "label": f"{episode.work.artist} - {episode.work.title}",
                }
                recording_nodes.append(node_id)
            work_id = sha1(f"work|{work_key(episode.work)}".encode()).hexdigest()
            work = works.setdefault(
                work_id,
                {
                    "schema_version": "1.0.0",
                    "generated_by": "id-detector/0.1.0",
                    "work_id": work_id,
                    "member_nodes": [],
                },
            )
            work["member_nodes"] = sorted(set(work["member_nodes"]) | {text_node, *recording_nodes})
            candidate_id = sha1(
                ("recording|" + work_id + "|" + "|".join(recording_nodes)).encode()
            ).hexdigest()
            candidates[candidate_id] = {
                "schema_version": "1.0.0",
                "generated_by": "id-detector/0.1.0",
                "canonical_id": candidate_id,
                "work_id": work_id,
                "member_nodes": recording_nodes or [text_node],
                "alternatives": [],
                "contested": False,
                "conflicts": [],
            }
            predictions.append(
                {
                    "work": episode.work,
                    "version": episode.version,
                    "candidate_id": candidate_id,
                    "evidence_support_ms": [[support_start, support_end]],
                    "start_no_later_than_ms": support_end,
                    "end_no_earlier_than_ms": support_start,
                    "start_pi": {
                        "lo": episode.start_ms_range[0],
                        "hi": episode.start_ms_range[1],
                        "coverage_target": 9_000,
                        "method": "truth-derived-plumbing-only",
                        "calibrated": True,
                    },
                    "end_pi": {
                        "lo": episode.end_ms_range[0],
                        "hi": episode.end_ms_range[1],
                        "coverage_target": 9_000,
                        "method": "truth-derived-plumbing-only",
                        "calibrated": True,
                    },
                    "best_start_ms": start,
                    "best_end_ms": end,
                    "role_segments": episode.role_segments,
                    "occurrence_index": episode.occurrence_index,
                    "claim": (
                        "component_evidence"
                        if any(segment.role == "component" for segment in episode.role_segments)
                        else "performed"
                    ),
                    "scores": {"work": 10_000, "version": 10_000, "boundary": 10_000},
                    "tiers": {
                        "work": "verified",
                        "version": "verified",
                        "boundary": "verified",
                    },
                    "alignment_events": events,
                }
            )
        sets.append(
            {
                "set_id": truth.set_id,
                "identities": {
                    "schema_version": "1.0.0",
                    "generated_by": "id-detector/0.1.0",
                    "nodes": sorted(nodes.values(), key=lambda item: item["id"]),
                    "assertions": [],
                    "works": sorted(works.values(), key=lambda item: item["work_id"]),
                    "candidates": sorted(
                        candidates.values(), key=lambda item: item["canonical_id"]
                    ),
                },
                "episodes": predictions,
            }
        )
    config_snapshot = {
        "schema_version": "1.0.0",
        "config_version": "stage-2a-controlled-plumbing-v1",
        "profile": args.profile,
        "bootstrap_seed": args.seed,
        "certification_targets": [],
    }
    atomic_write_json(
        args.out,
        {
            "corpus_version": versions.pop(),
            "profile": args.profile,
            "config_hash": sha256(canonical_json_bytes(config_snapshot)).hexdigest(),
            "config_snapshot": config_snapshot,
            "sets": sets,
            "cost": {
                "requests": 0,
                "physical_attempts": 0,
                "billable_seconds": 0,
                "usd_e2": 0,
                "wall_ms": 0,
            },
        },
    )
    print(f"wrote truth-derived plumbing predictions for {len(sets)} sets to {args.out}")


if __name__ == "__main__":
    main()
