"""Fuse what the model heard with what Discogs and Last.fm say, in one explainable rule.

The rule, in words (it is also shown on the page):

1. **Heard share.**  The model gives each of the 400 styles an independent probability ("would
   Discogs tag this record X?").  Squaring them and dividing by their total turns those into one
   share per style that sums to 100%; squaring keeps the model's ranking and stops dozens of small
   maybes from diluting a clear winner.
2. **Look-up shares.**  Discogs and Last.fm each give a share per style (see ``lookup``).  A broad
   umbrella style (House, Techno, Breakbeat...) counts half, because Discogs often files a specific
   record under the umbrella alone.
3. **Weighted average.**  ``fused = (heard + wD * discogs + wL * lastfm) / (1 + wD + wL)`` where
   ``wD = 0.7 * Discogs match quality`` and ``wL = 0.5 * Last.fm quality`` (track tags 1, artist
   tags 0.5).  With no lookups the answer is the heard share alone.
4. **Plausibility gate.**  A style only one lookup names, and that the model gives under 5%, is
   dropped from that lookup: it is usually a name clash (Last.fm's "T2" is also a rock band) or a
   compilation's style, not this record.  Two lookups agreeing still count whatever was heard.

So agreement adds up (a style both heard and looked up rises), and disagreement shows both: the
heard favourite and the looked-up favourite each keep their part of the score, with their sources
marked.  The model's own ranking is returned unchanged next to the fused one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from id_detector.genre import vocab
from id_detector.genre.lookup import SourceEvidence

DISCOGS_WEIGHT = 0.7
LASTFM_WEIGHT = 0.5
#: Umbrella styles a lookup often gives instead of the specific one; their lookup share counts half.
BROAD = frozenset(
    f"{vocab.ELECTRONIC}---{name}"
    for name in (
        "House",
        "Techno",
        "Trance",
        "Breakbeat",
        "Electro",
        "Ambient",
        "Downtempo",
        "Experimental",
        "Dance-pop",
        "Disco",
    )
)
BROAD_FACTOR = 0.5
#: A style counts as "heard" for the source marks when its heard share is at least this.
HEARD_MARK = 0.08
#: Below this model probability, a style named by only one lookup is treated as a mismatch.
IMPLAUSIBLE = 0.05


@dataclass
class Fused:
    ranking: list[dict[str, Any]]
    model_ranking: list[dict[str, Any]]
    parent: str
    parent_share: float
    agreement: str  # "agree" | "partial" | "disagree" | "heard-only"
    summary: str
    weights: dict[str, float]


def heard_shares(probabilities: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(probabilities, dtype=np.float64), 0.0, 1.0)
    squared = p * p
    total = squared.sum()
    return squared / total if total > 0 else squared


def _lookup_shares(shares: dict[str, float]) -> dict[str, float]:
    weighted = {
        label: share * (BROAD_FACTOR if label in BROAD else 1.0) for label, share in shares.items()
    }
    total = sum(weighted.values())
    return {k: v / total for k, v in weighted.items()} if total > 0 else {}


def fuse(
    probabilities: np.ndarray,
    discogs: SourceEvidence | None = None,
    lastfm: SourceEvidence | None = None,
    *,
    top: int = 3,
) -> Fused:
    labels = vocab.labels()
    heard = heard_shares(probabilities)
    probs = np.asarray(probabilities, dtype=np.float64)
    sources: list[tuple[str, dict[str, float], float]] = []
    weights = {"heard": 1.0, "discogs": 0.0, "lastfm": 0.0}
    for evidence, base in ((discogs, DISCOGS_WEIGHT), (lastfm, LASTFM_WEIGHT)):
        if evidence is None or evidence.status != "ok" or not evidence.shares:
            continue
        weight = base * evidence.quality
        if weight <= 0:
            continue
        weights[evidence.source] = round(weight, 3)
        sources.append((evidence.source, evidence.shares, weight))
    index = {label: i for i, label in enumerate(labels)}
    named = [set(shares) for _, shares, _ in sources]
    sources = [
        (
            name,
            _lookup_shares(
                {
                    label: share
                    for label, share in shares.items()
                    if probs[index[label]] >= IMPLAUSIBLE or sum(label in n for n in named) >= 2
                }
            ),
            weight,
        )
        for name, shares, weight in sources
    ]
    denominator = 1.0 + sum(weight for _, _, weight in sources)
    fused = heard.copy()
    for _, shares, weight in sources:
        for label, share in shares.items():
            fused[index[label]] += weight * share
    fused /= denominator

    order = np.argsort(-fused)
    ranking = []
    for i in order[: max(top, 1) + 2]:
        label = labels[i]
        marks = []
        if heard[i] >= HEARD_MARK or i == int(np.argmax(heard)):
            marks.append("heard")
        for name, shares, _ in sources:
            if shares.get(label, 0.0) > 0:
                marks.append(name)
        ranking.append(
            {
                "label": label,
                "name": vocab.display(label),
                "parent": vocab.parent(label),
                "share": round(float(fused[i]), 4),
                "heard": round(float(heard[i]), 4),
                "probability": round(float(probs[i]), 4),
                "lookup": {name: round(shares.get(label, 0.0), 4) for name, shares, _ in sources},
                "sources": marks,
            }
        )
    model_order = np.argsort(-probs)[:10]
    model_ranking = [
        {
            "label": labels[i],
            "name": vocab.display(labels[i]),
            "probability": round(float(probs[i]), 4),
            "heard": round(float(heard[i]), 4),
        }
        for i in model_order
    ]

    parents: dict[str, float] = {}
    for i, label in enumerate(labels):
        parents[vocab.parent(label)] = parents.get(vocab.parent(label), 0.0) + float(fused[i])
    parent = max(parents, key=lambda k: parents[k])

    heard_top = labels[int(np.argmax(heard))]
    lookup_tops = [max(shares, key=lambda k: shares[k]) for _, shares, _ in sources if shares]
    if not sources:
        agreement = "heard-only"
    elif all(top_label == heard_top for top_label in lookup_tops):
        agreement = "agree"
    elif any(top_label == heard_top for top_label in lookup_tops) or any(
        shares.get(heard_top, 0) > 0 for _, shares, _ in sources
    ):
        agreement = "partial"
    else:
        agreement = "disagree"
    summary = _summary(agreement, heard_top, lookup_tops)
    return Fused(
        ranking=ranking[:top],
        model_ranking=model_ranking,
        parent=parent,
        parent_share=round(parents[parent], 4),
        agreement=agreement,
        summary=summary,
        weights=weights,
    )


def _summary(agreement: str, heard_top: str, lookup_tops: list[str]) -> str:
    heard = vocab.display(heard_top)
    if agreement == "heard-only":
        return f"From listening alone: it sounds most like {heard}."
    if agreement == "agree":
        return f"Listening and the look-ups agree: {heard}."
    looked = ", ".join(dict.fromkeys(vocab.display(label) for label in lookup_tops))
    if agreement == "partial":
        return f"Mostly agreed. It sounds like {heard}; the look-ups lean {looked}."
    return f"They disagree. It sounds like {heard}, but the look-ups say {looked}."
