"""Phase 1b-ii gate: corroboration across trust families and honest crowd rows (plan §2.3.4
step 5) over the fixtures under ``tests/fixtures/deep/``.

*Vote* fixtures (``c3-scenario``, ``single-coincidence``, ``two-separated``) say which frozen
windows each engine matched; the test turns them into observations through the real adapter
converters (``shazam.response_to_observation`` with the shipped measured config,
``audd.clip_response_to_observation``) so the trial source, the anchors and the provider ids are
exactly what a run produces, then fuses and flattens them offline.  ``crowd-contradiction`` is
comment text read by the real hint parser and relations pass.  The two end-to-end runs use the
injected fakes; nothing here contacts a provider.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from hashlib import sha1
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from id_detector import cli, pipeline
from id_detector.contracts import (
    GENERATED_BY,
    EpisodeRecord,
    EpisodesFile,
    HintRecord,
    IdentitiesRecord,
    ObservationRecord,
    QueryRecord,
    SourceRecord,
    WindowQueryTarget,
    WindowRecord,
    compose_natural_key,
    make_id,
)
from id_detector.fuse.alignment import select_logical_trial_points
from id_detector.fuse.episodes import (
    COMMERCIAL_PROVIDERS,
    CORROBORATION_OVERLAP_MIN_MS,
    CORROBORATION_SEPARATION_MIN_MS,
    DISCOUNTED_TRIAL_E4,
    FULL_TRIAL_E4,
    TRUST_FAMILIES,
    _independent_trials_e4,
    build_episodes,
    crowd_answer_clusters,
    discounted_providers,
    engine_agreements,
    plausible_crowd_label,
    rank_crowd_works,
    separated_agreements,
    trust_family,
)
from id_detector.fuse.identity import (
    IdentityBuildResult,
    _word_sets_corroborate,
    build_identity_graph,
)
from id_detector.fuse.scanners import scanner_logical_trial_id
from id_detector.hints.parse import HintInput, parse_hint_inputs
from id_detector.hints.relations import apply_relations
from id_detector.io import atomic_write_json
from id_detector.present.bundles import shown_result_dir
from id_detector.present.exports import (
    _candidate_label,
    export_tracklist,
    flatten_tracklist,
    hidden_reason,
    short_track,
)
from id_detector.present.page import PAGE_VERSION, render_page
from id_detector.providers.audd import clip_response_to_observation
from id_detector.providers.base import AppConfig
from id_detector.recipes import get_recipe
from id_detector.recognise import load_provider_config
from id_detector.secondary_targeting import (
    SecondaryCandidate,
    allocate_secondary_windows,
    select_secondary_candidates,
)
from id_detector.shazam import response_to_observation
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff

ROOT = Path(__file__).resolve().parents[1]
DEEP = ROOT / "tests" / "fixtures" / "deep"
GOLDEN = ROOT / "tests" / "golden"
AUDIO_60 = ROOT / "tests" / "fixtures" / "audio" / "tone-60s.wav"
MEDIA_KEY = "5" * 64
#: The shipped measured Shazam config: its anchors are ``reliable``, as in a real run.
SHAZAM_CONFIG = load_provider_config(ROOT)[0]


def _fixture(name: str) -> dict:
    return json.loads((DEEP / f"{name}.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------------
# Observations through the real adapter converters
# --------------------------------------------------------------------------------------------------
def _window(start_ms: int, *, window_ms: int = 12_000) -> WindowRecord:
    base = WindowRecord.model_validate(
        json.loads((GOLDEN / "window.json").read_text(encoding="utf-8"))
    )
    return base.model_copy(
        update={
            "id": f"{start_ms:040x}",
            "logical_trial_id": f"{start_ms:040x}",
            "start_ms": start_ms,
            "support_ms": (start_ms, start_ms + window_ms),
            "output_ms": window_ms,
            "wav_sha256": f"{start_ms:064x}",
        }
    )


def _case_windows(case: dict) -> list[WindowRecord]:
    if "window_starts_ms" in case:
        return [_window(start, window_ms=case["window_ms"]) for start in case["window_starts_ms"]]
    hop_ms, window_ms = case["windows"]["hop_ms"], case["windows"]["window_ms"]
    return [
        _window(start, window_ms=window_ms)
        for start in range(0, case["duration_ms"] - window_ms + 1, hop_ms)
    ]


def _sha1(*parts: str) -> str:
    """A deterministic 40-hex id for a synthetic record (its natural key is not the point)."""

    return sha1("|".join(parts).encode("utf-8"), usedforsecurity=False).hexdigest()


def _query(window: WindowRecord, provider: str) -> QueryRecord:
    base = QueryRecord.model_validate_json((GOLDEN / "query.json").read_text(encoding="utf-8"))
    return base.model_copy(
        update={
            "id": _sha1("query", provider, window.id),
            "provider": provider,
            "target": WindowQueryTarget(window_id=window.id),
        }
    )


def _shazam_vote(
    window: WindowRecord, artist: str, title: str, *, key: str = "fixture-key"
) -> ObservationRecord:
    response = {
        "matches": [{"offset": window.support_ms[0] // 1000, "frequencyskew": 0, "timeskew": 0}],
        "track": {"key": key, "subtitle": artist, "title": title, "sections": []},
    }
    return response_to_observation(
        response, _query(window, "shazam"), window, SHAZAM_CONFIG, "fixture:shazam", MEDIA_KEY
    )


def _audd_vote(
    window: WindowRecord, artist: str, title: str, *, isrc: str | None = None
) -> ObservationRecord:
    result: dict[str, Any] = {
        "artist": artist,
        "title": title,
        "timecode": window.support_ms[0] // 1000,
        "song_link": "fixture:song",
    }
    if isrc is not None:
        result["isrc"] = isrc
    return clip_response_to_observation(
        {"status": "success", "result": result},
        query=_query(window, "audd"),
        window=window,
        media_key=MEDIA_KEY,
        raw_response_ref="fixture:audd",
    )


def _vote(provider: str, window: WindowRecord, label: dict, **extra: Any) -> ObservationRecord:
    artist, title = label["artist"], label["title"]
    if provider == "shazam":
        return _shazam_vote(window, artist, title, **extra)
    if provider == "audd":
        return _audd_vote(window, artist, title, **extra)
    observation_id = _sha1("observation", provider, window.id)
    audd = _audd_vote(window, artist, title)
    if provider == "acrcloud":  # the other commercial catalogue: its own clip trial source
        return audd.model_copy(
            update={
                "id": observation_id,
                "provider": "acrcloud",
                "native": {**audd.native, "simultaneous_source": "acrcloud"},
            }
        )
    if provider == "panako":  # the local index: a scanner, with its own trial keys
        return audd.model_copy(
            update={
                "id": observation_id,
                "provider": "panako",
                "capability": "local_index_query",
                "logical_trial_id": scanner_logical_trial_id("panako", window.support_ms[0]),
                "provider_ids": {"panako": "resource-1"},
                "native": {"strategy": "fixture"},
                "transform": None,
            }
        )
    raise AssertionError(provider)


def _case_votes(case: dict, spec: dict[str, list[int]] | None = None) -> list[ObservationRecord]:
    windows = _case_windows(case)
    return [
        _vote(provider, windows[index], case["label"])
        for provider, indices in (spec or case["votes"]).items()
        for index in indices
    ]


def _fuse(
    observations: list[ObservationRecord],
    *,
    duration_ms: int,
    windows: list[WindowRecord] | None = None,
    hints: list[HintRecord] | tuple[HintRecord, ...] = (),
    overlap_min_ms: int = CORROBORATION_OVERLAP_MIN_MS,
    separation_min_ms: int = CORROBORATION_SEPARATION_MIN_MS,
) -> tuple[EpisodesFile, IdentityBuildResult]:
    identity = build_identity_graph(MEDIA_KEY, observations, hints=hints)
    episodes, _ = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=duration_ms,
        observations=observations,
        windows=windows or [],
        identity=identity,
        hints=hints,
        overlap_min_ms=overlap_min_ms,
        separation_min_ms=separation_min_ms,
    )
    return episodes, identity


def _rows(episodes: EpisodesFile, identity: IdentityBuildResult, min_track_ms: int) -> list[dict]:
    """Every track row as the page and exports see it, with the floor's verdict attached."""

    entries = flatten_tracklist(
        episodes, identity.record, collapse=False, min_track_ms=min_track_ms, include_hidden=True
    )
    return [
        {**entry, "hidden": hidden_reason(entry, min_track_ms)}
        for entry in entries
        if entry["kind"] == "track"
    ]


