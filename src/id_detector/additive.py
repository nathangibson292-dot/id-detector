"""Paid evidence that only ADDS to the free result: which windows it checks, and how it is fused.

The Deep recipe is free-first (restoring the original "free-first, paid fills gaps" design): the
full free Shazam sweep runs (or is reused from a stored Free result), is fused, and only then does
the paid engine check the windows a policy selects from that free result.  This module is the one
place that decides

* the policy's target spans (:func:`policy_targets`) and the exact window clips they select
  (:func:`paid_windows`) -- shared by the pipeline, ``idea cost`` and the offline measurement, so
  the price the owner confirms is the count the sweep dispatches;
* what a cached paid answer says without sending anything (:func:`paid_observations_from_cache`);
* the fusion invariant (:func:`additive_merge`): paid evidence may confirm or add a row, never
  remove a row the free evidence supports on its own.

Nothing here touches the network, the money fences or the request that is sent to AudD.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from id_detector.contracts import (
    EpisodeRecord,
    EpisodesFile,
    HintRecord,
    IdentitiesRecord,
    ObservationRecord,
    WindowRecord,
)
from id_detector.io import path_is_file, read_text
from id_detector.scan_targeting import Span, select_gap_targets, select_scan_targets

PaidPolicy = Literal["everywhere", "gaps", "gaps_and_unsure"]
POLICIES: tuple[PaidPolicy, ...] = ("everywhere", "gaps", "gaps_and_unsure")
#: The policy the Deep recipe runs (its ``secondary_priority``), chosen from the offline measurement
#: on the owner's three mixes (docs/reviews/build-local-additive-deep.md): the same recall as paying
#: everywhere, for about two thirds of the requests.
DEEP_POLICY: PaidPolicy = "gaps"


def policy_targets(
    policy: PaidPolicy, episodes: Sequence[EpisodeRecord], duration_ms: int
) -> tuple[Span, ...]:
    """The mix spans the paid engine checks under ``policy``, read from the FREE result."""

    if duration_ms <= 0:
        return ()
    if policy == "everywhere":
        return ((0, duration_ms),)
    if policy == "gaps":
        return select_gap_targets(list(episodes), duration_ms)
    if policy == "gaps_and_unsure":
        return select_scan_targets(list(episodes), duration_ms)
    raise ValueError(f"unknown paid policy {policy!r}")


def paid_windows(
    windows: Iterable[WindowRecord], targets: Sequence[Span], *, density: int = 1
) -> list[WindowRecord]:
    """Exactly the clips the paid sweep dispatches for ``targets`` (its own selection rule)."""

    from id_detector.paid_clip import _windows_in_targets
    from id_detector.windows import WindowsResult

    if density <= 0:
        raise ValueError("density must be positive")
    if not targets:
        return []
    records = tuple(windows)
    eligible = _windows_in_targets(
        WindowsResult(records=records, record_path=Path("."), cached=True), tuple(targets)
    )
    eligible = [
        window for window in eligible if window.generation == 0 and window.transform.type == "none"
    ]
    return eligible[::density]


def paid_cache_path(media_dir: Path, window: WindowRecord) -> Path:
    """Where the live sweep stores (and reads) the paid answer for this clip's content."""

    from id_detector.contracts import clip_cache_key
    from id_detector.paid_clip import CLIP_CONFIG_VERSION

    key = clip_cache_key(window.wav_sha256, "audd", CLIP_CONFIG_VERSION)
    return media_dir / "recognise" / "invocations" / "live-audd-clip-v1" / "raw" / f"{key}.json"


def cached_paid_answer(media_dir: Path, window: WindowRecord) -> dict[str, Any] | None:
    """A cached match/no-match body for this clip, or ``None`` (reading only; never sending)."""

    from id_detector.paid_clip import _read_cached

    return _read_cached(paid_cache_path(media_dir, window), frozenset())


def uncached_paid_windows(media_dir: Path, windows: Sequence[WindowRecord]) -> int:
    """How many of ``windows`` would cost a request: no cached match or no-match answer."""

    return sum(cached_paid_answer(media_dir, window) is None for window in windows)


