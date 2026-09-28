"""Free-side accuracy (fusion:5): one tolerant identity rule, one row per track, the phantom rule.

Everything here is offline: audio votes go through the real Shazam/AudD converters, comments
through the real parser and relations pass, and the result through real fusion, presentation and
the corpus scorer.  Nothing contacts a provider.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from id_detector.benchmark.corpus import _label_for_candidate
from id_detector.contracts import EpisodesFile, HintRecord, ObservationRecord, TruthWork
from id_detector.fuse.episodes import ambiguous_vote
from id_detector.fuse.identity import (
    build_identity_graph,
    hint_label_corroborates,
    same_work_labels,
    solid_names,
)
from id_detector.hints.parse import HintInput, parse_hint_inputs
from id_detector.hints.relations import apply_relations
from id_detector.present.exports import flatten_tracklist
from id_detector.shazam import response_to_observation
from scripts.score_corpus import (
    ListedWork,
    MixedFusionVersions,
    compare_scores,
    identity_label,
    listed_works_from_episodes,
    load_run_list,
    main,
    match_works,
    recognised_solid_names,
    run_fusion_version,
    score_run_list,
)
from tests.test_phase1b_fusion import (
    MEDIA_KEY,
    SHAZAM_CONFIG,
    _audd_vote,
    _fuse,
    _query,
    _rows,
    _shazam_vote,
    _window,
)

HOP_MS = 9_000
MIN_TRACK_MS = 30_000
SOLID = solid_names(
    [
        [("Flansie & Mall Grab", "No One Else Will")],
        [("Jordon Alexander", "Winter (feat. Mall Grab)")],
        [("Mall Grab", "Spirit Wave")],
    ]
)


# --------------------------------------------------------------------------------------------------
# 1. The one identity rule
# --------------------------------------------------------------------------------------------------
TOLERATED = [
    # (a label, another label of the same work, the one tolerance it needs)
    (("t e s t p r e s s", "Forz4"), ("Testpress", "FORZ4"), ("spaced",)),
    (("rock the house", "kettama"), ("KETTAMA", "Rok da House"), ("phonetic",)),
    (("KETTAMA", "Feel Emotion (UR)"), ("KETTAMA", "Feeling Emotions (Extended mix)"), ("stem",)),
    (("MG", "Long Season Intro Edit"), ("Fishmans", "Long season MG Edit"), ("descriptor",)),
    (("MG", "Long Season Intro Edit"), ("Fishmans", "Long Season (MG Edit) (UR)"), ("descriptor",)),
    (("MG", "1ofthozedaze"), ("1ofthozedaze", "Mall Grab"), ("initials",)),
    (("Long Season Intro Edit", "Mall Grab"), ("MG", "Long Season Intro Edit"), ("initials",)),
    (
        ("KETTAMA", "ID (LET ME SEE U / ROK DA HOUSE!) (UR)"),
        ("KETTAMA", "Rok da House"),
        ("alt_title",),
    ),
    (("Paige Tomlison", "I Wanna Feel"), ("Paige Tomlinson", "I Wanna Feel"), ("artist_slip",)),
    (
        ("FULL TRACK LIST:", "Fishmans - Long season MG Edit (UR)"),
        ("Fishmans", "Long season MG Edit"),
        ("lead_in",),
    ),
]


@pytest.mark.parametrize(("left", "right", "tolerances"), TOLERATED)
def test_each_tolerance_joins_the_spelling_it_was_built_for(left, right, tolerances) -> None:
    # The established rule alone does not see these as one work (that is why they needed a rule).
    assert not hint_label_corroborates(*left, *right)
    assert not hint_label_corroborates(*right, *left)
    match = same_work_labels(left, right, solid=SOLID)
    assert match is not None and match.tolerances == tolerances


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (("4raws", "beau james"), ("Beau James", "4 Raws Edit")),
        (("Flowdan", "Shell a Verse"), ("Sammy Virji & Flowdan", "Shella Verse")),
        (
            ("DJ Heartstring", "Another Year Alone (UR)"),
            ("DJ HEARTSTRING", "Another Year Alone (i love you)"),
        ),  # noqa: E501
        (("mallgrab", "BB MG (UR)"), ("Mall Grab", "BB MG (Soundcloud)")),
    ],
)
def test_the_established_readings_still_match_with_no_tolerance(left, right) -> None:
    match = same_work_labels(left, right, solid=SOLID)
    assert match is not None and match.tolerances == ()


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # a tolerant artist AND a tolerant title: never
        (("MG", "Spirit Wav"), ("Mall Grab", "Spirit Wave")),
        (("Paige Tomlison", "Feeling"), ("Paige Tomlinson", "Feel")),
        (("Long Season Intro Edit", "Mall Grab"), ("Fishmans", "Long Season (MG Edit)")),
        # titles that merely share words
        (("MPH", "Run"), ("MPH", "Run It")),
        (("Artist", "Song"), ("Artist", "Song Two")),
        (("MPH", "Kill"), ("MPH", "Kill Bill Dub")),
        (("Upper90", "I Am Ready 2"), ("I'm ready", "Upper90")),
        (("Artist", "Live Forever"), ("Artist", "Love Forever")),
        # same title, another artist
        (("Beau James", "18Hunna"), ("Headie One", "18HUNNA (feat. Dave)")),
        (("Simula", "Daddy Issues"), ("Simula", "Descent")),
        # initials of nobody solid
        (("XY", "Spirit Wave"), ("Mall Grab", "Spirit Wave")),
        # fix pass 1: a ONE-word title tolerates no changed word, and no misspelt name beside it
        (("Artist", "Dreams"), ("Artist", "Dream")),
        (("Artist", "Night"), ("Artist", "Nite")),
        (("Paige Tomlison", "Feel"), ("Paige Tomlinson", "Feel")),
    ],
)
def test_no_tolerance_lets_these_near_misses_through(left, right) -> None:
    assert same_work_labels(left, right, solid=SOLID) is None


def test_initials_resolve_only_to_one_name_solid_in_this_mix() -> None:
    label, other = ("MG", "Spirit Wave"), ("Mall Grab", "Spirit Wave")
    assert same_work_labels(label, other, solid=SOLID) is not None
    # Not solid: Mall Grab credited on one recognised work only.
    assert same_work_labels(label, other, solid=solid_names([[other]])) is None
    # Two solid names with those initials: "MG" says which of them no better than a guess.
    two = SOLID | solid_names([[("Marcus Gray", "One")], [("Marcus Gray", "Two")]])
    assert ("marcus", "gray") in two
    assert same_work_labels(label, other, solid=two) is None


# --------------------------------------------------------------------------------------------------
# Fusion through the rule
# --------------------------------------------------------------------------------------------------
def _track(artist: str, title: str, start_ms: int, windows: int, *, key: str) -> list:
    return [
        _shazam_vote(_window(start_ms + index * HOP_MS), artist, title, key=key)
        for index in range(windows)
    ]


def _hints(comments: list[tuple[str, str, int]], duration_ms: int) -> list[HintRecord]:
    inputs = [
        HintInput(
            connector="sc_comments",
            source_record_id=comment_id,
            text=text,
            position_ms=at_ms,
            position_kind="comment_timestamp",
            author_pseudo_id=f"fan-{comment_id}",
            parent_source_id=None,
        )
        for comment_id, text, at_ms in comments
    ]
    hints = parse_hint_inputs(MEDIA_KEY, duration_ms, inputs)
    return apply_relations(MEDIA_KEY, duration_ms, hints, inputs)


def _mall_grab_mix() -> list[ObservationRecord]:
    """Two recognised Mall Grab works: the name is SOLID in this mix."""

    return [
        *_track("Mall Grab", "Spirit Wave", 600_000, 8, key="k-spirit"),
        *_track("Flansie & Mall Grab", "No One Else Will", 900_000, 8, key="k-noone"),
    ]


def test_a_tolerant_answer_backs_the_one_recognised_work_it_names() -> None:
    duration = 1_200_000
    votes = [*_mall_grab_mix(), *_track("KETTAMA", "Rok da House", 300_000, 8, key="k-rok")]
    hints = _hints([("c1", "rock the house - kettama", 318_000)], duration)
    episodes, identity = _fuse(votes, duration_ms=duration, hints=hints)
    (rok,) = [e for e in episodes.episodes if e.evidence_support_ms[0][0] == 300_000]
    assert "hint_supported" in rok.flags
    assert [link[0] for link in identity.tolerant_links] == ["hint_to_audio"]
    assert identity.tolerant_links[0][3] == ("phonetic",)


def test_a_tolerant_answer_that_fits_two_recognised_works_backs_neither() -> None:
    duration = 1_200_000
    votes = [
        *_track("KETTAMA", "Feel Emotion", 300_000, 6, key="k-one"),
        *_track("KETTAMA", "Feel Emotions", 700_000, 6, key="k-two"),
    ]
    hints = _hints([("c1", "Kettama - Feeling Emotion", 318_000)], duration)
    episodes, identity = _fuse(votes, duration_ms=duration, hints=hints)
    assert not any("hint_supported" in e.flags for e in episodes.episodes)
    assert identity.tolerant_links == ()
    # ... and with only one of them heard, the same answer backs it.
    episodes, _ = _fuse(votes[:6], duration_ms=duration, hints=hints)
    assert any("hint_supported" in e.flags for e in episodes.episodes)


def _crowd_rows(episodes: EpisodesFile, identity, word: str) -> list[dict]:
    return [
        row
        for row in _rows(episodes, identity, MIN_TRACK_MS)
        if row["hint_only"] and row["hidden"] is None and word in row["display_label"].casefold()
    ]


def test_two_spellings_of_one_track_in_one_playing_are_one_row() -> None:
    """The Mall Grab opener, named by a comment and a listener's shorthand, heard by no engine."""

    duration = 1_200_000
    hints = _hints(
        [
            ("c1", "Fishmans - Long season MG Edit", 46_000),
            ("c2", "MG - Long Season Intro Edit", 124_000),
        ],
        duration,
    )
    episodes, identity = _fuse(_mall_grab_mix(), duration_ms=duration, hints=hints)
    rows = _crowd_rows(episodes, identity, "season")
    assert len(rows) == 1
    kinds = [link[3] for link in identity.tolerant_links if link[0] == "hint_to_hint"]
    assert kinds == [("descriptor",)]


