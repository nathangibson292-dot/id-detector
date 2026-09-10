"""Cross-engine corroboration: two trust families agreeing at one moment earns "confirmed twice".

Guards the signal that makes a paid cross-check worth running — when Shazam and AudD both land on
the same track at the same time, it is flagged ``engine_corroborated`` and treated as confident
the way an independent hint is, without disturbing the calibrated badge maths.  Since 1b-ii the
agreement is read across trust FAMILIES (the commercial catalogues are one family) and needs the
supports to overlap by at least ``overlap_min_ms``; ``tests/test_phase1b_fusion.py`` covers the
fused flags, the separation rule and the presentation.
"""

from __future__ import annotations

from dataclasses import dataclass

from id_detector.fuse.episodes import _engine_corroborated, engine_agreements
from id_detector.scan_targeting import CONFIDENT_FLAGS, _is_confident


@dataclass
class _Vote:
    provider: str
    support_ms: tuple[int, int] = (0, 12_000)
    id: str = "0" * 40


@dataclass
class _Episode:
    badge: str
    flags: list[str]


def test_two_distinct_families_corroborate() -> None:
    assert _engine_corroborated([_Vote("shazam"), _Vote("audd", id="1" * 40)])  # type: ignore[list-item]
    # Two AudD windows + a Shazam window still count (a second family is present).
    assert _engine_corroborated(  # type: ignore[list-item]
        [_Vote("shazam"), _Vote("audd", id="1" * 40), _Vote("audd", (9_000, 21_000), "2" * 40)]
    )
    # The local index is a third family: Shazam + Panako agree too.
    assert _engine_corroborated([_Vote("shazam"), _Vote("panako", id="1" * 40)])  # type: ignore[list-item]


def test_a_single_family_is_not_corroboration() -> None:
    # Many Shazam detections are one engine — never cross-engine corroboration.
    assert not _engine_corroborated([_Vote("shazam"), _Vote("shazam", id="1" * 40)])  # type: ignore[list-item]
    assert not _engine_corroborated([_Vote("audd")])  # type: ignore[list-item]
    # AudD and ACRCloud sell the same catalogue: one family queried twice (review M1).
    assert not _engine_corroborated([_Vote("audd"), _Vote("acrcloud", id="1" * 40)])  # type: ignore[list-item]
    assert not _engine_corroborated([])


def test_agreement_needs_the_supports_to_overlap_by_the_floor() -> None:
    shazam = _Vote("shazam", (0, 12_000))
    # A 3 s coincidence is not the same moment; 6 s is.
    assert engine_agreements([shazam, _Vote("audd", (9_000, 21_000), "1" * 40)]) == []  # type: ignore[list-item]
    assert engine_agreements([shazam, _Vote("audd", (6_000, 18_000), "1" * 40)]) == [  # type: ignore[list-item]
        (6_000, 12_000)
    ]
    # Disjoint windows never agree, whatever the families.
    assert not _engine_corroborated([shazam, _Vote("audd", (30_000, 42_000), "1" * 40)])  # type: ignore[list-item]


def test_engine_corroborated_is_a_confident_flag() -> None:
    assert "engine_corroborated" in CONFIDENT_FLAGS
    # A merely "possible" badge becomes confident once a second engine corroborates it.
    assert _is_confident(_Episode("possible", ["engine_corroborated"]))  # type: ignore[arg-type]
    assert not _is_confident(_Episode("possible", []))  # type: ignore[arg-type]