@dataclass(frozen=True)
class CachedPaid:
    observations: tuple[ObservationRecord, ...]
    cached: int
    missing: int

    @property
    def matches(self) -> int:
        return sum(item.status == "match" for item in self.observations)


def paid_observations_from_cache(
    *, media_key: str, media_dir: Path, windows: Sequence[WindowRecord]
) -> CachedPaid:
    """The observations the live sweep would record for ``windows``, from cached answers ONLY.

    A window without a cached answer is counted in ``missing`` (it would cost one request) and
    contributes nothing: this function has no way to send anything.
    """

    from id_detector.paid_clip import _clip_query
    from id_detector.providers.audd import (
        DEFAULT_ANCHOR_MAX_MS,
        DEFAULT_ANCHOR_SLACK_MS,
        clip_response_to_observation,
    )

    observations: list[ObservationRecord] = []
    missing = 0
    for window in windows:
        path = paid_cache_path(media_dir, window)
        response = cached_paid_answer(media_dir, window)
        if response is None:
            missing += 1
            continue
        query = _clip_query(media_key, window)
        observations.append(
            clip_response_to_observation(
                response,
                query=query,
                window=window,
                media_key=media_key,
                raw_response_ref=path.relative_to(media_dir).as_posix(),
                anchor_max_ms=DEFAULT_ANCHOR_MAX_MS,
                anchor_slack_ms=DEFAULT_ANCHOR_SLACK_MS,
            )
        )
    return CachedPaid(tuple(observations), cached=len(observations), missing=missing)


@dataclass(frozen=True)
class Fused:
    episodes: EpisodesFile
    identities: IdentitiesRecord


def fuse_offline(
    *,
    media_key: str,
    duration_ms: int,
    observations: Sequence[ObservationRecord],
    windows: Sequence[WindowRecord],
    hints: Sequence[HintRecord] = (),
    config: Any = None,
    generation: int = 0,
) -> Fused:
    """One plain fusion pass over ``observations`` (the pipeline's generation-0 fusion, offline)."""

    from id_detector.fuse.episodes import build_episodes
    from id_detector.fuse.identity import build_identity_graph
    from id_detector.orchestrate import scanned_windows

    identity = build_identity_graph(media_key, tuple(observations), hints=tuple(hints))
    episodes, _requests = build_episodes(
        media_key=media_key,
        duration_ms=duration_ms,
        observations=tuple(observations),
        windows=tuple(scanned_windows(windows, observations)),
        identity=identity,
        hints=tuple(hints),
        generation=generation,
        config=config,
    )
    return Fused(episodes, identity.record)


#: Evidence tiers and badges, weakest first (the scorer's and the page's own order).
_TIER = {"unclear": 0, "possible": 1, "likely": 2, "verified": 3}
#: Flags that record a free row's corroboration by a tracklist or crowd line.
_SUPPORT_FLAGS = frozenset({"hint_supported", "hint_only"})


def listed_episode_ids(fused: Fused, min_track_ms: int) -> frozenset[str]:
    """The rows the page and every export LIST (not hidden) -- the presentation floor itself."""

    from id_detector.present.exports import flatten_tracklist, hidden_reason

    entries = flatten_tracklist(
        fused.episodes,
        fused.identities,
        collapse=False,
        min_track_ms=min_track_ms,
        include_hidden=True,
    )
    return frozenset(
        str(entry["episode_id"])
        for entry in entries
        if entry.get("kind") == "track" and hidden_reason(entry, min_track_ms) is None
    )


def _normalised(text: str) -> str:
    return "".join(character for character in text.casefold() if character.isalnum())