def _check_vote_case(
    case: dict, spec: dict[str, list[int]], expect: dict
) -> tuple[EpisodesFile, IdentityBuildResult, list[ObservationRecord]]:
    windows = _case_windows(case)
    observations = _case_votes(case, spec)
    episodes, identity = _fuse(observations, duration_ms=case["duration_ms"], windows=windows)
    # E-C3: AudD is its own trial source, so every vote of both engines is selected and none is
    # pushed into ``hypothesis_rejected``.
    selection = select_logical_trial_points(observations, identity.observation_candidates)
    assert len(selection.selected_observation_ids) == expect["selected_votes"]
    assert selection.hypothesis_rejected == ()
    assert len(episodes.episodes) == expect["episodes"]
    (episode,) = episodes.episodes
    assert episode.badge == expect["badge"]
    assert "hypothesis_rejected" not in episode.flags
    for flag in expect["flags"]:
        assert flag in episode.flags, (flag, episode.flags)
    for flag in expect["not_flags"]:
        assert flag not in episode.flags, (flag, episode.flags)
    assert episode.suppressed == expect["suppressed"]
    votes = [item for item in observations if item.id in episode.evidence]
    assert len(engine_agreements(votes)) == expect["agreements"]
    (row,) = _rows(episodes, identity, case["min_track_ms"])
    assert row["hidden"] == expect["hidden"]
    assert row["engine_corroborated"] is ("engine_corroborated" in expect["flags"])
    assert row["engine_corroborated_separated"] is (
        "engine_corroborated_separated" in expect["flags"]
    )
    return episodes, identity, observations


