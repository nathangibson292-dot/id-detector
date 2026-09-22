"""Three measured accuracy fixes, each pinned on the REAL evidence shape that showed the fault.

``tests/fixtures/deep/accuracy-fixes.json`` carries window positions copied from the cached
release-1 mixes (see ``docs/accuracy/release-1-miss-analysis.md``) under invented names, so no
rule can pass by knowing a title.  The votes go through the real Shazam converter, the comments
through the real parser and relations pass, and everything is fused and presented offline.

1. A ``likely`` badge counted up from smeared single windows is no proof against ``scatter``, and
   a confident episode buries only under the audio it proved.
2. A short two-window row is listed when its artist is solidly in the same mix.
3. A listener's answer at the very edge of a long episode is about the neighbouring track.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from id_detector.contracts import EpisodeRecord, EpisodesFile, ObservationRecord
from id_detector.fuse.episodes import (
    COVER_RUN_GAP_MS,
    EDGE_ANSWER_MIN_ON_AIR_MS,
    EDGE_ANSWER_MS,
    LIKELY_CORE_RUN_MS,
)
from id_detector.present.exports import (
    SAME_ARTIST_MIN_ON_AIR_MS,
    SOLID_ARTIST_ROW_MS,
    flatten_tracklist,
    mark_same_artist_rows,
)
from tests.test_hint_corroboration import _comment, _episode_of, _hints
from tests.test_phase1b_fusion import _fuse, _rows, _shazam_vote, _window

CASE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "deep" / "accuracy-fixes.json").read_text(
        encoding="utf-8"
    )
)
DURATION_MS = CASE["duration_ms"]
FLOOR_MS = 30_000


def _track(shape: dict, *, starts_s: list[int] | None = None) -> list[ObservationRecord]:
    """One recognised label on the fixture's window positions (its own provider key).

    Where the shape says which windows had a reliable offset anchor in the cached run, the others
    lose theirs — that is how a smeared phantom earns ``likely`` in the first place: one anchored
    window is trivially a "global alignment", and the rest only have to be counted.
    """

    votes = [
        _shazam_vote(
            _window(start * 1000, window_ms=CASE["window_ms"]),
            shape["artist"],
            shape["title"],
            key=f"key-{shape['artist']}-{shape['title']}",
        )
        for start in (shape["starts_s"] if starts_s is None else starts_s)
    ]
    if "anchored_s" not in shape:
        return votes
    anchored = {start * 1000 for start in shape["anchored_s"]}
    return [
        vote if vote.support_ms[0] in anchored else vote.model_copy(update={"anchor": None})
        for vote in votes
    ]


def _fused(*tracks: list[ObservationRecord], comments: list[dict] | None = None):
    return _fuse(
        [vote for track in tracks for vote in track],
        duration_ms=DURATION_MS,
        hints=_hints(comments) if comments else (),
    )


def _on_air_ms(episode: EpisodeRecord) -> int:
    return sum(end - start for start, end in episode.evidence_support_ms)


def _hull_ms(episode: EpisodeRecord) -> int:
    return episode.evidence_support_ms[-1][1] - episode.evidence_support_ms[0][0]


# --------------------------------------------------------------------------------------------------
# 1. Recurring phantom famous tracks
# --------------------------------------------------------------------------------------------------
def test_a_likely_badge_counted_up_from_smeared_windows_is_scatter_and_buries_nothing() -> None:
    shape = CASE["smeared_phantom"]
    phantom = _track(shape["phantom"])
    real = [_track(item) for item in shape["buried"]]
    episodes, _identity = _fused(phantom, *real)

    heard = _episode_of(episodes, phantom)
    # The shape is the fault: five windows, 57 s on air, a ten-minute hull — and a likely badge.
    assert heard.badge == "likely"
    assert _on_air_ms(heard) == 57_000 and _hull_ms(heard) == 642_000
    assert heard.suppressed == "scatter"
    for votes in real:
        episode = _episode_of(episodes, votes)
        assert episode.badge == "possible" and episode.suppressed is None

    listed = {row["title"] for row in _rows(episodes, _identity, FLOOR_MS) if not row["hidden"]}
    assert listed == {"Overdue", "Keep Hold"}


def test_a_listed_likely_episode_buries_only_under_the_audio_it_proved() -> None:
    shape = CASE["gapped_phantom"]
    phantom = _track(shape["phantom"])
    in_the_gap = [_track(item) for item in shape["in_the_gap"]]
    under_a_run = _track(shape["under_a_run"])
    episodes, _identity = _fused(phantom, *in_the_gap, under_a_run)

    heard = _episode_of(episodes, phantom)
    # Not scattered (132 s on air in a 282 s hull): nothing here can tell it from a real track,
    # so it stays listed — what changes is what it may hide.
    assert heard.badge == "likely" and heard.suppressed is None
    assert _on_air_ms(heard) == 132_000 and _hull_ms(heard) == 282_000
    # The two real tracks sit in the 132 s hole between its two runs (a hole wider than the gap
    # that still counts as one run): the hull covered them, the proved audio does not.
    hole_ms = 1_161_000 - 1_029_000
    assert hole_ms >= COVER_RUN_GAP_MS
    for votes in in_the_gap:
        assert _episode_of(episodes, votes).suppressed is None
    # Near-miss that must stay hidden: a fragment UNDER one of its runs is still buried.
    assert _episode_of(episodes, under_a_run).suppressed == "buried"


def test_a_track_wholly_inside_a_59_second_hole_of_a_confident_episode_stays_listed() -> None:
    """Review P1: burial reads the audio actually matched.  Two solid runs of one confident
    episode with 59 s between them, and a real 39 s track inside that hole: not under anything."""

    confident = {
        "artist": "Long Act",
        "title": "Two Halves",
        "starts_s": [*range(0, 91, 9), *range(161, 252, 9)],
    }
    inside = {
        "artist": "Real Visitor",
        "title": "Short Stay",
        "starts_s": [108, 117, 126, 135],
        "anchored_s": [],
    }
    episodes, _identity = _fused(_track(confident), _track(inside))
    cover = _episode_of(episodes, _track(confident))
    assert cover.badge == "likely" and cover.suppressed is None
    first_run_end, second_run_start = 90_000 + 12_000, 161_000
    assert second_run_start - first_run_end == 59_000 >= COVER_RUN_GAP_MS
    visitor = _episode_of(episodes, _track(inside))
    assert visitor.evidence_support_ms[0][0] >= first_run_end
    assert visitor.evidence_support_ms[-1][1] <= second_run_start
    assert visitor.badge == "possible" and visitor.suppressed is None
    # ... while truly adjacent windows (a 6 s hole, as a density-2 sweep leaves) are one stretch.
    sparse = {"artist": "Long Act", "title": "Sparse Sweep", "starts_s": list(range(600, 760, 18))}
    under = {"artist": "Stray Label", "title": "In The Seam", "starts_s": [611], "anchored_s": []}
    episodes, _identity = _fused(_track(sparse), _track(under))
    assert _episode_of(episodes, _track(sparse)).badge == "likely"
    assert _episode_of(episodes, _track(under)).suppressed == "buried"


@pytest.mark.parametrize(
    ("shape", "core_ms", "expected"),
    [("with_core", 57_000, None), ("without_core", 39_000, "scatter")],
)
def test_a_stretched_likely_row_is_kept_only_by_an_unbroken_core(
    shape: str, core_ms: int, expected: str | None
) -> None:
    """The correct ``likely`` row the fix would otherwise have cost, and its near-miss."""

    votes = _track(CASE["core_guard"][shape])
    episodes, _identity = _fused(votes)
    episode = _episode_of(episodes, votes)
    assert episode.badge == "likely"
    assert _hull_ms(episode) == 318_000 and _hull_ms(episode) >= 3 * _on_air_ms(episode)
    assert max(end - start for start, end in episode.evidence_support_ms) == core_ms
    assert (core_ms >= LIKELY_CORE_RUN_MS) == (expected is None)
    assert episode.suppressed == expected


def test_a_comment_on_a_matched_window_still_keeps_a_smeared_row() -> None:
    """The fix takes immunity from the BADGE only: a listener naming the track on one of its
    windows is evidence the smear test cannot overrule (unchanged behaviour)."""

    shape = CASE["smeared_phantom"]["phantom"]
    votes = _track(shape)
    named = _comment("named", f"{shape['artist']} - {shape['title']}", 3_760_000)
    episodes, _identity = _fused(votes, comments=[named])
    episode = _episode_of(episodes, votes)
    assert "hint_supported" in episode.flags and episode.suppressed is None


# --------------------------------------------------------------------------------------------------
# 3. A comment at the very edge of a long track is not a contradiction
# --------------------------------------------------------------------------------------------------
def _edge_case(at_ms: int, *, starts_s: list[int] | None = None) -> tuple[EpisodesFile, list, list]:
    shape = CASE["edge_comment"]
    long_track = _track(shape["long_track"], starts_s=starts_s)
    next_track = _track(shape["next_track"])
    answer = _comment("next", shape["comment_text"], at_ms)
    episodes, _identity = _fused(long_track, next_track, comments=[answer])
    return episodes, long_track, next_track


def test_an_answer_naming_the_next_track_at_the_very_edge_is_not_a_contradiction() -> None:
    shape = CASE["edge_comment"]
    episodes, long_track, next_track = _edge_case(shape["comment_at_ms"])
    episode = _episode_of(episodes, long_track)
    # 123 s on air; the answer's +/-5 s range starts 1.8 s before the episode's last window ends.
    assert _on_air_ms(episode) == 123_000 >= EDGE_ANSWER_MIN_ON_AIR_MS
    assert episode.evidence_support_ms[-1][1] - (shape["comment_at_ms"] - 5_000) < EDGE_ANSWER_MS
    assert episode.suppressed is None
    # The comment was right too: it backs the track it names, which is listed as well.
    named = _episode_of(episodes, next_track)
    assert "hint_supported" in named.flags and named.suppressed is None


@pytest.mark.parametrize("at_ms", CASE["edge_comment"]["inside_at_ms"])
def test_an_answer_naming_another_track_from_the_inside_still_contradicts(at_ms: int) -> None:
    """Near-miss that must stay hidden: mid-track, and 25 s from the end (past the 15 s edge)."""

    episodes, long_track, _next_track = _edge_case(at_ms)
    assert _episode_of(episodes, long_track).suppressed == "contradicted"


def test_a_short_episode_gets_no_edge_allowance() -> None:
    """Near-miss that must stay hidden: the same edge answer over a 39 s episode."""

    shape = CASE["edge_comment"]
    episodes, short_track, _next_track = _edge_case(
        shape["comment_at_ms"], starts_s=shape["short_track_starts_s"]
    )
    episode = _episode_of(episodes, short_track)
    assert _on_air_ms(episode) == 39_000 < EDGE_ANSWER_MIN_ON_AIR_MS
    assert episode.suppressed == "contradicted"


@pytest.mark.parametrize(
    ("text", "kind", "suppressed", "crowd_row"),
    [
        ("actually reckoning - soundboy teller", "correction", "contradicted", True),
        ("reckoning - soundboy teller", "answer", None, False),
    ],
)
def test_an_explicit_correction_at_the_edge_still_contradicts_and_is_listed_from_the_comment(
    text: str, kind: str, suppressed: str | None, crowd_row: bool
) -> None:
    """Review P1: "actually it is X" is a listener saying THIS is wrong, wherever it is typed.
    No engine heard the corrected work here, so it can only be listed from the comment — which
    the long episode's span would block if the episode were left standing."""

    shape = CASE["edge_comment"]
    long_track = _track(shape["long_track"])
    hints = _hints([_comment("fix", text, shape["comment_at_ms"])])
    assert [hint.kind for hint in hints] == [kind]
    episodes, identity = _fuse(long_track, duration_ms=DURATION_MS, hints=hints)
    assert _episode_of(episodes, long_track).suppressed == suppressed
    crowd = [episode for episode in episodes.episodes if "hint_only" in episode.flags]
    assert bool(crowd) is crowd_row
    if crowd_row:
        rows = [row for row in _rows(episodes, identity, FLOOR_MS) if not row["hidden"]]
        assert [row["hint_only"] for row in rows] == [True]
        assert {rows[0]["artist"].casefold(), rows[0]["title"].casefold()} == {
            "reckoning",
            "soundboy teller",
        }