def test_spellings_joined_only_through_a_third_are_not_chained() -> None:
    """Fix pass 1: "MG - Long Season Intro Edit" fits both of the others, but they do not fit each
    other ("Fishmans … MG Edit" / "… - Mall Grab"): a loose match must resolve to ONE work, and
    loose matches never chain, so none of the three is joined (three rows, as before fusion:5)."""

    duration = 1_200_000
    hints = _hints(
        [
            ("c1", "Fishmans - Long season MG Edit", 46_000),
            ("c2", "MG - Long Season Intro Edit", 124_000),
            ("c3", "Long Season Intro Edit - Mall Grab", 152_000),
        ],
        duration,
    )
    episodes, identity = _fuse(_mall_grab_mix(), duration_ms=duration, hints=hints)
    assert len(_crowd_rows(episodes, identity, "season")) == 3
    assert [link for link in identity.tolerant_links if link[0] == "hint_to_hint"] == []


def _work_of(identity, label: str) -> str:
    (node,) = [node.id for node in identity.record.nodes if node.label == label]
    (work,) = [work.work_id for work in identity.record.works if node in work.member_nodes]
    return work


def test_three_artist_spellings_are_not_bridged_into_one_work() -> None:
    """The reviewer's counterexample: "Anderson" fits "Andersson" and "Anderon" (one letter each),
    which do not fit each other.  None of the three may be joined."""

    duration = 1_200_000
    labels = ["Alex Anderson - Some Tune", "Alex Andersson - Some Tune", "Alex Anderon - Some Tune"]
    hints = _hints(
        [(f"c{index}", label, 100_000 + index * 300_000) for index, label in enumerate(labels)],
        duration,
    )
    _, identity = _fuse([], duration_ms=duration, hints=hints)
    assert len({_work_of(identity, label) for label in labels}) == 3
    # Positive control: with only two of them, the one-letter slip joins them.
    pair = _hints([("c0", labels[0], 100_000), ("c1", labels[1], 400_000)], duration)
    _, identity = _fuse([], duration_ms=duration, hints=pair)
    assert _work_of(identity, labels[0]) == _work_of(identity, labels[1])