# --------------------------------------------------------------------------------------------------
# Corroboration across trust families (E-C3, E-M1, E-S6)
# --------------------------------------------------------------------------------------------------
def test_trust_families_group_the_catalogue_engines_and_leave_the_rest_apart() -> None:
    assert trust_family("audd") == trust_family("acrcloud") == "catalogue"
    assert trust_family("shazam") == "shazam" and trust_family("panako") == "local_index"
    assert len({trust_family(name) for name in ("audd", "shazam", "panako")}) == 3
    assert trust_family("local_fixture") == "local_fixture"  # an unknown provider is its own
    # The Deep recipe's thresholds are the fuser's defaults (the Free recipe defines neither).
    deep = get_recipe("deep")
    assert (deep.overlap_min_ms, deep.separation_min_ms) == (
        CORROBORATION_OVERLAP_MIN_MS,
        CORROBORATION_SEPARATION_MIN_MS,
    )


def test_c3_scenario_keeps_likely_and_gains_corroboration() -> None:
    """Review C3: seven Shazam windows + seven agreeing AudD windows.  Before the trial source,
    AudD's anchor-less votes won every window and the badge fell to ``possible``; now both
    engines' votes are selected, the ``likely`` rule (untouched, E-S6) still holds, and every
    shared window is a cross-family agreement."""

    case = _fixture("c3-scenario")
    episodes, _identity, observations = _check_vote_case(case, case["votes"], case["expect"])
    (episode,) = episodes.episodes
    assert episode.has_global_alignment and episode.tiers.work == "likely"
    # Every AudD vote carries the clip trial source and a reliable timecode anchor.
    audd = [item for item in observations if item.provider == "audd"]
    assert all(item.native["simultaneous_source"] == "audd" for item in audd)
    assert all(item.anchor is not None and item.anchor.reliable for item in observations)
    # Fourteen selected votes but still four independent trials: both engines share the window's
    # logical trial, so agreement never inflates ``T_ind``.
    votes = [item for item in observations if item.id in episode.evidence]
    assert _independent_trials_e4(votes) == 4 * FULL_TRIAL_E4


def test_single_coincidence_is_confirmed_twice_but_bypasses_nothing() -> None:
    case = _fixture("single-coincidence")
    _check_vote_case(case, case["votes"], case["expect"])


def test_two_separated_agreements_bypass_suppression_and_the_floor() -> None:
    case = _fixture("two-separated")
    _check_vote_case(case, case["votes"], case["expect"])
    # The same evidence with one agreement: "confirmed twice" shown, but scatter still hides it.
    variant = case["variant"]
    _check_vote_case(case, variant["votes"], variant["expect"])


def test_separation_is_start_to_start_and_needs_two_agreements() -> None:
    assert not separated_agreements([])
    assert not separated_agreements([(0, 12_000)])
    assert not separated_agreements([(0, 12_000), (59_000, 71_000)])
    assert separated_agreements([(0, 12_000), (60_000, 72_000)])
    assert separated_agreements([(0, 12_000), (30_000, 42_000), (60_000, 72_000)])
    assert separated_agreements([(0, 12_000), (30_000, 42_000)], separation_min_ms=30_000)


def test_agreement_is_read_across_families_not_providers() -> None:
    """Review M1: AudD + ACRCloud agreeing is one catalogue queried twice; Shazam + Panako is
    two families."""

    case = _fixture("single-coincidence")
    windows = _case_windows(case)
    catalogue_twice = [_vote(name, windows[2], case["label"]) for name in ("audd", "acrcloud")]
    episodes, _ = _fuse(catalogue_twice, duration_ms=case["duration_ms"], windows=windows)
    assert all("engine_corroborated" not in episode.flags for episode in episodes.episodes)
    assert engine_agreements(catalogue_twice) == []

    local_index = [_vote(name, windows[2], case["label"]) for name in ("shazam", "panako")]
    episodes, _ = _fuse(local_index, duration_ms=case["duration_ms"], windows=windows)
    (episode,) = episodes.episodes
    assert "engine_corroborated" in episode.flags
    assert engine_agreements(local_index) == [(18_000, 30_000)]


def test_agreement_needs_the_overlap_floor_and_the_recipe_can_move_it() -> None:
    case = _fixture("single-coincidence")
    windows = _case_windows(case)
    label = case["label"]
    # Adjacent windows overlap by 3 s (9 s hop, 12 s windows): not the same moment.
    adjacent = [_vote("shazam", windows[2], label), _vote("audd", windows[3], label)]
    episodes, _ = _fuse(adjacent, duration_ms=case["duration_ms"], windows=windows)
    assert all("engine_corroborated" not in episode.flags for episode in episodes.episodes)
    # The very same evidence agrees under a 3 s floor, and a 12 s coincidence stops agreeing
    # under a 13 s floor — the threshold is the caller's (the recipe's), not a constant.
    episodes, _ = _fuse(
        adjacent, duration_ms=case["duration_ms"], windows=windows, overlap_min_ms=3_000
    )
    assert any("engine_corroborated" in episode.flags for episode in episodes.episodes)
    same_window = _case_votes(case)
    episodes, _ = _fuse(
        same_window, duration_ms=case["duration_ms"], windows=windows, overlap_min_ms=13_000
    )
    assert all("engine_corroborated" not in episode.flags for episode in episodes.episodes)


