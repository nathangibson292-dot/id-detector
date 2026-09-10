from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from id_detector.contracts import GroundTruthRecord
from id_detector.io import atomic_write_json
from id_detector.truth import (
    freeze_truth,
    resolve_truth,
    second_pass_truth,
    seed_truth,
    verify_truth,
)


def _script(values: list[str]) -> tuple[callable, list[str]]:
    iterator: Iterator[str] = iter(values)
    prompts: list[str] = []

    def scripted(prompt: str) -> str:
        prompts.append(prompt)
        return next(iterator)

    return scripted, prompts


def test_seed_verify_second_pass_freeze_state_machine(tmp_path: Path) -> None:
    project = tmp_path / "project"
    truth_dir = tmp_path / "truth" / "set-one"
    truth_dir.mkdir(parents=True)
    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text(
        "00:10 Artist One - Track One\n01:00 Artist Two - Track Two\n", encoding="utf-8"
    )
    truth_path = truth_dir / "ground_truth.json"
    seeded = seed_truth(
        out_path=truth_path,
        set_id="set-one",
        duration_ms=120_000,
        media_key="a" * 64,
        tracklist=tracklist,
        split="test",
        source_url="source-location",
        uploader="local-uploader-reference",
        project_root=project,
    )
    assert len(seeded.episodes) == 2
    assert all(episode.draft for episode in seeded.episodes)
    assert all(episode.verified_against is None for episode in seeded.episodes)
    assert all(not episode.version_verified for episode in seeded.episodes)
    local_links = project / "data/local/source_links.json"
    assert local_links.exists()
    assert "source-location" in local_links.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="still draft"):
        freeze_truth(truth_dir.parent, corpus_version="frozen-v1", out_path=tmp_path / "bad.json")

    first_payload = seeded.model_dump(mode="json")
    for episode, start, end, evidence in zip(
        first_payload["episodes"],
        ([9_000, 11_000], [59_000, 61_000]),
        ([59_000, 61_000], [119_000, 120_000]),
        ("source_recording", "authoritative_metadata"),
        strict=True,
    ):
        episode.update(
            {
                "version": {"qualifier": "Exact", "ids": {"isrc": episode["work"]["title"]}},
                "version_verified": True,
                "verified_against": evidence,
                "start_ms_range": start,
                "end_ms_range": end,
                "role_segments": [{"from_ms": start[0], "to_ms": end[1], "role": "dominant"}],
                "draft": False,
            }
        )
    first_annotation = tmp_path / "first.json"
    atomic_write_json(first_annotation, first_payload)
    verified = verify_truth(
        truth_path,
        annotator_ref="annotator-first",
        annotation_path=first_annotation,
    )
    assert all(not episode.draft for episode in verified.episodes)
    assert all(episode.annotator_ref == "annotator-first" for episode in verified.episodes)
    with pytest.raises(ValueError, match="blind second pass"):
        freeze_truth(truth_dir.parent, corpus_version="frozen-v1", out_path=tmp_path / "bad.json")

    second_payload = json.loads(json.dumps(first_payload))
    second_payload["episodes"][1].update(
        {
            "start_ms_range": [60_000, 62_000],
            "end_ms_range": [118_000, 120_000],
            "role_segments": [{"from_ms": 60_000, "to_ms": 120_000, "role": "dominant"}],
        }
    )
    second_annotation = tmp_path / "second.json"
    atomic_write_json(second_annotation, second_payload)
    with pytest.raises(ValueError, match="must differ"):
        second_pass_truth(
            truth_path,
            annotator_ref="annotator-first",
            annotation_path=second_annotation,
        )
    second = second_pass_truth(
        truth_path,
        annotator_ref="annotator-second",
        annotation_path=second_annotation,
    )
    assert second.episodes[0].disagreement_resolution == "unresolved:third-annotator-required"
    assert second.episodes[1].start_ms_range == (59_000, 61_000)
    assert (truth_dir / "annotation-first.json").is_file()
    assert (truth_dir / "annotation-second.json").is_file()
    with pytest.raises(ValueError, match="unresolved disagreement"):
        freeze_truth(truth_dir.parent, corpus_version="frozen-v1", out_path=tmp_path / "bad.json")

    with pytest.raises(ValueError, match="third, distinct"):
        resolve_truth(
            truth_path,
            resolver_ref="annotator-second",
            annotation_path=second_annotation,
        )
    resolved = resolve_truth(
        truth_path,
        resolver_ref="annotator-third",
        annotation_path=second_annotation,
    )
    assert resolved.episodes[1].start_ms_range == (60_000, 62_000)
    assert all(
        episode.disagreement_resolution == "resolved-by:annotator-third"
        for episode in resolved.episodes
    )

    manifest_path = tmp_path / "corpus-version.json"
    manifest = freeze_truth(truth_dir.parent, corpus_version="frozen-v1", out_path=manifest_path)
    assert manifest["corpus_version"] == "frozen-v1"
    assert manifest["sets"][0]["path"] == "set-one/ground_truth.json"
    assert len(manifest["sets"][0]["sha256"]) == 64
    assert all(
        len(manifest["sets"][0]["annotation_passes"][name]) == 64
        for name in ("first", "second", "resolution")
    )
    frozen = GroundTruthRecord.model_validate_json(truth_path.read_text(encoding="utf-8"))
    assert frozen.corpus_version == "frozen-v1"
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest


