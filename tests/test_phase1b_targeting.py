"""Phase 1b-i gate: the ``targeting:1`` secondary scheduler (plan §2.3.4 step 4) over the fixtures
under ``tests/fixtures/deep/``.

*Scheduler* fixtures describe synthetic frozen windows (a hop and a window length over the
duration), per-window RMS energies, the first fuse's candidate spans and the picks the allocation
must make.  The free-first additive Deep recipe no longer runs this Shazam secondary (its paid
step checks the free result's gaps instead; see ``tests/test_additive_deep.py``); the scheduler's
own unit tests stay while the module does.
"""

from __future__ import annotations

import json
import wave
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from id_detector.contracts import (
    EpisodeRecord,
    EpisodesFile,
    IdentitiesRecord,
    ObservationRecord,
    WindowRecord,
)
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE, get_recipe
from id_detector.secondary_targeting import (
    SecondaryCandidate,
    SecondaryPick,
    allocate_secondary_windows,
    blank_spans,
    confirmation_windows,
    distribute_secondary_windows,
    energy_reader,
    listed_text_keys,
    new_identity_discoveries,
    rank_windows,
    secondary_capacity,
    secondary_reserve,
    serve_confirmations,
    window_rms_energy,
)

ROOT = Path(__file__).resolve().parents[1]
DEEP = ROOT / "tests" / "fixtures" / "deep"
GOLDEN = ROOT / "tests" / "golden"