def test_agreements_are_pooled_per_work_across_recording_candidates() -> None:
    """An AudD match carrying an ISRC and a Shazam match carrying its key are two recording
    candidates of one work (nothing asserts ``same_recording`` between them); the agreement is
    the work's, so both episodes read as confirmed twice."""

    case = _fixture("c3-scenario")
    windows = _case_windows(case)
    label = case["label"]
    observations = [
        *(_shazam_vote(windows[index], label["artist"], label["title"]) for index in range(7)),
        *(
            _audd_vote(windows[index], label["artist"], label["title"], isrc="GBFIX2600001")
            for index in range(7)
        ),
    ]
    episodes, identity = _fuse(observations, duration_ms=case["duration_ms"], windows=windows)
    candidates = identity.record.candidates
    assert len(candidates) == 2 and len({item.work_id for item in candidates}) == 1
    assert len(episodes.episodes) == 2
    assert all("engine_corroborated" in episode.flags for episode in episodes.episodes)


def test_a_trial_is_attributed_by_family_not_by_the_alphabetically_first_provider() -> None:
    """Review L5: a window Shazam, AudD and ACRCloud all voted on is a full trial even when
    ACRCloud is the discounted second commercial engine."""

    case = _fixture("c3-scenario")
    windows = _case_windows(case)
    label = case["label"]

    def _on_trial(observation: ObservationRecord, trial: int) -> ObservationRecord:
        return observation.model_copy(
            update={
                "id": f"{trial:030x}{observation.id[30:]}",
                "logical_trial_id": f"{trial:040x}",
                "support_ms": (trial * 20_000, trial * 20_000 + 12_000),
                "mix_span_ms": (trial * 20_000, trial * 20_000 + 12_000),
            }
        )

    votes = [
        _on_trial(_vote("shazam", windows[0], label), 1),
        _on_trial(_vote("audd", windows[0], label), 1),
        _on_trial(_vote("acrcloud", windows[0], label), 1),
        _on_trial(_vote("acrcloud", windows[0], label), 2),
        _on_trial(_vote("audd", windows[0], label), 3),
        _on_trial(_vote("audd", windows[0], label), 4),
    ]
    assert discounted_providers(votes) == frozenset({"acrcloud"})
    # Trials 1 (mixed), 3 and 4 are full; only trial 2 (ACRCloud alone) carries the half prior.
    assert _independent_trials_e4(votes) == 3 * FULL_TRIAL_E4 + DISCOUNTED_TRIAL_E4
    # Alphabetical attribution would have discounted trial 1 as well.
    assert _independent_trials_e4(votes) != 2 * FULL_TRIAL_E4 + 2 * DISCOUNTED_TRIAL_E4


# --------------------------------------------------------------------------------------------------
# Crowd rows (E-M4, U-F13)
# --------------------------------------------------------------------------------------------------
def _comment_inputs(case: dict) -> list[HintInput]:
    return [
        HintInput(
            connector="sc_comments",
            source_record_id=comment["comment"],
            text=comment["text"],
            position_ms=comment["at_ms"],
            position_kind="comment_timestamp",
            author_pseudo_id=comment["author"],
            is_uploader=comment.get("uploader", False),
        )
        for comment in case["comments"]
    ]


def _crowd_hints(case: dict) -> list[HintRecord]:
    inputs = _comment_inputs(case)
    hints = parse_hint_inputs(MEDIA_KEY, case["duration_ms"], inputs)
    return apply_relations(MEDIA_KEY, case["duration_ms"], hints, inputs)


def _hint_id(comment: str) -> str:
    values = {"connector": "sc_comments", "source_record_id": f"{comment}:0"}
    return make_id(MEDIA_KEY, "hint", compose_natural_key("hint", values))


def _source() -> SourceRecord:
    return SourceRecord.model_validate_json((GOLDEN / "source.json").read_text(encoding="utf-8"))


