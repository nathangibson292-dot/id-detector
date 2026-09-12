"""Cycle 3a-i: every presentation surface consumes one canonical projection."""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from id_detector.contracts import AcquireFile, EpisodesFile, IdentitiesRecord
from id_detector.io import atomic_write_json, read_bytes, read_text
from id_detector.playlists import PLAYLIST_CSS, PLAYLIST_JS
from id_detector.present import bundles, page, refresh
from id_detector.present.exports import CanonicalProjection, _format_time, build_projection
from id_detector.present.server import AnalysedSet, _fresh_sets, _mix_card_html, _set_summary
from id_detector.providers.base import AppConfig
from id_detector.semantics import interval_length, normalise_intervals
from tests.test_stage7_page import _source

FIXTURES = Path(__file__).parent / "fixtures" / "present"

#: Every fixture carries a hand-computed ``expected`` block per collapse mode.  The assertions below
#: compare against THAT, never against a second call into the code under test — a projection bug
#: that also corrupts the comparison value is exactly the class of defect this gate exists to catch.
FIXTURE_NAMES = ("garage", "boiler", "crowd", "cluster")
#: ``collapse=True`` is the production default (``providers.base.AppConfig.collapse``); the gate has
#: to run the code the product actually ships, not only the historical flat view.
COLLAPSE_MODES = (("collapsed", True), ("flat", False))


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _inputs(name: str) -> tuple[dict, EpisodesFile, IdentitiesRecord, AcquireFile | None]:
    fixture = _load_fixture(name)
    nodes = []
    works = []
    candidates = []
    episodes = []
    acquire_rows = []
    candidate_number = 1

    def add_identity(artist: str, title: str) -> str:
        nonlocal candidate_number
        candidate_id = f"{candidate_number:040x}"
        work_id = f"{candidate_number + 1000:040x}"
        node_id = f"text:{candidate_number}"
        candidate_number += 1
        nodes.append(
            {
                "schema_version": "1.0.0",
                "generated_by": "fixture",
                "id": node_id,
                "ns": "text",
                "label": f"{artist} - {title}",
            }
        )
        works.append(
            {
                "schema_version": "1.0.0",
                "generated_by": "fixture",
                "work_id": work_id,
                "member_nodes": [node_id],
            }
        )
        candidates.append(
            {
                "schema_version": "1.0.0",
                "generated_by": "fixture",
                "canonical_id": candidate_id,
                "work_id": work_id,
                "member_nodes": [node_id],
                "alternatives": [],
                "contested": False,
                "conflicts": [],
            }
        )
        return candidate_id

    for index, track in enumerate(fixture["tracks"], 1):
        candidate_id = add_identity(track["artist"], track["title"])
        alternatives = [
            add_identity(item["artist"], item["title"]) for item in track.get("alternatives", [])
        ]
        episode_id = f"{index + 5000:040x}"
        flags = track.get("flags", [])
        episodes.append(
            {
                "schema_version": "1.0.0",
                "generated_by": "fixture",
                "id": episode_id,
                "candidate_id": candidate_id,
                "alternatives": alternatives,
                "claim": "performed",
                "start_no_later_than_ms": track["start_ms"],
                "end_no_earlier_than_ms": track["end_ms"],
                "evidence_support_ms": [[track["start_ms"], track["end_ms"]]],
                "start_no_earlier_than_ms": None,
                "end_no_later_than_ms": None,
                "start_pi": None,
                "end_pi": None,
                "best_start_ms": track["start_ms"],
                "best_end_ms": track["end_ms"],
                "role_segments": [
                    {
                        "from_ms": track["start_ms"],
                        "to_ms": track["end_ms"],
                        "role": "dominant",
                    }
                ],
                "occurrence_index": index,
                "overlaps": [],
                "alignment_segments": [],
                "alignment_events": [],
                "has_global_alignment": True,
                "scores": {"work": 8000, "version": 5000, "boundary": 6000},
                "score_kind": "heuristic",
                "tiers": {
                    "work": track["badge"],
                    "version": "unclear",
                    "boundary": "possible",
                },
                "badge": track["badge"],
                "version_status": track.get("version_status", "unverified"),
                "evidence": [f"{index + 9000:040x}"],
                "rejected_evidence": [],
                "flags": flags,
                "rescan_state": "not_requested",
                "suppressed": track.get("suppressed"),
            }
        )
        if track.get("acquire"):
            acquire_rows.append(
                {
                    "episode_id": episode_id,
                    "candidate_id": candidate_id,
                    "artist": track["artist"],
                    "title": track["title"],
                    "version_qualifier": None,
                    "version_status": track.get("version_status", "unverified"),
                    "direct": [
                        {
                            "source": "apple",
                            "url": "https://example.invalid/buy",
                            "kind": track["acquire"],
                            "match_confidence": 9000,
                            "corroborates_version": False,
                        }
                    ],
                    "search": [],
                    "soundcloud": None,
                }
            )

    gaps = [
        {
            "schema_version": "1.0.0",
            "generated_by": "fixture",
            "id": f"{index + 8000:040x}",
            "start_ms": gap["start_ms"],
            "end_ms": gap["end_ms"],
            "bounded_by": [],
            "evidence": {
                "n_windows": 1,
                "n_no_match": 1,
                "n_error": 0,
                "n_unclear_candidates": 0,
                "n_hint_events": 0,
                "n_novelty_events": 0,
            },
            "reason": gap["reason"],
            "truncated": False,
            "best_unclear_candidate": None,
        }
        for index, gap in enumerate(fixture["gaps"], 1)
    ]
    # The fuser's duration block is a PARTITION of the media built from unions of intervals
    # (``semantics.duration_partition``: ``normalise_intervals`` then an exact-cover assertion), so
    # a fixture must be one too.  Summing raw episode lengths instead would double-count every
    # overlap — on an overlapping fixture that alone invents coverage the set does not have, and
    # the hero's honest-coverage figure is derived from exactly this number.
    total_ms = int(fixture["duration_ms"])
    evidence_ms = interval_length(
        normalise_intervals(
            [(track["start_ms"], track["end_ms"]) for track in fixture["tracks"]], total_ms
        ),
        total_ms,
    )
    no_evidence_ms = interval_length(
        normalise_intervals([(gap["start_ms"], gap["end_ms"]) for gap in gaps], total_ms), total_ms
    )
    episode_file = EpisodesFile.model_validate(
        {
            "schema_version": "1.0.0",
            "generated_by": "fixture",
            "generation": 0,
            "episodes": episodes,
            "gaps": gaps,
            "durations": {
                "schema_version": "1.0.0",
                "generated_by": "fixture",
                "evidence_supported_ms": evidence_ms,
                "predicted_episode_ms": 0,
                "unresolved_boundary_ms": 0,
                "unclear_ms": 0,
                "no_evidence_ms": no_evidence_ms,
                # Whatever neither evidence nor a gap claims (the holes between short matches).
                "unscanned_ms": total_ms - evidence_ms - no_evidence_ms,
            },
            "certification": {"profile": "free", "per": []},
        }
    )
    assert (
        sum(
            getattr(episode_file.durations, field)
            for field in (
                "evidence_supported_ms",
                "predicted_episode_ms",
                "unresolved_boundary_ms",
                "unclear_ms",
                "no_evidence_ms",
                "unscanned_ms",
            )
        )
        == total_ms
    ), "fixture durations must partition the media exactly once, as the fuser's do"
    identities = IdentitiesRecord.model_validate(
        {
            "schema_version": "1.0.0",
            "generated_by": "fixture",
            "nodes": nodes,
            "assertions": [],
            "works": works,
            "candidates": candidates,
        }
    )
    acquire = (
        AcquireFile.model_validate(
            {
                "schema_version": "1.0.0",
                "generated_by": "fixture",
                "media_key": "a" * 64,
                "generation": 0,
                "episodes": acquire_rows,
            }
        )
        if acquire_rows
        else None
    )
    return fixture, episode_file, identities, acquire