class _Graph:
    """Read-only lookups over one fusion's identity record."""

    def __init__(self, identities: IdentitiesRecord) -> None:
        from id_detector.present.exports import _candidate_label

        self.identities = identities
        self.candidates = {item.canonical_id: item for item in identities.candidates}
        self.works = {item.work_id: item for item in identities.works}
        self._label = _candidate_label

    def provider_nodes(self, candidate_id: str) -> frozenset[str]:
        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            return frozenset()
        return frozenset(node for node in candidate.member_nodes if not node.startswith("text:"))

    def text_nodes(self, candidate_id: str) -> frozenset[str]:
        """The tracklist/crowd lines linked to this candidate's work (what names it for readers)."""

        candidate = self.candidates.get(candidate_id)
        work = self.works.get(candidate.work_id) if candidate is not None else None
        if work is None:
            return frozenset()
        return frozenset(node for node in work.member_nodes if node.startswith("text:"))

    def label(self, candidate_id: str) -> str:
        try:
            artist, title = self._label(self.identities, candidate_id)
        except StopIteration:
            return ""
        return _normalised(f"{artist} {title}")

    def presented(self, candidate_id: str) -> tuple[tuple[str, str], tuple[str, str]] | None:
        """Exactly what a row of this candidate publishes: the page/export artist and title, and
        the work label the tracklist exports and the scorer read."""

        from id_detector.benchmark.corpus import _label_for_candidate

        try:
            return (
                tuple(self._label(self.identities, candidate_id)),  # type: ignore[return-value]
                tuple(_label_for_candidate(self.identities, candidate_id)),
            )
        except StopIteration:
            return None


def _overlaps(left: EpisodeRecord, right: EpisodeRecord) -> bool:
    def span(item: EpisodeRecord) -> tuple[int, int]:
        points = [int(point) for pair in item.evidence_support_ms for point in pair]
        points += [item.best_start_ms, item.best_end_ms]
        return min(points), max(points)

    (a_lo, a_hi), (b_lo, b_hi) = span(left), span(right)
    return a_lo <= b_hi and b_lo <= a_hi


def _same_work(
    paid_row: EpisodeRecord, paid_graph: _Graph, free_row: EpisodeRecord, free_graph: _Graph
) -> bool:
    if paid_graph.provider_nodes(paid_row.candidate_id) & free_graph.provider_nodes(
        free_row.candidate_id
    ):
        return True
    label = paid_graph.label(paid_row.candidate_id)
    return bool(label) and label == free_graph.label(free_row.candidate_id)


def _preserves(
    paid_row: EpisodeRecord,
    paid_graph: _Graph,
    paid_listed: frozenset[str],
    free_row: EpisodeRecord,
    free_graph: _Graph,
    free_listed: frozenset[str],
    hint_ids: frozenset[str],
) -> bool:
    """Whether ``paid_row`` keeps everything the free evidence gave ``free_row``.

    Listed stays listed -- under exactly the same artist and title; the tier and badge never
    drop; every tracklist/crowd line that supported or named the free row still does.  Only then
    may the paid-touched row stand in for it.  (A paid engine that names the same recording with
    different metadata therefore never renames a row the free result lists: that row is kept.)
    """

    if free_row.id in free_listed:
        if paid_row.id not in paid_listed:
            return False
        shown = free_graph.presented(free_row.candidate_id)
        if shown is None or paid_graph.presented(paid_row.candidate_id) != shown:
            return False
    if _TIER[paid_row.tiers.work] < _TIER[free_row.tiers.work]:
        return False
    if _TIER[paid_row.badge] < _TIER[free_row.badge]:
        return False
    free_hints = hint_ids.intersection(free_row.evidence)
    if not free_hints <= set(paid_row.evidence):
        return False
    if not (_SUPPORT_FLAGS & set(free_row.flags)) <= set(paid_row.flags):
        return False
    return free_graph.text_nodes(free_row.candidate_id) <= paid_graph.text_nodes(
        paid_row.candidate_id
    )


