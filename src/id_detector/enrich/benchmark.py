"""Link-correctness benchmark: a stratified sample for a human to mark, and its precision scorer.

The plan's Stage 6 gate is *direct-link precision ≥ 95 % on ≥ 60 links*.  Precision is a human
judgement (is this direct link really the right recording?), so this module only prepares a
deterministic, stratified marking sheet (by version ambiguity) and, once a human has filled in the
marks, scores precision with a one-sided 95 % Clopper–Pearson lower bound.  The automated block on
each row reports what the tool can check on its own — the policy that produced the link and its
match confidence — but never substitutes for the human mark.
"""

from __future__ import annotations

from hashlib import sha1
from typing import Any

from id_detector.benchmark.scorer import clopper_pearson_lower_e4
from id_detector.contracts import AcquireFile

STRATA = ("has_qualifier", "no_qualifier", "contested")


def _link_id(episode_id: str, source: str, url: str) -> str:
    return sha1(f"{episode_id}|{source}|{url}".encode(), usedforsecurity=False).hexdigest()


def _stratum(version_qualifier: str | None, version_status: str) -> str:
    if version_status == "contested":
        return "contested"
    return "has_qualifier" if version_qualifier else "no_qualifier"


def collect_links(acquire_files: list[AcquireFile]) -> list[dict[str, Any]]:
    """Every direct link across the given ``acquire.json`` files, as flat marking rows."""

    rows: list[dict[str, Any]] = []
    for acquire in acquire_files:
        for episode in acquire.episodes:
            stratum = _stratum(episode.version_qualifier, episode.version_status)
            for link in episode.direct:
                rows.append(
                    {
                        "link_id": _link_id(episode.episode_id, link.source, link.url),
                        "media_key": acquire.media_key,
                        "episode_id": episode.episode_id,
                        "artist": episode.artist,
                        "title": episode.title,
                        "version_qualifier": episode.version_qualifier,
                        "stratum": stratum,
                        "source": link.source,
                        "url": link.url,
                        "kind": link.kind,
                        "automated": {
                            "match_confidence": link.match_confidence,
                            "corroborates_version": link.corroborates_version,
                            "policy": "exact_id_or_strong_agreement",
                        },
                        "mark": None,
                    }
                )
    rows.sort(key=lambda row: row["link_id"])
    return rows


def _largest_remainder(counts: dict[str, int], total: int) -> dict[str, int]:
    available = sum(counts.values())
    target = min(total, available)
    if available == 0:
        return {name: 0 for name in counts}
    exact = {name: value * target / available for name, value in counts.items()}
    allocation = {name: min(counts[name], int(exact[name])) for name in counts}
    while sum(allocation.values()) < target:
        # Give the next slot to the stratum with the largest fractional shortfall and headroom.
        candidates = [
            (exact[name] - allocation[name], name)
            for name in counts
            if allocation[name] < counts[name]
        ]
        if not candidates:
            break
        _, chosen = max(candidates, key=lambda item: (item[0], item[1]))
        allocation[chosen] += 1
    return allocation


def build_link_sample(acquire_files: list[AcquireFile], *, sample_size: int = 60) -> dict[str, Any]:
    """A deterministic, stratified sample of direct links for human marking."""

    rows = collect_links(acquire_files)
    by_stratum: dict[str, list[dict[str, Any]]] = {name: [] for name in STRATA}
    for row in rows:
        by_stratum[row["stratum"]].append(row)
    counts = {name: len(items) for name, items in by_stratum.items()}
    allocation = _largest_remainder(counts, sample_size)
    sample: list[dict[str, Any]] = []
    for name in STRATA:
        sample.extend(by_stratum[name][: allocation[name]])
    sample.sort(key=lambda row: row["link_id"])
    return {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "kind": "link_correctness_sample",
        "requested_sample": sample_size,
        "total_direct_links": len(rows),
        "strata_totals": counts,
        "strata_sampled": {name: allocation[name] for name in STRATA},
        "gate": {"target_e4": 9_500, "min_links": 60, "status": "pending_owner_marking"},
        "links": sample,
    }


def score_link_sample(marked: dict[str, Any]) -> dict[str, Any]:
    """Score a human-marked sheet: precision + one-sided 95 % Clopper–Pearson lower bound."""

    links = marked.get("links", [])
    marks = [str(row.get("mark")).lower() for row in links if row.get("mark") is not None]
    correct = sum(mark == "correct" for mark in marks)
    incorrect = sum(mark == "incorrect" for mark in marks)
    total = correct + incorrect
    unmarked = len(links) - total
    lower = clopper_pearson_lower_e4(correct, total) if total else 0
    precision_e4 = round(correct * 10_000 / total) if total else 0
    target_e4 = int(marked.get("gate", {}).get("target_e4", 9_500))
    min_links = int(marked.get("gate", {}).get("min_links", 60))
    passed = total >= min_links and lower >= target_e4
    return {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "kind": "link_correctness_score",
        "marked_links": total,
        "unmarked_links": unmarked,
        "correct": correct,
        "incorrect": incorrect,
        "precision_e4": precision_e4,
        "one_sided_95_lower_e4": lower,
        "gate": {
            "target_e4": target_e4,
            "min_links": min_links,
            "pass": passed,
            "status": "certified" if passed else "pending_owner_marking",
        },
    }
