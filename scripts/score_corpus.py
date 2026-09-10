"""Score one recipe's runs over a corpus and pool the counts (plan 1b-iii; launch gate L3).

Usage::

    uv run python scripts/score_corpus.py --run-list data/corpus/release-1/runs-deep.json \\
        --out docs/accuracy/release-1-deep.json [--print]

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
of what a reader is actually shown.  What remains goes to the existing benchmark scorer
(:func:`id_detector.benchmark.scorer.score_corpus_detailed`, the function behind ``idea benchmark
score``) and that mix's full report is written next to ``--out``.  Across mixes the counts are
**pooled**: numerators and denominators are summed before any ratio is taken, never a mean of
per-mix ratios, so a forty-track mix weighs forty times a one-track mix.  Three e4 integers come
out of the pooled metrics:

- ``likely_precision_e4`` — ``empirical_tier_precision_e4["likely"]``: of the listed predictions
  whose work tier is ``likely`` or better, the share that named a track really played there;
- ``listed_precision_e4`` — ``selective_precision_e4`` after the floor: of every listed
  prediction, the share associated with a truth occurrence;
- ``work_recall_e4`` — ``identification_work.recall_e4``: of the distinct works in the truth,
  the share the listed predictions named.

A crowd ID (fusion's ``hint_only`` episode) claims a position *range* rather than engine-proved
bounds, which the benchmark's ``ScoredEpisode`` contract cannot express; :func:`proved_bounds`
re-states those two bounds in the contract's convention and the output counts them as
``range_claims``, so a listed name with no audio proof is scored, never hidden.

Draft truth (rows with ``draft: true`` — e.g. the seeded ``data/corpus/release-1/`` files with
their placeholder equal-slice timings) is accepted, but the output is labelled ``"truth_status":
"draft"``; only truth covered by a frozen, hash-checked ``corpus-version.json`` with every row
verified reads ``"verified"``, and only that can back the L3 numbers.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from id_detector.benchmark.corpus import prediction_set_from_fusion
from id_detector.benchmark.scorer import (
    PredictionDocument,
    ScoreState,
    ScoringConfigSnapshot,
    SetScore,
    load_truth_directory,
    pooled_metrics,
    score_corpus_detailed,
    truth_is_frozen_verified,
)
from id_detector.contracts import (
    BenchmarkMetrics,
    EpisodesFile,
    GroundTruthRecord,
    IdentitiesRecord,
)
from id_detector.io import (
    atomic_write_json,
    canonical_json_bytes,
    completion_sidecar_path,
    read_text,
)
from id_detector.present.exports import flatten_tracklist, hidden_reason

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


@dataclass(frozen=True)
class MixScore:
    entry: RunEntry
    truth: GroundTruthRecord
    truth_status: TruthStatus
    episodes_total: int
    hidden_by_reason: dict[str, int]
    #: Listed rows whose bounds were a position range, not engine-proved (see `proved_bounds`).
    range_claims: int
    score: SetScore
    metrics: BenchmarkMetrics
    report_path: Path


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
        spans = [(int(start), int(end)) for start, end in episode["evidence_support_ms"]]
        lo, hi = min(start for start, _ in spans), max(end for _, end in spans)
        calibrated = any(
            getattr(episode[key], "calibrated", False) for key in ("start_pi", "end_pi")
        )
        if (
            calibrated
            or episode["start_no_later_than_ms"] > lo
            or episode["end_no_earlier_than_ms"] < hi
        ):
            normalised.append(episode)
            continue
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


def score_mix(entry: RunEntry, *, recipe: str, artefact_dir: Path) -> MixScore:
    """Pre-filter one mix's episodes, score them with the benchmark scorer, keep the raw counts."""

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
    prediction_set = prediction_set_from_fusion(truth.set_id, identities, listed)
    prediction_set["episodes"], range_claims = proved_bounds(prediction_set["episodes"])
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
        },
    )
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
    atomic_write_json(predictions_path, document)
    report_path = mix_dir / "report.json"
    report, scores = score_corpus_detailed(entry.truth, predictions_path, out_path=report_path)
    (score,) = scores
    return MixScore(
        entry=entry,
        truth=truth,
        truth_status=status,
        episodes_total=len(episodes.episodes),
        hidden_by_reason=hidden_by_reason,
        range_claims=range_claims,
        score=score,
        metrics=report.overall,
        report_path=report_path,
    )


