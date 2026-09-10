from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from id_detector.hints.parse import (
    HintInput,
    is_track_question,
    parse_hint_inputs,
    parse_hint_timestamp,
    parse_text_units,
)

FIXTURE = Path("data/fixtures/hints/synthetic/parsing_traps.json")
DURATION_MS = 8_000_000
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _units(case: dict[str, object]):
    return parse_text_units(
        str(case["text"]),
        media_duration_ms=DURATION_MS,
        comment_timestamp_ms=case.get("timestamp_ms")
        if isinstance(case.get("timestamp_ms"), int)
        else None,
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case["name"]))
def test_every_synthetic_parsing_trap(case: dict[str, object]) -> None:
    name = str(case["name"])
    text = str(case["text"])
    units = _units(case)
    if "expected_ms" in case:
        assert parse_hint_timestamp(text, media_duration_ms=DURATION_MS) == case["expected_ms"]
    if "expected_range_ms" in case:
        assert units[0].position_range_ms == tuple(case["expected_range_ms"])

    expected = case.get("expected")
    if isinstance(expected, bool):
        if name == "compatible_correction_time":
            assert bool(units) is expected
            assert units[0].kind == "correction"
        else:
            assert is_track_question(text) is expected
    elif expected is None and "expected" in case:
        assert parse_hint_timestamp(text, media_duration_ms=DURATION_MS) is None
    elif expected == "tracklist_line":
        assert units[0].kind == "tracklist_line"
    elif expected == "work_only_unsplit":
        assert units[0].artist is None
        assert units[0].parse_confidence <= 3_000
    elif expected == "block_delimiter":
        assert len(units) == 3
        assert all(unit.parse_confidence == 5_000 and unit.artist for unit in units)
    elif expected == "unknown_boundary":
        assert units[0].flags.id_unknown
        assert units[0].position_range_ms is not None
    elif expected == "three_lines":
        assert len(units) == 3
    elif expected == "standalone_answer":
        assert units[0].kind == "answer"
        assert units[0].artist == "Example Artist"
    elif expected == "contested_relation":
        assert units[0].kind == "correction"
        assert units[0].artist == "Example Artist"
    elif expected == "separator_priority":
        assert len(units) == 6
        assert [(unit.artist, unit.title) for unit in units] == [("A", "B")] * 6
    if name == "minute_only_cue":
        assert units[0].position_kind == "cue_minute"
    if name in {"malformed_component", "four_component_timestamp"}:
        assert not units


def test_no_space_hyphen_block_with_heading_uses_consistent_delimiter() -> None:
    with_heading = parse_text_units(
        "Tracklist so far\n00:00 ArtistA-TitleA\n05:00 ArtistB-TitleB\n10:00 ArtistC-TitleC",
        media_duration_ms=DURATION_MS,
    )
    assert len(with_heading) == 3
    assert all(unit.parse_confidence == 5_000 for unit in with_heading)


def test_invalid_multiline_tracklist_block_is_not_accepted() -> None:
    units = parse_text_units(
        "10:00 Artist - Later\n05:00 Artist - Earlier", media_duration_ms=DURATION_MS
    )
    assert not any(unit.kind == "tracklist_line" for unit in units)


def test_flags_title_first_answers_and_specificity() -> None:
    units = parse_text_units(
        "Track ID: Signal Path (Artist Remix) by Example Artist [Label]\n"
        "00:00 A w/ B - Work vs. Other (VIP)\n"
        "05:00 ID - ID (Unreleased*)",
        media_duration_ms=DURATION_MS,
    )
    answer = units[0]
    assert (answer.artist, answer.title) == ("Example Artist", "Signal Path")
    assert answer.label == "Label"
    assert answer.version_qualifier == "Artist Remix"
    assert answer.identity_specificity == 10_000
    assert units[1].flags.mashup_with and units[1].flags.edit
    assert units[2].flags.id_unknown and units[2].flags.unreleased