def test_initials_without_a_solid_name_keep_the_spellings_apart() -> None:
    duration = 1_200_000
    hints = _hints(
        [
            ("c1", "MG - 1ofthozedaze", 100_000),
            ("c2", "1ofthozedaze - Mall Grab", 300_000),
        ],
        duration,
    )
    # No recognised Mall Grab work at all: "MG" may stand for anybody.
    episodes, identity = _fuse([], duration_ms=duration, hints=hints)
    assert len(_crowd_rows(episodes, identity, "1ofthozedaze")) == 2
    # With Mall Grab solid in the mix, they are one track and one row.
    episodes, identity = _fuse(_mall_grab_mix(), duration_ms=duration, hints=hints)
    assert len(_crowd_rows(episodes, identity, "1ofthozedaze")) == 1


def test_two_crowd_works_whose_titles_merely_share_words_stay_two_rows() -> None:
    """The old crowd-merge rule joined any label whose words were a subset of another's."""

    duration = 1_200_000
    hints = _hints(
        [
            ("c1", "Some Artist - Deep Water", 100_000),
            ("c2", "Some Artist - Deep Water Blues", 400_000),
        ],
        duration,
    )
    episodes, identity = _fuse([], duration_ms=duration, hints=hints)
    titles = sorted(row["title"] for row in _crowd_rows(episodes, identity, "deep water"))
    assert titles == ["Deep Water", "Deep Water Blues"]


# --------------------------------------------------------------------------------------------------
# 3. The recurring phantom
# --------------------------------------------------------------------------------------------------
def _ambiguous(start_ms: int, artist: str, title: str, key: str, *, own: bool = False):
    """A reply that offers several recordings disagreeing where the audio sits.  ``own``: the
    reported recording is one of them (many releases of ONE track)."""

    window = _window(start_ms)
    base = start_ms // 1000
    ids = [key if own and index == 0 else f"{key}-other-{index}" for index in range(4)]
    response = {
        "matches": [
            {"id": ident, "offset": base + 37 * index + 5, "frequencyskew": 0, "timeskew": 0}
            for index, ident in enumerate(ids)
        ],
        "track": {"key": key, "subtitle": artist, "title": title, "sections": []},
    }
    return response_to_observation(
        response, _query(window, "shazam"), window, SHAZAM_CONFIG, "fixture:shazam", MEDIA_KEY
    )


def _phantom(start_ms: int, windows: int, *, clean_every: int | None = None, own: bool = False):
    votes = []
    for index in range(windows):
        at = start_ms + index * HOP_MS
        if clean_every is not None and index % clean_every == 0:
            votes.append(_shazam_vote(_window(at), "Darude", "Sandstorm", key="k-sand"))
        else:
            votes.append(_ambiguous(at, "Darude", "Sandstorm", "k-sand", own=own))
    return votes