def _union_identities(first: IdentitiesRecord, second: IdentitiesRecord) -> IdentitiesRecord:
    """Every record of ``first``, plus the ones only ``second`` has (``first`` wins on an id)."""

    def merged(left: list[Any], right: list[Any], key: str) -> list[Any]:
        seen = {getattr(item, key) for item in left}
        return [*left, *(item for item in right if getattr(item, key) not in seen)]

    return first.model_copy(
        update={
            "nodes": merged(first.nodes, second.nodes, "id"),
            "assertions": merged(first.assertions, second.assertions, "id"),
            "works": merged(first.works, second.works, "work_id"),
            "candidates": merged(first.candidates, second.candidates, "canonical_id"),
        }
    )


@dataclass(frozen=True)
class MergeReport:
    added: int = 0
    confirmed: int = 0
    kept_free: int = 0


def additive_merge(
    *,
    free: Fused,
    combined: Fused,
    paid_observation_ids: Iterable[str],
    hint_ids: Iterable[str] = (),
    min_track_ms: int,
) -> tuple[Fused, MergeReport]:
    """The Deep result: the free result, plus what paid evidence ADDS or CONFIRMS -- never less.

    ``free`` is the fusion of the free evidence alone, ``combined`` the fusion of free + paid.
    A combined row that no paid observation touched is the free engine's own row, and the free
    fusion already decided it: it is ignored.  A paid-touched combined row

    * that overlaps no free row of the same work is an ADDITION (a track the free pass missed);
    * that overlaps free rows of the same work CONFIRMS them -- and replaces them -- only when it
      keeps everything the free evidence gave each of them (:func:`_preserves`); otherwise the free
      rows stay exactly as free fusion made them and the paid row is dropped.

    So a row the free evidence lists on its own is never removed, demoted or re-labelled by paid
    evidence; a free row that was already hidden on free evidence alone may be lifted by a
    confirming paid row, never protected.  The identity record is the free one plus whatever the
    kept combined rows need.
    """

    paid_ids = frozenset(paid_observation_ids)
    hints = frozenset(hint_ids)
    free_graph, paid_graph = _Graph(free.identities), _Graph(combined.identities)
    free_listed = listed_episode_ids(free, min_track_ms)
    combined_listed = listed_episode_ids(combined, min_track_ms)
    free_rows = list(free.episodes.episodes)
    replaced: set[str] = set()
    extra: list[EpisodeRecord] = []
    report = MergeReport()
    for row in combined.episodes.episodes:
        if not paid_ids.intersection(row.evidence):
            continue
        same = [
            item
            for item in free_rows
            if _overlaps(row, item) and _same_work(row, paid_graph, item, free_graph)
        ]
        if not same:
            extra.append(row)
            report = MergeReport(report.added + 1, report.confirmed, report.kept_free)
            continue
        if all(
            _preserves(row, paid_graph, combined_listed, item, free_graph, free_listed, hints)
            for item in same
        ):
            replaced.update(item.id for item in same)
            extra.append(row)
            report = MergeReport(report.added, report.confirmed + 1, report.kept_free)
        else:
            report = MergeReport(report.added, report.confirmed, report.kept_free + 1)
    if not extra:
        return free, report
    kept = [item for item in free_rows if item.id not in replaced]
    seen = {item.id for item in kept}
    merged_rows = [*kept, *(item for item in extra if item.id not in seen)]
    merged_rows.sort(key=lambda item: (item.best_start_ms, item.best_end_ms, item.id))
    episodes = combined.episodes.model_copy(update={"episodes": merged_rows})
    return Fused(episodes, _union_identities(free.identities, combined.identities)), report


@dataclass(frozen=True)
class PaidPlan:
    """What the paid step of a Deep run would check and cost -- known once the free pass is done.

    ``clips`` is exactly the set the sweep dispatches (:func:`paid_windows`); ``cached`` of them
    already have a stored paid answer and cost nothing, so ``to_send`` is the exact request count.
    """

    duration_ms: int
    policy: str
    spans: int
    target_ms: int
    clips: int
    cached: int
    density: int
    unit_usd_e6: int

    @property
    def to_send(self) -> int:
        return max(0, self.clips - self.cached)

    @property
    def estimate_e6(self) -> int:
        return self.to_send * self.unit_usd_e6

    @property
    def reservation_e6(self) -> int:
        return (self.estimate_e6 * 105 + 99) // 100