def _publish(
    tmp_path: Path, name: str, *, collapse: bool
) -> tuple[Path, CanonicalProjection, AnalysedSet, dict]:
    """Publish one fixture exactly as production does and hand back its one projection."""

    fixture, episodes, identities, acquire = _inputs(name)
    media_dir = tmp_path / (name[0] * 64) / (name[-1] * 64)
    source = _source("file").model_copy(update={"title": fixture["title"], "media_key": "a" * 64})
    atomic_write_json(media_dir / "ingest/source.json", source)
    atomic_write_json(media_dir / "fuse/episodes.json", episodes)
    atomic_write_json(media_dir / "fuse/identities.gen0.json", identities)
    config = AppConfig(present_min_track_ms=fixture["min_track_ms"], collapse=collapse)
    projection = build_projection(
        episodes,
        identities,
        acquire,
        collapse=config.collapse,
        same_track_bridge_ms=config.same_track_bridge_ms,
        min_track_ms=config.present_min_track_ms,
    )
    directory = bundles.publish_result(
        media_dir=media_dir,
        source=source,
        episodes=episodes,
        identities=identities,
        duration_ms=fixture["duration_ms"],
        metadata={"run_id": f"run-{name}-{collapse}", "status": "complete", "achieved": "free"},
        config=config,
        acquire=acquire,
    )
    item = AnalysedSet(
        source_key=source.source_key,
        media_key=source.media_key,
        media_dir=media_dir,
        title=fixture["title"],
        platform="file",
    )
    return directory, projection, item, fixture