def _episode(episodes: EpisodesFile, start_ms: int):
    (episode,) = [e for e in episodes.episodes if e.evidence_support_ms[0][0] == start_ms]
    return episode


def test_a_window_is_ambiguous_only_without_an_anchor_and_without_its_own_recording() -> None:
    assert ambiguous_vote(_ambiguous(0, "A", "B", "k"))
    assert not ambiguous_vote(_ambiguous(0, "A", "B", "k", own=True))
    assert not ambiguous_vote(_shazam_vote(_window(0), "A", "B", key="k"))


def test_a_phantom_of_ambiguous_windows_is_not_listed_and_buries_nothing() -> None:
    """Sandstorm on the Boomtown mix: thirteen windows, one anchored — enough for ``likely`` —
    smeared over a real track that the phantom used to hide."""

    duration = 600_000
    phantom = _phantom(100_000, 13, clean_every=13)
    real = [
        _shazam_vote(_window(start), "Cotto", "Murda Sound", key="k-cotto")
        for start in (140_500, 149_500, 158_500, 167_500)
    ]
    episodes, identity = _fuse([*phantom, *real], duration_ms=duration)
    sand = _episode(episodes, 100_000)
    assert sand.badge == "likely"
    assert "ambiguous_evidence" in sand.flags and sand.suppressed == "ambiguous"
    cotto = _episode(episodes, 140_500)
    assert cotto.suppressed is None
    listed = {row["title"] for row in _rows(episodes, identity, MIN_TRACK_MS) if not row["hidden"]}
    assert listed == {"Murda Sound"}


@pytest.mark.parametrize("backing", ["comment", "clean_core", "second_engine"])
def test_a_phantom_is_still_listed_when_the_evidence_says_it_really_played(backing: str) -> None:
    duration = 600_000
    votes = _phantom(100_000, 10)
    hints: list[HintRecord] = []
    if backing == "comment":
        hints = _hints([("c1", "Darude - Sandstorm", 127_000)], duration)
    elif backing == "clean_core":
        # Six back-to-back clean windows: 57 s of one recording at one consistent offset.
        votes = [
            *_track("Darude", "Sandstorm", 100_000, 6, key="k-sand"),
            *_phantom(154_000, 8),
        ]
    else:
        votes = [*votes, _audd_vote(_window(118_000), "Darude", "Sandstorm")]
    episodes, _ = _fuse(votes, duration_ms=duration, hints=hints)
    sand = _episode(episodes, 100_000)
    assert sand.suppressed is None, sand.flags


def test_a_dj_who_really_drops_sandstorm_gets_it_listed() -> None:
    """No name list: the same track with clean, anchored windows is an ordinary row."""

    episodes, identity = _fuse(
        _track("Darude", "Sandstorm", 100_000, 5, key="k"), duration_ms=600_000
    )
    sand = _episode(episodes, 100_000)
    assert "ambiguous_evidence" not in sand.flags and sand.suppressed is None
    assert [
        row["title"] for row in _rows(episodes, identity, MIN_TRACK_MS) if not row["hidden"]
    ] == ["Sandstorm"]


def test_many_releases_of_one_track_in_a_reply_are_not_ambiguous() -> None:
    """A heavily-released track ("Work" and its remixes): the reply lists several recordings,
    one of them the reported one — that is one track, not a guess."""

    episodes, _ = _fuse(_phantom(100_000, 6, own=True), duration_ms=600_000)
    sand = _episode(episodes, 100_000)
    assert "ambiguous_evidence" not in sand.flags and sand.suppressed != "ambiguous"


# --------------------------------------------------------------------------------------------------
# 0 + 1. The scorer: one rule, the label the page shows, and the fusion version stated
# --------------------------------------------------------------------------------------------------
def test_the_scorer_matches_by_the_same_rule_and_only_when_unique() -> None:
    truth = [
        TruthWork(artist="Testpress", title="FORZ4"),
        TruthWork(artist="Upper90", title="I Am Ready 2"),
        TruthWork(artist="KETTAMA", title="Feel Emotion"),
        TruthWork(artist="KETTAMA", title="Feel Emotions"),
    ]
    listed = [
        ListedWork("e1", "t e s t p r e s s", "Forz4", "possible", "w1"),
        ListedWork("e2", "I'm ready", "Upper90", "possible", "w2"),
        ListedWork("e3", "KETTAMA", "Feeling Emotion", "possible", "w3"),
    ]
    match = match_works(truth, listed)
    assert match.assignments == {0: 0}  # the stem fits two truth works: counted for neither
    assert match.tolerances == {0: ("spaced",)}


def test_the_scorer_reads_a_row_under_the_label_the_page_shows() -> None:
    duration = 600_000
    votes = _track("Jordon Alexander", "Winter (feat. Mall Grab)", 100_000, 6, key="k-w")
    hints = _hints([("c1", "#1 Jordon Alexander - Winter", 118_000)], duration)
    identity = build_identity_graph(MEDIA_KEY, votes, hints=hints)
    (candidate,) = [
        item for item in identity.record.candidates if "shazam:k-w" in item.member_nodes
    ]
    # The old choice was the alphabetically first text label of the work: a comment's spelling.
    assert _label_for_candidate(identity.record, candidate.canonical_id)[0] == "#1 Jordon Alexander"
    assert identity_label(identity.record, candidate.canonical_id) == (
        "Jordon Alexander",
        "Winter (feat. Mall Grab)",
    )
    # Solid names come from engine labels only, never from a comment.
    assert recognised_solid_names(identity.record) == frozenset()


