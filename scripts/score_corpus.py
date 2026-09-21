"""Score one recipe's runs over a corpus and pool the counts (plan 1b-iii; launch gate L3).

Usage::

    uv run python scripts/score_corpus.py --run-list data/corpus/release-1/runs-deep.json \\
        --out docs/accuracy/release-1-deep.json [--print] [--match auto|time|work]

The run list is ``{"recipe": "deep" | "free", "runs": [{"mix_id", "truth", "episodes",
"min_track_ms": 30000}, ...]}``; ``truth`` is a ``ground_truth.json`` (or the directory holding
exactly one), ``episodes`` a run's ``fuse/episodes.json`` (its identity graph is the one that
run's completion sidecars name, or an explicit ``"identities"`` path), and relative paths resolve
against the run list's own directory.

Every mix is checked against the media it claims: the run's media key — from
``<media_dir>/ingest/source.json``, or the media directory's own name under ``work/<source_key>/``,
or an explicit ``"media_key"`` in the entry — must equal the truth's ``source.media_key``, and no
two entries may score the same truth set.  A run list that points at the wrong run therefore fails
loudly instead of quietly publishing a number for a different mix.

Per mix, the episodes are first pre-filtered with the **presentation floor** —
:func:`id_detector.present.exports.hidden_reason`, the one predicate the page and the exports drop
rows by (the on-air floor ``min_track_ms`` and fusion's ``suppressed`` reasons) — so the score is
of what a reader is actually shown.  What remains is matched to the truth one of two ways
(``--match``, default ``auto``):

- ``time`` — the existing benchmark scorer (``benchmark.scorer.score_corpus_detailed``, the
  function behind ``idea benchmark score``): a listed prediction is right when its support
  overlaps the truth occurrence it names, and that mix's full report is written next to
  ``--out``.  Needs truth with start times.  The scorer is untouched; what it is handed is
  canonicalised first (:func:`parity_identities`) so that its strict string comparison sees the
  same work the ``work`` matcher below sees — "MPH ft. Cecelia - Rush" in the truth and "MPH -
  Rush (feat. Cecelia)" from the engine are one work at one time, not a miss and a wrong ID.
  Three e4 integers come out of the pooled metrics:
  ``likely_precision_e4`` (``empirical_tier_precision_e4["likely"]``: of the listed predictions
  whose work tier is ``likely`` or better, the share that named a track really played there),
  ``listed_precision_e4`` (``selective_precision_e4`` after the floor: of every listed prediction,
  the share associated with a truth occurrence) and ``work_recall_e4``
  (``identification_work.recall_e4``: of the distinct works in the truth, the share the listed
  predictions named); ``work_precision_e4`` (``identification_work.precision_e4``) rides along.
- ``work`` — time-agnostic (:func:`match_works`): each listed prediction is matched to at most one
  truth row by normalised work identity alone, through the one normaliser fusion uses to decide two
  crowd hints name the same track (``hints.relations._normalise``: a parenthesised mix descriptor
  such as "(Extended Mix)", "(Original Mix)" or "(Edit)" dropped, case and punctuation folded) and
  its order-independent word-set rule (``fuse.identity._word_sets_corroborate``: swapped
  artist/title, "feat." clauses and extra collaborators never block a match).  It reports
  ``work_precision_e4`` (of the distinct works listed, the share really played),
  ``work_recall_e4`` (of the distinct works played, the share listed) and ``likely_precision_e4``
  (a ``likely`` row is right iff it names any truth row); ``listed_precision_e4`` and every timing
  number are ``null``, and so is ``l3.thresholds_met`` — the listed-precision bar needs timed truth.
  The only thing added to fusion's rule here is that the featuring marker itself ("ft", "feat",
  "featuring") carries no identity: the featured name does, and stays.
- ``auto`` picks ``time`` only for **fully timed** truth and ``work`` for anything still carrying
  ``idea truth seed``'s placeholder equal-slice timings (:func:`truth_timing`) — a whole file of
  them (``order-only``, as the rekordbox-playlist drafts under ``data/corpus/release-1/`` are) or
  just some rows (``partial``, a half-finished verify pass).  A run list mixing both pools the
  work-only numbers (the only ones every mix has) and keeps each timed mix's time numbers on its
  own row.

Whatever the mode, every mix also gets the work-only numbers under ``work_only`` (pooled at the
top level too) and a ``work-match.json`` beside its ``predictions.json`` listing every assignment,
so a timed corpus and an order-only one stay comparable; the per-mix rows name the truth works no
prediction matched (what was missed) and the listed labels no truth row matched (what was wrong),
always under their original labels.  A timed mix also carries ``median_offset_ms`` — the median
of (first engine evidence of a listed track − the tracklist's start of the truth row the work
matcher paired it with), a diagnostic for a tracklist whose clock does not start where the video
does (an intro, a cut); it is reported, never corrected for.

Across mixes the counts are **pooled**: numerators and denominators are summed before any ratio is
taken, never a mean of per-mix ratios, so a forty-track mix weighs forty times a one-track mix.

A crowd ID (fusion's ``hint_only`` episode) claims a position *range* rather than engine-proved
bounds, which the benchmark's ``ScoredEpisode`` contract cannot express; :func:`proved_bounds`
re-states those two bounds in the contract's convention and the output counts them as
``range_claims``, so a listed name with no audio proof is scored, never hidden.

Draft truth (rows with ``draft: true`` — e.g. the seeded ``data/corpus/release-1/`` files with
their placeholder equal-slice timings) is accepted, but the output is labelled ``"truth_status":
"draft"``; only truth covered by a frozen, hash-checked ``corpus-version.json`` with every row
verified reads ``"verified"``, and only that, matched by time, can back the L3 numbers.

``l3.certifiable`` is true only when, on top of that, the run list is exactly ONE WHOLE frozen
corpus: every truth path belongs to the same frozen corpus, the run list names exactly that
corpus's frozen manifest inventory, and independence is judged over that complete inventory
(``benchmark.scorer.certification_scope``).  Leaving a set out -- say, the one reviewed with IDea's
predictions on screen -- never makes the rest certifiable.  A partial or draft run list is still
scored, for development; ``l3.not_certifiable_because`` says in ordinary words why it is not
certifiable and what to do next, and ``l3.scope`` carries the scope verdict on its own.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, fields
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from id_detector.benchmark.corpus import prediction_set_from_fusion
from id_detector.benchmark.scorer import (
    TIER_ORDER,
    CertificationScope,
    PredictionDocument,
    ScoreState,
    ScoringConfigSnapshot,
    SetScore,
    _ratio_e4,
    certification_scope,
    corpus_independent,
    load_truth_directory,
    pooled_metrics,
    score_corpus_detailed,
    truth_is_frozen_verified,
    work_key,
)
from id_detector.contracts import (
    SCHEMA_VERSION,
    BenchmarkMetrics,
    EpisodesFile,
    GroundTruthRecord,
    IdentitiesRecord,
    IdentityNode,
    TruthWork,
)

# The work-only matcher must be fusion's own notion of "the same track", not a second normaliser:
# these two private helpers are exactly what attaches a crowd hint to an audio match.
from id_detector.fuse.identity import _word_sets_corroborate
from id_detector.hints.relations import _normalise
from id_detector.io import (
    atomic_write_json,
    canonical_json_bytes,
    completion_sidecar_path,
    read_text,
)
from id_detector.present.exports import flatten_tracklist, hidden_reason
from id_detector.truth import (
    CERTIFICATION_DISABLED,
    CERTIFICATION_DISABLED_NEXT_STEP,
    certifiable_under_gate,
    certification_enabled,
    refuse_generated_output,
)

GENERATED_BY = "id-detector/0.1.0 scripts/score_corpus.py"
CONFIG_VERSION = "score-corpus-v1"
#: Fixed so a re-run over the same inputs writes byte-identical per-mix reports (the bootstrap
#: intervals in them are seeded from it; the three pooled numbers never depend on it).
BOOTSTRAP_SEED = 20_260_910
DEFAULT_MIN_TRACK_MS = 30_000
#: Plan §6.3 L3 thresholds per recipe, e4.
L3_THRESHOLDS: dict[str, dict[str, int]] = {
    "free": {"likely_precision_e4": 9_000, "listed_precision_e4": 8_000, "work_recall_e4": 7_000},
    "deep": {"likely_precision_e4": 9_000, "listed_precision_e4": 8_000, "work_recall_e4": 7_500},
}
_IDENTITIES_GEN = re.compile(r"^identities\.gen(\d+)\.json$")
#: Completion-sidecar ``upstream`` keys are media-dir-relative posix paths.
_UPSTREAM_EPISODES = re.compile(r"^fuse/episodes\.gen\d+\.json$")
_UPSTREAM_IDENTITIES = re.compile(r"^fuse/identities\.gen\d+\.json$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRUTH_RANK = {"draft": 0, "unverified": 1, "verified": 2}
TruthStatus = Literal["draft", "unverified", "verified"]
MatchChoice = Literal["auto", "time", "work"]
MatchMode = Literal["time", "work"]
Timing = Literal["timed", "partial", "order-only"]
#: What ``idea truth seed`` writes on every row it cannot time (``truth.py``).
SEED_AUDIBLE_RULE = "manual annotation required"
#: The featuring marker carries no identity of its own ("MPH ft. Cecelia" and "MPH (feat.
#: Cecelia)" name the same people); the featured name stays in the word set.
_FEATURING = frozenset({"ft", "feat", "featuring"})
#: Plan §6.3 L3 also asks for a corpus shape these three numbers cannot judge; named in ``--print``.
L3_CORPUS_NOTE = (
    "L3 also needs >= 5 owner-verified mixes, >= 3 DJs, >= 2 platforms and >= 4 h of audio, "
    "which this score does not check"
)


class RunEntry(BaseModel):
    """One mix of the run list: where its truth and its run's fuse artefacts are."""

    model_config = ConfigDict(extra="forbid")

    mix_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    truth: Path
    episodes: Path
    identities: Path | None = None
    #: Asserts which media the episodes belong to when the run directory cannot say (see
    #: :func:`run_media_key`); it must still equal the truth's ``source.media_key``.
    media_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    min_track_ms: int = Field(default=DEFAULT_MIN_TRACK_MS, ge=0)

    def resolved(self, base: Path) -> RunEntry:
        """Relative paths resolve against the run list's directory, absolute ones stand."""

        return self.model_copy(
            update={
                "truth": base / self.truth,
                "episodes": base / self.episodes,
                "identities": None if self.identities is None else base / self.identities,
            }
        )