def _fixture(name: str) -> dict:
    return json.loads((DEEP / f"{name}.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------------
# Synthetic records
# --------------------------------------------------------------------------------------------------
def _window(start_ms: int, *, window_ms: int = 12_000) -> WindowRecord:
    base = WindowRecord.model_validate(
        json.loads((GOLDEN / "window.json").read_text(encoding="utf-8"))
    )
    return base.model_copy(
        update={
            "id": f"{start_ms:040x}",
            "logical_trial_id": f"{start_ms:040x}",
            "start_ms": start_ms,
            "support_ms": (start_ms, start_ms + window_ms),
            "output_ms": window_ms,
            "wav_sha256": f"{start_ms:064x}",
        }
    )


def _windows(duration_ms: int, *, hop_ms: int, window_ms: int) -> list[WindowRecord]:
    return [
        _window(start, window_ms=window_ms)
        for start in range(0, duration_ms - window_ms + 1, hop_ms)
    ]


def _episode(
    span: tuple[int, int],
    *,
    candidate_id: str | None = None,
    suppressed: str | None = None,
) -> EpisodeRecord:
    base = EpisodeRecord.model_validate(
        json.loads((GOLDEN / "episode.json").read_text(encoding="utf-8"))
    )
    return base.model_copy(
        update={
            "id": f"{span[0]:040x}",
            "candidate_id": candidate_id or base.candidate_id,
            "best_start_ms": span[0],
            "best_end_ms": span[1],
            "evidence_support_ms": [span],
            "badge": "possible",
            "flags": [],
            "suppressed": suppressed,
        }
    )


def _episodes_file(*episodes: EpisodeRecord) -> EpisodesFile:
    base = EpisodesFile.model_validate(
        json.loads((GOLDEN / "episodes.json").read_text(encoding="utf-8"))
    )
    return base.model_copy(update={"episodes": list(episodes), "gaps": []})


def _observation(
    window: WindowRecord, *, artist: str, title: str, status: str = "match"
) -> ObservationRecord:
    base = ObservationRecord.model_validate(
        json.loads((GOLDEN / "observation.json").read_text(encoding="utf-8"))
    )
    return base.model_copy(
        update={
            "id": f"{window.support_ms[0] + 1:040x}",
            "status": status,
            "support_ms": window.support_ms,
            "mix_span_ms": window.support_ms,
            "logical_trial_id": window.id,
            "raw_label": base.raw_label.model_copy(update={"artist": artist, "title": title}),
            "source_ids": [f"window:{window.id}"],
        }
    )


def _energy_from(table: dict[str, float]) -> Callable[[WindowRecord], float]:
    return lambda window: table.get(str(window.support_ms[0]), 0.0)


def _candidates(items: list[dict]) -> list[SecondaryCandidate]:
    return [
        SecondaryCandidate(item["priority"], tuple(item["span"]), item.get("episode_id"))
        for item in items
    ]


def _starts(picks: tuple[SecondaryPick, ...] | list[SecondaryPick]) -> list[int]:
    return [pick.window.support_ms[0] for pick in picks]


# --------------------------------------------------------------------------------------------------
# End-to-end plumbing
# --------------------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
# The recipe identity
# --------------------------------------------------------------------------------------------------
def test_deep_algorithm_version_is_bumped_and_a_targeting_1_result_is_incompatible() -> None:
    """The free-first additive recipe retired the paid-first ``targeting:1`` one: a Deep result
    that scheduler produced is never served as today's Deep (its raw answers stay reusable)."""

    assert DEEP_RECIPE.algorithm_version == "additive:1,fusion:4"
    assert get_recipe("deep", primary_density=2).algorithm_version == "additive:1,fusion:4"
    assert FREE_RECIPE.algorithm_version == "fusion:4"
    previous = replace(DEEP_RECIPE, algorithm_version="targeting:1,fusion:4")
    assert previous.recipe_id != DEEP_RECIPE.recipe_id  # a bump changes the recipe identity
    from id_detector.compat import fusion_stale_only, versions_current

    stored = {
        "recipe_name": "deep",
        "algorithm_version": previous.algorithm_version,
        "adapter_versions": dict(previous.adapter_versions),
        "status": "complete",
    }
    assert not versions_current(stored)
    # Nor is it a mere fusion bump that could be re-fused offline and served as today's recipe.
    assert not fusion_stale_only(stored)
    assert stored["adapter_versions"] == dict(DEEP_RECIPE.adapter_versions)  # only the version


# --------------------------------------------------------------------------------------------------
# Capacity, reserve, allocation
# --------------------------------------------------------------------------------------------------
def test_capacity_reserve_and_allocation_scale_with_duration() -> None:
    case = _fixture("duration-scaling")
    expect = case["expect"]
    capacity = secondary_capacity(case["duration_ms"], case["clips_per_minute"])
    reserve = secondary_reserve(capacity, case["reserve_fraction"])
    assert (capacity, reserve, capacity - reserve) == (
        expect["capacity"],
        expect["reserve"],
        expect["allocation"],
    )
    assert secondary_capacity(7_200_000, 2) == 240  # 120 min -> C = 240
    assert secondary_reserve(240, 0.25) == 60
    assert [secondary_reserve(c, 0.25) for c in (0, 1, 2, 3, 4, 7, 8, 12)] == [
        0,
        0,
        0,
        0,
        1,
        1,
        2,
        3,
    ]
    windows = _windows(case["duration_ms"], **case["windows"])
    picks = allocate_secondary_windows(
        windows,
        _candidates(case["candidates"]),
        allocation=capacity - reserve,
        min_intersection_ms=case["min_intersection_ms"],
    )
    assert len(picks) == expect["picks"] == 180
    assert len({pick.window.id for pick in picks}) == 180
    assert [pick.round for pick in picks] == ["first"] + ["proportional"] * 179


@pytest.mark.parametrize(
    "name",
    ["hint-only-first", "overflow", "largest-remainder-ties", "replacement-after-duplicate"],
)
def test_scheduler_fixture_allocates_exactly_the_expected_windows(name: str) -> None:
    case = _fixture(name)
    expect = case["expect"]
    capacity = secondary_capacity(case["duration_ms"], case["clips_per_minute"])
    reserve = secondary_reserve(capacity, case["reserve_fraction"])
    assert (capacity, reserve, capacity - reserve) == (
        expect["capacity"],
        expect["reserve"],
        expect["allocation"],
    )
    picks = allocate_secondary_windows(
        _windows(case["duration_ms"], **case["windows"]),
        _candidates(case["candidates"]),
        allocation=capacity - reserve,
        min_intersection_ms=case["min_intersection_ms"],
        energy=_energy_from(case["energy"]),
    )
    assert _starts(picks) == expect["starts"]
    assert [pick.round for pick in picks] == expect["rounds"]
    assert len({pick.window.id for pick in picks}) == len(picks)


def test_overflow_makes_nine_first_round_picks_and_nothing_else() -> None:
    case = _fixture("overflow")
    assert len(case["candidates"]) == 30
    picks = allocate_secondary_windows(
        _windows(case["duration_ms"], **case["windows"]),
        _candidates(case["candidates"]),
        allocation=9,
        min_intersection_ms=4_000,
    )
    assert len(picks) == 9 and {pick.round for pick in picks} == {"first"}
    # Candidates 10-30 never get a window: the allocation ran out in step (i).
    assert {pick.candidate.span[0] for pick in picks} == {k * 12_000 for k in range(9)}


def test_a_span_with_no_eligible_windows_returns_its_quota_to_the_others() -> None:
    windows = _windows(90_000, hop_ms=9_000, window_ms=12_000)
    # A 3 s listed sliver can never make a window eligible (>= 4 s); its shares go to the blank.
    candidates = [
        SecondaryCandidate("listed_not_confident", (40_000, 43_000), "a" * 40),
        SecondaryCandidate("blank", (0, 30_000)),
    ]
    picks = allocate_secondary_windows(windows, candidates, allocation=3, min_intersection_ms=4_000)
    assert [pick.candidate.priority for pick in picks] == ["blank"] * 3
    assert _starts(picks) == [0, 9_000, 18_000]
    # A span with fewer eligible windows than its quota keeps what it can; the surplus is shared
    # out again among the spans that still have windows.
    candidates = [
        SecondaryCandidate("listed_not_confident", (0, 12_000), "b" * 40),  # window 0 only
        SecondaryCandidate("listed_not_confident", (20_000, 80_000), "c" * 40),
    ]
    picks = allocate_secondary_windows(windows, candidates, allocation=6, min_intersection_ms=4_000)
    assert len(picks) == 6 and len({pick.window.id for pick in picks}) == 6
    assert sum(pick.candidate.span == (0, 12_000) for pick in picks) == 1
    assert sum(pick.candidate.span == (20_000, 80_000) for pick in picks) == 5
    # Nothing eligible anywhere: no pick, no crash.
    assert (
        allocate_secondary_windows(
            windows,
            [SecondaryCandidate("blank", (88_000, 90_000))],
            allocation=2,
            min_intersection_ms=4_000,
        )
        == ()
    )


def test_a_zero_length_span_never_divides_the_proportional_share_by_zero() -> None:
    """With no intersection floor a zero-length span is "eligible" for every window yet carries
    no proportional weight, so step (ii) must skip it rather than divide by a zero total.
    ``blank_spans`` emits such a span whenever its ``min_ms`` floor is 0."""

    assert blank_spans(_episodes_file(_episode((0, 30_000))), 30_000, min_ms=0) == [
        (0, 0),
        (30_000, 30_000),
    ]
    windows = _windows(60_000, hop_ms=9_000, window_ms=12_000)
    empty = [
        SecondaryCandidate("blank", (5_000, 5_000)),
        SecondaryCandidate("blank", (7_000, 7_000)),
    ]
    # Step (i) still gives each candidate its one window; step (ii) has nothing to share out.
    assert _starts(
        allocate_secondary_windows(windows, empty, allocation=5, min_intersection_ms=0)
    ) == [0, 9_000]
    assert distribute_secondary_windows(windows, empty, quota=3, min_intersection_ms=0) == ()
    # A span with real length beside them takes the whole proportional quota.
    mixed = [*empty, SecondaryCandidate("blank", (0, 30_000))]
    picks = allocate_secondary_windows(windows, mixed, allocation=5, min_intersection_ms=0)
    assert len(picks) == 5 and sum(pick.round == "proportional" for pick in picks) == 2


def test_ranking_needs_the_minimum_intersection_then_energy_then_start() -> None:
    windows = _windows(60_000, hop_ms=9_000, window_ms=12_000)
    span = (21_000, 45_000)
    # 9-21 overlaps 0 s, 18-30 overlaps 9 s, 27-39 and 36-48 overlap 12 s and 9 s, 45-57 0 s.
    ranked = rank_windows(windows, span, min_intersection_ms=4_000)
    assert _starts(
        [SecondaryPick(w, SecondaryCandidate("blank", span), "first") for w in ranked]
    ) == [
        27_000,
        18_000,
        36_000,
    ]
    # Exactly the minimum is eligible; one millisecond less is not.
    assert [
        w.support_ms[0] for w in rank_windows(windows, (44_000, 60_000), min_intersection_ms=4_000)
    ] == [
        45_000,
        36_000,
    ]
    assert [
        w.support_ms[0] for w in rank_windows(windows, (44_001, 60_000), min_intersection_ms=4_000)
    ] == [
        45_000,
    ]
    # Equal intersections: the louder window first, then the earlier start.
    loud = _energy_from({"36000": 0.8, "18000": 0.8})
    assert [
        w.support_ms[0]
        for w in rank_windows(windows, (18_000, 48_000), min_intersection_ms=4_000, energy=loud)
    ] == [18_000, 36_000, 27_000]


# --------------------------------------------------------------------------------------------------
# The reserve
# --------------------------------------------------------------------------------------------------
def test_confirmation_windows_are_the_blank_s_farthest_eligible_pair_30_s_apart() -> None:
    windows = _windows(180_000, hop_ms=9_000, window_ms=12_000)
    blank = (48_000, 180_000)
    probe = windows[6]  # 54-66 s, a blank probe
    common = dict(
        within=blank,
        probe_id=probe.id,
        search_ms=45_000,
        min_separation_ms=30_000,
        min_intersection_ms=4_000,
    )
    # Starts in [9 s, 111 s]; eligible for the blank means start >= 45 s (window 5 overlaps 9 s);
    # the farthest pair is 45 s and 108 s -- 63 s apart.  Windows 9-21 s (inside a listed track
    # next door) are in range but not eligible for this blank.
    pair = confirmation_windows(windows, probe.support_ms, picked=(), limit=2, **common)
    assert [w.support_ms[0] for w in pair] == [45_000, 108_000]
    # Already-picked windows are skipped (same-engine exclusion); the next farthest pair serves.
    pair = confirmation_windows(
        windows, probe.support_ms, picked={windows[5].id, windows[12].id}, limit=2, **common
    )
    assert [w.support_ms[0] for w in pair] == [63_000, 99_000]
    # One reserve clip left: the earliest eligible window only.
    assert [
        w.support_ms[0]
        for w in confirmation_windows(windows, probe.support_ms, picked=(), limit=1, **common)
    ] == [45_000]
    # No reserve: nothing.
    assert confirmation_windows(windows, probe.support_ms, picked=(), limit=0, **common) == ()
    # Only one window in reach: one.  Two in reach but under 30 s apart: one.
    few = [windows[6], windows[7]]
    assert [
        w.support_ms[0]
        for w in confirmation_windows(few, probe.support_ms, picked=(), limit=2, **common)
    ] == [63_000]
    close = [windows[6], windows[7], windows[8]]
    assert [
        w.support_ms[0]
        for w in confirmation_windows(close, probe.support_ms, picked=(), limit=2, **common)
    ] == [63_000]
    # Nothing eligible in reach at all.
    assert confirmation_windows([windows[6]], probe.support_ms, picked=(), limit=2, **common) == ()


def test_reserve_serves_confirmations_first_come_until_exhausted() -> None:
    windows = _windows(360_000, hop_ms=9_000, window_ms=12_000)
    blanks = ((48_000, 108_000), (138_000, 198_000), (228_000, 360_000))
    probes = (windows[6], windows[19], windows[27])
    discoveries = [
        (probe.support_ms, probe.id, blank) for probe, blank in zip(probes, blanks, strict=True)
    ]
    common = dict(
        picked={probe.id for probe in probes},
        search_ms=45_000,
        min_separation_ms=30_000,
        min_intersection_ms=4_000,
    )
    # R = 3 (C = 12 for six minutes): two clips to the first discovery, the one left to the
    # second, none to the third -- it is listed uncorroborated.
    served, unused = serve_confirmations(windows, discoveries, reserve=3, **common)
    assert [[w.support_ms[0] for w in chosen] for chosen in served] == [
        [45_000, 99_000],
        [135_000],
        [],
    ]
    assert unused == 0
    # R = 4: two each to the first two, none to the third.  R = 7: everyone served, one unused.
    served, unused = serve_confirmations(windows, discoveries, reserve=4, **common)
    assert [len(chosen) for chosen in served] == [2, 2, 0] and unused == 0
    served, unused = serve_confirmations(windows, discoveries, reserve=7, **common)
    assert [len(chosen) for chosen in served] == [2, 2, 2] and unused == 1
    # The third probe (243-255 s) reaches starts in [198 s, 300 s]: 225 s and 297 s, 72 s apart.
    assert served[2][0].support_ms[0] == 225_000 and served[2][1].support_ms[0] == 297_000
    # R = 0: nothing is served and nothing is consumed.
    served, unused = serve_confirmations(windows, discoveries, reserve=0, **common)
    assert [len(chosen) for chosen in served] == [0, 0, 0] and unused == 0
    # A confirmation window never repeats a window served to an earlier discovery.
    served, _ = serve_confirmations(windows, discoveries, reserve=7, **common)
    ids = [w.id for chosen in served for w in chosen]
    assert len(ids) == len(set(ids))


def test_unused_reserve_is_shared_out_proportionally_without_repeats() -> None:
    windows = _windows(90_000, hop_ms=9_000, window_ms=12_000)
    candidates = [
        SecondaryCandidate("listed_not_confident", (0, 30_000), "a" * 40),
        SecondaryCandidate("blank", (30_000, 90_000)),
    ]
    picked = {windows[0].id, windows[4].id}
    spare = distribute_secondary_windows(
        windows, candidates, quota=3, min_intersection_ms=4_000, picked=picked
    )
    # 3 x 30/90 = 1 for the listed span, 3 x 60/90 = 2 for the blank; nothing already picked.
    assert [pick.round for pick in spare] == ["reserve_unused"] * 3
    assert sorted(pick.candidate.span[0] for pick in spare) == [0, 30_000, 30_000]
    assert not {pick.window.id for pick in spare} & picked
    assert (
        distribute_secondary_windows(windows, candidates, quota=0, min_intersection_ms=4_000) == ()
    )


def test_discoveries_are_blank_probes_naming_an_identity_no_listed_episode_carries() -> None:
    windows = _windows(90_000, hop_ms=9_000, window_ms=12_000)
    identities = IdentitiesRecord.model_validate(
        json.loads((GOLDEN / "identities.json").read_text(encoding="utf-8"))
    )
    listed = identities.candidates[0].model_copy(
        update={"member_nodes": [*identities.candidates[0].member_nodes, "text:known act|old cut"]}
    )
    hidden = listed.model_copy(
        update={"canonical_id": "f" * 40, "member_nodes": ["text:hidden act|buried cut"]}
    )
    identities = identities.model_copy(update={"candidates": [listed, hidden]})
    episodes = _episodes_file(
        _episode((0, 21_000), candidate_id=listed.canonical_id),
        _episode((60_000, 80_000), candidate_id=hidden.canonical_id, suppressed="scatter"),
    )
    # Only the listed episode's identity counts as known; a suppressed one does not.
    known = listed_text_keys(episodes, identities)
    assert known == frozenset({"known act|old cut"})

    blank = SecondaryCandidate("blank", (21_000, 60_000))
    track = SecondaryCandidate("listed_not_confident", (0, 21_000), "a" * 40)
    picks = [
        SecondaryPick(windows[0], track, "first"),
        SecondaryPick(windows[3], blank, "first"),
        SecondaryPick(windows[4], blank, "proportional"),
        SecondaryPick(windows[5], blank, "proportional"),
    ]
    observations = [
        # A listed-span probe naming something new is not a blank discovery.
        _observation(windows[0], artist="Someone", title="Else"),
        # Blank probes: a known identity, a new one, the same new one again, a no-match.
        _observation(windows[3], artist="Known Act", title="Old Cut"),
        _observation(windows[4], artist="New Act", title="Fresh Cut"),
        _observation(windows[5], artist="NEW ACT", title="fresh cut"),
        _observation(windows[2], artist="", title="", status="no_match"),
    ]
    found = new_identity_discoveries(observations, picks, known)
    assert [(item.text_key, item.pick.window.support_ms[0]) for item in found] == [
        ("new act|fresh cut", 36_000)
    ]
    # Discoveries come in start order, whatever order the observations arrived in.
    observations.append(_observation(windows[3], artist="Later Act", title="Third Cut"))
    found = new_identity_discoveries(reversed(observations), picks, known)
    assert [item.pick.window.support_ms[0] for item in found] == [27_000, 36_000]


def test_window_rms_energy_reads_each_clip_once(tmp_path: Path) -> None:
    def write(path: Path, amplitude: int, width: int = 2) -> None:
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(width)
            out.setframerate(16_000)
            frames = bytes(
                b
                for _ in range(1_600)
                for b in (
                    amplitude.to_bytes(width, "little", signed=True)
                    if width == 2
                    else bytes([amplitude])
                )
            )
            out.writeframes(frames)

    (tmp_path / "windows").mkdir()
    write(tmp_path / "windows" / "quiet.wav", 1_000)
    write(tmp_path / "windows" / "loud.wav", 20_000)
    write(tmp_path / "windows" / "byte.wav", 200, width=1)
    assert window_rms_energy(tmp_path / "windows" / "quiet.wav") == pytest.approx(1_000 / 32_768)
    assert window_rms_energy(tmp_path / "windows" / "loud.wav") == pytest.approx(20_000 / 32_768)
    assert window_rms_energy(tmp_path / "windows" / "byte.wav") == 0.0  # not 16-bit: no ranking
    quiet = _window(0).model_copy(update={"wav_path": "windows/quiet.wav"})
    loud = _window(9_000).model_copy(update={"wav_path": "windows/loud.wav"})
    energy = energy_reader(tmp_path)
    assert energy(loud) > energy(quiet)
    (tmp_path / "windows" / "loud.wav").unlink()
    assert energy(loud) == pytest.approx(20_000 / 32_768)  # cached: the clip is not re-read


def test_a_clip_that_cannot_be_read_ranks_as_zero_instead_of_failing_the_run(
    tmp_path: Path,
) -> None:
    """Energy is only the second ranking key and the secondary runs after the paid primary is
    already spent, so a pruned or corrupt clip falls back to 0 — the tie then falls through to
    start order — rather than raising and losing the whole run."""

    (tmp_path / "windows").mkdir()
    (tmp_path / "windows" / "corrupt.wav").write_bytes(b"not a wav at all")
    (tmp_path / "windows" / "truncated.wav").write_bytes(b"RIFF\x08\x00\x00\x00WAVEfmt ")
    missing = _window(0).model_copy(update={"wav_path": "windows/gone.wav"})
    corrupt = _window(9_000).model_copy(update={"wav_path": "windows/corrupt.wav"})
    truncated = _window(18_000).model_copy(update={"wav_path": "windows/truncated.wav"})
    energy = energy_reader(tmp_path)
    assert (energy(missing), energy(corrupt), energy(truncated)) == (0.0, 0.0, 0.0)
    picks = allocate_secondary_windows(
        [missing, corrupt, truncated],
        [SecondaryCandidate("blank", (0, 30_000))],
        allocation=2,
        min_intersection_ms=4_000,
        energy=energy,
    )
    assert _starts(picks) == [0, 9_000]


# --------------------------------------------------------------------------------------------------
# End to end through ``_analyse``
# --------------------------------------------------------------------------------------------------
def test_blank_spans_cover_the_tail_after_a_stopped_primary() -> None:
    # The scheduler-level half of the stopped-primary fixture: only the A hull is listed.
    episodes = _episodes_file(_episode((0, 21_000)))
    assert blank_spans(episodes, 60_000, min_ms=4_000) == [(21_000, 60_000)]
    picks = allocate_secondary_windows(
        _windows(60_000, hop_ms=9_000, window_ms=12_000),
        [
            SecondaryCandidate("listed_not_confident", (0, 21_000), "a" * 40),
            SecondaryCandidate("blank", (21_000, 60_000)),
        ],
        allocation=2,
        min_intersection_ms=4_000,
    )
    assert [(pick.candidate.priority, pick.window.support_ms[0]) for pick in picks] == [
        ("listed_not_confident", 0),
        ("blank", 27_000),
    ]