def _headline(metrics: BenchmarkMetrics) -> dict[str, int]:
    """The three L3 numbers, read from the scorer's own field names."""

    return {
        "likely_precision_e4": metrics.empirical_tier_precision_e4["likely"],
        "listed_precision_e4": metrics.selective_precision_e4,
        "work_recall_e4": metrics.identification_work.recall_e4,
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


def _relative(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def score_run_list(
    run_list: RunList, *, run_list_dir: Path, artefact_dir: Path, out_dir: Path
) -> dict[str, Any]:
    """Score every mix, pool the counts, and build the output document."""

    mixes: list[MixScore] = []
    scored_by_set: dict[str, str] = {}
    for entry in run_list.runs:
        mix = score_mix(entry, recipe=run_list.recipe, artefact_dir=artefact_dir)
        first = scored_by_set.setdefault(mix.truth.set_id, entry.mix_id)
        if first != entry.mix_id:
            # Pooling one mix twice double-weights it and inflates every headline number.
            raise ValueError(
                f"{entry.mix_id}: truth set {mix.truth.set_id} is already scored as {first}; "
                "one mix may appear in a run list once"
            )
        mixes.append(mix)
    pooled_state = ScoreState()
    for mix in mixes:
        pooled_state.add(mix.score.state)
    pooled = pooled_metrics([pooled_state])
    headline = _headline(pooled)
    thresholds = L3_THRESHOLDS[run_list.recipe]
    status = min((mix.truth_status for mix in mixes), key=_TRUTH_RANK.__getitem__)
    hidden_total: Counter[str] = Counter()
    for mix in mixes:
        hidden_total.update(mix.hidden_by_reason)
    return {
        "schema_version": "1.0.0",
        "generated_by": GENERATED_BY,
        "recipe": run_list.recipe,
        "truth_status": status,
        **headline,
        "l3": {
            "thresholds": thresholds,
            # The three thresholds only. L3 also asks for a corpus shape (mixes, DJs, platforms,
            # hours, two-pass truth) that these numbers cannot judge — see L3_CORPUS_NOTE.
            "thresholds_met": all(headline[key] >= value for key, value in thresholds.items()),
            "certifiable": status == "verified",
        },
        "counts": {
            "mixes": len(mixes),
            "episodes": {
                "total": sum(mix.episodes_total for mix in mixes),
                "listed": sum(
                    mix.episodes_total - sum(mix.hidden_by_reason.values()) for mix in mixes
                ),
                "hidden": sum(hidden_total.values()),
            },
            "hidden_by_reason": dict(sorted(hidden_total.items())),
            "range_claims": sum(mix.range_claims for mix in mixes),
            **_counts(pooled_state),
        },
        "mixes": [
            {
                "mix_id": mix.entry.mix_id,
                "set_id": mix.truth.set_id,
                "truth_status": mix.truth_status,
                "truth": _relative(mix.entry.truth, run_list_dir),
                "episodes": _relative(mix.entry.episodes, run_list_dir),
                "min_track_ms": mix.entry.min_track_ms,
                "episodes_total": mix.episodes_total,
                "episodes_listed": mix.episodes_total - sum(mix.hidden_by_reason.values()),
                "hidden_by_reason": mix.hidden_by_reason,
                "range_claims": mix.range_claims,
                **_headline(mix.metrics),
                "counts": _counts(mix.score.state),
                "report": _relative(mix.report_path, out_dir),
            }
            for mix in mixes
        ],
    }


def _pct(e4: int) -> str:
    return f"{e4 / 100:.1f}%"


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
    shortfalls = [
        f"{name.removesuffix('_e4').replace('_', ' ')} {_pct(document[name])} < {_pct(target)}"
        for name, target in thresholds.items()
        if document[name] < target
    ]
    verdict = (
        f"all three thresholds are met on these numbers ({L3_CORPUS_NOTE})"
        if not shortfalls
        else "the L3 bar is not met (" + "; ".join(shortfalls) + ")"
    )
    if status != "verified":
        verdict += ", and a non-verified score cannot clear L3 either way"
    floor = counts["hidden_by_reason"]
    hidden_note = (
        "the presentation floor hid " + ", ".join(f"{n} as {reason}" for reason, n in floor.items())
        if floor
        else "the presentation floor hid nothing"
    )
    where = f" Full numbers: {out.as_posix()}." if out is not None else ""
    return (
        f"The {document['recipe']} recipe was scored over {counts['mixes']} mix(es) ({mix_ids}) "
        f"against {truth_note}. Of the {counts['episodes']['total']} tracks the tool found, "
        f"{hidden_note}, leaving {counts['episodes']['listed']} listed; "
        f"{counts['listed']['correct']} of the {counts['listed']['predicted']} scored were tracks "
        f"really played there (listed precision {_pct(document['listed_precision_e4'])}); "
        f"{counts['likely']['correct']} of the {counts['likely']['predicted']} it marked "
        f"'likely' or better were right (likely precision "
        f"{_pct(document['likely_precision_e4'])}); and it named "
        f"{counts['work']['correct']} of the {counts['work']['truth']} distinct tracks actually "
        f"played (work recall {_pct(document['work_recall_e4'])}). L3 asks for likely >= "
        f"{_pct(thresholds['likely_precision_e4'])}, listed >= "
        f"{_pct(thresholds['listed_precision_e4'])} and recall >= "
        f"{_pct(thresholds['work_recall_e4'])} for {document['recipe']}: {verdict}.{where}"
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
        "--print", action="store_true", help="Print a one-paragraph plain-English summary."
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
            out: Path = args.out.resolve()
            artefact_dir = out.parent / f"{out.stem}-mixes"
            document = score_run_list(
                run_list,
                run_list_dir=args.run_list.resolve().parent,
                artefact_dir=artefact_dir,
                out_dir=out.parent,
            )
            atomic_write_json(out, document)
        else:
            with tempfile.TemporaryDirectory(prefix="idea-score-corpus-") as scratch:
                document = score_run_list(
                    run_list,
                    run_list_dir=args.run_list.resolve().parent,
                    artefact_dir=Path(scratch),
                    out_dir=Path(scratch),
                )
            out = None
    # KeyError/StopIteration: a fuse artefact whose identity graph is inconsistent beyond the
    # candidate check (a missing work or node) surfaces from a `next(...)` deep in the mapping.
    except (OSError, ValueError, ValidationError, KeyError, StopIteration) as exc:
        print(f"scoring failed: {exc if str(exc) else exc!r}", file=sys.stderr)
        return 1
    if args.print:
        print(summary(document, out))
    elif out is not None:
        print(
            f"scored {document['counts']['mixes']} mix(es) [{document['truth_status']} truth]: "
            f"likely {document['likely_precision_e4']}/10000, "
            f"listed {document['listed_precision_e4']}/10000, "
            f"recall {document['work_recall_e4']}/10000; report={out}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