class RunList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe: Literal["free", "deep"]
    runs: list[RunEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _mix_ids_are_unique(self) -> RunList:
        seen = Counter(run.mix_id for run in self.runs)
        duplicates = sorted(mix_id for mix_id, count in seen.items() if count > 1)
        if duplicates:
            raise ValueError(f"duplicate mix_id: {', '.join(duplicates)}")
        return self


@dataclass
class WorkCounts:
    """Numerators and denominators of the work-only match (:func:`match_works`); pooled by sum."""

    #: Listed rows that named a truth row, of all listed rows.
    rows_correct: int = 0
    rows: int = 0
    #: Listed rows whose work tier is ``likely`` or better that named a truth row, of those rows.
    likely_correct: int = 0
    likely: int = 0
    #: Distinct listed works (the identity graph's) with a matched row, of all distinct listed
    #: works.
    works_correct: int = 0
    works: int = 0
    #: Distinct truth works some row named, of all distinct truth works.
    truth_matched: int = 0
    truth: int = 0

    def add(self, other: WorkCounts) -> None:
        for item in fields(self):
            setattr(self, item.name, getattr(self, item.name) + getattr(other, item.name))

    def headline(self) -> dict[str, int]:
        return {
            "likely_precision_e4": _ratio_e4(self.likely_correct, self.likely),
            "work_precision_e4": _ratio_e4(self.works_correct, self.works),
            "work_recall_e4": _ratio_e4(self.truth_matched, self.truth),
        }

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {
            "rows": {"correct": self.rows_correct, "predicted": self.rows},
            "likely": {"correct": self.likely_correct, "predicted": self.likely},
            "works": {"correct": self.works_correct, "predicted": self.works},
            "truth_works": {"matched": self.truth_matched, "total": self.truth},
        }


@dataclass(frozen=True)
class ListedWork:
    """A listed prediction as the work-only matcher sees it."""

    episode_id: str
    artist: str
    title: str
    #: The episode's ``tiers.work``.
    tier: str
    #: The identity graph's work: the unit "distinct listed works" are counted in.
    work_id: str


@dataclass(frozen=True)
class WorkMatch:
    """One mix's listed predictions matched to its truth rows by work identity alone."""

    #: Listed index -> truth row index.  A prediction matches at most one row; a row may be named
    #: by any number of predictions (a track the tool lists twice is not a wrong ID).
    assignments: dict[int, int]
    counts: WorkCounts
    #: Distinct truth works no prediction named (what was missed), in truth order.
    unmatched_truth: list[dict[str, Any]]
    #: Distinct listed labels that named no truth row (what was wrong), in listing order.
    unmatched_predictions: list[dict[str, Any]]


@dataclass(frozen=True)
class MixScore:
    entry: RunEntry
    truth: GroundTruthRecord
    truth_status: TruthStatus
    #: ``False`` when predictions were visible during the truth's review (see `truth_independent`).
    independent: bool
    timing: Timing
    match_mode: MatchMode
    episodes_total: int
    hidden_by_reason: dict[str, int]
    #: Listed rows whose bounds were a position range, not engine-proved (see `proved_bounds`).
    range_claims: int
    work_match: WorkMatch
    work_match_path: Path
    #: Truth keys granted to a predicted work for the time path (see `parity_identities`).
    parity_keys: int
    #: Median of (first engine evidence − tracklist start) over the work-matched pairs of really
    #: timed truth rows; ``None`` when there are no such pairs (see `median_offset`).
    median_offset_ms: int | None
    offset_pairs: int
    #: The benchmark scorer's result; ``None`` when the mix was matched by work.
    score: SetScore | None
    metrics: BenchmarkMetrics | None
    report_path: Path | None


def load_run_list(path: Path) -> RunList:
    """Parse and validate a run list; paths come back resolved against the list's directory."""

    run_list = RunList.model_validate(json.loads(read_text(path)))
    base = path.resolve().parent
    return run_list.model_copy(update={"runs": [run.resolved(base) for run in run_list.runs]})


def _sidecar_upstream(artefact: Path) -> dict[str, str]:
    """The ``upstream`` map of an artefact's ``X.done.json`` completion sidecar, or ``{}``."""

    sidecar = completion_sidecar_path(artefact)
    if not sidecar.is_file():
        return {}
    upstream = json.loads(read_text(sidecar)).get("upstream")
    return upstream if isinstance(upstream, dict) else {}


def identities_path(entry: RunEntry, episodes: EpisodesFile) -> Path:
    """The identity graph this run's ``episodes.json`` was actually fused from.

    The completion sidecars record the chain, so follow it: ``episodes.done.json`` names the
    ``fuse/episodes.gen<N>.json`` the final file was copied from, and that generation's own sidecar
    names the ``fuse/identities.gen<N>.json`` it was fused from.  Taking the *newest* generation
    beside the episodes instead is wrong whenever a media directory holds the artefacts of more
    than one analysis (a later run's ``identities.gen2.json`` re-clusters the graph, so the earlier
    generation's ``candidate_id``s are simply absent from it).  Failing that, pair by the
    generation the episodes themselves declare, then the newest as a last resort.  An explicit
    ``"identities"`` in the run list always wins.
    """

    if entry.identities is not None:
        return entry.identities
    fuse_dir = entry.episodes.parent
    media_dir = fuse_dir.parent
    generation_file = next(
        (
            media_dir / name
            for name in sorted(_sidecar_upstream(entry.episodes))
            if _UPSTREAM_EPISODES.match(name)
        ),
        entry.episodes,
    )
    fused_from = next(
        (
            media_dir / name
            for name in sorted(_sidecar_upstream(generation_file))
            if _UPSTREAM_IDENTITIES.match(name)
        ),
        None,
    )
    if fused_from is not None and fused_from.is_file():
        return fused_from
    same_generation = fuse_dir / f"identities.gen{episodes.generation}.json"
    if same_generation.is_file():
        return same_generation
    generations = sorted(
        (int(match.group(1)), path)
        for path in fuse_dir.glob("identities.gen*.json")
        if (match := _IDENTITIES_GEN.match(path.name))
    )
    if not generations:
        raise FileNotFoundError(
            f"{entry.mix_id}: no identities.gen*.json beside {entry.episodes}; "
            'name one with "identities"'
        )
    return generations[-1][1]


def assert_identities_cover(
    episodes: EpisodesFile, identities: IdentitiesRecord, path: Path, mix_id: str
) -> None:
    """Every episode must name a candidate the identity graph describes.

    Without this the mismatch surfaces deep inside the presentation layer as a bare
    ``StopIteration`` from ``_candidate_label``; here it names the file that is wrong.
    """

    known = {item.canonical_id for item in identities.candidates}
    missing = sorted({episode.candidate_id for episode in episodes.episodes} - known)
    if missing:
        raise ValueError(
            f"{mix_id}: {path.name} does not describe {len(missing)} candidate(s) this run's "
            f"episodes name (e.g. {missing[0]}); it is not the identity graph generation "
            f"{episodes.generation} was fused from — check the run directory or name the right "
            'file with "identities"'
        )


def run_media_key(episodes: Path) -> str | None:
    """The media a run's fuse artefacts belong to, or ``None`` when the directory cannot say.

    A run lives at ``work/<source_key>/<media_key>/fuse/``, so the media directory's own name is
    the media key; ``ingest/source.json`` states it outright and wins when present.
    """

    media_dir = episodes.parent.parent
    source = media_dir / "ingest" / "source.json"
    if source.is_file():
        stated = json.loads(read_text(source)).get("media_key")
        if isinstance(stated, str) and _SHA256.match(stated):
            return stated
    return media_dir.name if _SHA256.match(media_dir.name) else None


def assert_media_matches(entry: RunEntry, truth: GroundTruthRecord) -> None:
    """The run's media must be the truth's media, or the score belongs to another mix."""

    known = {key for key in (entry.media_key, run_media_key(entry.episodes)) if key is not None}
    if not known:
        raise ValueError(
            f"{entry.mix_id}: cannot tell which media {entry.episodes} belongs to; point at the "
            "run's work/<source_key>/<media_key>/fuse/episodes.json or state the media in the "
            'run list with "media_key"'
        )
    wrong = sorted(key for key in known if key != truth.source.media_key)
    if wrong:
        raise ValueError(
            f"{entry.mix_id}: those episodes are media {wrong[0][:12]}… but truth "
            f"{truth.set_id} is media {truth.source.media_key[:12]}…; the run list points at "
            "another mix's run"
        )


def truth_status(truth_path: Path, truth: GroundTruthRecord) -> TruthStatus:
    """``verified`` only for frozen, hash-checked, fully verified truth; ``draft`` when any row is
    still a draft (seeded, placeholder timings); ``unverified`` for a settled but unfrozen file."""

    if truth_is_frozen_verified(truth_path, [truth]):
        return "verified"
    if any(episode.draft for episode in truth.episodes):
        return "draft"
    return "unverified"


def truth_independent(truth_path: Path, truth: GroundTruthRecord) -> bool:
    """``False`` when the set's own records, or its freeze manifest, say IDea's predictions were
    visible while the truth was reviewed.  Such a set is still scored; it can back no L3 claim."""

    # Through the corpus gateway: the corpus's vetted records and its manifest, never a raw walk.
    return corpus_independent(truth_path, [truth])


def placeholder_rows(truth: GroundTruthRecord) -> frozenset[int]:
    """The rows still carrying ``idea truth seed``'s placeholder equal-slice timings.

    Row *i* of *n* sits on both slice points ``i * duration // n`` and ``(i + 1) * duration // n``
    and is flagged ``manual annotation required``; see :func:`truth_timing` for why both bounds
    are read.  Such a row holds no timing to score, or measure an offset, against.
    """

    count = len(truth.episodes)
    duration = truth.source.duration_ms
    return frozenset(
        index
        for index, episode in enumerate(truth.episodes)
        if episode.audible_rule == SEED_AUDIBLE_RULE
        and tuple(episode.start_ms_range) == (index * duration // count,) * 2
        and tuple(episode.end_ms_range) == ((index + 1) * duration // count,) * 2
    )


def truth_timing(truth: GroundTruthRecord) -> Timing:
    """How much of a truth is really timed, read from its content rather than from a flag.

    An order-only tracklist (a rekordbox playlist) gives the tracks and their order but no start
    times, so the seed splits the mix into equal slices — row *i* of *n* runs from the single point
    ``i * duration // n`` to the next slice point — and marks every row ``audible_rule: "manual
    annotation required"``.  A row that still looks exactly like that holds no timing to score
    against.  Both bounds are checked, not just the start: a *timed* tracklist whose first track
    begins at 0:00 has row 0 sitting on the first slice point by arithmetic, and only its end range
    says that it was really timed.

    - ``timed`` — no row is a placeholder any more (a timestamped tracklist, or every row listened
      to); only this can be matched by time.
    - ``order-only`` — every row is still a placeholder, as the committed ``data/corpus/release-1/``
      drafts are, and as a verifier who leaves the placeholders in place (the corpus README's
      "work-only scoring" option) leaves them.
    - ``partial`` — SOME rows are timed and some are not, which is what a half-finished
      ``idea truth verify`` pass leaves behind (it keeps the seed's ``audible_rule`` and, on a blank
      answer, the seed's range, and it clears ``draft`` either way).  Time-matching such a file
      scores the untimed rows against an arbitrary equal-slice point, i.e. as misses, so it is
      matched by work like an order-only file, not by time.
    """

    placeholders = len(placeholder_rows(truth))
    if placeholders == 0:
        return "timed"
    return "order-only" if placeholders == len(truth.episodes) else "partial"


def work_identity(artist: str, title: str) -> tuple[tuple[str, str], frozenset[str]]:
    """Fusion's normalised work identity: the (artist, title) key and its word set.

    ``hints.relations._normalise`` is the normaliser fusion applies to decide two crowd hints name
    the same track: NFKC, casefold, a parenthesised mix descriptor ("(Extended Mix)", "(Original
    Mix)", "(Edit)", "(VIP)", "(Club Dub)") dropped, everything non-alphanumeric a space.  The word
    set of both fields together is what ``fuse.identity`` compares order-independently to attach a
    hint to an audio match, so a "feat." clause, an extra collaborator or a swapped artist/title
    never blocks a match.  One thing is added to that word set: the featuring marker itself is
    dropped (``_FEATURING``) — "MPH ft. Cecelia - Rush" and "MPH - Rush (feat. Cecelia)" differ by
    nothing but the spelling of "featuring", and fusion's near-spelling tolerance only covers
    tokens of four letters or more.  The featured name stays.  Nothing else is stripped: the
    scorer is not a second normaliser.
    """

    artist_key, title_key = _normalise(artist), _normalise(title)
    return (artist_key, title_key), frozenset(f"{artist_key} {title_key}".split()) - _FEATURING


def match_works(truth_works: list[TruthWork], listed: list[ListedWork]) -> WorkMatch:
    """Match each listed prediction to at most one truth row by normalised work identity.

    Exact first (the normalised artist and title are both equal *and* both non-empty, as fusion's
    own ``hints.relations._identity_key`` requires — two labels that normalise away to nothing, an
    unnamed "ID" row against a prediction whose whole title was a mix descriptor, name no work and
    must not count as an identification), else fusion's word-set rule
    (:func:`~id_detector.fuse.identity._word_sets_corroborate`: one word set contains the other
    with at least two words, or they differ by a single near-spelled long token); among several
    candidate rows the closest word set wins, ties to the earlier row.  Time is never consulted.
    """

    truth_rows = [work_identity(work.artist, work.title) for work in truth_works]
    assignments: dict[int, int] = {}
    for index, item in enumerate(listed):
        key, words = work_identity(item.artist, item.title)
        exact = (
            [row for row, (truth_key, _) in enumerate(truth_rows) if truth_key == key]
            if all(key)
            else []
        )
        candidates = exact or [
            row
            for row, (_, truth_words) in enumerate(truth_rows)
            if _word_sets_corroborate(words, truth_words)
        ]
        if candidates:
            assignments[index] = min(
                candidates, key=lambda row: (len(words ^ truth_rows[row][1]), row)
            )

    truth_keys = [key for key, _ in truth_rows]
    matched_keys = {truth_keys[row] for row in assignments.values()}
    first_row_by_key: dict[tuple[str, str], TruthWork] = {}
    for key, work in zip(truth_keys, truth_works, strict=True):
        first_row_by_key.setdefault(key, work)
    likely_rows = [
        index for index, item in enumerate(listed) if TIER_ORDER[item.tier] >= TIER_ORDER["likely"]
    ]
    wrong: dict[tuple[str, str], dict[str, Any]] = {}
    for index, item in enumerate(listed):
        if index in assignments:
            continue
        label = wrong.setdefault(
            (item.artist, item.title),
            {"artist": item.artist, "title": item.title, "tier": item.tier, "rows": 0},
        )
        label["rows"] += 1
        if TIER_ORDER[item.tier] > TIER_ORDER[label["tier"]]:
            label["tier"] = item.tier
    return WorkMatch(
        assignments=assignments,
        counts=WorkCounts(
            rows_correct=len(assignments),
            rows=len(listed),
            likely_correct=sum(index in assignments for index in likely_rows),
            likely=len(likely_rows),
            works_correct=len({listed[index].work_id for index in assignments}),
            works=len({item.work_id for item in listed}),
            truth_matched=len(matched_keys),
            truth=len(first_row_by_key),
        ),
        unmatched_truth=[
            {"artist": work.artist, "title": work.title}
            for key, work in first_row_by_key.items()
            if key not in matched_keys
        ],
        unmatched_predictions=list(wrong.values()),
    )


def listed_episodes(
    episodes: EpisodesFile, identities: IdentitiesRecord, min_track_ms: int
) -> tuple[EpisodesFile, dict[str, int]]:
    """The episodes the presentation layer lists, and how many it hid per reason.

    Every episode is flattened to its own row (``collapse=False``) so the floor is applied per
    scored prediction; :func:`hidden_reason` then decides exactly as the page and exports do.
    """

    entries = flatten_tracklist(
        episodes, identities, collapse=False, min_track_ms=min_track_ms, include_hidden=True
    )
    hidden: dict[str, str] = {}
    for entry in entries:
        if entry["kind"] != "track":
            continue
        reason = hidden_reason(entry, min_track_ms)
        if reason is not None:
            hidden[entry["episode_id"]] = reason
    kept = [episode for episode in episodes.episodes if episode.id not in hidden]
    by_reason = dict(sorted(Counter(hidden.values()).items()))
    return episodes.model_copy(update={"episodes": kept}), by_reason


def is_range_claim(episode: dict[str, Any]) -> bool:
    """A row whose bounds are the two ends of a position range, not engine-proved bounds."""

    spans = [(int(start), int(end)) for start, end in episode["evidence_support_ms"]]
    lo, hi = min(start for start, _ in spans), max(end for _, end in spans)
    calibrated = any(getattr(episode[key], "calibrated", False) for key in ("start_pi", "end_pi"))
    return not (
        calibrated
        or episode["start_no_later_than_ms"] > lo
        or episode["end_no_earlier_than_ms"] < hi
    )


def proved_bounds(episodes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Re-express range claims in the benchmark contract's proved-bound convention.

    :class:`~id_detector.benchmark.scorer.ScoredEpisode` states bounds an engine PROVED: the track
    started no later than the first match's *end* and ended no earlier than the last match's
    *start*.  A crowd ID (fusion's ``hint_only`` episode, kept listed by ``hint_supported``) has no
    audio match at all: its ``evidence_support_ms`` is the position range a comment gave and its
    bounds are that range's two ends, which the contract rejects outright.  "Played somewhere in
    ``[lo, hi]``" says exactly ``start <= hi`` and ``end >= lo`` in that convention, so the two
    bounds swap (``best_start_ms``/``best_end_ms`` follow them, as the contract requires; the
    contract allows such one-sided proofs to cross).  Nothing else changes, and none of the three
    L3 numbers reads these fields — they come from work identity, the support spans and the tiers.
    """

    normalised: list[dict[str, Any]] = []
    claims = 0
    for episode in episodes:
        if not is_range_claim(episode):
            normalised.append(episode)
            continue
        spans = [(int(start), int(end)) for start, end in episode["evidence_support_ms"]]
        lo, hi = min(start for start, _ in spans), max(end for _, end in spans)
        claims += 1
        normalised.append(
            {
                **episode,
                "start_no_later_than_ms": hi,
                "end_no_earlier_than_ms": lo,
                "best_start_ms": hi,
                "best_end_ms": lo,
            }
        )
    return normalised, claims


def parity_identities(
    identities: IdentitiesRecord,
    truth: GroundTruthRecord,
    listed: list[ListedWork],
    work_match: WorkMatch,
) -> tuple[IdentitiesRecord, int]:
    """Canonicalise the identity graph for the time path: the truth's own key joins the work.

    The benchmark scorer never compares a prediction's label with the truth's; it asks whether the
    predicted work holds a ``text:`` node whose id equals the truth row's ``work_key`` — a strict
    string comparison, stricter than fusion's own notion of "the same track" and stricter than
    the ``work`` matcher, which reuses fusion's normaliser and word-set rule.  So the two sides are
    brought to one canonical string exactly the way fusion does it for a corroborating crowd
    label: each truth row the work matcher paired with a listed prediction lends its own scorer
    key (``text:<artist>|<title>``, already in the scorer's normal form) to that prediction's work
    as one more text node, and the scorer's strict comparison then sees identical strings for
    identical works.  Only the identity graph handed to the scorer changes; the run's artefacts,
    the truth file and every label in the output stay as they were.

    A truth row may have been paired with rows of several works (the work matcher is many-to-one);
    the scorer resolves a truth to at most ONE work, so the key goes to the closest word set,
    ties to the earlier listed row.  A key another work already holds is left where it is (the
    scorer already resolves that truth there); an existing node is reused, never duplicated.
    Returns the graph and the number of keys granted.
    """

    ranked: dict[int, tuple[tuple[int, int], str]] = {}
    for index, row in work_match.assignments.items():
        _, words = work_identity(listed[index].artist, listed[index].title)
        work = truth.episodes[row].work
        _, truth_words = work_identity(work.artist, work.title)
        rank = (len(words ^ truth_words), index)
        if row not in ranked or rank < ranked[row][0]:
            ranked[row] = (rank, listed[index].work_id)
    known = {node.id for node in identities.nodes}
    member_of = {node: work.work_id for work in identities.works for node in work.member_nodes}
    new_nodes: list[IdentityNode] = []
    granted: dict[str, list[str]] = {}
    for row, (_, work_id) in sorted(ranked.items()):
        episode = truth.episodes[row]
        node_id = f"text:{work_key(episode.work)}"
        owner = member_of.get(node_id)
        if owner is not None:
            continue
        if node_id not in known:
            new_nodes.append(
                IdentityNode(
                    schema_version=SCHEMA_VERSION,
                    generated_by=GENERATED_BY,
                    id=node_id,
                    ns="text",
                    label=f"{episode.work.artist} - {episode.work.title}",
                )
            )
            known.add(node_id)
        granted.setdefault(work_id, []).append(node_id)
        member_of[node_id] = work_id
    if not granted:
        return identities, 0
    works = [
        work.model_copy(update={"member_nodes": [*work.member_nodes, *granted[work.work_id]]})
        if work.work_id in granted
        else work
        for work in identities.works
    ]
    canonical = identities.model_copy(
        update={"nodes": [*identities.nodes, *new_nodes], "works": works}
    )
    return canonical, sum(len(nodes) for nodes in granted.values())


def median_offset(
    listed: EpisodesFile,
    work_match: WorkMatch,
    truth: GroundTruthRecord,
    *,
    range_claims: set[int],
) -> tuple[int | None, int]:
    """Median of (first engine evidence − tracklist start) over the work-matched pairs.

    A diagnostic for the owner, not a correction: a comment tracklist whose clock starts at the
    set's first beat while the video carries an intro (or a cut) puts every truth start early (or
    late) by the same amount, and a by-time score then misses tracks the tool named at the right
    place.  The pairs are the work matcher's (time-agnostic, so the offset can be seen even when
    time-matching fails), restricted to truth rows that are really timed (never a seed
    placeholder) and to listed rows with engine evidence — a crowd row's position range comes
    from the same comments, so it can say nothing about the video.  Lower median for an even
    count; ``None`` with no pairs.
    """

    placeholders = placeholder_rows(truth)
    offsets = [
        min(start for start, _ in listed.episodes[index].evidence_support_ms)
        - truth.episodes[row].start_ms_range[0]
        for index, row in sorted(work_match.assignments.items())
        if row not in placeholders and index not in range_claims
    ]
    if not offsets:
        return None, 0
    return statistics.median_low(offsets), len(offsets)


def _work_match_document(
    mix: RunEntry,
    truth: GroundTruthRecord,
    *,
    timing: Timing,
    match_mode: MatchMode,
    listed: list[ListedWork],
    work_match: WorkMatch,
) -> dict[str, Any]:
    """Every assignment of one mix, for the owner to audit beside ``predictions.json``."""

    named_by: dict[int, list[str]] = {}
    for index, row in work_match.assignments.items():
        named_by.setdefault(row, []).append(listed[index].episode_id)
    return {
        "schema_version": "1.0.0",
        "generated_by": GENERATED_BY,
        "mix_id": mix.mix_id,
        "set_id": truth.set_id,
        "timing": timing,
        "match_mode": match_mode,
        "truth": [
            {
                "index": index,
                "artist": episode.work.artist,
                "title": episode.work.title,
                "occurrence_index": episode.occurrence_index,
                "named_by": named_by.get(index, []),
            }
            for index, episode in enumerate(truth.episodes)
        ],
        "predictions": [
            {
                "episode_id": item.episode_id,
                "artist": item.artist,
                "title": item.title,
                "tier": item.tier,
                "work_id": item.work_id,
                "truth_index": work_match.assignments.get(index),
            }
            for index, item in enumerate(listed)
        ],
        "counts": work_match.counts.as_dict(),
    }


def score_mix(
    entry: RunEntry, *, recipe: str, artefact_dir: Path, match: MatchChoice = "auto"
) -> MixScore:
    """Pre-filter one mix's episodes, match them to the truth by time or by work, keep counts."""

    truths = load_truth_directory(entry.truth)
    if len(truths) != 1:
        raise ValueError(f"{entry.mix_id}: truth must hold exactly one set, found {len(truths)}")
    truth = truths[0]
    assert_media_matches(entry, truth)
    episodes = EpisodesFile.model_validate_json(read_text(entry.episodes))
    graph_path = identities_path(entry, episodes)
    identities = IdentitiesRecord.model_validate_json(read_text(graph_path))
    assert_identities_cover(episodes, identities, graph_path, entry.mix_id)
    listed, hidden_by_reason = listed_episodes(episodes, identities, entry.min_track_ms)
    status = truth_status(entry.truth, truth)
    independent = truth_independent(entry.truth, truth)
    timing = truth_timing(truth)
    # Only a fully timed truth can be matched by time; a partly timed one would score its
    # still-placeholder rows against an equal-slice point, i.e. as misses.
    mode: MatchMode = ("time" if timing == "timed" else "work") if match == "auto" else match
    prediction_set = prediction_set_from_fusion(truth.set_id, identities, listed)
    range_claim_rows = {
        index for index, episode in enumerate(prediction_set["episodes"]) if is_range_claim(episode)
    }
    prediction_set["episodes"], range_claims = proved_bounds(prediction_set["episodes"])
    candidates = {item.canonical_id: item for item in identities.candidates}
    listed_works = [
        ListedWork(
            episode_id=episode.id,
            artist=prediction["work"]["artist"],
            title=prediction["work"]["title"],
            tier=prediction["tiers"].work,
            work_id=candidates[prediction["candidate_id"]].work_id,
        )
        for episode, prediction in zip(listed.episodes, prediction_set["episodes"], strict=True)
    ]
    work_match = match_works([episode.work for episode in truth.episodes], listed_works)
    parity_keys = 0
    if mode == "time":
        # The certified scorer compares strings; hand it a graph in which the works it must
        # recognise carry the truth's own strings (see `parity_identities`).
        prediction_set["identities"], parity_keys = parity_identities(
            identities, truth, listed_works, work_match
        )
    snapshot = ScoringConfigSnapshot(
        schema_version="1.0.0",
        config_version=CONFIG_VERSION,
        profile=recipe,
        bootstrap_seed=BOOTSTRAP_SEED,
        certification_targets=[],
        run_config={
            "mix_id": entry.mix_id,
            "presentation_floor": "present.exports.hidden_reason",
            "min_track_ms": entry.min_track_ms,
            "episodes_total": len(episodes.episodes),
            "episodes_listed": len(listed.episodes),
            "hidden_by_reason": hidden_by_reason,
            "range_claims": range_claims,
            "timing": timing,
            "match_mode": mode,
            "parity_keys": parity_keys,
        },
    )
    # Built in both modes: the contract check on the run's artefacts is worth having either way.
    document = PredictionDocument(
        corpus_version=truth.corpus_version,
        profile=recipe,
        config_hash=sha256(canonical_json_bytes(snapshot)).hexdigest(),
        config_snapshot=snapshot,
        sets=[prediction_set],
        unverified_seed_comparison=status != "verified",
    )
    mix_dir = artefact_dir / entry.mix_id
    predictions_path = mix_dir / "predictions.json"
    refuse_generated_output(predictions_path)
    atomic_write_json(predictions_path, document)
    work_match_path = mix_dir / "work-match.json"
    refuse_generated_output(work_match_path)
    atomic_write_json(
        work_match_path,
        _work_match_document(
            entry, truth, timing=timing, match_mode=mode, listed=listed_works, work_match=work_match
        ),
    )
    offset_ms, offset_pairs = median_offset(
        listed, work_match, truth, range_claims=range_claim_rows
    )
    score: SetScore | None = None
    metrics: BenchmarkMetrics | None = None
    report_path: Path | None = None
    if mode == "time":
        report_path = mix_dir / "report.json"
        report, scores = score_corpus_detailed(entry.truth, predictions_path, out_path=report_path)
        (score,) = scores
        metrics = report.overall
    return MixScore(
        entry=entry,
        truth=truth,
        truth_status=status,
        independent=independent,
        timing=timing,
        match_mode=mode,
        episodes_total=len(episodes.episodes),
        hidden_by_reason=hidden_by_reason,
        range_claims=range_claims,
        work_match=work_match,
        work_match_path=work_match_path,
        parity_keys=parity_keys,
        median_offset_ms=offset_ms,
        offset_pairs=offset_pairs,
        score=score,
        metrics=metrics,
        report_path=report_path,
    )


def _headline(metrics: BenchmarkMetrics) -> dict[str, int]:
    """The L3 numbers of a time-matched score, read from the scorer's own field names."""

    return {
        "likely_precision_e4": metrics.empirical_tier_precision_e4["likely"],
        "listed_precision_e4": metrics.selective_precision_e4,
        "work_precision_e4": metrics.identification_work.precision_e4,
        "work_recall_e4": metrics.identification_work.recall_e4,
    }


def _work_headline(counts: WorkCounts) -> dict[str, int | None]:
    """The same four keys for a work-matched score; listed precision has no time to stand on."""

    return {
        "likely_precision_e4": counts.headline()["likely_precision_e4"],
        "listed_precision_e4": None,
        "work_precision_e4": counts.headline()["work_precision_e4"],
        "work_recall_e4": counts.headline()["work_recall_e4"],
    }


def _counts(state: ScoreState) -> dict[str, dict[str, int]]:
    """The raw numerators and denominators behind :func:`_headline` for one state."""

    likely_correct, likely_predicted = state.tier_work.get("likely", (0, 0))
    return {
        "likely": {"correct": likely_correct, "predicted": likely_predicted},
        "listed": {
            "correct": state.occurrence.correct,
            "predicted": state.occurrence.predicted,
            "truth": state.occurrence.truth,
        },
        "work": {
            "correct": state.identification_work.correct,
            "predicted": state.identification_work.predicted,
            "truth": state.identification_work.truth,
        },
    }


def _work_counts(counts: WorkCounts) -> dict[str, dict[str, int] | None]:
    """The raw counts behind :func:`_work_headline`; ``listed`` is ``None`` (no time to match by).

    ``work.correct`` counts the listed side (distinct listed works that named a truth row — the
    precision numerator) and ``work.truth_matched`` the truth side (distinct truth works some row
    named — the recall numerator); two listed works naming one truth row inflate neither.
    """

    return {
        "likely": {"correct": counts.likely_correct, "predicted": counts.likely},
        "listed": None,
        "work": {
            "correct": counts.works_correct,
            "predicted": counts.works,
            "truth": counts.truth,
            "truth_matched": counts.truth_matched,
        },
    }


def _work_only(counts: WorkCounts) -> dict[str, Any]:
    return {**counts.headline(), "counts": counts.as_dict()}


def _relative(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def score_run_list(
    run_list: RunList,
    *,
    run_list_dir: Path,
    artefact_dir: Path,
    out_dir: Path,
    match: MatchChoice = "auto",
) -> dict[str, Any]:
    """Score every mix, pool the counts, and build the output document."""

    refuse_generated_output(artefact_dir)

    mixes: list[MixScore] = []
    scored_by_set: dict[str, str] = {}
    for entry in run_list.runs:
        mix = score_mix(entry, recipe=run_list.recipe, artefact_dir=artefact_dir, match=match)
        first = scored_by_set.setdefault(mix.truth.set_id, entry.mix_id)
        if first != entry.mix_id:
            # Pooling one mix twice double-weights it and inflates every headline number.
            raise ValueError(
                f"{entry.mix_id}: truth set {mix.truth.set_id} is already scored as {first}; "
                "one mix may appear in a run list once"
            )
        mixes.append(mix)
    # One order-only mix has no time numbers to pool, so the run pools the work-only numbers —
    # the only ones every mix has; a timed mix keeps its own time numbers on its row.
    mode: MatchMode = "work" if any(mix.match_mode == "work" for mix in mixes) else "time"
    work_only = WorkCounts()
    for mix in mixes:
        work_only.add(mix.work_match.counts)
    thresholds = L3_THRESHOLDS[run_list.recipe]
    headline: dict[str, int | None]
    if mode == "time":
        pooled_state = ScoreState()
        for mix in mixes:
            assert mix.score is not None
            pooled_state.add(mix.score.state)
        headline = dict(_headline(pooled_metrics([pooled_state])))
        counts_by_mode = _counts(pooled_state)
        thresholds_met: bool | None = all(
            (headline[key] or 0) >= value for key, value in thresholds.items()
        )
    else:
        headline = _work_headline(work_only)
        counts_by_mode = _work_counts(work_only)
        # The listed-precision bar needs timed truth; a work-only score cannot judge L3.
        thresholds_met = None
    if thresholds_met is True and not certification_enabled():
        # No positive L3 claim while certification is disabled: the thresholds are left
        # unjudged (null), never reported as met.
        thresholds_met = None
    status = min((mix.truth_status for mix in mixes), key=_TRUTH_RANK.__getitem__)
    # The scope of the claim: every truth path in ONE frozen corpus, the run list naming EXACTLY
    # that corpus's frozen inventory, and independence judged over that complete inventory -- so
    # leaving an exposed set out of the run list never makes the rest certifiable.
    scope = certification_scope([mix.entry.truth for mix in mixes])
    independent = all(mix.independent for mix in mixes) and scope.independent is not False
    eligible = status == "verified" and mode == "time" and independent and scope.certifiable
    not_certifiable = _not_certifiable_reasons(mixes, status=status, mode=mode, scope=scope)
    hidden_total: Counter[str] = Counter()
    for mix in mixes:
        hidden_total.update(mix.hidden_by_reason)
    return {
        "schema_version": "1.0.0",
        "generated_by": GENERATED_BY,
        "recipe": run_list.recipe,
        "truth_status": status,
        "match_requested": match,
        "match_mode": mode,
        **headline,
        "work_only": _work_only(work_only),
        "l3": {
            "thresholds": thresholds,
            # The three thresholds only. L3 also asks for a corpus shape (mixes, DJs, platforms,
            # hours, two-pass truth) that these numbers cannot judge — see L3_CORPUS_NOTE.
            "thresholds_met": thresholds_met,
            # Frozen, verified and timed is not enough: truth reviewed while IDea's predictions
            # were on screen is not independent of them, and can back no L3 claim however it scores.
            "independent": independent,
            # Certifiable only for frozen, verified, timed, independent truth whose run list is
            # exactly one whole frozen corpus (see `certification_scope`), and never while the
            # certification gate is closed.
            "certifiable": certifiable_under_gate(eligible),
            "certification": None if certification_enabled() else CERTIFICATION_DISABLED,
            # Every reason this score is not certifiable, in ordinary words; empty when it is.
            "not_certifiable_because": not_certifiable,
            # The scope reasons alone: what `--print` adds to a score of frozen truth.
            "scope": {
                "whole_frozen_corpus": scope.complete,
                "corpus_independent": scope.independent,
                "reasons": list(scope.reasons),
            },
        },
        "counts": {
            "mixes": len(mixes),
            "timing": dict(sorted(Counter(mix.timing for mix in mixes).items())),
            "episodes": {
                "total": sum(mix.episodes_total for mix in mixes),
                "listed": sum(
                    mix.episodes_total - sum(mix.hidden_by_reason.values()) for mix in mixes
                ),
                "hidden": sum(hidden_total.values()),
            },
            "hidden_by_reason": dict(sorted(hidden_total.items())),
            "range_claims": sum(mix.range_claims for mix in mixes),
            **counts_by_mode,
        },
        "mixes": [
            {
                "mix_id": mix.entry.mix_id,
                "set_id": mix.truth.set_id,
                "truth_status": mix.truth_status,
                "independent": mix.independent,
                "timing": mix.timing,
                "match_mode": mix.match_mode,
                "truth": _relative(mix.entry.truth, run_list_dir),
                "episodes": _relative(mix.entry.episodes, run_list_dir),
                "min_track_ms": mix.entry.min_track_ms,
                "episodes_total": mix.episodes_total,
                "episodes_listed": mix.episodes_total - sum(mix.hidden_by_reason.values()),
                "hidden_by_reason": mix.hidden_by_reason,
                "range_claims": mix.range_claims,
                **(
                    _headline(mix.metrics)
                    if mix.metrics is not None
                    else _work_headline(mix.work_match.counts)
                ),
                "counts": (
                    _counts(mix.score.state)
                    if mix.score is not None
                    else _work_counts(mix.work_match.counts)
                ),
                "work_only": _work_only(mix.work_match.counts),
                "median_offset_ms": mix.median_offset_ms,
                "offset_pairs": mix.offset_pairs,
                "unmatched_truth": mix.work_match.unmatched_truth,
                "unmatched_predictions": mix.work_match.unmatched_predictions,
                "report": (
                    None if mix.report_path is None else _relative(mix.report_path, out_dir)
                ),
                "work_match": _relative(mix.work_match_path, out_dir),
            }
            for mix in mixes
        ],
    }


def _not_certifiable_reasons(
    mixes: list[MixScore], *, status: TruthStatus, mode: MatchMode, scope: CertificationScope
) -> list[str]:
    """Every reason a score cannot be called certifiable, each saying what to do next."""

    reasons: list[str] = []
    if not certification_enabled():
        reasons.append(CERTIFICATION_DISABLED)
    # One unfrozen corpus: the scope reason below already says so, and what to do about it.
    one_unfrozen_corpus = scope.corpus_root is not None and scope.independent is None
    if status != "verified" and not one_unfrozen_corpus:
        unsettled = ", ".join(mix.entry.mix_id for mix in mixes if mix.truth_status != "verified")
        reasons.append(
            f"the truth for {unsettled} is not frozen and verified (this score is labelled "
            f"{status}), so this is a development score. Finish checking those tracklists by "
            "ear; a frozen corpus needs every row verified before `idea truth freeze`"
        )
    if mode != "time":
        reasons.append(
            "the tracks were matched by name only, not by when they played, because some truth "
            "has no real start times yet. Add start times to every row so the score can be "
            "matched by time"
        )
    exposed = ", ".join(mix.entry.mix_id for mix in mixes if not mix.independent)
    if exposed:
        reasons.append(
            f"the truth for {exposed} was reviewed with IDea's predictions visible, so it is not "
            "independent of them. It can be scored, but it can never back a certified number"
        )
    reasons.extend(scope.reasons)
    return reasons


def _pct(e4: int) -> str:
    return f"{e4 / 100:.1f}%"


def _matching_note(document: dict[str, Any]) -> str:
    """Which matching ran, and why (``--print``)."""

    mixes = document["mixes"]
    order_only = sum(mix["timing"] == "order-only" for mix in mixes)
    partial = sum(mix["timing"] == "partial" for mix in mixes)
    requested = document["match_requested"]
    if document["match_mode"] == "work":
        if partial:
            # A half-verified file is the dangerous case: it looks finished but its untimed rows
            # would score as misses, so say how many files are in which state.
            untimed = (
                f"{order_only + partial} of the {len(mixes)} truth file(s) still carry the seed's "
                f"placeholder equal-slice timings on some or all rows ({order_only} order-only, "
                f"{partial} only partly timed)"
            )
        else:
            untimed = (
                f"{order_only} of the {len(mixes)} truth file(s) are order-only (a tracklist still "
                "carrying the seed's placeholder equal-slice timings)"
            )
        why = untimed if requested == "auto" else "--match work was given"
        return (
            f"Matching is WORK-ONLY (time-agnostic) because {why}: each listed track is matched to "
            "the tracklist by normalised artist and title alone (mix suffixes, case, word order "
            "and featured artists ignored), never by when it played"
        )
    if order_only or partial:
        state = (
            f"{order_only + partial} of the {len(mixes)} truth file(s) still carry placeholder "
            "timings on some or all rows"
            if partial
            else f"{order_only} of the {len(mixes)} truth file(s) are order-only (placeholder "
            "timings)"
        )
        return (
            f"Matching is by TIME because --match time was given, although {state}, so their "
            "time-based numbers are not meaningful; read the work-only line instead"
        )
    return (
        "Matching is by TIME: every truth file has start times, so a listed track is right only "
        "when it overlaps the occurrence it names"
    )


def summary(document: dict[str, Any], out: Path | None) -> str:
    """One plain-English paragraph for the owner."""

    counts = document["counts"]
    mix_ids = ", ".join(mix["mix_id"] for mix in document["mixes"])
    status = document["truth_status"]
    truth_note = {
        "draft": (
            "DRAFT truth (seeded tracklists with placeholder timings, not yet verified), so these "
            "are working numbers, not release numbers"
        ),
        "unverified": "truth that is settled but not yet frozen, so these are not release numbers",
        "verified": "frozen, verified truth",
    }[status]
    thresholds = document["l3"]["thresholds"]
    bar = (
        f"likely >= {_pct(thresholds['likely_precision_e4'])}, listed >= "
        f"{_pct(thresholds['listed_precision_e4'])} and recall >= "
        f"{_pct(thresholds['work_recall_e4'])} for {document['recipe']}"
    )
    floor = counts["hidden_by_reason"]
    hidden_note = (
        "the presentation floor hid " + ", ".join(f"{n} as {reason}" for reason, n in floor.items())
        if floor
        else "the presentation floor hid nothing"
    )
    where = f" Full numbers: {out.as_posix()}." if out is not None else ""
    exposed = [mix["mix_id"] for mix in document["mixes"] if mix.get("independent") is False]
    independence = (
        f" The truth for {', '.join(exposed)} is not independent of IDea's predictions (they were "
        "visible while it was reviewed), so this score cannot back an L3 or certification claim."
        if exposed
        else ""
    )
    if document["match_mode"] == "work":
        work = counts["work"]
        seen = [
            f"{name.removesuffix('_e4').replace('_', ' ')} {_pct(document[name])} "
            f"{'>=' if document[name] >= thresholds[name] else '<'} {_pct(thresholds[name])}"
            for name in ("likely_precision_e4", "work_recall_e4")
        ]
        numbers = (
            f"it named {work['truth_matched']} of the {work['truth']} distinct tracks actually "
            f"played (work recall {_pct(document['work_recall_e4'])}); {work['correct']} of the "
            f"{work['predicted']} distinct tracks it listed were really played (work precision "
            f"{_pct(document['work_precision_e4'])}); and {counts['likely']['correct']} of the "
            f"{counts['likely']['predicted']} it marked 'likely' or better were right (likely "
            f"precision {_pct(document['likely_precision_e4'])})"
        )
        verdict = (
            f"L3 asks for {bar}; listed precision and every timing number need timed truth "
            "(reported as null), so the L3 bar cannot be judged from a work-only score, and the "
            "two numbers it can see are indicative only (a work-only match is looser than a "
            f"timed one): {'; '.join(seen)}"
        )
    else:
        shortfalls = [
            f"{name.removesuffix('_e4').replace('_', ' ')} {_pct(document[name])} < {_pct(target)}"
            for name, target in thresholds.items()
            if document[name] < target
        ]
        numbers = (
            f"{counts['listed']['correct']} of the {counts['listed']['predicted']} scored were "
            f"tracks really played there (listed precision "
            f"{_pct(document['listed_precision_e4'])}); {counts['likely']['correct']} of the "
            f"{counts['likely']['predicted']} it marked 'likely' or better were right (likely "
            f"precision {_pct(document['likely_precision_e4'])}); and it named "
            f"{counts['work']['correct']} of the {counts['work']['truth']} distinct tracks "
            f"actually played (work recall {_pct(document['work_recall_e4'])})"
        )
        if not shortfalls and not certification_enabled():
            # No positive L3 claim is printed while certification is disabled.
            verdict = (
                f"L3 asks for {bar}: no L3 threshold claim is made because "
                f"{CERTIFICATION_DISABLED} ({L3_CORPUS_NOTE})"
            )
        else:
            verdict = f"L3 asks for {bar}: " + (
                f"all three thresholds are met on these numbers ({L3_CORPUS_NOTE})"
                if not shortfalls
                else "the L3 bar is not met (" + "; ".join(shortfalls) + ")"
            )
        if status != "verified":
            verdict += ", and a non-verified score cannot clear L3 either way"
    gate = document["l3"].get("certification")
    gated = f" {gate}." if gate and gate not in verdict else ""
    # A score of frozen truth says why its scope cannot be certified (a partial run list, an
    # exposed frozen sibling); draft truth is already called a draft above.
    scope_reasons = document["l3"].get("scope", {}).get("reasons", [])
    if status == "verified":
        gated += "".join(f" NOT CERTIFIABLE: {reason}." for reason in scope_reasons)
    return (
        f"The {document['recipe']} recipe was scored over {counts['mixes']} mix(es) ({mix_ids}) "
        f"against {truth_note}. {_matching_note(document)}. Of the "
        f"{counts['episodes']['total']} tracks the tool found, {hidden_note}, leaving "
        f"{counts['episodes']['listed']} listed; {numbers}. {verdict}.{independence}{gated}{where}"
    )


def work_only_line(document: dict[str, Any]) -> str:
    """The time-agnostic numbers of a time-matched run, so the two kinds of run compare."""

    work_only = document["work_only"]
    counts = work_only["counts"]
    return (
        "Work-only (time-agnostic) numbers for comparison: it named "
        f"{counts['truth_works']['matched']} of the {counts['truth_works']['total']} distinct "
        f"tracks actually played (work recall {_pct(work_only['work_recall_e4'])}); "
        f"{counts['works']['correct']} of the {counts['works']['predicted']} distinct tracks it "
        f"listed were really played (work precision {_pct(work_only['work_precision_e4'])}); "
        f"{counts['likely']['correct']} of the {counts['likely']['predicted']} it marked 'likely' "
        f"or better named a played track (likely precision "
        f"{_pct(work_only['likely_precision_e4'])})."
    )


def mix_line(mix: dict[str, Any]) -> str:
    """One line per mix: its numbers, the offset diagnostic when timed, what was missed and what
    was wrong (both under their original labels)."""

    work_only = mix["work_only"]
    counts = work_only["counts"]
    by_time = ""
    if mix["match_mode"] == "time":
        timed = mix["counts"]
        by_time = (
            f"by time: likely {timed['likely']['correct']}/{timed['likely']['predicted']} "
            f"({_pct(mix['likely_precision_e4'])}), listed {timed['listed']['correct']}/"
            f"{timed['listed']['predicted']} ({_pct(mix['listed_precision_e4'])}), recall "
            f"{timed['work']['correct']}/{timed['work']['truth']} "
            f"({_pct(mix['work_recall_e4'])}); "
        )
    by_work = (
        f"work-only: recall {counts['truth_works']['matched']}/{counts['truth_works']['total']} "
        f"({_pct(work_only['work_recall_e4'])}), precision {counts['works']['correct']}/"
        f"{counts['works']['predicted']} ({_pct(work_only['work_precision_e4'])}), likely "
        f"{counts['likely']['correct']}/{counts['likely']['predicted']} "
        f"({_pct(work_only['likely_precision_e4'])})"
    )
    offset = ""
    if mix["median_offset_ms"] is not None:
        pairs = mix["offset_pairs"]
        seconds = mix["median_offset_ms"] / 1000
        offset = (
            f"; median offset (tool start minus tracklist start) {seconds:+.1f} s over {pairs} "
            f"work-matched pair{'s' if pairs != 1 else ''}"
        )
    elif mix["timing"] != "order-only":
        offset = "; median offset n/a (no work-matched pair with engine evidence on a timed row)"
    missed = (
        "; ".join(f"{item['artist']} - {item['title']}" for item in mix["unmatched_truth"])
        or "nothing"
    )
    wrong = (
        "; ".join(
            f"{item['artist']} - {item['title']} [{item['tier']}]"
            for item in mix["unmatched_predictions"]
        )
        or "nothing"
    )
    return (
        f"- {mix['mix_id']} [{mix['timing']}; matched by {mix['match_mode']}]: {by_time}{by_work}"
        f"{offset}. Missed: {missed}. Wrong: {wrong}."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score one recipe's runs over a corpus; pool the counts (plan 1b-iii / L3)."
    )
    parser.add_argument("--run-list", type=Path, required=True, help="Run-list JSON (see module).")
    parser.add_argument(
        "--out",
        type=Path,
        help="Output JSON; per-mix predictions and reports go to <out stem>-mixes/ beside it.",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        help="Print a plain-English paragraph, then one line per mix (missed / wrong tracks).",
    )
    parser.add_argument(
        "--match",
        choices=("auto", "time", "work"),
        default="auto",
        help=(
            "How a listed track is matched to the truth: by time overlap (the benchmark scorer), "
            "by normalised work identity alone, or (default) work for order-only truth and time "
            "otherwise."
        ),
    )
    args = parser.parse_args(argv)
    if args.out is None and not args.print:
        parser.error("give --out, --print, or both")
    try:
        run_list = load_run_list(args.run_list)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"invalid run list {args.run_list}: {exc}", file=sys.stderr)
        return 2
    try:
        if args.out is not None:
            refuse_generated_output(args.out)  # on the path as given, before it is resolved
            out: Path = args.out.resolve()
            artefact_dir = out.parent / f"{out.stem}-mixes"
            refuse_generated_output(artefact_dir)
            document = score_run_list(
                run_list,
                run_list_dir=args.run_list.resolve().parent,
                artefact_dir=artefact_dir,
                out_dir=out.parent,
                match=args.match,
            )
            refuse_generated_output(out)  # revalidated immediately before the write
            atomic_write_json(out, document)
        else:
            with tempfile.TemporaryDirectory(prefix="idea-score-corpus-") as scratch:
                document = score_run_list(
                    run_list,
                    run_list_dir=args.run_list.resolve().parent,
                    artefact_dir=Path(scratch),
                    out_dir=Path(scratch),
                    match=args.match,
                )
            out = None
    # KeyError/StopIteration: a fuse artefact whose identity graph is inconsistent beyond the
    # candidate check (a missing work or node) surfaces from a `next(...)` deep in the mapping.
    except (OSError, ValueError, ValidationError, KeyError, StopIteration) as exc:
        print(f"scoring failed: {exc if str(exc) else exc!r}", file=sys.stderr)
        return 1
    if args.print:
        # The per-mix lines carry arbitrary track titles; a narrow console encoding must not turn
        # a finished score into a UnicodeEncodeError.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="backslashreplace")
        print(summary(document, out))
        if document["match_mode"] == "time":
            print(work_only_line(document))
        for mix in document["mixes"]:
            print(mix_line(mix))
    elif out is not None:
        listed = document["listed_precision_e4"]
        print(
            f"scored {document['counts']['mixes']} mix(es) [{document['truth_status']} truth, "
            f"matched by {document['match_mode']}]: "
            f"likely {document['likely_precision_e4']}/10000, "
            f"listed {'n/a' if listed is None else f'{listed}/10000'}, "
            f"recall {document['work_recall_e4']}/10000; report={out}"
        )
        if document["l3"].get("certification"):
            print(
                f"NOT CERTIFIABLE: {document['l3']['certification']}. "
                f"{CERTIFICATION_DISABLED_NEXT_STEP}"
            )
        exposed = [mix["mix_id"] for mix in document["mixes"] if mix["independent"] is False]
        if exposed:
            print(
                f"NOT CERTIFIABLE: the truth for {', '.join(exposed)} is not independent of "
                "IDea's predictions (they were visible while it was reviewed); it cannot back L3"
            )
        if document["truth_status"] == "verified":
            for reason in document["l3"]["scope"]["reasons"]:
                print(f"NOT CERTIFIABLE: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