def plan_paid_step(
    *,
    policy: str,
    free_episodes: Sequence[EpisodeRecord],
    windows: Iterable[WindowRecord],
    duration_ms: int,
    density: int,
    unit_usd_e6: int,
    media_dir: Path | None,
    refresh: bool = False,
) -> tuple[PaidPlan, tuple[Span, ...]]:
    """The exact paid plan for a free result: its targets, its clips and how many are cached.

    ``media_dir`` is where the paid clip cache lives (``None``: nothing counts as cached).  With
    ``refresh`` every clip is re-sent, so none counts as cached.  Reads files only; sends nothing.
    """

    targets = policy_targets(policy, free_episodes, duration_ms)  # type: ignore[arg-type]
    chosen = paid_windows(windows, targets, density=density)
    cached = (
        0
        if refresh or media_dir is None
        else len(chosen) - uncached_paid_windows(media_dir, chosen)
    )
    plan = PaidPlan(
        duration_ms=duration_ms,
        policy=policy,
        spans=len(targets),
        target_ms=sum(max(0, end - start) for start, end in targets),
        clips=len(chosen),
        cached=cached,
        density=density,
        unit_usd_e6=unit_usd_e6,
    )
    return plan, targets


@dataclass(frozen=True)
class FreeEvidence:
    """A stored Free result's recognition evidence, reused by a Deep run instead of a sweep."""

    observations: tuple[ObservationRecord, ...]
    source: Path


def _gen0_windows_of(observations: Sequence[ObservationRecord]) -> set[str] | None:
    """The generation-0 window ids ``observations`` answer; ``None`` if any is not reusable."""

    ids: set[str] = set()
    for item in observations:
        if item.generation != 0 or item.provider != "shazam":
            return None
        ids.update(
            source.removeprefix("window:")
            for source in item.source_ids
            if source.startswith("window:")
        )
    return ids


def find_free_evidence(
    media_dir: Path,
    request: Any,
    *,
    windows: Sequence[WindowRecord],
    local: bool,
) -> FreeEvidence | None:
    """The newest stored Free result's Shazam evidence for this mix, or ``None``.

    First the compatibility contract's own reusable Free evidence (a sealed Free bundle that
    carries its ``shazam-observations.json``).  Locally, also a Free result whose recognition files
    are PROVEN by its own completion records -- a pre-bundle result, or a re-fused one -- as long as
    every observation is a generation-0 Shazam answer for one of today's frozen windows.  Anything
    that cannot be proven is simply not reused: the Deep run then sweeps the mix itself.
    """

    from id_detector.compat import find_result, load_free_observations

    bundle = find_result(media_dir, request, free_evidence=True)
    frozen = {window.id for window in windows if window.generation == 0}
    if bundle is not None:
        observations, path = load_free_observations(bundle)
        answered = _gen0_windows_of(observations)  # type: ignore[arg-type]
        if answered is not None and answered <= frozen:
            return FreeEvidence(tuple(observations), path)  # type: ignore[arg-type]
    if not local:
        return None
    for observations, _hints, source in proven_free_results(media_dir, windows):
        return FreeEvidence(observations, source)
    return None