def _expected_rows(expected: dict) -> list[tuple[str, str, bool]]:
    """The fixture's hand-written copy block, parsed into ``(time, label, from comments)``.

    This is the independent expectation every surface is held to: the fixture states the tracklist
    a human should see, in order, and nothing here consults the projection to find it out.
    """

    rows: list[tuple[str, str, bool]] = []
    for line in expected["copy"]:
        time, label = line.split("  ", 1)
        crowd = label.endswith(" (from comments)")
        if crowd:
            label = label.removesuffix(" (from comments)")
        rows.append((time, label, crowd))
    return rows


def _tracklist_text_via_node(html: str) -> str:
    """Run the page's own ``tracklistText()`` over its own ``COPY_ENTRIES``.

    Reading ``COPY_ENTRIES`` out of the HTML and re-joining it in Python would only prove the
    payload, not the Copy button: the defect being fixed was the *function* deriving a different
    tracklist from the DOM.  So the shipped function is executed verbatim.
    """

    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI and this workstation both have node
        pytest.skip("node is needed to execute the page's own Copy function")
    entries = re.search(r"^const COPY_ENTRIES = (\[.*?\]);$", html, re.MULTILINE)
    function = re.search(r"^function tracklistText\(\)\{.*?\n\}$", html, re.MULTILINE | re.DOTALL)
    assert entries and function, "the page must still ship COPY_ENTRIES and tracklistText()"
    script = f"{entries.group(0)}\n{function.group(0)}\nprocess.stdout.write(tracklistText());"
    completed = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout


def _row_html(html: str, episode_id: str, *, short: bool) -> str:
    """The one ``<tr>`` whose ``id`` attribute is ``episode_id`` — the playlist anchor contract.

    ``\\sid="`` is deliberate: a bare ``id="…"`` also matches the tail of ``data-episode-id="…"``,
    so the looser pattern passes even if the ``<tr id>`` the playlists feature needs is gone.
    """

    pattern = rf'<tr class="(track[^"]*)"[^>]*\sid="{episode_id}"[^>]*>.*?</tr>'
    match = re.search(pattern, html, re.DOTALL)
    assert match, f"no <tr> carries id={episode_id!r} as its own id attribute"
    classes = match.group(1).split()
    assert ("short" in classes) is short, f"row {episode_id!r} has the wrong hidden state"
    return match.group(0)