def test_run_fusion_version_reads_each_kind_of_result(tmp_path: Path) -> None:
    media = tmp_path / "w" / "s" / "m"
    refused = media / "fuse" / "runs" / "refuse0005-x"
    refused.mkdir(parents=True)
    (refused / "refusion.json").write_text(json.dumps({"fusion_version": 5}), encoding="utf-8")
    assert run_fusion_version(refused / "episodes.json") == 5
    frozen = media / "fuse" / "runs" / "abc"
    frozen.mkdir(parents=True)
    bundle = media / "present" / "bundles" / "c"
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text(
        json.dumps(
            {"fuse_run": "fuse/runs/abc", "compatibility": {"algorithm_version": "fusion:3"}}
        ),
        encoding="utf-8",
    )
    assert run_fusion_version(frozen / "episodes.json") == 3
    (media / "invocations.jsonl").write_text(
        json.dumps({"status": "complete", "algorithm_version": "fusion:1"}) + "\n",
        encoding="utf-8",
    )
    assert run_fusion_version(media / "fuse" / "episodes.json") == 1
    assert run_fusion_version(tmp_path / "elsewhere" / "episodes.json") is None


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "corpus-mini"


def _corpus_mini(tmp_path: Path, versions: tuple[int | None, int | None]) -> Path:
    root = tmp_path / "corpus-mini"
    shutil.copytree(FIXTURE, root)
    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    for run, version in zip(run_list["runs"], versions, strict=True):
        run["truth"] = str(root / run["truth"])
        run["episodes"] = str(root / run["episodes"])
        if version is not None:
            run["fusion_version"] = version
    (root / "run-list.json").write_text(json.dumps(run_list), encoding="utf-8")
    return root / "run-list.json"


def test_one_score_of_two_fusion_versions_is_refused_or_loudly_warned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_list = _corpus_mini(tmp_path, (1, 4))
    with pytest.raises(MixedFusionVersions, match="fusion:1 \\(mini-a\\), fusion:4 \\(mini-b\\)"):
        score_run_list(
            load_run_list(run_list),
            run_list_dir=run_list.parent,
            artefact_dir=tmp_path / "a",
            out_dir=tmp_path,
        )
    assert main(["--run-list", str(run_list), "--print"]) == 1
    assert "DIFFERENT fusion versions" in capsys.readouterr().err
    assert main(["--run-list", str(run_list), "--print", "--allow-mixed-fusion"]) == 0
    assert capsys.readouterr().out.startswith("WARNING — NOT COMPARABLE: these runs were decided")