# --------------------------------------------------------------------------------------------------
# 2. Short rows by an artist already solidly in the mix
# --------------------------------------------------------------------------------------------------
def _same_artist_rows(*names: str, comments: list[dict] | None = None) -> dict[str, dict]:
    shapes = CASE["same_artist"]
    episodes, identity = _fused(*(_track(shapes[name]) for name in names), comments=comments)
    rows = _rows(episodes, identity, FLOOR_MS)
    by_title = {row["title"]: row for row in rows}
    assert len(by_title) == len(rows)
    return {name: by_title[shapes[name]["title"]] for name in names}


def test_a_two_window_row_is_listed_when_its_artist_is_solidly_in_the_mix() -> None:
    rows = _same_artist_rows("solid", "two_windows", "two_windows_collab")
    solid = rows["solid"]
    assert solid["badge"] == "likely" and solid["on_air_ms"] >= SOLID_ARTIST_ROW_MS
    assert "same_artist_supported" not in solid  # an ordinary row carries no new key
    for name, on_air_ms in (("two_windows", 24_000), ("two_windows_collab", 21_000)):
        row = rows[name]
        assert SAME_ARTIST_MIN_ON_AIR_MS <= row["on_air_ms"] == on_air_ms < FLOOR_MS
        assert row["same_artist_supported"] is True
        # ``hidden`` is the corpus scorer's own second ``hidden_reason`` call on the row.
        assert row["hidden"] is None and row["hidden_reason"] is None