def test_crowd_contradiction_lists_the_best_supported_answer_with_the_other_as_alternative(
    tmp_path: Path,
) -> None:
    case = _fixture("crowd-contradiction")
    hints = _crowd_hints(case)
    by_id = {hint.id: hint for hint in hints}
    # The parser let the paragraph and the link through as answers; fusion must not list them.
    for source_id in case["expect"]["dropped"]:
        assert by_id[_hint_id(source_id)].kind == "answer"
    episodes, identity = _fuse([], duration_ms=case["duration_ms"], hints=hints)
    crowd = sorted(
        (episode for episode in episodes.episodes if "hint_only" in episode.flags),
        key=lambda episode: episode.best_start_ms,
    )
    rows = _rows(episodes, identity, case["min_track_ms"])
    assert len(crowd) == len(rows) == len(case["expect"]["rows"])
    for episode, row, expect in zip(crowd, rows, case["expect"]["rows"], strict=True):
        assert (episode.best_start_ms, episode.best_end_ms) == (
            expect["start_ms"],
            expect["end_ms"],
        )
        assert episode.evidence_support_ms == [(expect["start_ms"], expect["end_ms"])]
        assert episode.evidence == sorted(_hint_id(item) for item in expect["evidence"])
        assert f"{row['artist']} - {row['title']}" == expect["label"]
        assert [alt["track"].replace(" — ", " - ") for alt in row["alternatives"]] == expect[
            "alternatives"
        ]
        assert row["also_count"] == len(expect["alternatives"])
        assert row["hidden"] is None and row["hint_only"] is True
    dropped = {_hint_id(item) for item in case["expect"]["dropped"]}
    assert not any(dropped & set(episode.evidence) for episode in episodes.episodes)
    # Position order, not hint-id order, decides which row a work is listed from: the loser is
    # not listed again by its own answer, and the listed span now blocks the timestamp.
    assert all(
        not any(hint_id in episode.evidence for hint_id in [_hint_id("y1")])
        for episode in episodes.episodes
    )

    # Presentation: the collapsed row keeps the contradicting answer as its alternative, the
    # page calls it another answer in the comments, and the Markdown names it as such.
    entries = flatten_tracklist(
        episodes, identity.record, collapse=True, min_track_ms=case["min_track_ms"]
    )
    listed = next(entry for entry in entries if entry["kind"] == "track" and entry["also_count"])
    assert listed["alternatives"][0]["track"] == "Fixture Artist Y — Title Y"
    page = render_page(
        source=_source(),
        episodes=episodes,
        identities=identity.record,
        duration_ms=case["duration_ms"],
        min_track_ms=case["min_track_ms"],
    )
    assert "▸ 1 other answer in the comments" in page
    assert "other version" not in page
    fuse_dir = tmp_path / "fuse"
    atomic_write_json(fuse_dir / "episodes.json", episodes)
    atomic_write_json(fuse_dir / "identities.gen0.json", identity.record)
    result = export_tracklist(
        media_dir=tmp_path,
        media_key=MEDIA_KEY,
        duration_ms=case["duration_ms"],
        episodes=episodes,
        identities=identity.record,
        episodes_path=fuse_dir / "episodes.json",
        identities_path=fuse_dir / "identities.gen0.json",
        min_track_ms=case["min_track_ms"],
    )
    markdown = result.markdown_path.read_text(encoding="utf-8")
    assert "also named in the comments — Fixture Artist Y — Title Y" in markdown
    assert "other version" not in markdown


def test_plausible_crowd_labels_are_track_shaped() -> None:
    assert plausible_crowd_label("Sammy Virji & Flowdan", "Shella Verse")
    # Reversed and long real titles pass: the rule is about the label's shape, not its order.
    assert plausible_crowd_label("Be your best when your best is needed", "clarkie")
    assert plausible_crowd_label("Is This Love", "Bob Marley")
    assert plausible_crowd_label("What Is Love", "Haddaway")
    assert plausible_crowd_label("Chase & Status", "Headtop (ft. IRAH) (Original Dub Mix)")
    # Comment text is not: a question, a link, a handle, a paragraph.
    assert not plausible_crowd_label("salute into Nova by MPH?", "Can we talk about that")
    assert not plausible_crowd_label("Someone", "https://example.invalid/track")
    assert not plausible_crowd_label("Someone", "www.example.invalid")
    assert not plausible_crowd_label("@someone Artist", "Title")
    assert not plausible_crowd_label("one two three four five six seven", "eight nine ten 11 12 13")
    assert plausible_crowd_label("one two three four five six", "seven eight nine ten 11 12")
    assert not plausible_crowd_label("a" * 100, "b" * 21)
    assert not plausible_crowd_label("Artist", "Title\nmore")
    assert not plausible_crowd_label(None, "Title") and not plausible_crowd_label("Artist", " ")


def test_crowd_ranking_prefers_independent_answers_then_a_trusted_voice() -> None:
    def _cluster(comments: list[dict]) -> list:
        case = {"duration_ms": 600_000, "comments": comments}
        hints = _crowd_hints(case)
        identity = build_identity_graph(MEDIA_KEY, [], hints=hints)
        clusters = crowd_answer_clusters(hints, identity, [])
        assert len(clusters) == 1
        return rank_crowd_works(clusters[0])

    # A tie on independent answers: the uploader's answer is listed, the fan's is the alternative.
    ranked = _cluster(
        [
            {"comment": "f", "text": "Fan Artist - Fan Title", "at_ms": 100_000, "author": "fan"},
            {
                "comment": "u",
                "text": "Uploader Artist - Uploader Title",
                "at_ms": 103_000,
                "author": "dj",
                "uploader": True,
            },
        ]
    )
    assert [hint.id for _, _, hints in ranked for hint in hints] == [_hint_id("u"), _hint_id("f")]
    # Two independent fans outvote one uploader.
    ranked = _cluster(
        [
            {"comment": "f1", "text": "Fan Artist - Fan Title", "at_ms": 100_000, "author": "a"},
            {
                "comment": "u",
                "text": "Uploader Artist - Uploader Title",
                "at_ms": 102_000,
                "author": "dj",
                "uploader": True,
            },
            {"comment": "f2", "text": "Fan Title - Fan Artist", "at_ms": 104_000, "author": "b"},
        ]
    )
    winner, runner_up = ranked
    assert {hint.id for hint in winner[2]} == {_hint_id("f1"), _hint_id("f2")}
    assert [hint.id for hint in runner_up[2]] == [_hint_id("u")]
    # Two answers 10 s apart do not intersect: two timestamps, two clusters.
    case = {
        "duration_ms": 600_000,
        "comments": [
            {"comment": "a", "text": "A Artist - A Title", "at_ms": 100_000, "author": "a"},
            {"comment": "b", "text": "B Artist - B Title", "at_ms": 110_000, "author": "b"},
        ],
    }
    hints = _crowd_hints(case)
    identity = build_identity_graph(MEDIA_KEY, [], hints=hints)
    assert len(crowd_answer_clusters(hints, identity, [])) == 2
    # ...and a listed track's span swallows the answers under it.
    assert crowd_answer_clusters(hints, identity, [(90_000, 120_000)]) == []


