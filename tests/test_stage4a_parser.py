from __future__ import annotations

import json
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
