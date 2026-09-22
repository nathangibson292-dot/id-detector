"""Hint corroboration: a casual ID answer backs the track the recogniser heard — and only that.

``tests/fixtures/deep/hint-corroboration.json`` is a two-hour mix with ten recognised tracks and
the comment shapes listeners really type.  Everything is authored; the audio votes go through the
real Shazam converter and the comments through the real parser and relations pass, then the whole
thing is fused offline.  Nothing here contacts a provider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from id_detector.contracts import EpisodeRecord, EpisodesFile, HintRecord, ObservationRecord
from id_detector.fuse.episodes import (
    HINT_LEAD_IN_MS,
    HINT_TRAIL_MS,
    hint_backed_plays,
    hint_reaches_supports,
)
from id_detector.fuse.identity import (
    IdentityBuildResult,
    build_identity_graph,
    hint_label_corroborates,
    names_a_title,
)
from id_detector.hints.parse import HintInput, parse_hint_inputs
from id_detector.hints.relations import apply_relations
from id_detector.present.exports import flatten_tracklist
from tests.test_phase1b_fusion import MEDIA_KEY, _fuse, _hint_id, _shazam_vote, _window

CASE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "deep" / "hint-corroboration.json").read_text(
        encoding="utf-8"
    )
)


def _observations() -> dict[str, list[ObservationRecord]]:
    votes: dict[str, list[ObservationRecord]] = {}
    for name, track in CASE["tracks"].items():
        votes[name] = [
            _shazam_vote(
                _window(start, window_ms=CASE["window_ms"]),
                track["artist"],
                track["title"],
                key=f"key-{name}",
            )
            for start in track["supports_ms"]
        ]
        if "fragment_ms" in track:
            # Another release of the same work, heard once just before the main body.
            votes[f"{name}-fragment"] = [
                _shazam_vote(
                    _window(start, window_ms=CASE["window_ms"]),
                    track["artist"],
                    track["title"],
                    key=f"key-{name}-other-release",
                )
                for start in track["fragment_ms"]
            ]
    return votes


def _hints(comments: list[dict] | None = None) -> list[HintRecord]:
    inputs = [
        HintInput(
            connector="sc_comments",
            source_record_id=comment["comment"],
            text=comment["text"],
            position_ms=comment.get("at_ms"),
            position_kind=("comment_timestamp" if comment.get("at_ms") is not None else "none"),
            author_pseudo_id=comment["author"],
            parent_source_id=comment.get("reply_to"),
        )
        for comment in (CASE["comments"] if comments is None else comments)
    ]
    hints = parse_hint_inputs(MEDIA_KEY, CASE["duration_ms"], inputs)
    return apply_relations(MEDIA_KEY, CASE["duration_ms"], hints, inputs)


def _fused() -> tuple[EpisodesFile, IdentityBuildResult, dict[str, list[ObservationRecord]]]:
    votes = _observations()
    episodes, identity = _fuse(
        [item for group in votes.values() for item in group],
        duration_ms=CASE["duration_ms"],
        hints=_hints(),
    )
    return episodes, identity, votes


def _backed(episodes: EpisodesFile, votes: dict[str, list[ObservationRecord]]) -> dict[str, str]:
    """comment -> the track whose episode carries that comment as evidence."""

    track_of_observation = {item.id: name for name, group in votes.items() for item in group}
    comment_of_hint = {
        _hint_id(comment["comment"]): comment["comment"] for comment in CASE["comments"]
    }
    backed: dict[str, str] = {}
    for episode in episodes.episodes:
        tracks = {track_of_observation[e] for e in episode.evidence if e in track_of_observation}
        for evidence in episode.evidence:
            if evidence in comment_of_hint and tracks:
                (track,) = tracks
                assert comment_of_hint[evidence] not in backed, "one comment backed two plays"
                backed[comment_of_hint[evidence]] = track
    return backed


def test_every_answer_in_the_fixture_backs_its_track_and_no_near_miss_backs_anything() -> None:
    episodes, _identity, votes = _fused()
    backed = _backed(episodes, votes)
    assert backed == CASE["expect"]["attaches"]
    assert not set(backed) & set(CASE["expect"]["never"])

    flagged = {
        name
        for name, group in votes.items()
        for episode in episodes.episodes
        if "hint_supported" in episode.flags and {item.id for item in group} & set(episode.evidence)
    }
    assert flagged == set(CASE["expect"]["hint_supported"])
    assert not flagged & set(CASE["expect"]["not_hint_supported"])


def test_a_corroborating_answer_never_becomes_a_track_of_its_own() -> None:
    """The answers that back an audio track add no row; the only crowd rows are the ones the
    existing hint-only policy already lists (an answer naming a work no engine matched)."""

    episodes, identity, votes = _fused()
    backed = _backed(episodes, votes)
    crowd_evidence = {
        evidence
        for episode in episodes.episodes
        if "hint_only" in episode.flags
        for evidence in episode.evidence
    }
    assert not crowd_evidence & {_hint_id(comment) for comment in backed}
    # The bare-hyphen answer has no node of its own to become a candidate from.
    labels = {node.label for node in identity.record.nodes}
    assert not any("glass gardens-mirrorball" in label.casefold() for label in labels)


def test_hints_vote_for_the_work_and_never_for_the_version() -> None:
    episodes, _identity, votes = _fused()
    senses = next(
        episode
        for episode in episodes.episodes
        if {item.id for item in votes["senses"]} & set(episode.evidence)
    )
    assert _hint_id("other-remix") in senses.evidence
    assert senses.tiers.version == "unclear" and senses.version_status == "unverified"


@pytest.mark.parametrize(
    ("hint", "audio"),
    [
        (("senses", "bakey"), ("Bakey", "Senses")),
        (("GOOD 4 U", "MPH"), ("Gentlemens Club & MPH", "GOOD 4 U")),
        (("h.l.t.", "cold start (unreleased)"), ("HLT", "Cold Start")),
        (("Tiesto", "Adagio for Strings"), ("Tiësto", "Adagio For Strings (Radio Edit)")),
        (("papertiger", "makes me (wanna move)"), ("Host & paper tiger", "Makes Me (Wanna Move)")),
        (("Some Duo & Flowdan", "Shella Verse"), ("Flowdan", "Shell a Verse")),
        (("feeling emotion", "kettama (unreleased)"),
         ("KETTAMA", "Feeling Emotions (Extended mix)")),
        (("pretty sure its Harbour Lights", "Low Tide Unit i think"),
         ("Low Tide Unit & Second Act", "Harbour Lights (Extended Mix)")),
        (("Voldex", "This Is Oldschool"), ("Voldex & Pal", "This Is Oldschool (Extended Mix)")),
        (("Sammy Virji", "Never Let You Go"),
         ("Hamdi", "Never Let You Go Edit (Original by Sammy Virji)")),
        (("warlord", "soul mass transit system"),
         ("Dominus & Daffy", "Warlord (Soul Mass Transit System Remix)")),
        (("DJ Example", "I Love You"), ("DJ Example", "Another Year Alone (i love you)")),
        ((None, "glass gardens-mirrorball"), ("Mirrorball", "Glass Gardens (Extended Mix)")),
    ],
)  # fmt: skip
def test_a_casual_label_names_the_recognised_work(
    hint: tuple[str | None, str], audio: tuple[str, str]
) -> None:
    assert hint_label_corroborates(*hint, *audio)


@pytest.mark.parametrize(
    ("hint", "audio"),
    [
        (("Bakey", "Take It Further"), ("Bakey", "Senses")),  # same artist, another title
        (("Somebody Else", "Senses"), ("Bakey", "Senses")),  # same title, another artist
        (("run", "low tide unit"), ("Low Tide Unit", "Run It")),  # one part only: title not named
        (("MPH", "Run"), ("MPH", "Run It")),
        (("Low Tide", "Unit"), ("Low Tide Unit", "Harbour Lights")),  # the artist alone, split
        (("Harbour", "Lights"), ("Low Tide Unit", "Harbour Lights")),  # the title alone, split
        (("Rush (feat. Somebody Else)", "MPH"), ("MPH", "Rush (feat. Cecelia)")),
        (("Effy", "up"), ("Effy", "us")),  # a short word is never a near-spelling
        (("ab", "cd"), ("a", "bcd")),  # words never join across the artist / title boundary
        (("Kettama", "Rock The House"), ("KETTAMA", "Rok da House")),  # two words off: no
    ],
)
def test_a_near_miss_never_names_the_recognised_work(
    hint: tuple[str, str], audio: tuple[str, str]
) -> None:
    assert not hint_label_corroborates(*hint, *audio)


def test_both_parts_are_needed_even_when_every_hint_word_is_in_the_audio_label() -> None:
    """The word-set rule alone let a hint whose words are all in the label through; a hint that
    names the artist and only the FIRST word of a two-word title names another track."""

    votes = [
        _shazam_vote(_window(start), "Low Tide Unit", "Run It", key="run-it")
        for start in (60_000, 72_000)
    ]
    hints = _hints(
        [{"comment": "c", "text": "run - low tide unit", "at_ms": 66_000, "author": "fan"}]
    )
    identity = build_identity_graph(MEDIA_KEY, votes, hints=hints)
    audio_work = next(
        item.work_id
        for item in identity.record.candidates
        if item.canonical_id == identity.observation_candidates[votes[0].id]
    )
    assert identity.hint_work_ids[hints[0].id] != audio_work


def test_a_bare_hyphen_answer_joins_one_work_or_none() -> None:
    gardens = [_shazam_vote(_window(60_000), "Mirrorball", "Glass Gardens", key="g")]
    other = [_shazam_vote(_window(600_000), "Mirrorball", "Glass Gardens Two", key="h")]
    comment = [{"comment": "c", "text": "glass gardens-mirrorball", "at_ms": 66_000, "author": "f"}]
    hints = _hints(comment)
    assert hints[0].artist is None  # the parser reads it as a title: that is the shape fixed here

    identity = build_identity_graph(MEDIA_KEY, gardens, hints=hints)
    assert identity.hint_work_ids[hints[0].id] == identity.record.candidates[0].work_id
    assert [node.ns for node in identity.record.nodes].count("text") == 1  # no node of its own

    # "glass gardens-mirrorball" does not name "Glass Gardens Two" (its title is not all there),
    # so it still joins the one work it names.
    identity = build_identity_graph(MEDIA_KEY, [*gardens, *other], hints=hints)
    gardens_work = next(
        item.work_id
        for item in identity.record.candidates
        if item.canonical_id == identity.observation_candidates[gardens[0].id]
    )
    assert identity.hint_work_ids[hints[0].id] == gardens_work

    # A sentence with a hyphenated word names nothing and joins nothing.
    chatter = _hints(
        [{"comment": "c", "text": "this is a well-mixed set", "at_ms": 66_000, "author": "f"}]
    )
    identity = build_identity_graph(MEDIA_KEY, gardens, hints=chatter)
    assert not identity.hint_work_ids


def test_a_release_note_is_not_a_title() -> None:
    assert names_a_title("Take My Hand (unreleased)")
    assert names_a_title("Unreleased Dub")
    assert not names_a_title("unreleased")
    assert not names_a_title("(UR)")
    hints = _hints(
        [
            {"comment": "a", "text": "Fixture Artist - unreleased", "at_ms": 60_000, "author": "f"},
            {
                "comment": "b",
                "text": "Fixture Artist & Friend - Take My Hand (unreleased)",
                "at_ms": 900_000,
                "author": "g",
            },
        ]
    )
    identity = build_identity_graph(MEDIA_KEY, [], hints=hints)
    by_comment = {hint.raw_text: identity.hint_work_ids.get(hint.id) for hint in hints}
    # It used to merge into "Take My Hand" (its two words are a subset) and list that track at
    # the wrong time; an answer that names no title names no work.
    assert by_comment["Fixture Artist - unreleased"] is None
    assert by_comment["Fixture Artist & Friend - Take My Hand (unreleased)"] is not None


def test_the_reach_is_ahead_of_between_and_just_after_the_proved_support() -> None:
    supports = [(600_000, 624_000), (636_000, 660_000)]
    assert hint_reaches_supports((626_000, 634_000), supports)  # between two matched windows
    assert hint_reaches_supports(
        (600_000 - HINT_LEAD_IN_MS, 600_000 - HINT_LEAD_IN_MS + 1), supports
    )
    assert not hint_reaches_supports((0, 600_000 - HINT_LEAD_IN_MS), supports)
    assert hint_reaches_supports((660_000 + HINT_TRAIL_MS - 1, 700_000 + HINT_TRAIL_MS), supports)
    assert not hint_reaches_supports((660_000 + HINT_TRAIL_MS, 800_000), supports)
    assert HINT_TRAIL_MS < HINT_LEAD_IN_MS  # a hint is early far more often than late
    assert not hint_reaches_supports((0, 10), [])


def test_a_hint_off_the_support_backs_the_main_body_only() -> None:
    plays = {
        "fragment": [(540_000, 552_000)],
        "body": [(600_000, 660_000)],
        "far": [(9_000_000, 9_060_000)],
    }
    # On a play's own support: that play, exactly as before (both, when it sits on both).
    assert hint_backed_plays((545_000, 555_000), plays, 10_000_000) == {"fragment"}
    # Ahead of both: the one with the most proved on-air time, not the nearest scrap.
    assert hint_backed_plays((495_000, 505_000), plays, 10_000_000) == {"body"}
    # Equal on-air time: the nearer one.
    level = {"near": [(600_000, 612_000)], "further": [(640_000, 652_000)]}
    assert hint_backed_plays((500_000, 510_000), level, 10_000_000) == {"near"}
    # Out of reach of everything: nothing.
    assert hint_backed_plays((3_000_000, 3_010_000), plays, 10_000_000) == set()


# --------------------------------------------------------------------------------------------------
# Fix pass (review FIX_FIRST): field-level labels, one hint one episode, indirect hints stay humble.
# Every test below FAILS on the first-pass code — the pooled-token matcher, the every-direct-play
# backing, the blanket ``hint_supported`` immunity — which is the unsafe rule each one guards.
# --------------------------------------------------------------------------------------------------
def _votes(artist: str, title: str, starts: list[int], key: str) -> list[ObservationRecord]:
    return [_shazam_vote(_window(start), artist, title, key=key) for start in starts]


def _comment(name: str, text: str, at_ms: int) -> dict:
    return {"comment": name, "text": text, "at_ms": at_ms, "author": f"fan-{name}"}


def _untimed(name: str, text: str) -> dict:
    return {"comment": name, "text": text, "at_ms": None, "author": f"fan-{name}"}


def _work_of(identity: IdentityBuildResult, observation: ObservationRecord) -> str:
    candidate = identity.observation_candidates[observation.id]
    return next(i.work_id for i in identity.record.candidates if i.canonical_id == candidate)


def _episode_of(episodes: EpisodesFile, votes: list[ObservationRecord]) -> EpisodeRecord:
    ids = {item.id for item in votes}
    (episode,) = [item for item in episodes.episodes if ids & set(item.evidence)]
    return episode


@pytest.mark.parametrize(
    ("hint", "audio"),
    [
        (("Artist", "Run It Back"), ("Artist", "Run It")),
        (("Run It Back", "Artist"), ("Artist", "Run It")),
        (("Artist", "Night Drive Home"), ("Artist", "Night Drive (Extended Mix)")),
    ],
)
def test_a_longer_title_that_contains_the_recognised_one_is_another_title(
    hint: tuple[str, str], audio: tuple[str, str]
) -> None:
    assert not hint_label_corroborates(*hint, *audio)
    assert hint_label_corroborates(audio[1], audio[0], *audio)


@pytest.mark.parametrize(
    ("hint", "audio"),
    [
        (("DJ", "Love"), ("DJ Alice", "Love")),
        (("The", "Love"), ("The Fixture Band", "Love")),
        (("MC & DJ", "Love"), ("DJ Alice & MC Bob", "Love")),
    ],
)
def test_a_common_word_is_not_a_credited_name(
    hint: tuple[str, str], audio: tuple[str, str]
) -> None:
    assert not hint_label_corroborates(*hint, *audio)
    assert hint_label_corroborates("Alice", "Love", "DJ Alice", "Love")  # the name itself is


@pytest.mark.parametrize(
    ("hint", "audio"),
    [
        (("DJ Artist", "Love"), ("DJ Love", "Artist")),  # each field borrows from the other
        (("Alpha Beta", "Gamma"), ("Alpha Gamma", "Beta")),
        (("Alpha", "Beta Gamma"), ("Alpha Gamma", "Beta")),
    ],
)
def test_a_word_of_one_field_never_stands_in_for_the_other(
    hint: tuple[str, str], audio: tuple[str, str]
) -> None:
    assert not hint_label_corroborates(*hint, *audio)
    # Swapping the two WHOLE fields is still fine: that is the order casual answers come in.
    assert hint_label_corroborates(audio[1], audio[0], *audio)


@pytest.mark.parametrize(
    ("hint_title", "audio_title"),
    [("Run", "Runs"), ("Angel", "Anger"), ("Falling", "Calling"), ("Grey Day", "Gray Day")],
)
def test_a_title_one_edit_away_is_another_title(hint_title: str, audio_title: str) -> None:
    assert not hint_label_corroborates("Artist", hint_title, "Artist", audio_title)


def test_the_spelling_slips_the_corpus_needed_still_pass() -> None:
    assert hint_label_corroborates("Mall Grab", "Temors", "Mall Grab", "Tremors")
    assert hint_label_corroborates("CLUBGIRLS", "Effy", "Effy", "CLUBGRLS")
    assert hint_label_corroborates("So u know", "Overmono", "Overmono", "So U Kno")
    assert hint_label_corroborates("Artist", "Song", "Artist", "Song feat. Guest")
    assert hint_label_corroborates("Artist", "Song", "Artist", "Song Original Mix")


@pytest.mark.parametrize("placeholder", ["ID", "TBC", "unknown", "unreleased", "ID (UR)"])
def test_a_placeholder_names_nothing_and_gets_no_identity(placeholder: str) -> None:
    assert not hint_label_corroborates("Fixture Artist", placeholder, "Fixture Artist", placeholder)
    assert not hint_label_corroborates(placeholder, "Fixture Artist", "Fixture Artist", placeholder)
    assert not names_a_title(placeholder)
    audio = _votes("Fixture Artist", placeholder, [60_000, 72_000], "placeholder")
    hints = _hints([_comment("c", f"Fixture Artist - {placeholder}", 66_000)])
    episodes, identity = _fuse(audio, duration_ms=CASE["duration_ms"], hints=hints)
    assert not identity.hint_work_ids  # no work of its own, and not the recogniser's either
    assert "hint_supported" not in _episode_of(episodes, audio).flags
    assert not any("hint_only" in episode.flags for episode in episodes.episodes)


def test_a_hint_that_fits_two_recognised_works_backs_neither_and_lists_nothing() -> None:
    solo = _votes("Fixture Artist", "Night Drive", [60_000, 72_000], "solo")
    collab = _votes("Fixture Artist & Second Act", "Night Drive", [600_000, 612_000], "collab")
    # (Typed the other way round, or the hint's text would simply BE the solo label's node.)
    hints = _hints([_comment("c", "night drive - fixture artist", 606_000)])
    episodes, identity = _fuse([*solo, *collab], duration_ms=CASE["duration_ms"], hints=hints)
    assert _work_of(identity, solo[0]) != _work_of(identity, collab[0])  # never merged by a hint
    assert not identity.hint_work_ids
    assert not any("hint_supported" in episode.flags for episode in episodes.episodes)
    assert not any("hint_only" in episode.flags for episode in episodes.episodes)


def test_two_labels_of_one_work_are_not_an_ambiguity() -> None:
    plain = _votes("Fixture Artist", "Party Drums", [60_000, 72_000], "plain")
    club = _votes("Fixture Artist", "Party Drums (Club Mix)", [84_000, 96_000], "club")
    hints = _hints([_comment("c", "party drums - fixture artist", 66_000)])
    _episodes, identity = _fuse([*plain, *club], duration_ms=CASE["duration_ms"], hints=hints)
    assert identity.hint_work_ids[hints[0].id] == _work_of(identity, plain[0])


def test_a_loosely_spelled_answer_is_not_carried_in_by_a_well_spelled_one() -> None:
    audio = _votes("Low Tide Unit", "Run It", [60_000, 72_000], "run-it")
    hints = _hints(
        [
            _comment("good", "run it - low tide unit", 66_000),
            _comment("other", "run - low tide unit", 70_000),
        ]
    )
    identity = build_identity_graph(MEDIA_KEY, audio, hints=hints)
    by_text = {hint.raw_text: identity.hint_work_ids.get(hint.id) for hint in hints}
    assert by_text["run it - low tide unit"] == _work_of(identity, audio[0])
    assert by_text["run - low tide unit"] != _work_of(identity, audio[0])


def test_the_previous_and_the_next_track_each_keep_their_own_hints() -> None:
    """Two adjacent tracks with look-alike names, and an answer typed right on the blend."""

    previous = _votes("Fixture Artist", "Run It", list(range(0, 240_000, 12_000)), "previous")
    following = _votes(
        "Fixture Artist", "Run It Back", list(range(240_000, 480_000, 12_000)), "next"
    )
    comments = {
        "on-the-blend": ("run it back - fixture artist", 241_000),  # 236-246 s: on both tracks
        "ahead": ("fixture artist - run it back!", 200_000),  # typed inside the previous track
        "for-previous": ("run it - fixture artist", 300_000),  # typed inside the next track
    }
    hints = _hints([_comment(name, text, at_ms) for name, (text, at_ms) in comments.items()])
    episodes, identity = _fuse(
        [*previous, *following], duration_ms=CASE["duration_ms"], hints=hints
    )
    assert _work_of(identity, previous[0]) != _work_of(identity, following[0])
    ids = {name: _hint_id(name) for name in comments}
    before = set(_episode_of(episodes, previous).evidence) & set(ids.values())
    after = set(_episode_of(episodes, following).evidence) & set(ids.values())
    assert before == {ids["for-previous"]}
    assert after == {ids["on-the-blend"], ids["ahead"]}


def test_one_hint_on_two_episodes_backs_the_one_it_overlaps_most() -> None:
    plays = {"a": [(600_000, 624_000)], "b": [(606_000, 618_000)]}
    assert hint_backed_plays((605_000, 615_000), plays, 10_000_000) == {"a"}  # 10 s against 9 s
    assert hint_backed_plays((608_000, 616_000), plays, 10_000_000) == {"a"}  # tie: more on air
    # The same thing through fusion: two releases of one work heard over the same seconds.
    one = _votes("Fixture Artist", "Night Drive", [600_000, 612_000], "release-one")
    two = _votes("Fixture Artist", "Night Drive", [606_000], "release-two")
    hints = _hints([_comment("c", "night drive - fixture artist", 610_000)])
    episodes, _identity = _fuse([*one, *two], duration_ms=CASE["duration_ms"], hints=hints)
    backed = [episode for episode in episodes.episodes if _hint_id("c") in episode.evidence]
    assert [episode.id for episode in backed] == [_episode_of(episodes, one).id]
    assert "hint_supported" not in _episode_of(episodes, two).flags


def test_an_indirect_hint_never_lifts_a_scattered_episode_but_a_direct_one_still_does() -> None:
    smeared = _votes("Fixture Artist", "Night Drive", [600_000, 700_000, 800_000], "smeared")
    for at_ms, expected in ((500_000, "scatter"), (606_000, None)):
        hints = _hints([_comment("c", "night drive - fixture artist", at_ms)])
        episodes, _identity = _fuse(smeared, duration_ms=CASE["duration_ms"], hints=hints)
        episode = _episode_of(episodes, smeared)
        assert "hint_supported" in episode.flags  # it still votes; it just does not overrule
        assert episode.suppressed == expected


def test_an_indirect_hint_never_lifts_a_contradicted_episode() -> None:
    audio = _votes("Fixture Artist", "Night Drive", [600_000, 612_000, 624_000], "heard")
    contradiction = _comment("no", "Somebody Else - Another Song", 615_000)
    for at_ms, expected in ((540_000, "contradicted"), (606_000, None)):
        hints = _hints([_comment("c", "night drive - fixture artist", at_ms), contradiction])
        episodes, _identity = _fuse(audio, duration_ms=CASE["duration_ms"], hints=hints)
        assert _episode_of(episodes, audio).suppressed == expected
    # With nothing wrong with the episode, the indirect hint keeps its first-pass value.
    hints = _hints([_comment("c", "night drive - fixture artist", 540_000)])
    episodes, _identity = _fuse(audio, duration_ms=CASE["duration_ms"], hints=hints)
    episode = _episode_of(episodes, audio)
    assert "hint_supported" in episode.flags and episode.suppressed is None


# --------------------------------------------------------------------------------------------------
# Fix pass 2 (review round 2): the ambiguity veto has no exact-label shortcut, and featured guests
# are whole names.  Each test fails on the fix-pass-1 code.
# --------------------------------------------------------------------------------------------------
def test_an_exact_label_that_also_fits_the_next_track_backs_and_contradicts_neither() -> None:
    """Two adjacent plays; the hint is the solo label LETTER FOR LETTER, in the exact field
    orientation — and it fits the collaboration next to it just as well."""

    solo = _votes("Fixture Artist", "Night Drive", list(range(0, 240_000, 12_000)), "solo")
    collab = _votes(
        "Fixture Artist & Second Act", "Night Drive", list(range(240_000, 480_000, 12_000)), "co"
    )
    hints = _hints([_comment("c", "Fixture Artist - Night Drive", 246_000)])  # on the collab
    assert (hints[0].artist, hints[0].title) == ("Fixture Artist", "Night Drive")
    episodes, identity = _fuse([*solo, *collab], duration_ms=CASE["duration_ms"], hints=hints)
    assert not identity.hint_work_ids and not identity.hint_candidates
    assert _work_of(identity, solo[0]) != _work_of(identity, collab[0])
    for votes in (solo, collab):
        episode = _episode_of(episodes, votes)
        assert "hint_supported" not in episode.flags
        assert episode.suppressed is None  # in particular not "contradicted"
        assert _hint_id("c") not in episode.evidence
    assert not any("hint_only" in episode.flags for episode in episodes.episodes)
    # With only ONE work it could mean, the same exact label still backs it.
    episodes, identity = _fuse(solo, duration_ms=CASE["duration_ms"], hints=hints)
    assert identity.hint_work_ids[hints[0].id] == _work_of(identity, solo[0])


@pytest.mark.parametrize(
    ("heard", "typed"),
    [("DJ Boring", "DJ Seinfeld"), ("Bob Jones", "Bob Smith")],
)
def test_featured_guests_are_compared_as_whole_names(heard: str, typed: str) -> None:
    assert not hint_label_corroborates(
        "Fixture Artist", f"Song (feat. {typed})", "Fixture Artist", f"Song (feat. {heard})"
    )
    assert hint_label_corroborates(
        "Fixture Artist", f"Song (feat. {heard})", "Fixture Artist", f"Song (feat. {heard})"
    )
    audio = _votes("Fixture Artist", f"Song (feat. {heard})", [600_000, 612_000], "heard")
    hints = _hints([_comment("c", f"song (feat. {typed}) - fixture artist", 606_000)])
    episodes, identity = _fuse(audio, duration_ms=CASE["duration_ms"], hints=hints)
    assert identity.hint_work_ids[hints[0].id] != _work_of(identity, audio[0])
    assert "hint_supported" not in _episode_of(episodes, audio).flags


@pytest.mark.parametrize(
    ("first", "second"),
    [("DJ Boring", "DJ Seinfeld"), ("Bob Jones", "Bob Smith")],
)
def test_two_works_with_different_featured_guests_are_never_merged_by_a_hint(
    first: str, second: str
) -> None:
    one = _votes("Fixture Artist", f"Song (feat. {first})", [600_000, 612_000], "one")
    two = _votes("Fixture Artist", f"Song (feat. {second})", [900_000, 912_000], "two")
    comments = [
        _comment("uncredited", "song - fixture artist", 606_000),  # fits both: vetoed
        _comment("credited", f"song (feat. {second}) - fixture artist", 906_000),  # fits one
    ]
    hints = _hints(comments)
    episodes, identity = _fuse([*one, *two], duration_ms=CASE["duration_ms"], hints=hints)
    assert _work_of(identity, one[0]) != _work_of(identity, two[0])
    works = {hint.raw_text: identity.hint_work_ids.get(hint.id) for hint in hints}
    assert works["song - fixture artist"] is None
    assert works[f"song (feat. {second}) - fixture artist"] == _work_of(identity, two[0])
    assert "hint_supported" not in _episode_of(episodes, one).flags
    assert _episode_of(episodes, one).suppressed is None
    assert _hint_id("credited") in _episode_of(episodes, two).evidence


def test_an_uncredited_label_still_joins_the_one_credited_version_there_is() -> None:
    plain = _votes("Fixture Artist", "Song", [600_000, 612_000], "plain")
    credited = _votes("Fixture Artist", "Song (feat. Bob Jones)", [624_000, 636_000], "credited")
    hints = _hints([_comment("c", "song - fixture artist", 606_000)])
    _episodes, identity = _fuse([*plain, *credited], duration_ms=CASE["duration_ms"], hints=hints)
    assert identity.hint_work_ids[hints[0].id] == _work_of(identity, plain[0])


# --------------------------------------------------------------------------------------------------
# Polish pass: an unambiguous untimed label supports the recognised work, and nothing positional.
# --------------------------------------------------------------------------------------------------
def test_an_untimed_hint_supports_the_one_recognised_work_without_retiming_it() -> None:
    audio = _votes("Fixture Artist", "Night Drive", [600_000, 612_000], "heard")
    baseline, _identity = _fuse(audio, duration_ms=CASE["duration_ms"], hints=[])
    hints = _hints([_untimed("c", "night drive - fixture artist")])
    episodes, identity = _fuse(audio, duration_ms=CASE["duration_ms"], hints=hints)
    before = _episode_of(baseline, audio)
    after = _episode_of(episodes, audio)

    assert identity.hint_work_ids[hints[0].id] == _work_of(identity, audio[0])
    assert "hint_supported" in after.flags and hints[0].id in after.evidence
    assert after.evidence_support_ms == before.evidence_support_ms
    assert after.start_no_later_than_ms == before.start_no_later_than_ms
    assert after.end_no_earlier_than_ms == before.end_no_earlier_than_ms
    assert after.best_start_ms == before.best_start_ms and after.best_end_ms == before.best_end_ms


def test_an_untimed_hint_backs_no_occurrence_when_its_work_plays_twice() -> None:
    """An unpositioned label cannot choose between a true row and a later false fragment."""

    first = _votes("Fixture Artist", "Night Drive", [60_000, 72_000, 84_000], "repeat-main")
    second = _votes("Fixture Artist", "Night Drive", [3_000_000], "repeat-fragment")
    hints = _hints(
        [
            _untimed("unpositioned", "Fixture Artist - Night Drive"),
            # The timed truth names only the first occurrence.  It is independently eligible to
            # publish that row, while the second 12-second fragment remains below the floor.
            _comment("timed-truth", "Fixture Artist - Night Drive", 66_000),
        ]
    )
    episodes, identity = _fuse([*first, *second], duration_ms=CASE["duration_ms"], hints=hints)
    untimed = next(hint for hint in hints if hint.position_range_ms is None)
    timed = next(hint for hint in hints if hint.position_range_ms is not None)
    first_episode = _episode_of(episodes, first)
    second_episode = _episode_of(episodes, second)

    assert identity.hint_work_ids[untimed.id] == _work_of(identity, first[0])
    assert all(untimed.id not in episode.evidence for episode in episodes.episodes)
    assert timed.id in first_episode.evidence
    assert "hint_supported" not in second_episode.flags

    rows = flatten_tracklist(episodes, identity.record, collapse=False, min_track_ms=30_000)
    shown = [row for row in rows if row["kind"] == "track"]
    assert [row["episode_id"] for row in shown] == [first_episode.id]


def test_an_untimed_hint_that_fits_two_recognised_works_backs_neither() -> None:
    solo = _votes("Fixture Artist", "Night Drive", [60_000, 72_000], "solo")
    collab = _votes("Fixture Artist & Second Act", "Night Drive", [600_000, 612_000], "co")
    hints = _hints([_untimed("c", "Fixture Artist - Night Drive")])
    episodes, identity = _fuse([*solo, *collab], duration_ms=CASE["duration_ms"], hints=hints)

    assert not identity.hint_work_ids
    assert all("hint_supported" not in episode.flags for episode in episodes.episodes)
    assert all(hints[0].id not in episode.evidence for episode in episodes.episodes)
    assert not any("hint_only" in episode.flags for episode in episodes.episodes)

    # The same label does attach when there is only one recognised work.  Keeping the positive
    # control in this guard test makes it prove the new untimed policy as well as its ambiguity
    # veto (and makes reverting the policy fail this test, rather than pass vacuously).
    solo_episodes, _identity = _fuse(solo, duration_ms=CASE["duration_ms"], hints=hints)
    assert "hint_supported" in _episode_of(solo_episodes, solo).flags


def test_an_untimed_hint_never_creates_a_crowd_only_track() -> None:
    heard = _votes("Fixture Artist", "Night Drive", [600_000, 612_000], "heard")
    hints = _hints(
        [
            _untimed("heard", "Fixture Artist - Night Drive"),
            _untimed("unheard", "Fixture Artist - Unheard Song"),
        ]
    )
    episodes, identity = _fuse(heard, duration_ms=CASE["duration_ms"], hints=hints)

    assert "hint_supported" in _episode_of(episodes, heard).flags
    assert hints[1].id in identity.hint_work_ids  # it names a work, but no recogniser heard it
    assert all(hints[1].id not in episode.evidence for episode in episodes.episodes)
    assert not any("hint_only" in episode.flags for episode in episodes.episodes)


def test_an_untimed_hint_never_overrides_scatter_or_contradicted() -> None:
    scattered = _votes("Fixture Artist", "Night Drive", [600_000, 700_000, 800_000], "s")
    support = _untimed("yes", "night drive - fixture artist")
    episodes, _identity = _fuse(scattered, duration_ms=CASE["duration_ms"], hints=_hints([support]))
    episode = _episode_of(episodes, scattered)
    assert "hint_supported" in episode.flags and episode.suppressed == "scatter"

    continuous = _votes("Fixture Artist", "Night Drive", [600_000, 612_000, 624_000], "c")
    contradiction = _comment("no", "Somebody Else - Another Song", 615_000)
    episodes, _identity = _fuse(
        continuous,
        duration_ms=CASE["duration_ms"],
        hints=_hints([support, contradiction]),
    )
    episode = _episode_of(episodes, continuous)
    assert "hint_supported" in episode.flags and episode.suppressed == "contradicted"