def _assert_surfaces(
    directory: Path,
    projection: CanonicalProjection,
    item: AnalysedSet,
    fixture: dict,
    mode: str,
) -> None:
    """Hold every consumer to the fixture's hand-computed expectation, surface by surface."""

    expected = fixture["expected"][mode]
    expected_rows = _expected_rows(expected)
    html = read_text(directory / "index.html")
    document = json.loads(read_text(directory / "tracklist.json"))
    shown = list(projection.shown_entries)
    shown_tracks = [entry for entry in shown if entry["kind"] == "track"]
    hidden_entries = [entry for entry in projection.entries if entry["hidden_reason"] is not None]

    # --- the projection itself matches the fixture, so every assertion below has a real anchor ---
    assert [
        (
            _format_time(entry["start_ms"]),
            entry["display_label"],
            bool(entry.get("hint_only")),
        )
        for entry in shown
    ] == expected_rows
    assert len(shown_tracks) == expected["tracks"]
    assert projection.suppressed_count == expected["suppressed"]
    assert projection.gap_count == expected["gaps"]
    assert sum(1 for entry in shown_tracks if entry.get("hint_only")) == expected["crowd"]
    assert {
        entry["display_label"]: [alt["track"] for alt in entry.get("alternatives", ())]
        for entry in shown_tracks
        if entry.get("alternatives")
    } == expected["alternatives"]
    assert sorted(entry["display_label"] for entry in hidden_entries) == sorted(fixture["hidden"])

    # --- JSON export ---
    assert document["entries"] == shown
    assert document["suppressed_count"] == expected["suppressed"]

    # --- hero tiles: count, honest coverage percentage, confidence mix, gap count ---
    assert f'<span class="big">{expected["tracks"]}</span><small>track' in html
    crowd_note = f" · {expected['crowd']} from comments" if expected["crowd"] else ""
    assert f"found{crowd_note}</small>" in html
    # The percentage the hero prints, held to a figure computed by hand from the SHOWN rows only.
    assert f"<b>{expected['pct']}%</b></div><div><b>of the set identified</b>" in html
    assert f'<span class="big">{expected["gaps"]}</span><small>ID gap' in html
    for badge, count in expected["badges"].items():
        assert f">{count} {badge}</span>" in html
    shown_total = sum(expected["badges"].values())
    for badge, count in expected["badges"].items():
        assert f'<i class="c-{badge}" style="width:{count * 100.0 / shown_total:.2f}%"></i>' in html
    # No badge the fixture does not expect may appear in the hero key at all.
    for badge in ("verified", "likely", "possible", "unclear"):
        if badge not in expected["badges"]:
            assert f'<span style="--k:var(--{badge})">' not in html
    if expected["note"]:
        assert expected["note"] in html
    else:
        assert 'id="short-note"' not in html

    # --- library card: count AND confidence bar, not merely the link ---
    summary = _set_summary(item)
    assert summary is not None
    assert summary.tracks == expected["tracks"]
    assert summary.badges == expected["badges"]
    card = _mix_card_html(item)
    assert f"<b>{expected['tracks']}</b> track" in card
    for badge, count in expected["badges"].items():
        assert f'<i class="c-{badge}" style="width:{count * 100.0 / shown_total:.2f}%"></i>' in card
    assert f'title="{html_title(expected["badges"])}"' in card
    assert f"/{item.source_key}/{item.media_key}/present/index.html" in card

    # --- Copy button: the shipped function, executed ---
    assert _tracklist_text_via_node(html).split("\n") == expected["copy"]

    # --- CUE: the identities, not just how many TRACK records there are ---
    cue = read_text(directory / "tracklist.cue")
    assert cue.count("  TRACK ") == len(expected_rows)
    cue_titles = re.findall(r'^    TITLE "(.*)"$', cue, re.MULTILINE)
    cue_performers = re.findall(r'^    PERFORMER "(.*)"$', cue, re.MULTILINE)
    assert len(cue_titles) == len(cue_performers) == len(expected_rows)
    assert [
        f"{performer} — {title}" if performer != "ID" else "ID"
        for performer, title in zip(cue_performers, cue_titles, strict=True)
    ] == [label for _, label, _ in expected_rows]

    # --- Markdown: the same rows, and none of the columns the UI hides ---
    markdown = read_text(directory / "tracklist.md")
    assert "| Time | Confidence | Track |" in markdown
    assert "| Version |" not in markdown and "| Role |" not in markdown
    markdown_rows = [
        [cell.strip() for cell in line.split("|")[1:-1]]
        for line in markdown.splitlines()
        if line.startswith("| ") and not line.startswith("| Time")
    ]
    # The Track cell may carry an "also: …" note; the row's own identity is what precedes it.
    markdown_labels = [
        "ID" if cells[2].startswith("ID — no evidence") else cells[2].split("<br>")[0].strip()
        for cells in markdown_rows
    ]
    assert markdown_labels == [label for _, label, _ in expected_rows]
    assert [cells[0] for cells in markdown_rows] == [time for time, _, _ in expected_rows]

    # --- nothing hidden may leak into any export, by any route ---
    # The three list equalities above are exhaustive, so a hidden row cannot be a LISTED row
    # anywhere.  What they do not cover is a hidden identity smuggled in as a note on a shown row:
    # an "also:" alternative, or the CUE ``REM`` lines rendered from ``overlap_labels``.  Both are
    # compared as whole, delimited values — a substring search would trip over one fixture label
    # being the prefix of another ("South — Night Bus" vs "South — Night Bus (Dub)").
    hidden_labels = {str(entry["display_label"]) for entry in hidden_entries}
    cue_notes = set(re.findall(r'^    REM (?:LAYER|OVERLAP) "(.*)"$', cue, re.MULTILINE))
    assert not cue_notes & {label.replace(" — ", " - ") for label in hidden_labels}
    shown_alternatives = {
        str(alt["track"]) for entry in shown_tracks for alt in entry.get("alternatives", ())
    }
    assert not shown_alternatives & hidden_labels
    assert not (directory / "tracklist.m3u").exists()
    assert "tracklist.m3u" not in html

    # --- page rows + the playlist contract ---
    for entry in shown_tracks:
        row = _row_html(html, str(entry["episode_id"]), short=False)
        assert f'data-episode-id="{entry["episode_id"]}"' in row
        assert f'data-candidate-id="{entry["candidate_id"]}"' in row
        assert "pl-actions" in row
        if entry.get("hint_only"):
            assert 'data-crowd="1"' in row
    for entry in hidden_entries:
        row = _row_html(html, str(entry["episode_id"]), short=True)
        assert "pl-actions" not in row
    assert PLAYLIST_CSS.strip() in html and PLAYLIST_JS.strip() in html
    assert "location.protocol" in html and "el.style.display = 'none'" in html

    # --- the timeline is the projection's, not a second grouping pass of its own ---
    lane_ids = re.findall(r'<div class="tl-lane" data-episode-id="([0-9a-f]+)"', html)
    assert lane_ids == [
        str(entry["episode_id"]) for entry in projection.entries if entry["kind"] == "track"
    ]
    spans = json.loads(
        re.search(r"^const EPISODE_SPANS = (\[.*?\]);$", html, re.MULTILINE).group(1)
    )
    assert [span["id"] for span in spans] == [str(entry["episode_id"]) for entry in shown_tracks]
    gap_markers = re.findall(r'<div class="tl-gap" ', html)
    assert len(gap_markers) == expected["gaps"]