def test_the_same_rows_stay_hidden_without_a_solid_row_by_their_artist() -> None:
    rows = _same_artist_rows("two_windows", "two_windows_collab")
    assert {row["hidden"] for row in rows.values()} == {"short"}
    assert not any("same_artist_supported" in row for row in rows.values())


@pytest.mark.parametrize(
    "near_miss",
    [
        "one_window",  # a single 12 s window is never enough
        "other_artist",  # two windows of somebody else: the phantom's shape
        "same_track_version_label",  # the solid row's own track under its radio-edit label
        "same_track_shorter_label",  # ... and under a label whose title merely starts the same
    ],
)
def test_near_misses_of_the_same_artist_rule_stay_hidden(near_miss: str) -> None:
    rows = _same_artist_rows("solid", "two_windows", near_miss)
    assert rows["two_windows"]["hidden"] is None  # the rule is live in this very mix
    assert rows[near_miss]["hidden"] == "short"
    assert "same_artist_supported" not in rows[near_miss]


def test_a_possible_row_never_vouches_for_its_artist_however_long() -> None:
    """The measured trap: a 48 s ``possible`` row that was itself a sample of another track."""

    rows = _same_artist_rows("possible_backer", "possible_backed")
    backer = rows["possible_backer"]
    assert backer["badge"] == "possible" and backer["on_air_ms"] >= SOLID_ARTIST_ROW_MS
    assert backer["hidden"] is None
    assert rows["possible_backed"]["hidden"] == "short"