def test_every_run_states_its_fusion_version(tmp_path: Path, capsys) -> None:
    run_list = _corpus_mini(tmp_path, (5, 5))
    document = score_run_list(
        load_run_list(run_list),
        run_list_dir=run_list.parent,
        artefact_dir=tmp_path / "a",
        out_dir=tmp_path,
    )
    assert document["fusion_versions"] == [5] and document["fusion_warning"] is None
    assert [mix["fusion_version"] for mix in document["mixes"]] == [5, 5]
    assert main(["--run-list", str(run_list), "--print"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("Every run was decided by fusion:5.")
    assert lines[1].startswith("- mini-a [fusion:5; order-only;")


def test_two_scores_at_different_fusion_versions_are_not_compared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    earlier = tmp_path / "earlier.json"
    assert (
        main(["--run-list", str(_corpus_mini(tmp_path / "x", (1, 1))), "--out", str(earlier)]) == 0
    )
    later_list = _corpus_mini(tmp_path / "y", (4, 4))
    capsys.readouterr()
    code = main(["--run-list", str(later_list), "--print", "--compare-with", str(earlier)])
    assert code == 3
    assert "earlier score was decided by fusion:1 and this one by fusion:4" in (
        capsys.readouterr().err
    )
    same_list = _corpus_mini(tmp_path / "z", (1, 1))
    assert main(["--run-list", str(same_list), "--print", "--compare-with", str(earlier)]) == 0
    assert "Compared (same fusion)" in capsys.readouterr().out
    unknown = json.loads(earlier.read_text("utf-8"))
    assert compare_scores({**unknown, "fusion_versions": []}, unknown)["fusion_warning"] == (
        "the fusion version of one of the two scores is unknown"
    )


# --------------------------------------------------------------------------------------------------
# 0. The run list names each mix's CURRENT result, never the stale working file
# --------------------------------------------------------------------------------------------------
GOLDEN = Path(__file__).resolve().parent / "golden"


def _cached_mix(media: Path, truth: dict, *, journal_fusion: int) -> None:
    """A cached mix whose flat ``fuse/episodes.json`` is an OLD fusion's working copy."""

    source = json.loads((GOLDEN / "source.json").read_text("utf-8"))
    source["media_key"] = truth["source"]["media_key"]
    pcm = json.loads((GOLDEN / "pcm.json").read_text("utf-8"))
    pcm["media_key"] = truth["source"]["media_key"]
    pcm["pcm"]["duration_ms"] = pcm["pcm"]["ffprobe_duration_ms"] = truth["source"]["duration_ms"]
    for relative, value in (("ingest/source.json", source), ("decode/pcm.json", pcm)):
        (media / relative).parent.mkdir(parents=True, exist_ok=True)
        (media / relative).write_text(json.dumps(value), encoding="utf-8")
    (media / "fuse").mkdir(parents=True, exist_ok=True)
    for name in ("episodes.json", "identities.gen0.json"):
        shutil.copy(FIXTURE / "mini-a" / "fuse" / name, media / "fuse" / name)
    (media / "invocations.jsonl").write_text(
        json.dumps({"status": "complete", "algorithm_version": f"fusion:{journal_fusion}"}) + "\n",
        encoding="utf-8",
    )


def _tree_fingerprint(root: Path) -> tuple[tuple[str, ...], tuple[tuple[str, int, int], ...]]:
    """Directory listing plus every file's size and modification time."""

    listing = tuple(
        sorted(
            f"{'d' if path.is_dir() else 'f'}:{path.relative_to(root).as_posix()}"
            for path in root.rglob("*")
        )
    )
    files = tuple(
        sorted(
            (path.relative_to(root).as_posix(), path.stat().st_size, path.stat().st_mtime_ns)
            for path in root.rglob("*")
            if path.is_file()
        )
    )
    return listing, files


def test_run_list_and_scorer_never_write_the_measured_work_root(tmp_path: Path) -> None:
    """A stale derived index is rebuilt in memory; generating and scoring a list touch nothing."""

    from scripts.make_run_list import main as make_run_list

    corpus = tmp_path / "corpus"
    (corpus / "mini-a").mkdir(parents=True)
    shutil.copy(FIXTURE / "mini-a" / "ground_truth.json", corpus / "mini-a/ground_truth.json")
    truth = json.loads((corpus / "mini-a/ground_truth.json").read_text("utf-8"))
    work = tmp_path / "work"
    _cached_mix(work / "source" / truth["source"]["media_key"], truth, journal_fusion=1)
    # This deliberately cannot describe the fixture.  Measurement must rebuild the map in memory,
    # never repair, replace or even touch the owner's derived index.
    (work / "index.json").write_text("{}\n", encoding="utf-8")
    before = _tree_fingerprint(work)

    run_list = tmp_path / "runs.json"
    score = tmp_path / "score.json"
    assert (
        make_run_list(["--corpus", str(corpus), "--work-root", str(work), "--out", str(run_list)])
        == 0
    )
    assert _tree_fingerprint(work) == before
    assert main(["--run-list", str(run_list), "--out", str(score)]) == 0
    assert _tree_fingerprint(work) == before


def test_the_run_list_names_each_mixs_current_result_and_its_fusion_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from id_detector.contracts import IdentitiesRecord, SourceRecord
    from id_detector.present.bundles import freeze_refused_run, publish_result
    from id_detector.providers.base import AppConfig
    from scripts.make_run_list import main as make_run_list

    corpus = tmp_path / "corpus"
    truths = {}
    for name in ("mini-a", "mini-b"):
        (corpus / name).mkdir(parents=True)
        shutil.copy(FIXTURE / name / "ground_truth.json", corpus / name / "ground_truth.json")
        truths[name] = json.loads((FIXTURE / name / "ground_truth.json").read_text("utf-8"))
    work = tmp_path / "work"
    # mini-a: re-fused offline (fusion:5) and published; its flat file is still fusion:1.  Its
    # directory is not named by its media key, so it is found by exact decoded duration.
    first = work / "s" / "m1"
    _cached_mix(first, truths["mini-a"], journal_fusion=1)
    episodes = EpisodesFile.model_validate_json(
        (FIXTURE / "mini-a" / "fuse" / "episodes.json").read_text("utf-8")
    )
    identities = IdentitiesRecord.model_validate_json(
        (FIXTURE / "mini-a" / "fuse" / "identities.gen0.json").read_text("utf-8")
    )
    run_id = "refuse0005-" + "a" * 32
    freeze_refused_run(
        first,
        run_id,
        episodes=episodes,
        identities=identities,
        provenance={"fusion_version": 5},
        carried={},
    )
    publish_result(
        media_dir=first,
        source=SourceRecord.model_validate_json((first / "ingest/source.json").read_text("utf-8")),
        episodes=episodes,
        identities=identities,
        duration_ms=truths["mini-a"]["source"]["duration_ms"],
        metadata={
            "run_id": run_id,
            "status": "complete",
            "achieved": "free",
            "compatibility": {"recipe_name": "free", "algorithm_version": "fusion:5"},
            "refusion": {"source_run_id": "x", "source_bundle": None, "fusion_version": 5},
        },
        config=AppConfig(),
    )
    # mini-b: a pre-bundle mix, found by its media key; its flat file IS its result (fusion:1).
    second = work / "s" / truths["mini-b"]["source"]["media_key"]
    _cached_mix(second, truths["mini-b"], journal_fusion=1)

    out = tmp_path / "runs.json"
    assert (
        make_run_list(["--corpus", str(corpus), "--work-root", str(work), "--out", str(out)]) == 0
    )
    printed = capsys.readouterr().out
    assert "mini-a: media b044c18d4700 (by duration); bundle result, fusion:5" in printed
    assert "mini-b: media c0a63ccabc42 (by media key); pre-bundle result, fusion:1" in printed
    assert "WARNING — these results were NOT all decided by one known fusion version" in printed
    runs = {run["mix_id"]: run for run in json.loads(out.read_text("utf-8"))["runs"]}
    frozen = (first / "fuse" / "runs" / run_id).resolve()
    assert Path(runs["mini-a"]["episodes"]) == frozen / "episodes.json"
    assert Path(runs["mini-a"]["identities"]) == frozen / "presentation-identities.json"
    assert runs["mini-a"]["fusion_version"] == 5
    assert Path(runs["mini-b"]["episodes"]) == (second / "fuse" / "episodes.json").resolve()
    assert runs["mini-b"]["fusion_version"] == 1
    # ... and the scorer refuses to pool the two.
    assert main(["--run-list", str(out), "--print"]) == 1
    assert "fusion:1 (mini-b), fusion:5 (mini-a)" in capsys.readouterr().err
    # Two mixes with the truth's exact duration and neither named by the key: no guess.
    twin = work / "s" / "m2"
    _cached_mix(twin, truths["mini-a"], journal_fusion=1)
    assert (
        make_run_list(["--corpus", str(corpus), "--work-root", str(work), "--out", str(out)]) == 1
    )
    assert "2 have its exact decoded duration" in capsys.readouterr().err


# --------------------------------------------------------------------------------------------------
# Fix pass 1
# --------------------------------------------------------------------------------------------------
def test_the_scorer_credits_no_one_word_stem_or_phonetic_title() -> None:
    truth = [TruthWork(artist="Artist", title="Dream"), TruthWork(artist="Artist", title="Nite")]
    listed = [
        ListedWork("e1", "Artist", "Dreams", "possible", "w1"),
        ListedWork("e2", "Artist", "Night", "possible", "w2"),
    ]
    assert match_works(truth, listed).assignments == {}
    # A two-word title still tolerates the changed ending (the genuine "Feel Emotion" credit).
    truth = [TruthWork(artist="KETTAMA", title="Feel Emotion (UR)")]
    listed = [ListedWork("e1", "KETTAMA", "Feeling Emotions (Extended mix)", "possible", "w1")]
    assert match_works(truth, listed).tolerances == {0: ("stem",)}


def test_a_replay_of_a_crowd_only_track_under_another_spelling_is_its_own_row() -> None:
    """The reviewer's case: the spaced Testpress spelling at 1:40, the normal one at 10:00, a
    different confident track between them — two playings, two rows (fix pass 1)."""

    duration = 1_200_000
    hints = _hints(
        [
            ("c1", "t e s t p r e s s - Forz4", 100_000),
            ("c2", "Testpress - FORZ4", 600_000),
        ],
        duration,
    )
    between = _track("Other Artist", "Some Tune", 300_000, 8, key="k-between")
    episodes, identity = _fuse(between, duration_ms=duration, hints=hints)
    rows = _crowd_rows(episodes, identity, "forz4")
    assert sorted(row["start_ms"] for row in rows) == [95_000, 595_000]
    # ... they ARE one work (the spellings were joined): only the playing differs.
    assert len({row["candidate_id"] for row in rows}) == 1
    # Without a different track between them it is one playing: one row.
    episodes, identity = _fuse([], duration_ms=duration, hints=hints)
    assert len(_crowd_rows(episodes, identity, "forz4")) == 1


def test_a_crowd_row_is_shown_under_the_fuller_tracklist_line_it_shortens() -> None:
    """The Mall Grab row at 17:15: a listener's "kettama - let me see" is shown as the tracklist's
    "KETTAMA - ID (LET ME SEE U / ROK DA HOUSE!)" — the placeholder stays display-only."""

    duration = 1_500_000
    rok = _track("KETTAMA", "Rok da House", 1_071_000, 8, key="k-rok")
    comments = [
        ("c1", "kettama - let me see", 1_035_000),
        ("c2", "KETTAMA - ID (LET ME SEE U / ROK DA HOUSE!)", 1_075_000),
    ]
    episodes, identity = _fuse(rok, duration_ms=duration, hints=_hints(comments, duration))
    (row,) = _crowd_rows(episodes, identity, "let me see")
    assert (row["artist"], row["title"]) == ("KETTAMA", "ID (LET ME SEE U / ROK DA HOUSE!)")
    (crowd_episode,) = [episode for episode in episodes.episodes if "hint_only" in episode.flags]
    assert identity_label(identity.record, crowd_episode.candidate_id) == ("kettama", "let me see")
    assert _work_of(identity, "kettama - let me see")
    assert not any(
        node.label == "KETTAMA - ID (LET ME SEE U / ROK DA HOUSE!)"
        for node in identity.record.nodes
    )
    # Two different lines could be the fuller one: no guess, the row keeps its own label.
    comments.append(("c3", "KETTAMA - ID (LET ME SEE YOU / ANOTHER ONE)", 1_400_000))
    episodes, identity = _fuse(rok, duration_ms=duration, hints=_hints(comments, duration))
    rows = _crowd_rows(episodes, identity, "let me see")
    assert ("kettama", "let me see") in {(row["artist"], row["title"]) for row in rows}


def test_a_placeholder_tracklist_label_is_display_only_for_an_independent_crowd_row() -> None:
    """The placeholder supplies spelling only: never a track, identity, backing or count."""

    duration = 600_000
    hints = _hints(
        [("comment", "artist - a", 100_000), ("tracklist", "Artist - ID (A / B)", 200_000)],
        duration,
    )
    episodes, identity = _fuse([], duration_ms=duration, hints=hints)
    (row,) = _crowd_rows(episodes, identity, "id (a / b)")
    assert (row["artist"], row["title"]) == ("Artist", "ID (A / B)")

    placeholder = next(hint for hint in hints if hint.raw_text == "Artist - ID (A / B)")
    assert placeholder.flags.id_unknown and placeholder.identity_specificity == 0
    assert identity.record.display_only_labels == ["Artist - ID (A / B)"]
    assert placeholder.id not in identity.hint_candidates
    assert placeholder.id not in identity.hint_work_ids
    assert all(placeholder.id not in episode.evidence for episode in episodes.episodes)
    assert not any(node.label == "Artist - ID (A / B)" for node in identity.record.nodes)
    assert len(identity.record.candidates) == 1


def test_a_placeholder_label_never_groups_or_scores_two_independent_crowd_rows() -> None:
    """Display may use the placeholder, but grouping/scoring keep each crowd identity distinct."""

    duration = 600_000
    hints = _hints(
        [
            ("comment-a", "artist - a", 100_000),
            ("comment-b", "artist - b", 150_000),
            ("tracklist", "Artist - ID (A / B)", 200_000),
        ],
        duration,
    )
    episodes, identity = _fuse([], duration_ms=duration, hints=hints)
    rows = [
        row
        for row in flatten_tracklist(episodes, identity.record, min_track_ms=MIN_TRACK_MS)
        if row["kind"] == "track" and row["hint_only"]
    ]
    assert len(rows) == 2
    assert {(row["artist"], row["title"]) for row in rows} == {
        ("Artist", "ID (A / B)"),
    }

    listed = listed_works_from_episodes(episodes, identity.record)
    assert {(work.artist, work.title) for work in listed} == {("artist", "a"), ("artist", "b")}
    match = match_works(
        [TruthWork(artist="artist", title="a"), TruthWork(artist="artist", title="b")], listed
    )
    assert match.assignments == {0: 0, 1: 1}


def test_a_run_list_fusion_version_that_contradicts_the_result_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    truth = json.loads((FIXTURE / "mini-a" / "ground_truth.json").read_text("utf-8"))
    media = tmp_path / "w" / "s" / "m"
    _cached_mix(media, truth, journal_fusion=1)  # the flat result says fusion:1
    run = {
        "mix_id": "mini-a",
        "truth": str(FIXTURE / "mini-a" / "ground_truth.json"),
        "episodes": str(media / "fuse" / "episodes.json"),
        "media_key": truth["source"]["media_key"],
    }
    run_list = tmp_path / "runs.json"
    run_list.write_text(
        json.dumps({"recipe": "free", "runs": [{**run, "fusion_version": 5}]}), encoding="utf-8"
    )
    assert main(["--run-list", str(run_list), "--print"]) == 1
    assert "the run list says fusion:5 but the result's own provenance says fusion:1" in (
        capsys.readouterr().err
    )
    # Agreeing (or leaving it to the provenance) scores normally, and states fusion:1.
    run_list.write_text(
        json.dumps({"recipe": "free", "runs": [{**run, "fusion_version": 1}]}), encoding="utf-8"
    )
    assert main(["--run-list", str(run_list), "--print"]) == 0
    assert capsys.readouterr().out.startswith("Every run was decided by fusion:1.")


def test_a_replay_150_seconds_later_around_a_different_track_is_its_own_row() -> None:
    """Fix pass 2: the evidence of a second playing is a confident different track wholly between
    the two mentions, not a time floor — DJs bring a track back within a couple of minutes."""

    duration = 1_200_000
    hints = _hints(
        [
            ("c1", "t e s t p r e s s - Forz4", 100_000),
            ("c2", "Testpress - FORZ4", 250_000),
        ],
        duration,
    )
    between = _track("Other Artist", "Some Tune", 125_000, 8, key="k-between")
    episodes, identity = _fuse(between, duration_ms=duration, hints=hints)
    rows = _crowd_rows(episodes, identity, "forz4")
    assert sorted(row["start_ms"] for row in rows) == [95_000, 245_000]
    # The same two mentions with nothing between them stay one playing.
    episodes, identity = _fuse([], duration_ms=duration, hints=hints)
    assert len(_crowd_rows(episodes, identity, "forz4")) == 1