def proven_free_results(
    media_dir: Path, windows: Sequence[WindowRecord]
) -> Iterator[tuple[tuple[ObservationRecord, ...], tuple[HintRecord, ...], Path]]:
    """Every complete stored Free result of this mix whose recognition files are PROVEN, newest
    first: ``(observations, hints, result directory)``.  Only generation-0 Shazam answers for
    ``windows`` qualify.  Reads files only."""

    from id_detector.present.bundles import (
        legacy_result_metadata,
        load_run_snapshot,
        read_bundle_manifest,
    )
    from id_detector.refusion import NotRebuildable, _source_fuse_dir

    frozen = {window.id for window in windows if window.generation == 0}
    candidates: list[tuple[str, Path | None]] = []
    for directory in sorted((media_dir / "present" / "bundles").glob("*")):
        manifest = read_bundle_manifest(directory)
        if (
            manifest
            and manifest.get("media_key") == media_dir.name
            and manifest.get("status") == "complete"
            and manifest.get("achieved") == "free"
        ):
            candidates.append((str(manifest.get("started_at") or ""), directory))
    legacy = legacy_result_metadata(media_dir)
    if legacy and legacy.get("status") == "complete" and legacy.get("achieved") == "free":
        candidates.append((str(legacy.get("started_at") or ""), None))
    for _, directory in sorted(candidates, key=lambda item: item[0], reverse=True):
        try:
            if directory is None:
                # A pre-bundle result's inputs are the flat fuse tree; whichever run last wrote
                # it, only proven generation-0 Shazam evidence is accepted below.
                fuse_dir = media_dir / "fuse"
            else:
                fuse_dir = _source_fuse_dir(
                    load_run_snapshot(media_dir, directory=directory), media_dir
                )
            observations, hints = _proven_recognition(media_dir, fuse_dir)
        except (NotRebuildable, OSError, ValueError, KeyError, TypeError):
            continue
        answered = _gen0_windows_of(observations)
        if not observations or answered is None or not answered <= frozen:
            continue
        yield observations, hints, directory or media_dir / "present"


def _proven_recognition(
    media_dir: Path, fuse_dir: Path
) -> tuple[tuple[ObservationRecord, ...], tuple[HintRecord, ...]]:
    """The recognition files a stored fusion names, each PROVEN against its recorded checksum.

    Narrower than :func:`id_detector.refusion.load_fusion_inputs` on purpose: reusing a sweep
    needs its observations only.  The hint file is re-gathered by every run (and pruned windows
    are re-cut), so neither may veto the reuse; the stored hints are returned when they still
    match their checksum and are empty otherwise.
    """

    from hashlib import sha256

    from id_detector.io import read_bytes
    from id_detector.refusion import _GENERATION, NotRebuildable, _recorded_digest, _sidecar

    _final_digest, final = _sidecar(fuse_dir / "episodes.done.json")
    references = [
        (int(match.group(1)), key) for key in final if (match := _GENERATION.fullmatch(key))
    ]
    if len(references) != 1:
        raise NotRebuildable("the stored fusion does not name exactly one generation")
    generation, generation_key = references[0]
    generation_digest, upstream = _sidecar(fuse_dir / f"episodes.gen{generation}.done.json")
    if generation_digest != _recorded_digest(final[generation_key], prunable=False):
        raise NotRebuildable("the stored fusion's records disagree with each other")

    def proven(key: str) -> bytes | None:
        expected = _recorded_digest(upstream.get(key), prunable=False)
        path = (media_dir / key).resolve()
        if expected is None or not path.is_relative_to(media_dir.resolve()):
            return None
        if not path_is_file(path):
            return None
        payload = read_bytes(path)
        return payload if sha256(payload).hexdigest() == expected else None

    keys = sorted(key for key in upstream if key.startswith("recognise/"))
    if not keys:
        raise NotRebuildable("the stored fusion names no recognition files")
    observations: list[ObservationRecord] = []
    for key in keys:
        payload = proven(key)
        if payload is None:
            raise NotRebuildable(f"recognition file {key} is missing or has changed")
        observations.extend(
            ObservationRecord.model_validate_json(line)
            for line in payload.decode("utf-8").splitlines()
            if line.strip()
        )
    hints_payload = proven("hints/hints.jsonl") if "hints/hints.jsonl" in upstream else None
    hints = (
        tuple(
            HintRecord.model_validate_json(line)
            for line in hints_payload.decode("utf-8").splitlines()
            if line.strip()
        )
        if hints_payload is not None
        else ()
    )
    return tuple(observations), hints


def load_jsonl(path: Path, model: Any) -> tuple[Any, ...]:
    if not path_is_file(path):
        return ()
    return tuple(
        model.model_validate(json.loads(line)) for line in read_text(path).splitlines() if line
    )