# --------------------------------------------------------------------------------------------------
# Matcher edges decided on the release-1 corpus A/B (see the build report)
# --------------------------------------------------------------------------------------------------
def test_featuring_marker_carries_no_identity_in_fusion() -> None:
    """A crowd or tracklist line "X ft. Y - Z" names the audio match "X - Z (feat. Y)"; the
    featured name still counts, so a different featured artist stays a different work."""

    case = _fixture("c3-scenario")
    windows = _case_windows(case)
    audio = [_shazam_vote(windows[0], "MPH", "Rush (feat. Cecelia)")]

    def _work_of(text: str) -> tuple[str, str]:
        inputs = [
            HintInput(
                connector="sc_comments",
                source_record_id="a",
                text=text,
                position_ms=3_000,
                position_kind="comment_timestamp",
                author_pseudo_id="fan",
            )
        ]
        hints = parse_hint_inputs(MEDIA_KEY, 60_000, inputs)
        identity = build_identity_graph(MEDIA_KEY, audio, hints=hints)
        (audio_work,) = {identity.observation_candidates[audio[0].id]}
        audio_work_id = next(
            item.work_id for item in identity.record.candidates if item.canonical_id == audio_work
        )
        return identity.hint_work_ids[hints[0].id], audio_work_id

    assert _word_sets_corroborate(
        frozenset({"mph", "ft", "cecelia", "rush"}) - {"ft", "feat"},
        frozenset({"mph", "rush", "feat", "cecelia"}) - {"ft", "feat"},
    )
    hint_work, audio_work = _work_of("MPH ft. Cecelia - Rush")
    assert hint_work == audio_work
    hint_work, audio_work = _work_of("Rush (feat. Somebody Else) - MPH")
    assert hint_work != audio_work


def test_a_short_word_one_trailing_letter_apart_is_the_same_word() -> None:
    def fs(*words: str) -> frozenset[str]:
        return frozenset(words)

    # The two corpus instances: "So u know - Overmono" vs "Overmono - So U Kno", "OD" vs "ODF".
    assert _word_sets_corroborate(
        fs("so", "u", "know", "overmono"), fs("overmono", "so", "u", "kno")
    )
    assert _word_sets_corroborate(
        fs("tim", "reaper", "special", "request", "od", "pull", "up"),
        fs("special", "request", "odf", "tim", "reaper", "pull", "up"),
    )
    # A substitution at that length, a digit, a single letter, or two letters apart: not the same.
    assert not _word_sets_corroborate(fs("effy", "up"), fs("effy", "us"))
    assert not _word_sets_corroborate(fs("artist", "untitled", "1"), fs("artist", "untitled", "12"))
    assert not _word_sets_corroborate(fs("artist", "a"), fs("artist", "ab"))
    assert not _word_sets_corroborate(fs("artist", "od"), fs("artist", "odfx"))
    # The rule still needs a shared word and exactly one differing token on each side.
    assert not _word_sets_corroborate(fs("od", "x"), fs("odf", "y"))


# --------------------------------------------------------------------------------------------------
# E-M3: phantom-only AudD coverage never blocks the free pass
# --------------------------------------------------------------------------------------------------
def _episode(span: tuple[int, int], *, badge: str = "unclear") -> EpisodeRecord:
    base = EpisodeRecord.model_validate(
        json.loads((GOLDEN / "episode.json").read_text(encoding="utf-8"))
    )
    return base.model_copy(
        update={
            "id": f"{span[0]:040x}",
            "candidate_id": f"{span[0]:040x}",
            "best_start_ms": span[0],
            "best_end_ms": span[1],
            "evidence_support_ms": [span],
            "badge": badge,
            "flags": [],
            "suppressed": None,
        }
    )


def test_a_lone_audd_phantom_is_probed_as_a_listed_candidate_not_treated_as_coverage() -> None:
    """Review M3 under ``targeting:1``: a 12 s AudD match inside a blank stretch is a
    ``listed_not_confident`` candidate — probed *ahead* of the blanks around it — never a hole
    the free pass is kept out of."""

    base = EpisodesFile.model_validate(
        json.loads((GOLDEN / "episodes.json").read_text(encoding="utf-8"))
    )
    episodes = base.model_copy(
        update={
            "episodes": [_episode((0, 60_000), badge="likely"), _episode((120_000, 132_000))],
            "gaps": [],
        }
    )
    candidates = select_secondary_candidates(
        episodes, duration_ms=300_000, suppressed_min_votes=2, min_intersection_ms=4_000
    )
    assert [(item.priority, item.span) for item in candidates] == [
        ("listed_not_confident", (120_000, 132_000)),
        ("blank", (60_000, 120_000)),
        ("blank", (132_000, 300_000)),
    ]
    windows = [_window(start) for start in range(0, 300_000 - 12_000 + 1, 9_000)]
    picks = allocate_secondary_windows(
        windows, list(candidates), allocation=3, min_intersection_ms=4_000
    )
    assert [pick.candidate.priority for pick in picks] == [
        "listed_not_confident",
        "blank",
        "blank",
    ]
    phantom = next(pick for pick in picks if pick.candidate.priority == "listed_not_confident")
    assert (
        min(phantom.window.support_ms[1], 132_000) - max(phantom.window.support_ms[0], 120_000)
        >= 4_000
    )
    assert isinstance(phantom.candidate, SecondaryCandidate)