def html_title(badges: dict[str, int]) -> str:
    """The library card's confidence-bar tooltip, spelled out independently of the renderer."""

    order = ("verified", "likely", "possible", "unclear")
    return " · ".join(f"{badges[key]} {key}" for key in order if badges.get(key))


@pytest.mark.parametrize("name", FIXTURE_NAMES)
@pytest.mark.parametrize(("mode", "collapse"), COLLAPSE_MODES)
def test_every_surface_renders_the_same_projection(
    tmp_path: Path, name: str, mode: str, collapse: bool
) -> None:
    directory, projection, item, fixture = _publish(tmp_path, name, collapse=collapse)
    _assert_surfaces(directory, projection, item, fixture, mode)


@pytest.mark.parametrize(("mode", "collapse"), COLLAPSE_MODES)
def test_a_suppressed_match_neither_rides_along_nor_hides_its_honest_group(
    tmp_path: Path, mode: str, collapse: bool
) -> None:
    """Suppression is decided per match BEFORE collapse (the ``cluster`` fixture's two traps).

    Trap 1 (``Work``): an unsuppressed primary with a ``contradicted`` near-duplicate must not ship
    a SHOWN row that carries the suppressed track as a silent "could also be" alternative while
    reporting nothing hidden.  Trap 2 (``Night Bus``): the suppressed match has the stronger badge,
    so collapsing first would let it win primary selection and hide the whole group — including the
    honest ``(Dub)`` row that should be shown.
    """

    directory, projection, item, fixture = _publish(tmp_path, "cluster", collapse=collapse)
    shown = {entry["display_label"] for entry in projection.shown_entries}
    hidden = {
        entry["display_label"]: entry["hidden_reason"]
        for entry in projection.entries
        if entry["hidden_reason"] is not None
    }

    # ``hidden_reason`` passes the fusion-side token through verbatim, so the page's note can say
    # WHY a row is hidden rather than only that it is.
    assert hidden == {
        "Pupa Nas-T — Work (Kevin McKay ViP)": "contradicted",
        "South — Night Bus": "buried",
    }
    # Trap 1: the honest primary shows, and it does NOT carry the suppressed twin.
    assert "Pupa Nas-T — Work" in shown
    (work,) = [e for e in projection.shown_entries if e["display_label"] == "Pupa Nas-T — Work"]
    assert [alt["track"] for alt in work.get("alternatives", ())] == []
    assert projection.suppressed_count == 2, "a hidden row must be counted, never absorbed"
    # Trap 2: the suppressed badge-winner is hidden and its honest group-mate survives.
    assert "South — Night Bus" not in shown
    assert "South — Night Bus (Dub)" in shown
    # And a legitimate near-duplicate cluster still folds when collapsing is on.
    if collapse:
        assert "Denise — Rise (Club Mix)" not in shown
    _assert_surfaces(directory, projection, item, fixture, mode)


