"""fusion:6 — a listener's comment tracklist may corroborate a recognised track, and only that.

On YouTube most sets' tracklists are typed into the comments by a listener, not the uploader.
Until fusion:5 such a line could not back the track the recogniser heard, although a single
listener's comment answer could.  Now it can, under exactly the answer's rule (same identity work,
in reach of the play).  It still never names a track by itself and never hides one.  Everything is
authored and fused offline; nothing here contacts a provider.
"""

from __future__ import annotations

from id_detector.contracts import EpisodeRecord, EpisodesFile, HintRecord, ObservationRecord
from id_detector.fuse.episodes import _eligible_tracklist_hint
from id_detector.hints.parse import HintInput, parse_hint_inputs
from id_detector.hints.relations import apply_relations
from id_detector.present.exports import flatten_tracklist
from id_detector.recipes import FUSION_VERSION
from tests.test_phase1b_fusion import MEDIA_KEY, _fuse, _shazam_vote, _window

DURATION_MS = 3_600_000
FLOOR_MS = 30_000


def _votes(artist: str, title: str, starts: list[int], key: str) -> list[ObservationRecord]:
    return [_shazam_vote(_window(start), artist, title, key=key) for start in starts]


def _listener_tracklist(text: str, *, connector: str = "yt_comments") -> list[HintRecord]:
    """One comment holding a timestamped tracklist, typed by an ordinary listener."""

    inputs = [
        HintInput(
            connector=connector,
            source_record_id="listener-list",
            text=text,
            author_pseudo_id="listener",
            is_uploader=False,
            is_pinned=False,
        )
    ]
    hints = parse_hint_inputs(MEDIA_KEY, DURATION_MS, inputs)
    return apply_relations(MEDIA_KEY, DURATION_MS, hints, inputs)


def _episode_of(episodes: EpisodesFile, votes: list[ObservationRecord]) -> EpisodeRecord:
    ids = {item.id for item in votes}
    (episode,) = [item for item in episodes.episodes if ids & set(item.evidence)]
    return episode


def _listed(episodes: EpisodesFile, identity) -> list[dict]:
    rows = flatten_tracklist(episodes, identity.record, collapse=False, min_track_ms=FLOOR_MS)
    return [row for row in rows if row["kind"] == "track"]


LIST = (
    "Tracklist:\n"
    "00:00 Opening Act - First Light\n"
    "31:36 Fixture Artist - Night Drive\n"
    "34:10 Closing Act - Last Call\n"
)


def test_the_version_is_bumped_so_stored_results_are_refused_offline() -> None:
    assert FUSION_VERSION == 6


def test_a_listener_tracklist_line_is_eligible_like_a_listener_answer() -> None:
    hints = _listener_tracklist(LIST)
    lines = [hint for hint in hints if hint.kind == "tracklist_line"]
    assert len(lines) == 3
    assert all(not hint.author.is_uploader and not hint.is_pinned for hint in lines)
    assert all(_eligible_tracklist_hint(hint) for hint in lines)
    # The guards that applied before still apply.
    quarantined = lines[0].model_copy(update={"mirror_status": "quarantined"})
    unknown = lines[0].model_copy(
        update={"flags": lines[0].flags.model_copy(update={"id_unknown": True})}
    )
    assert not _eligible_tracklist_hint(quarantined)
    assert not _eligible_tracklist_hint(unknown)


def test_a_listener_tracklist_line_lists_the_short_track_the_recogniser_heard() -> None:
    """One 12 s window is below the 30 s floor on its own; a listener's line naming the SAME work,
    a little before it (a listener's clock), backs it and it is listed."""

    heard = _votes("Fixture Artist", "Night Drive", [1_935_000], "heard")
    hints = _listener_tracklist(LIST)

    baseline, base_identity = _fuse(heard, duration_ms=DURATION_MS, hints=[])
    assert _listed(baseline, base_identity) == []

    episodes, identity = _fuse(heard, duration_ms=DURATION_MS, hints=hints)
    episode = _episode_of(episodes, heard)
    line = next(hint for hint in hints if hint.title == "Night Drive")
    assert "hint_supported" in episode.flags and line.id in episode.evidence
    assert [row["episode_id"] for row in _listed(episodes, identity)] == [episode.id]
    # Corroboration never retimes the audio.
    assert episode.evidence_support_ms == _episode_of(baseline, heard).evidence_support_ms


def test_a_listener_line_naming_another_track_backs_nothing_and_hides_nothing() -> None:
    """The recogniser heard "Other Artist - Daylight" (36 s) where the listener typed Night Drive:
    the line does not back it (different work), does not contradict it (a listener's clock may be
    a minute off), and does not become a track of its own."""

    heard = _votes("Other Artist", "Daylight", [1_935_000, 1_947_000, 1_959_000], "other")
    hints = _listener_tracklist(LIST)
    episodes, identity = _fuse(heard, duration_ms=DURATION_MS, hints=hints)
    episode = _episode_of(episodes, heard)

    assert "hint_supported" not in episode.flags
    assert episode.suppressed is None
    assert not any("hint_only" in item.flags for item in episodes.episodes)
    assert [row["episode_id"] for row in _listed(episodes, identity)] == [episode.id]


def test_a_listener_line_far_from_the_play_backs_nothing() -> None:
    """Out of reach (the line is twenty minutes before the play): no backing, so the 12 s
    fragment stays below the floor."""

    heard = _votes("Fixture Artist", "Night Drive", [3_100_000], "late")
    hints = _listener_tracklist(LIST)
    episodes, identity = _fuse(heard, duration_ms=DURATION_MS, hints=hints)

    assert "hint_supported" not in _episode_of(episodes, heard).flags
    assert _listed(episodes, identity) == []
