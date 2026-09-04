"""Stage 12 — collapse competing near-duplicate matches into one display row with alternatives.

Deterministic and network-free: every fixture is synthetic, so the grouping, the primary choice,
the disclosure markup and the privacy guarantees are pinned without touching a real mix.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from id_detector.contracts import (
    EpisodesFile,
    IdentitiesRecord,
    SourceRecord,
    derive_source_key,
)
from id_detector.present.exports import flatten_tracklist
from id_detector.present.grouping import (
    DEFAULT_MERGE_GAP_MS,
    group_display_tracks,
    normalise_title,
    work_key,
)
from id_detector.present.page import render_page
from scripts.audit_fixtures import _HANDLE, _ID_FIELD

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "golden"


def _cid(index: int) -> str:
    return format(0xC0DE0000 + index, "040x")


def _identities(mapping: dict[str, tuple[str, str]]) -> IdentitiesRecord:
    """Build a minimal identities record mapping each candidate id to an ``Artist - Title``."""

    nodes = []
    works = []
    candidates = []
    for index, (candidate_id, (artist, title)) in enumerate(mapping.items()):
        node_id = f"shazam:track-{index}"
        work_id = format(0x1000 + index, "040x")
        nodes.append(
            {
                "schema_version": "1.0.0",
                "generated_by": "id-detector/0.1.0",
                "id": node_id,
                "ns": "shazam",
                "label": f"{artist} - {title}",
            }
        )
        works.append(
            {
                "schema_version": "1.0.0",
                "generated_by": "id-detector/0.1.0",
                "work_id": work_id,
                "member_nodes": [node_id],
            }
        )
        candidates.append(
            {
                "schema_version": "1.0.0",
                "generated_by": "id-detector/0.1.0",
                "canonical_id": candidate_id,
                "work_id": work_id,
                "member_nodes": [node_id],
                "alternatives": [],
                "contested": False,
                "conflicts": [],
            }
        )
    return IdentitiesRecord.model_validate(
        {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "nodes": nodes,
            "assertions": [],
            "works": works,
            "candidates": candidates,
        }
    )


def _episode(
    *,
    idx: int,
    candidate_id: str,
    best_start_ms: int,
    best_end_ms: int,
    badge: str = "unclear",
    support: list[list[int]] | None = None,
    role: str = "incoming",
    occurrence_index: int = 0,
) -> dict:
    support = support if support is not None else [[best_start_ms, best_start_ms + 12_000]]
    return {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "id": format(0xE0000000 + idx, "040x"),
        "candidate_id": candidate_id,
        "alternatives": [],
        "claim": "performed",
        "start_no_later_than_ms": best_start_ms,
        "end_no_earlier_than_ms": best_end_ms,
        "evidence_support_ms": support,
        "start_no_earlier_than_ms": None,
        "end_no_later_than_ms": None,
        "start_pi": None,
        "end_pi": None,
        "best_start_ms": best_start_ms,
        "best_end_ms": best_end_ms,
        "role_segments": [{"from_ms": best_start_ms, "to_ms": best_end_ms, "role": role}],
        "occurrence_index": occurrence_index,
        "overlaps": [],
        "alignment_segments": [],
        "alignment_events": [],
        "has_global_alignment": True,
        "scores": {"work": 6000, "version": 5000, "boundary": 6000},
        "score_kind": "heuristic",
        "tiers": {"work": "possible", "version": "unclear", "boundary": "possible"},
        "badge": badge,
        "version_status": "unverified",
        "evidence": ["a" * 40],
        "rejected_evidence": [],
        "flags": [],
        "rescan_state": "not_requested",
    }


def _episodes_file(episodes: list[dict]) -> EpisodesFile:
    return EpisodesFile.model_validate(
        {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "generation": 0,
            "episodes": episodes,
            "gaps": [],
            "durations": {
                "schema_version": "1.0.0",
                "generated_by": "id-detector/0.1.0",
                "evidence_supported_ms": 0,
                "predicted_episode_ms": 0,
                "unresolved_boundary_ms": 0,
                "unclear_ms": 0,
                "no_evidence_ms": 0,
                "unscanned_ms": 0,
            },
            "certification": {
                "profile": "free",
                "per": [
                    {
                        "dimension": "work",
                        "tier": "possible",
                        "status": "provisional",
                        "n_test_predictions": 0,
                        "lower_bound_e4": 0,
                        "test_version": "not-run",
                    }
                ],
            },
        }
    )


def _source() -> SourceRecord:
    url = "https://soundcloud.com/example/mix"
    base = json.loads((GOLDEN / "source.json").read_text("utf-8"))
    base.update(
        {
            "platform": "soundcloud",
            "canonical_url": url,
            "input_url": url,
            "source_key": derive_source_key(url),
            "platform_id": "12345",
            "title": "Fixture Live Set",
        }
    )
    return SourceRecord.model_validate(base)


# ------------------------------------------------------------------------------------------------
# The "Work" cluster: six different official releases of one sampled vocal over 11:36–12:48.
_WORK_VARIANTS = [
    ("Kevin McKay, Pupa Nas T & Denise Belfon", "Work (CVMPANILE & Draxx Remix)", "unclear"),
    ("Pupa Nas T & SHUFFA", "Work Dub (feat. Denise Belfon)", "unclear"),
    ("Chris Lorenzo, Denise & Puppah Nas-T", "Work", "unclear"),
    ("Pupa Nas T, Kevin McKay & Denise Belfon", "Work (Kevin McKay ViP)", "possible"),
    ("Masters At Work", "Work (DJ's Of The Planet Remix)", "unclear"),
    ("Puppah Nas-T", 'Work (Full Acapella) [feat. Denise "Saucey Wow" Belfon]', "unclear"),
]


def _work_cluster() -> tuple[EpisodesFile, IdentitiesRecord]:
    mapping = {_cid(i): (artist, title) for i, (artist, title, _) in enumerate(_WORK_VARIANTS)}
    identities = _identities(mapping)
    episodes = []
    for i, (_, _, badge) in enumerate(_WORK_VARIANTS):
        start = 696_000 + i * 12_000  # 11:36, 11:48, … within one tight cluster
        episodes.append(
            _episode(
                idx=i,
                candidate_id=_cid(i),
                best_start_ms=start,
                best_end_ms=start + 12_000,
                badge=badge,
            )
        )
    return _episodes_file(episodes), identities


def test_work_cluster_collapses_to_one_display_track() -> None:
    episodes, identities = _work_cluster()
    tracks = group_display_tracks(list(episodes.episodes), identities, duration_ms=800_000)
    assert len(tracks) == 1
    track = tracks[0]
    # The lone "possible" release is the closest match; the other five ride along as alternatives.
    assert track.primary.candidate_id == _cid(3)
    assert track.primary.badge == "possible"
    assert len(track.alternatives) == 5
    assert _cid(3) not in {alt.candidate_id for alt in track.alternatives}

    entries = flatten_tracklist(episodes, identities, collapse=True)
    track_rows = [entry for entry in entries if entry["kind"] == "track"]
    assert len(track_rows) == 1
    row = track_rows[0]
    assert row["also_count"] == 5
    assert len(row["alternatives"]) == 5
    assert row["title"] == "Work (Kevin McKay ViP)"
    # The ungrouped view still emits one row per episode.
    ungrouped = flatten_tracklist(episodes, identities, collapse=False)
    assert sum(1 for entry in ungrouped if entry["kind"] == "track") == 6


def test_two_different_adjacent_tracks_stay_two_rows() -> None:
    mapping = {_cid(0): ("Alpha", "Sunrise"), _cid(1): ("Beta", "Moonfall")}
    identities = _identities(mapping)
    episodes = _episodes_file(
        [
            _episode(idx=0, candidate_id=_cid(0), best_start_ms=100_000, best_end_ms=112_000),
            # Adjacent (well within the gap) but a genuinely different work.
            _episode(idx=1, candidate_id=_cid(1), best_start_ms=115_000, best_end_ms=127_000),
        ]
    )
    tracks = group_display_tracks(list(episodes.episodes), identities, duration_ms=800_000)
    assert len(tracks) == 2
    assert all(track.alternatives == () for track in tracks)


def test_real_repeat_later_in_set_is_not_merged() -> None:
    mapping = {_cid(0): ("Same", "Anthem"), _cid(1): ("Other", "Interlude")}
    identities = _identities(mapping)
    episodes = _episodes_file(
        [
            _episode(idx=0, candidate_id=_cid(0), best_start_ms=100_000, best_end_ms=112_000),
            _episode(idx=1, candidate_id=_cid(1), best_start_ms=400_000, best_end_ms=412_000),
            # The same candidate returns 10 minutes later, separated by another track.
            _episode(
                idx=2,
                candidate_id=_cid(0),
                best_start_ms=700_000,
                best_end_ms=712_000,
                occurrence_index=1,
            ),
        ]
    )
    tracks = group_display_tracks(list(episodes.episodes), identities, duration_ms=800_000)
    assert len(tracks) == 3
    first = next(
        t for t in tracks if t.primary.occurrence_index == 0 and t.primary.candidate_id == _cid(0)
    )
    later = next(t for t in tracks if t.primary.occurrence_index == 1)
    assert first is not later
    assert later.alternatives == ()  # occurrence 1 was not folded into occurrence 0


def test_clean_gap_wider_than_threshold_prevents_merge() -> None:
    mapping = {_cid(0): ("Artist", "Track")}
    identities = _identities(mapping)
    gap = DEFAULT_MERGE_GAP_MS + 8_000
    episodes = _episodes_file(
        [
            _episode(idx=0, candidate_id=_cid(0), best_start_ms=100_000, best_end_ms=112_000),
            _episode(
                idx=1,
                candidate_id=_cid(0),
                best_start_ms=112_000 + gap,
                best_end_ms=124_000 + gap,
                occurrence_index=1,
            ),
        ]
    )
    tracks = group_display_tracks(list(episodes.episodes), identities, duration_ms=800_000)
    assert len(tracks) == 2


def test_work_key_normaliser_strips_qualifiers() -> None:
    assert normalise_title("Work (Kevin McKay ViP)") == "work"
    assert normalise_title("Work Dub (feat. Denise Belfon)") == "work"
    assert normalise_title('Work (Full Acapella) [feat. Denise "Saucey Wow" Belfon]') == "work"
    assert normalise_title("Work (CVMPANILE & Draxx Remix)") == "work"
    assert normalise_title("Murderation (4 The Bristol Crew Mix)") == "murderation"
    # A genuinely different work keeps its own key.
    assert normalise_title("Ibiza (Bootleg Version)") == "ibiza"
    assert normalise_title("Ibiza (Bootleg Version)") != normalise_title("Work")
    # A title that is only a qualifier keeps its identity rather than collapsing to empty.
    assert normalise_title("Dub") == "dub"
    # The full work key carries the (normalised) artist so identical releases collapse cleanly.
    assert work_key("Masters At Work", "Work") == "masters at work|work"
    assert work_key("Puppah Nas-T", "Work (Full Acapella)").endswith("|work")


class _Validator(HTMLParser):
    _VOID = {"meta", "br", "img", "input", "hr", "link"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag not in self._VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._VOID:
            return
        if tag not in self.stack:
            self.errors.append(f"stray </{tag}>")
            return
        while self.stack and self.stack[-1] != tag:
            self.stack.pop()
        if self.stack:
            self.stack.pop()


def test_page_disclosure_and_playhead_present_against_collapsed_rows() -> None:
    episodes, identities = _work_cluster()
    page = render_page(
        source=_source(),
        episodes=episodes,
        identities=identities,
        duration_ms=800_000,
        collapse=True,
    )
    # The collapsed disclosure is inline (no extra requests).
    assert '<details class="alts">' in page
    assert "other version" in page
    assert "▸" in page
    # Exactly one tracklist row survives for the Work cluster.
    assert page.count('<tr class="track"') == 1
    # The Stage 11 playhead + current-row highlight still work against the collapsed row.
    assert '<div class="playhead" id="playhead" hidden' in page
    assert "function updatePlayhead" in page
    assert "const EPISODE_SPANS = [" in page
    assert page.count('"id": "') == 1  # one display-track span (the primary's id)
    assert f'"id": "{episodes.episodes[3].id}"' in page  # the primary (ViP)
    assert "tr.track.current" in page
    # The row seeks / the disclosure toggle does not.
    assert "e.target.closest('a,button,details,summary')" in page
    validator = _Validator()
    validator.feed(page)
    assert validator.errors == []


def test_collapsed_page_has_no_usernames_or_comment_text() -> None:
    episodes, identities = _work_cluster()
    page = render_page(
        source=_source(),
        episodes=episodes,
        identities=identities,
        duration_ms=800_000,
        collapse=True,
    )
    without_style = re.sub(r"<style>.*?</style>", "", page, flags=re.DOTALL)
    assert _HANDLE.search(without_style) is None
    assert _ID_FIELD.search(page) is None
