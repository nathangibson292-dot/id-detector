"""Cross-engine corroboration: two independent recognizers agreeing earns the confident tier.

Guards the signal that makes a paid cross-check worth running — when Shazam and AudD both land on
the same track at the same time, it is flagged ``engine_corroborated`` and treated as confident the
way an independent hint is, without disturbing the calibrated badge maths.
"""

from __future__ import annotations

from dataclasses import dataclass

from id_detector.fuse.episodes import _engine_corroborated
from id_detector.scan_targeting import CONFIDENT_FLAGS, _is_confident


@dataclass
class _Vote:
    provider: str


@dataclass
class _Episode:
    badge: str
    flags: list[str]


def test_two_distinct_providers_corroborate() -> None:
    assert _engine_corroborated([_Vote("shazam"), _Vote("audd")])  # type: ignore[list-item]
    # Two AudD windows + a Shazam window still count (distinct providers present).
    assert _engine_corroborated(  # type: ignore[list-item]
        [_Vote("shazam"), _Vote("audd"), _Vote("audd")]
    )


def test_a_single_provider_is_not_corroboration() -> None:
    # Many Shazam detections are one engine — never cross-engine corroboration.
    assert not _engine_corroborated([_Vote("shazam"), _Vote("shazam")])  # type: ignore[list-item]
    assert not _engine_corroborated([_Vote("audd")])  # type: ignore[list-item]
    assert not _engine_corroborated([])


def test_engine_corroborated_is_a_confident_flag() -> None:
    assert "engine_corroborated" in CONFIDENT_FLAGS
    # A merely "possible" badge becomes confident once a second engine corroborates it.
    assert _is_confident(_Episode("possible", ["engine_corroborated"]))  # type: ignore[arg-type]
    assert not _is_confident(_Episode("possible", []))  # type: ignore[arg-type]