def test_derived_hint_fixture_corpus_is_safe_and_sensible() -> None:
    inputs: list[HintInput] = []
    expected_questions = 0
    for path in sorted(Path("data/fixtures/hints/derived").rglob("*.jsonl")):
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if not line:
                continue
            item = json.loads(line)
            expected_questions += item["category"] == "id_question" and is_track_question(
                item["text"]
            )
            inputs.append(
                HintInput(
                    connector=path.parent.name,
                    source_record_id=f"{path.stem}-{index}",
                    text=item["text"],
                    position_ms=item["position_ms"],
                    position_kind="comment_timestamp",
                    author_pseudo_id=item["author"],
                )
            )
    hints = parse_hint_inputs("a" * 64, DURATION_MS, inputs)
    assert expected_questions >= 50
    assert sum(hint.kind == "question" for hint in hints) == expected_questions
    assert all(0 <= hint.parse_confidence <= 10_000 for hint in hints)


@pytest.mark.parametrize(
    "text",
    ["What mixer is this?", "How good is this?", "Anyone?", "What's this?", "Can I see your ID?"],
)
def test_negative_questions_never_classify(text: str) -> None:
    assert not is_track_question(text)


# --- crowd-label cleanup (scorer part iii) -------------------------------------------------------

CLEANUP_FIXTURE = Path("tests/fixtures/hints/crowd-label-cleanup-authored.json")
CLEANUP_CASES = json.loads(CLEANUP_FIXTURE.read_text(encoding="utf-8"))["cases"]
# Synthetic handles, built here rather than in a fixture file: no committed fixture may carry one.
HANDLE_A, HANDLE_B = "@fan_alpha_7", "@fan-beta-8"


def _labels(units) -> list[tuple[str, str | None, str | None, str | None, int | None]]:
    return [(unit.kind, unit.artist, unit.title, unit.label, unit.cue_ms) for unit in units]


@pytest.mark.parametrize("case", CLEANUP_CASES, ids=lambda case: str(case["name"]))
def test_list_furniture_headers_and_mentions_never_reach_a_label(case: dict[str, object]) -> None:
    """Enumerators ("53.", "07.", "7)"), bracket bullets ("[]", "[x]", the "[]" a bracketed cue
    leaves behind), dash bullets, "TRACK LIST"-style headers and mention prefixes are stripped
    BEFORE the "Artist - Title" split; cues, trailing labels and dotted timestamps are untouched."""

    units = parse_text_units(str(case["text"]), media_duration_ms=DURATION_MS)
    expected = [tuple(unit) for unit in case["units"]]  # type: ignore[union-attr]
    assert _labels(units) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"{HANDLE_A} {HANDLE_B}  Example Artist - Signal Path",
            [("answer", "Example Artist", "Signal Path", None, None)],
        ),
        (
            f"{HANDLE_A}: {HANDLE_B} this is Example Artist - Signal Path",
            [("answer", "Example Artist", "Signal Path", None, None)],
        ),
        (
            f"{HANDLE_A} {HANDLE_B} actually Example Artist - Alternate Title",
            [("correction", "Example Artist", "Alternate Title", None, None)],
        ),
        (
            f"Example Artist {HANDLE_A} - Signal Path [Example Label]",
            [("answer", "Example Artist", "Signal Path", "Example Label", None)],
        ),
        (
            f"22:48 {HANDLE_A} Example Artist - Signal Path",
            [("tracklist_line", "Example Artist", "Signal Path", None, 1_368_000)],
        ),
        # "Live @ Fabric" is not a handle: the sign must be glued to a name.
        ("Live @ Fabric - Signal Path", [("answer", "Live @ Fabric", "Signal Path", None, None)]),
    ],
)
def test_handles_are_scrubbed_wherever_they_sit(text: str, expected: list) -> None:
    assert _labels(parse_text_units(text, media_duration_ms=DURATION_MS)) == expected


def test_no_label_field_ever_carries_a_handle() -> None:
    """Privacy invariant: whatever a comment looks like, ``@`` followed by a name is not emitted in
    artist, title, version qualifier or label — as a parsed unit or as a hint record."""

    texts = [str(case["text"]) for case in CLEANUP_CASES] + [
        f"{HANDLE_A} {HANDLE_B}  Example Artist - Signal Path",
        f"Signal Path by {HANDLE_A} :)",
        f"Signal Path (Remix by {HANDLE_A}) - Example Artist [{HANDLE_B}]",
        f"track id: {HANDLE_A} Example Artist - Signal Path",
        f"{HANDLE_A}",
    ]
    handle = re.compile(r"@\w")
    for text in texts:
        for unit in parse_text_units(text, media_duration_ms=DURATION_MS):
            for value in (unit.artist, unit.title, unit.version_qualifier, unit.label):
                assert value is None or not handle.search(value), (text, value)
    records = parse_hint_inputs(
        "a" * 64,
        DURATION_MS,
        [
            HintInput(
                connector="test",
                source_record_id=f"record-{index}",
                text=text,
                position_ms=60_000,
                position_kind="comment_timestamp",
            )
            for index, text in enumerate(texts)
        ],
    )
    assert records, "the handle-led answers must still be parsed, just cleaned"
    for record in records:
        for value in (record.artist, record.title, record.version_qualifier, record.label):
            assert value is None or not handle.search(value), (record.raw_text, value)
    assert any(record.artist == "Example Artist" for record in records)