@pytest.mark.parametrize(("mode", "collapse"), COLLAPSE_MODES)
def test_the_hero_percentage_describes_only_the_rows_the_tracklist_lists(
    tmp_path: Path, mode: str, collapse: bool
) -> None:
    """F-2: the boiler hero must read 50 %, not the 90 % the fused durations still account for.

    The fixture is 300 s.  Five sub-floor matches and one suppressed match hold 120 s of proved
    evidence between them; the two rows the page actually lists hold 150 s.  Reading coverage off
    ``episodes.durations`` (270 s → 90 %) advertises tracks the tracklist does not contain.
    """

    directory, projection, item, fixture = _publish(tmp_path, "boiler", collapse=collapse)
    html = read_text(directory / "index.html")
    assert fixture["duration_ms"] == 300_000
    assert projection.suppressed_count == 6
    # What the old code would have said, computed from the same inputs, for contrast.
    episodes = bundles.load_run_snapshot(item.media_dir, directory=directory).episodes
    unfiltered = episodes.durations.evidence_supported_ms + episodes.durations.predicted_episode_ms
    assert round(unfiltered * 100 / 300_000) == 90
    assert projection.covered_ms == 150_000
    assert "<b>50%</b></div><div><b>of the set identified</b>" in html
    assert "90%" not in html
    _assert_surfaces(directory, projection, item, fixture, mode)