# --------------------------------------------------------------------------------------------------
# E-M6: ``--profile max_accuracy`` alone is the free recipe
# --------------------------------------------------------------------------------------------------
def test_profile_max_accuracy_alone_selects_the_free_recipe(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_analyse(url: str, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(pipeline, "run_analysis", fake_analyse)
    runner = CliRunner()
    for arguments, expected in (
        (["--profile", "max_accuracy"], "free"),
        (["--profile", "max_accuracy", "--recipe", "deep"], "deep"),
        (["--profile", "max_accuracy", "--engine", "audd"], "deep"),
        (["--profile", "free"], "free"),
    ):
        result = runner.invoke(cli.app, ["analyse", "http://example/set", *arguments])
        assert result.exit_code == 0, result.output
        assert captured["recipe"].name == expected, arguments  # type: ignore[attr-defined]
    # The option's help says so; the stale "defaults to deep for max_accuracy" line is gone.
    command = typer.main.get_command(cli.app).commands["analyse"]  # type: ignore[attr-defined]
    recipe_help = next(param.help for param in command.params if param.name == "recipe")
    assert "never selects paid work" in recipe_help
    assert "Defaults to deep for the legacy" not in recipe_help


# --------------------------------------------------------------------------------------------------
# Presentation: "confirmed twice" and the floor
# --------------------------------------------------------------------------------------------------
def test_the_floor_is_bypassed_by_separated_agreements_only() -> None:
    row = {"kind": "track", "badge": "unclear", "on_air_ms": 12_000, "hint_supported": False}
    assert short_track({**row, "engine_corroborated": True}, 30_000)
    assert not short_track({**row, "engine_corroborated_separated": True}, 30_000)
    assert not short_track({**row, "engine_corroborated": True}, 0)


def test_confirmed_twice_is_what_the_page_and_the_markdown_say(tmp_path: Path) -> None:
    case = _fixture("single-coincidence")
    windows = _case_windows(case)
    episodes, identity = _fuse(_case_votes(case), duration_ms=case["duration_ms"], windows=windows)
    page = render_page(
        source=_source(),
        episodes=episodes,
        identities=identity.record,
        duration_ms=case["duration_ms"],
        min_track_ms=0,
    )
    assert ">confirmed twice</span>" in page and "cross-checked" not in page
    assert PAGE_VERSION >= 18  # the wording change reaches stale pages on open
    fuse_dir = tmp_path / "fuse"
    atomic_write_json(fuse_dir / "episodes.json", episodes)
    atomic_write_json(fuse_dir / "identities.gen0.json", identity.record)
    result = export_tracklist(
        media_dir=tmp_path,
        media_key=MEDIA_KEY,
        duration_ms=case["duration_ms"],
        episodes=episodes,
        identities=identity.record,
        episodes_path=fuse_dir / "episodes.json",
        identities_path=fuse_dir / "identities.gen0.json",
        min_track_ms=0,
    )
    markdown = result.markdown_path.read_text(encoding="utf-8")
    assert "+CONFIRMED TWICE" in markdown and "2ENGINES" not in markdown
    (entry,) = result.entries
    assert entry["engine_corroborated"] is True
    assert entry["engine_corroborated_separated"] is False


# --------------------------------------------------------------------------------------------------
# End to end: the recipe's thresholds reach the fuse through ``_analyse``
# --------------------------------------------------------------------------------------------------
def _run(tmp_path: Path, name: str, *, recipe) -> tuple[int, Path]:
    script = json.loads((DEEP / f"{name}.json").read_text(encoding="utf-8"))
    work_root = tmp_path / "work"
    code = asyncio.run(
        cli._analyse(
            str(AUDIO_60),
            work_root=work_root,
            print_raw=False,
            refresh=False,
            max_requests=100,
            tracklist=None,
            no_hints=True,
            app_config=AppConfig(
                transforms_policy="off",
                recognise_concurrency=1,
                shazam_requests_per_minute=1_000_000,
                audd_requests_per_minute=1_000_000,
                present_min_track_ms=0,  # the fixture's tones are shorter than the floor
            ),
            max_generations=0,
            novelty=False,
            recipe=recipe,
            paid_scan_adapters={"audd": FakeAudD(script)},
            shazam_http_client=FakeShazamHTTP(script),
            paid_sleep=no_backoff,
        )
    )
    (journal,) = work_root.rglob("invocations.jsonl")
    return code, journal.parent


@pytest.mark.parametrize("overlap_min_ms", [None, 13_000])
def test_deep_run_marks_the_shazam_probed_tracks_confirmed_twice(
    tmp_path: Path, overlap_min_ms: int | None
) -> None:
    """``overlap-allowed``: the two Shazam probes land on AudD windows and agree with them, so
    those tracks are confirmed twice in ``fuse/episodes.json`` and ``tracklist.json`` — unless
    the recipe's overlap floor is raised past a window, which proves the threshold is the
    recipe's and reaches the fuse."""

    recipe = get_recipe("deep")
    if overlap_min_ms is not None:
        recipe = replace(recipe, overlap_min_ms=overlap_min_ms)
    code, media_dir = _run(tmp_path, "overlap-allowed", recipe=recipe)
    assert code == 0
    episodes = json.loads((media_dir / "fuse" / "episodes.json").read_text(encoding="utf-8"))
    tracklist = json.loads(
        (shown_result_dir(media_dir) / "tracklist.json").read_text(encoding="utf-8")
    )
    confirmed = [item for item in episodes["episodes"] if "engine_corroborated" in item["flags"]]
    rows = [entry for entry in tracklist["entries"] if entry["kind"] == "track"]
    assert rows
    if overlap_min_ms is None:
        assert confirmed
        assert any(entry["engine_corroborated"] for entry in rows)
        # A 60 s tone never yields two agreements 60 s apart.
        assert not any(entry["engine_corroborated_separated"] for entry in rows)
        markdown = (shown_result_dir(media_dir) / "tracklist.md").read_text(encoding="utf-8")
        assert "+CONFIRMED TWICE" in markdown
    else:
        assert not confirmed
        assert not any(entry["engine_corroborated"] for entry in rows)


def test_a_recipe_threshold_of_zero_is_not_mistaken_for_an_unset_one(tmp_path: Path) -> None:
    """``_fuse`` falls back to the fuser's defaults only when the recipe leaves a threshold
    ``None``.  A recipe that deliberately says ``0`` means "no floor": with no overlap floor the
    two Shazam probes agree with their neighbouring AudD windows as well (two agreements each) and
    with no separation floor those two are "separated" — neither of which happens under the Deep
    defaults this run would fall back to."""

    recipe = replace(get_recipe("deep"), overlap_min_ms=0, separation_min_ms=0)
    code, media_dir = _run(tmp_path, "overlap-allowed", recipe=recipe)
    assert code == 0
    tracklist = json.loads(
        (shown_result_dir(media_dir) / "tracklist.json").read_text(encoding="utf-8")
    )
    rows = [entry for entry in tracklist["entries"] if entry["kind"] == "track"]
    assert any(entry["engine_corroborated_separated"] for entry in rows)


# --------------------------------------------------------------------------------------------------
# Review + fix pass (1b-ii): the M1 invariant and comment lead-ins in a displayed label
# --------------------------------------------------------------------------------------------------
def test_a_commercial_catalogue_engine_can_never_become_its_own_trust_family() -> None:
    """Review M1 is enforced by ONE list: the catalogue family is derived from
    ``COMMERCIAL_PROVIDERS``, so an engine added there cannot corroborate AudD by being absent
    from a second, hand-maintained table."""

    assert TRUST_FAMILIES.keys() >= COMMERCIAL_PROVIDERS
    assert {trust_family(provider) for provider in COMMERCIAL_PROVIDERS} == {"catalogue"}
    assert set(TRUST_FAMILIES.values()) == {"catalogue", "shazam", "local_index"}


def test_a_comment_lead_in_is_not_a_track_label_and_never_names_a_crowd_row() -> None:
    """U-F13 also decides what a text-only candidate is CALLED.  A work's nodes include the
    tracklist line a listener pasted ("FULL TRACK LIST: - …") and lexicographic ``min`` preferred
    it over the clean answer — the release-1 Mall Grab crowd row."""

    assert not plausible_crowd_label("FULL TRACK LIST:", "Fishmans - Long Season")
    assert not plausible_crowd_label("Artist", "Title:")
    assert plausible_crowd_label("Artist: The Sequel", "Title")  # a colon inside a name is fine
    clean, lead_in = "text:fishmans|long season", "text:full track list:|fishmans - long season"
    stamp = {"generated_by": GENERATED_BY, "schema_version": "1.0.0"}
    identities = IdentitiesRecord.model_validate(
        {
            **stamp,
            "assertions": [],
            "candidates": [
                {
                    **stamp,
                    "alternatives": [],
                    "canonical_id": "a" * 40,
                    "conflicts": [],
                    "contested": False,
                    "member_nodes": [clean],
                    "work_id": "b" * 40,
                }
            ],
            "nodes": [
                {**stamp, "id": clean, "label": "Fishmans - Long Season", "ns": "text"},
                {
                    **stamp,
                    "id": lead_in,
                    "label": "FULL TRACK LIST: - Fishmans - Long Season",
                    "ns": "text",
                },
            ],
            "works": [{**stamp, "work_id": "b" * 40, "member_nodes": [clean, lead_in]}],
        }
    )
    assert _candidate_label(identities, "a" * 40) == ("Fishmans", "Long Season")
    # With no plausible label at all the row still gets its name (never "Unknown").
    only_lead_in = identities.model_copy(
        update={
            "candidates": [identities.candidates[0].model_copy(update={"member_nodes": [lead_in]})],
            "works": [identities.works[0].model_copy(update={"member_nodes": [lead_in]})],
        }
    )
    assert _candidate_label(only_lead_in, "a" * 40) == (
        "FULL TRACK LIST:",
        "Fishmans - Long Season",
    )