def test_the_cleanup_fixture_itself_carries_no_handle() -> None:
    assert not re.search(r"@\w", CLEANUP_FIXTURE.read_text(encoding="utf-8"))


# --- review + fix pass (scorer part iii): what the cleanup must NOT touch ------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A real artist whose name STARTS with a digit is not an enumerator: the enumerator needs
        # its "." or ")" ("53. ...", "7) ..."), so "2 Bad Mice" and "4 Hero" survive whole.
        ("2 Bad Mice - Bombscare", [("answer", "2 Bad Mice", "Bombscare", None, None)]),
        ("4 Hero - Mr Kirk's Nightmare", [("answer", "4 Hero", "Mr Kirk's Nightmare", None, None)]),
        # ... and one whose name starts with a dash is not a bullet: a bullet is followed by a
        # space ("- Mall Grab - Winter"), a name is not.
        ("-Ziq - Hasty Boom Alert", [("answer", "-Ziq", "Hasty Boom Alert", None, None)]),
        # (NFKC folds the micro sign to a mu before any of this runs; the dash still survives.)
        ("\u00b5-Ziq - Tango N' Vectif", [("answer", "\u03bc-Ziq", "Tango N' Vectif", None, None)]),
        # The bullet in front of a digit-led artist still goes.
        ("- 2 Bad Mice - Bombscare", [("answer", "2 Bad Mice", "Bombscare", None, None)]),
        ("1. 2 Bad Mice - Bombscare", [("answer", "2 Bad Mice", "Bombscare", None, None)]),
    ],
)
def test_an_artist_that_looks_like_list_furniture_is_never_stripped(
    text: str, expected: list
) -> None:
    assert _labels(parse_text_units(text, media_duration_ms=DURATION_MS)) == expected


@pytest.mark.parametrize(
    "text",
    [
        "TLC - No Scrubs",
        "Tracklist:\nTLC - No Scrubs",
        "1. Example Artist - One\nTLC - No Scrubs",
    ],
)
def test_a_header_word_only_counts_when_the_word_ends_there(text: str) -> None:
    """ "TL"/"TRACK LIST" opens a header; "TLC" opens a track.  The header strip runs per line
    now, so an artist merely starting with those letters must survive anywhere in a paste."""

    units = parse_text_units(text, media_duration_ms=DURATION_MS)
    assert ("TLC", "No Scrubs") in [(unit.artist, unit.title) for unit in units]
    # The genuine headers still go.
    header = parse_text_units(
        "TL so far:\nExample Artist - Signal Path", media_duration_ms=DURATION_MS
    )
    assert _labels(header) == [("answer", "Example Artist", "Signal Path", None, None)]


@pytest.mark.parametrize(
    ("text", "artist", "title", "label", "qualifier"),
    [
        (
            f"Example Artist - Signal Path [{HANDLE_A} Records]",
            "Example Artist",
            "Signal Path",
            "Records",
            None,
        ),
        (
            f"Example Artist - Signal Path ({HANDLE_B} Remix)",
            "Example Artist",
            "Signal Path",
            None,
            "Remix",
        ),
    ],
)
def test_a_handle_inside_a_label_or_qualifier_takes_the_whole_bracket_with_it(
    text: str, artist: str, title: str, label: str | None, qualifier: str | None
) -> None:
    """Scrubbing the handle must not orphan the bracket: "[@x Records]" becomes "[Records]", which
    the label reader still recognises, so the title comes out clean rather than "Signal Path
    [ Records]"."""

    (unit,) = parse_text_units(text, media_duration_ms=DURATION_MS)
    assert (unit.artist, unit.title) == (artist, title)
    assert (unit.label, unit.version_qualifier) == (label, qualifier)