def test_a_likely_badge_bought_by_a_comment_on_a_brief_row_does_not_vouch() -> None:
    """Three windows plus a listener's answer is ``likely`` — but 36 s on air is not SOLID."""

    shapes = CASE["same_artist"]
    brief = {**shapes["solid"], "starts_s": [3276, 3291, 3306]}
    named = _comment("named", f"{brief['artist']} - {brief['title']}", 3_282_000)
    episodes, identity = _fused(_track(brief), _track(shapes["two_windows"]), comments=[named])
    rows = {row["title"]: row for row in _rows(episodes, identity, FLOOR_MS)}
    assert rows[brief["title"]]["badge"] == "likely"
    assert rows[brief["title"]]["on_air_ms"] == 36_000 < SOLID_ARTIST_ROW_MS
    assert rows[shapes["two_windows"]["title"]]["hidden"] == "short"


def test_the_same_artist_rule_never_lifts_a_row_fusion_suppressed() -> None:
    """A two-window row by the solid artist, but UNDER another confident track: still buried."""

    shapes = CASE["same_artist"]
    starts_s = list(range(4400, 4500, 9))
    cover = {"artist": "Somebody Else", "title": "Long Blend", "starts_s": starts_s}
    episodes, identity = _fused(
        _track(shapes["solid"]), _track(shapes["two_windows"]), _track(cover)
    )
    rows = {row["title"]: row for row in _rows(episodes, identity, FLOOR_MS)}
    assert rows["Long Blend"]["badge"] == "likely"
    assert rows[shapes["two_windows"]["title"]]["hidden"] == "buried"
    assert "same_artist_supported" not in rows[shapes["two_windows"]["title"]]


def test_a_row_of_a_work_that_is_already_listed_is_not_lifted_whatever_it_is_called() -> None:
    """The identity graph may know two labels as ONE work (a hint joined them); the label test
    cannot see that, the work id can."""

    def row(candidate: str, title: str, badge: str, on_air_ms: int) -> dict:
        return {
            "kind": "track",
            "candidate_id": candidate,
            "artist": "Chasing Status",
            "title": title,
            "badge": badge,
            "on_air_ms": on_air_ms,
        }

    for works, lifted in (
        ({"solid": "work-1", "short": "work-2"}, True),
        ({"solid": "work-1", "short": "work-1"}, False),
    ):
        short = row("short", "Working Title", "possible", 24_000)
        mark_same_artist_rows(
            [row("solid", "Released Name", "likely", 99_000), short], works, FLOOR_MS
        )
        assert short.get("same_artist_supported", False) is lifted


def test_with_the_floor_off_nothing_is_marked() -> None:
    shapes = CASE["same_artist"]
    episodes, identity = _fused(_track(shapes["solid"]), _track(shapes["two_windows"]))
    rows = flatten_tracklist(episodes, identity.record, collapse=False, min_track_ms=0)
    assert len(rows) == 2 and not any("same_artist_supported" in row for row in rows)


def test_the_page_view_lists_the_same_rows_as_the_per_episode_view() -> None:
    shapes = CASE["same_artist"]
    episodes, identity = _fused(
        _track(shapes["solid"]), _track(shapes["two_windows"]), _track(shapes["one_window"])
    )
    for collapse in (True, False):
        shown = flatten_tracklist(
            episodes, identity.record, collapse=collapse, min_track_ms=FLOOR_MS
        )
        assert {row["title"] for row in shown} == {
            shapes["solid"]["title"],
            shapes["two_windows"]["title"],
        }
