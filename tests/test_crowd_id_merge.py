"""Two crowd IDs of the same track merge into one identity (no duplicate row).

Guards the hint<->hint union in build_identity_graph: when the comments name the same track twice
with a version/"unreleased" suffix — the real "Entasia - Satalite" / "Entasia - Satalite
(unreleased)" case — the two answers must land in one work, not two.
"""

from __future__ import annotations

from id_detector.fuse.identity import build_identity_graph
from id_detector.hints.parse import HintInput, parse_hint_inputs

MEDIA_KEY = "4" * 64
DURATION_MS = 600_000


def _answer(sid: str, text: str, pos: int) -> HintInput:
    return HintInput(
        connector="sc_comments",
        source_record_id=sid,
        text=text,
        position_ms=pos,
        position_kind="comment_timestamp",
        author_pseudo_id=f"fan-{sid}",
    )


def _work_ids(texts: list[tuple[str, str, int]]) -> list[str | None]:
    hints = parse_hint_inputs(MEDIA_KEY, DURATION_MS, [_answer(s, t, p) for s, t, p in texts])
    identity = build_identity_graph(MEDIA_KEY, [], hints=hints)
    return [identity.hint_work_ids.get(hint.id) for hint in hints]


def test_same_track_named_twice_with_a_version_suffix_is_one_work() -> None:
    work_ids = _work_ids(
        [
            ("a", "Entasia - Satalite", 360_000),
            ("b", "Entasia - Satalite (unreleased)", 461_000),
        ]
    )
    assert None not in work_ids
    assert len(set(work_ids)) == 1  # merged -> a single row, not two


def test_two_genuinely_different_tracks_stay_separate() -> None:
    work_ids = _work_ids(
        [
            ("a", "Entasia - Satalite", 360_000),
            ("b", "Bicep - Glue", 500_000),  # unrelated -> must not merge
        ]
    )
    assert None not in work_ids
    assert len(set(work_ids)) == 2