def test_seed_combines_hints_and_manual_tracklist_and_marks_repeat(tmp_path: Path) -> None:
    hints = tmp_path / "hints.jsonl"
    hints.write_text(
        json.dumps(
            {
                "artist": "Artist",
                "title": "Repeat",
                "version_qualifier": None,
                "position_range_ms": [1_000, 2_000],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text("00:20 Artist - Repeat\n", encoding="utf-8")
    truth = seed_truth(
        out_path=tmp_path / "ground_truth.json",
        set_id="repeat-set",
        duration_ms=40_000,
        media_key="b" * 64,
        hints=hints,
        tracklist=tracklist,
    )
    assert [episode.occurrence_index for episode in truth.episodes] == [0, 1]


def test_mixed_timed_and_untimed_seed_requires_explicit_cues(tmp_path: Path) -> None:
    hints = tmp_path / "hints.json"
    hints.write_text(
        json.dumps({"artist": "Timed", "title": "Track", "position_range_ms": [30_000, 31_000]}),
        encoding="utf-8",
    )
    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text("Untimed - Track\n", encoding="utf-8")
    with pytest.raises(ValueError, match="explicit cue for every entry"):
        seed_truth(
            out_path=tmp_path / "ground_truth.json",
            set_id="mixed",
            duration_ms=60_000,
            media_key="c" * 64,
            hints=hints,
            tracklist=tracklist,
        )


def test_first_pass_can_replace_seed_with_full_timeline_annotation(tmp_path: Path) -> None:
    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text("Seed Artist - Seed Track\n", encoding="utf-8")
    truth_path = tmp_path / "ground_truth.json"
    seeded = seed_truth(
        out_path=truth_path,
        set_id="editable",
        duration_ms=40_000,
        media_key="d" * 64,
        tracklist=tracklist,
    )
    payload = seeded.model_dump(mode="json")
    common = {
        "version_verified": True,
        "verified_against": "source_recording",
        "audible_rule": "manual full annotation",
        "occurrence_index": 0,
        "in_reference_pool": True,
        "annotator_ref": None,
        "second_pass_ref": None,
        "disagreement_resolution": None,
        "note": None,
        "draft": False,
    }
    payload["episodes"] = [
        {
            **common,
            "work": {"artist": "Corrected Artist", "title": "Corrected Track"},
            "version": {"qualifier": "Club Mix", "ids": {"isrc": "EDIT-ONE"}},
            "start_ms_range": [0, 1_000],
            "end_ms_range": [24_000, 25_000],
            "role_segments": [
                {"from_ms": 0, "to_ms": 10_000, "role": "dominant"},
                {"from_ms": 10_000, "to_ms": 25_000, "role": "outgoing"},
            ],
            "overlaps_with": [1],
        },
        {
            **common,
            "work": {"artist": "Missed Artist", "title": "Added Track"},
            "version": {"qualifier": "Original", "ids": {"mb_recording": "EDIT-TWO"}},
            "start_ms_range": [10_000, 11_000],
            "end_ms_range": [34_000, 35_000],
            "role_segments": [
                {"from_ms": 10_000, "to_ms": 20_000, "role": "incoming"},
                {"from_ms": 20_000, "to_ms": 35_000, "role": "dominant"},
            ],
            "overlaps_with": [0],
        },
    ]
    payload["regions"] = [{"start_ms": 35_000, "end_ms": 40_000, "type": "unresolved"}]
    annotation = tmp_path / "full-first-pass.json"
    atomic_write_json(annotation, payload)
    verified = verify_truth(
        truth_path,
        annotator_ref="first-independent",
        annotation_path=annotation,
    )
    assert [episode.work.title for episode in verified.episodes] == [
        "Corrected Track",
        "Added Track",
    ]
    assert verified.episodes[0].version.ids == {"isrc": "EDIT-ONE"}
    assert [segment.role for segment in verified.episodes[0].role_segments] == [
        "dominant",
        "outgoing",
    ]
    assert verified.episodes[0].overlaps_with == [1]
    assert verified.regions[0].type == "unresolved"


def test_work_only_truth_can_freeze_without_exact_version_evidence(tmp_path: Path) -> None:
    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text("Work Artist - Work Only\n", encoding="utf-8")
    truth_path = tmp_path / "set" / "ground_truth.json"
    seeded = seed_truth(
        out_path=truth_path,
        set_id="work-only",
        duration_ms=30_000,
        media_key="e" * 64,
        tracklist=tracklist,
    )
    payload = seeded.model_dump(mode="json")
    episode = payload["episodes"][0]
    episode.update(
        {
            "verified_against": "audio",
            "version_verified": False,
            "start_ms_range": [0, 1_000],
            "end_ms_range": [29_000, 30_000],
            "role_segments": [{"from_ms": 0, "to_ms": 30_000, "role": "dominant"}],
            "draft": False,
        }
    )
    annotation = tmp_path / "work-only.json"
    atomic_write_json(annotation, payload)
    verify_truth(truth_path, annotator_ref="work-reviewer", annotation_path=annotation)
    manifest = freeze_truth(
        truth_path.parent,
        corpus_version="work-only-v1",
        out_path=tmp_path / "work-only-manifest.json",
    )
    assert manifest["sets"][0]["set_id"] == "work-only"


def _valid_timeline_payload() -> dict:
    return {
        "schema_version": "1.0.0",
        "generated_by": "timeline-vector",
        "set_id": "timeline",
        "source": {
            "url_ref": "source",
            "media_key": "f" * 64,
            "duration_ms": 20_000,
            "platform": "file",
            "uploader_ref": "uploader",
            "event_ref": None,
            "date": None,
        },
        "stratum": "controlled",
        "split": "controlled",
        "corpus_version": "timeline-v1",
        "selection_basis": "semantic vector",
        "episodes": [
            {
                "work": {"artist": "Artist", "title": "One"},
                "version": {"qualifier": None, "ids": {}},
                "version_verified": False,
                "verified_against": "audio",
                "start_ms_range": [1_000, 2_000],
                "end_ms_range": [9_000, 10_000],
                "audible_rule": "vector",
                "role_segments": [{"from_ms": 1_000, "to_ms": 10_000, "role": "dominant"}],
                "overlaps_with": [],
                "occurrence_index": 0,
                "in_reference_pool": False,
                "annotator_ref": "a",
                "second_pass_ref": None,
                "disagreement_resolution": None,
                "note": None,
                "draft": False,
            }
        ],
        "events": [],
        "regions": [{"start_ms": 10_000, "end_ms": 20_000, "type": "unresolved"}],
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["episodes"][0].update(start_ms_range=[2_000, 1_000]), "ordered"),
        (lambda value: value["episodes"][0].update(end_ms_range=[21_000, 22_000]), "duration"),
        (lambda value: value["episodes"][0].update(end_ms_range=[1_500, 3_000]), "cross"),
        (
            lambda value: value["episodes"][0].update(
                role_segments=[{"from_ms": 0, "to_ms": 10_000, "role": "dominant"}]
            ),
            "audible span",
        ),
        (lambda value: value["episodes"][0].update(overlaps_with=[0]), "overlap"),
        (lambda value: value["regions"][0].update(start_ms=20_000, end_ms=10_000), "duration"),
    ],
)
def test_ground_truth_rejects_impossible_timelines(mutation: callable, message: str) -> None:
    payload = _valid_timeline_payload()
    mutation(payload)
    with pytest.raises(ValueError, match=message):
        GroundTruthRecord.model_validate(payload)


def test_ground_truth_requires_symmetric_overlaps_and_unique_occurrences() -> None:
    payload = _valid_timeline_payload()
    second = json.loads(json.dumps(payload["episodes"][0]))
    second["work"]["title"] = "Two"
    second["start_ms_range"] = [5_000, 6_000]
    second["end_ms_range"] = [14_000, 15_000]
    second["role_segments"] = [{"from_ms": 5_000, "to_ms": 15_000, "role": "incoming"}]
    payload["episodes"][0]["overlaps_with"] = [1]
    payload["episodes"].append(second)
    with pytest.raises(ValueError, match="symmetric"):
        GroundTruthRecord.model_validate(payload)

    duplicate = _valid_timeline_payload()
    duplicate["episodes"].append(json.loads(json.dumps(duplicate["episodes"][0])))
    with pytest.raises(ValueError, match="occurrence_index"):
        GroundTruthRecord.model_validate(duplicate)


# --- overlays (scorer part iii): a second track blended in at a row's time -----------------------


def _seed_with_overlays(tmp_path: Path, tracklist_text: str, overlays_text: str, **kwargs):
    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text(tracklist_text, encoding="utf-8")
    overlays = tmp_path / "overlays.txt"
    overlays.write_text(overlays_text, encoding="utf-8")
    return seed_truth(
        out_path=tmp_path / "ground_truth.json",
        set_id="overlay-set",
        duration_ms=kwargs.pop("duration_ms", 180_000),
        media_key="d" * 64,
        tracklist=tracklist,
        overlays=overlays,
        project_root=tmp_path,
        **kwargs,
    )


def test_seed_overlays_become_layered_episodes_linked_both_ways(tmp_path: Path) -> None:
    """Each overlay line ("H:MM:SS - Artist - Title (w/ overlay)", the suffix optional) becomes an
    episode that starts at its time and ends where the row it plays over ends, marked as a layer
    and linked to that row in both directions; it sits right after that row in the file."""

    truth = _seed_with_overlays(
        tmp_path,
        "# main list, bulleted after the time as comment tracklists are\n"
        "0:00:10 - Artist A - One\n"
        "0:01:00 - Artist B - Two\n"
        "0:02:00 - Artist C - Three\n",
        "# overlays for the set\n"
        "0:01:00 - Artist D - Four (w/ overlay)\n"
        "0:02:30 - Artist E - Five\n"
        "0:02:30 - Artist E - Five (W/ Overlay)\n",
    )
    reloaded = GroundTruthRecord.model_validate_json(
        (tmp_path / "ground_truth.json").read_text(encoding="utf-8")
    )
    assert reloaded == truth
    assert [(episode.work.artist, episode.work.title) for episode in truth.episodes] == [
        ("Artist A", "One"),
        ("Artist B", "Two"),
        ("Artist D", "Four"),
        ("Artist C", "Three"),
        ("Artist E", "Five"),
    ]
    four, five = truth.episodes[2], truth.episodes[4]
    assert (tuple(four.start_ms_range), tuple(four.end_ms_range)) == (
        (60_000, 60_000),
        (120_000, 120_000),
    )
    assert [(segment.from_ms, segment.to_ms, segment.role) for segment in four.role_segments] == [
        (60_000, 120_000, "layer")
    ]
    assert four.overlaps_with == [1] and truth.episodes[1].overlaps_with == [2]
    # An overlay after the last row's start ends where the mix ends.
    assert (tuple(five.start_ms_range), tuple(five.end_ms_range)) == (
        (150_000, 150_000),
        (180_000, 180_000),
    )
    assert five.overlaps_with == [3] and truth.episodes[3].overlaps_with == [4]
    assert five.note is not None and "overlay" in five.note
    # Main rows are untouched by the overlays: still uncertain, still ending at the next row.
    assert [segment.role for segment in truth.episodes[1].role_segments] == ["uncertain"]
    assert tuple(truth.episodes[1].end_ms_range) == (120_000, 120_000)
    assert all(
        episode.draft and episode.audible_rule == "manual annotation required"
        for episode in truth.episodes
    )
    assert [episode.occurrence_index for episode in truth.episodes] == [0, 0, 0, 0, 0]


def test_seed_overlay_of_a_repeated_work_counts_its_occurrence(tmp_path: Path) -> None:
    truth = _seed_with_overlays(
        tmp_path,
        "0:00:00 Artist A - One\n0:01:00 Artist B - Two\n",
        "0:01:00 - Artist A - One (w/ overlay)\n",
    )
    assert [episode.occurrence_index for episode in truth.episodes] == [0, 0, 1]
    assert truth.episodes[2].overlaps_with == [1]


@pytest.mark.parametrize(
    ("tracklist_text", "overlays_text", "message"),
    [
        (
            "0:00:10 Artist A - One\n0:01:00 Artist B - Two\n",
            "Artist D - Four (w/ overlay)\n",
            "needs a timestamp",
        ),
        (
            "0:00:10 Artist A - One\n0:01:00 Artist B - Two\n",
            "0:00:05 - Artist D - Four\n",
            "precedes the first tracklist row",
        ),
        (
            "0:00:10 Artist A - One\n0:01:00 Artist B - Two\n",
            "0:03:00 - Artist D - Four\n",
            "at or beyond the end of the mix",
        ),
        (
            "Artist A - One\nArtist B - Two\n",
            "0:01:00 - Artist D - Four\n",
            "overlays need a timestamped tracklist",
        ),
        (
            "0:00:10 Artist A - One\n",
            "0:00:30 no separator here\n",
            "invalid overlays line 1",
        ),
    ],
    ids=["untimed-overlay", "before-first-row", "past-the-end", "order-only-main", "malformed"],
)
def test_seed_overlays_reject_what_cannot_be_placed(
    tmp_path: Path, tracklist_text: str, overlays_text: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _seed_with_overlays(tmp_path, tracklist_text, overlays_text)
    assert not (tmp_path / "ground_truth.json").exists()


def test_seed_strips_a_leading_bullet_from_a_timed_tracklist_row(tmp_path: Path) -> None:
    """ "0:17:09 - Royal-T - Tokyo Dub": the bullet after the time is list furniture, not the first
    character of the artist (the Mall Grab draft was seeded as "- Mall Grab")."""

    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text(
        "0:00:00 - Artist A - One\n0:17:09 – Royal-T - Tokyo Dub\n- Artist C - Three\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="explicit cue"):
        seed_truth(
            out_path=tmp_path / "ground_truth.json",
            set_id="bullets",
            duration_ms=3_600_000,
            media_key="e" * 64,
            tracklist=tracklist,
            project_root=tmp_path,
        )
    tracklist.write_text(
        "0:00:00 - Artist A - One\n0:17:09 – Royal-T - Tokyo Dub\n", encoding="utf-8"
    )
    truth = seed_truth(
        out_path=tmp_path / "ground_truth.json",
        set_id="bullets",
        duration_ms=3_600_000,
        media_key="e" * 64,
        tracklist=tracklist,
        project_root=tmp_path,
    )
    assert [(episode.work.artist, episode.work.title) for episode in truth.episodes] == [
        ("Artist A", "One"),
        ("Royal-T", "Tokyo Dub"),
    ]
    assert tuple(truth.episodes[1].start_ms_range) == (1_029_000, 1_029_000)


def test_truth_seed_cli_exposes_overlays() -> None:
    from typer.testing import CliRunner

    from id_detector.cli import app

    result = CliRunner().invoke(app, ["truth", "seed", "--help"])
    assert result.exit_code == 0
    assert "--overlays" in result.stdout


def test_seed_overlays_of_one_row_are_ordered_by_time_whatever_the_file_says(
    tmp_path: Path,
) -> None:
    """Two overlays over the same row: the episode list stays in mix order however the overlays
    file was written, so a truth is never handed to the scorer out of chronological order."""

    truth = _seed_with_overlays(
        tmp_path,
        "0:00:00 - Artist A - One\n0:02:00 - Artist B - Two\n",
        "0:01:30 - Artist D - Late (w/ overlay)\n0:00:30 - Artist C - Early (w/ overlay)\n",
    )
    assert [episode.work.title for episode in truth.episodes] == [
        "One",
        "Early",
        "Late",
        "Two",
    ]
    assert [episode.start_ms_range[0] for episode in truth.episodes] == [0, 30_000, 90_000, 120_000]
    assert truth.episodes[0].overlaps_with == [1, 2]
    assert truth.episodes[1].overlaps_with == [0] and truth.episodes[2].overlaps_with == [0]
    # Same file, already in order: the same truth, so a sorted file seeds byte-identically.
    sorted_dir = tmp_path / "sorted"
    sorted_dir.mkdir()
    in_order = _seed_with_overlays(
        sorted_dir,
        "0:00:00 - Artist A - One\n0:02:00 - Artist B - Two\n",
        "0:00:30 - Artist C - Early (w/ overlay)\n0:01:30 - Artist D - Late (w/ overlay)\n",
    )
    assert in_order.episodes == truth.episodes