@pytest.mark.parametrize(("mode", "collapse"), COLLAPSE_MODES)
def test_refresh_mints_a_complete_consistent_bundle_from_the_frozen_run(
    tmp_path: Path, mode: str, collapse: bool
) -> None:
    """Re-rendering mints a NEW bundle from the frozen snapshot, and never rewrites a sealed one.

    ``fuse/episodes.json`` is deleted first: cycle 2b's retention prunes that mutable tree, so a
    refresh has to work from the bundle's own frozen copy or the feature dies after pruning.
    """

    first, first_projection, item, fixture = _publish(tmp_path, "boiler", collapse=collapse)
    first_bytes = {path.name: read_bytes(path) for path in first.iterdir()}
    assert first_projection.suppressed_count == 6
    (item.media_dir / "fuse/episodes.json").unlink()
    second_index = refresh.regenerate_page(
        item.media_dir, config=AppConfig(present_min_track_ms=0, collapse=collapse)
    )
    second = second_index.parent
    snapshot = bundles.load_run_snapshot(item.media_dir, directory=second)
    second_projection = build_projection(
        snapshot.episodes,
        snapshot.identities,
        snapshot.acquire,
        collapse=collapse,
        min_track_ms=0,
    )
    assert second != first, "a re-render must mint a new immutable bundle"
    assert second_projection.suppressed_count == 1, "only the suppressed row survives floor=0"
    assert {path.name: read_bytes(path) for path in first.iterdir()} == first_bytes
    # The new bundle's page and its exports agree with each other, exactly as the first one's did.
    second_document = json.loads(read_text(second / "tracklist.json"))
    assert second_document["entries"] == list(second_projection.shown_entries)
    assert second_document["suppressed_count"] == 1
    second_html = read_text(second / "index.html")
    assert _tracklist_text_via_node(second_html).split("\n") == [
        f"{_format_time(entry['start_ms'])}  {entry['display_label']}"
        for entry in second_projection.shown_entries
    ]
    assert read_text(second / "tracklist.cue").count("  TRACK ") == len(
        second_projection.shown_entries
    )
    assert fixture["expected"][mode]["note"] not in second_html


def test_library_refreshes_a_stale_bundle_before_reading_its_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _, item, _ = _publish(tmp_path, "boiler", collapse=True)
    monkeypatch.setattr(page, "PAGE_VERSION", page.PAGE_VERSION + 1)
    monkeypatch.setattr(refresh, "PAGE_VERSION", page.PAGE_VERSION)
    (discovered,) = _fresh_sets(tmp_path, AppConfig(present_min_track_ms=30_000, collapse=True))
    second = bundles.result_dir(discovered.media_dir)
    assert second != first
    assert _set_summary(discovered) == _set_summary(item)
    document = json.loads(read_text(second / "tracklist.json"))
    summary = _set_summary(discovered)
    assert summary is not None
    assert summary.tracks == sum(entry["kind"] == "track" for entry in document["entries"])
    # The card's number is the fixture's, not merely self-consistent with a stale document.
    assert summary.tracks == 2


def test_the_projection_cannot_be_edited_by_the_surface_rendering_it(tmp_path: Path) -> None:
    """One projection object is handed to the page, the exports and the card in turn.

    If a surface could edit a row in place, the next surface would render a different tracklist —
    which is the whole defect this cycle removes.  The accessors therefore hand out copies.
    """

    _, projection, _, _ = _publish(tmp_path, "boiler", collapse=True)
    before = projection.shown_entries
    stolen = projection.shown_entries[0]
    stolen["display_label"] = "Vandal — Not A Real Track"
    stolen["hidden_reason"] = "short"
    assert projection.shown_entries == before
    assert projection.shown_entries[0]["display_label"] == "Anchor — Opening"
    assert projection.suppressed_count == 6
    # The tuple itself, and the dataclass's own fields, are frozen too.
    with pytest.raises(dataclasses.FrozenInstanceError):
        projection.covered_ms = 0  # type: ignore[misc]
