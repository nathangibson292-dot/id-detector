"""Phase 1b-i gate: the ``targeting:1`` secondary scheduler (plan §2.3.4 step 4) over the fixtures
under ``tests/fixtures/deep/``.

Two fixture shapes live there.  *Scheduler* fixtures describe synthetic frozen windows (a hop and
a window length over the duration), per-window RMS energies, the first fuse's candidate spans and
the picks the allocation must make.  *Provider* fixtures are fake-provider scripts (with the tone
length to generate) driven end to end through ``_analyse`` — every run is offline.
"""

from __future__ import annotations

import asyncio
import json
import os
import wave
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from id_detector import cli
from id_detector.contracts import (
    EpisodeRecord,
    EpisodesFile,
    IdentitiesRecord,
    ObservationRecord,
    WindowRecord,
)
from id_detector.io import native_path, path_is_file
from id_detector.present.bundles import shown_result_dir
from id_detector.providers.base import AppConfig
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
from scripts.make_audio_fixtures import generate
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff

ROOT = Path(__file__).resolve().parents[1]
DEEP = ROOT / "tests" / "fixtures" / "deep"
AUDIO_60 = ROOT / "tests" / "fixtures" / "audio" / "tone-60s.wav"
GOLDEN = ROOT / "tests" / "golden"

HOP_S = 9  # the default window schedule: 12 s windows every 9 s


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
@pytest.fixture(scope="session")
def tone(tmp_path_factory: pytest.TempPathFactory) -> Callable[[int], Path]:
    """The committed 60 s tone, or a longer one generated once per session (same generator)."""

    root = tmp_path_factory.mktemp("deep-audio")

    def make(seconds: int) -> Path:
        if seconds == 60:
            return AUDIO_60
        path = root / f"tone-{seconds}s.wav"
        if not path.exists():
            generate(path, seconds)
        return path

    return make


def _run(
    tmp_path: Path, name: str, audio: Path, *, density: int = 1
) -> tuple[int, FakeAudD, FakeShazamHTTP, dict[str, object], Path]:
    script = _fixture(name)
    audd = FakeAudD(script)
    shazam = FakeShazamHTTP(script)
    work_root = tmp_path / "work"
    code = asyncio.run(
        cli._analyse(
            str(audio),
            work_root=work_root,
            print_raw=False,
            refresh=False,
            max_requests=100,
            tracklist=None,
            no_hints=True,
            app_config=AppConfig(
                transforms_policy="off",
                recognise_concurrency=1,
                shazam_requests_per_minute=1_000_000,
                audd_requests_per_minute=1_000_000,
            ),
            max_generations=0,
            novelty=False,
            recipe=get_recipe("deep", primary_density=density),
            paid_scan_adapters={"audd": audd},
            shazam_http_client=shazam,
            paid_sleep=no_backoff,
        )
    )
    (journal,) = work_root.rglob("invocations.jsonl")
    entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    return code, audd, shazam, entries[-1], journal.parent


def _shazam_indices(shazam: FakeShazamHTTP) -> list[int]:
    return [int(item["window"]) for item in shazam.attempts]  # type: ignore[call-overload]


def _audd_indices(audd: FakeAudD) -> set[int]:
    return {int(item["window"]) for item in audd.attempts}  # type: ignore[call-overload]


def _episode_hulls(media_dir: Path) -> list[tuple[int, int, str | None]]:
    payload = json.loads((media_dir / "fuse" / "episodes.json").read_text(encoding="utf-8"))
    return [
        (
            min(start for start, _ in item["evidence_support_ms"]),
            max(end for _, end in item["evidence_support_ms"]),
            item["suppressed"],
        )
        for item in payload["episodes"]
    ]


def _listed_inside(hulls: list[tuple[int, int, str | None]], blank: tuple[int, int]) -> bool:
    """A non-suppressed re-fused episode lying in ``blank``: its hull overlaps the blank by at
    least a window and starts no earlier than one window before it (a confirmation clip may
    overhang the blank's edge by a few seconds; a neighbouring listed track does not count)."""

    return any(
        suppressed is None
        and min(end, blank[1]) - max(start, blank[0]) >= 12_000
        and start >= blank[0] - 12_000
        for start, end, suppressed in hulls
    )


def _expected_confirmations(
    probe: int, blank: range, taken: set[int], *, limit: int = 2
) -> list[int]:
    """Plan §2.3.4 step 4's reserve rule in window indices: the windows eligible for the blank
    (``blank`` is its index range) starting within 45 s of the probe's ``[m0, m1]``, the probe
    and every window already picked excluded; the farthest pair when >= 30 s apart, else the
    earliest one."""

    low, high = probe * HOP_S - 45, probe * HOP_S + 12 + 45
    eligible = [
        index
        for index in blank
        if low <= index * HOP_S <= high and index != probe and index not in taken
    ]
    if limit >= 2 and eligible and (eligible[-1] - eligible[0]) * HOP_S >= 30:
        return [eligible[0], eligible[-1]]
    return eligible[:1]


# --------------------------------------------------------------------------------------------------
# The recipe identity
# --------------------------------------------------------------------------------------------------
def test_deep_algorithm_version_is_bumped_and_a_targeting_0_result_is_incompatible() -> None:
    assert DEEP_RECIPE.algorithm_version == "targeting:1,fusion:4"
    assert get_recipe("deep", primary_density=2).algorithm_version == "targeting:1,fusion:4"
    assert FREE_RECIPE.algorithm_version == "fusion:4"
    previous = replace(DEEP_RECIPE, algorithm_version="targeting:0,fusion:1")
    assert previous.recipe_id != DEEP_RECIPE.recipe_id  # a bump changes the recipe identity
    # §3.4: a stored result is served only with an *equal* algorithm_version (and adapter
    # versions).  A Deep result the Phase-0 scheduler produced fails that test against the new
    # recipe, so it is never served; its raw provider responses remain reusable.
    stored = {
        "requested_recipe_id": previous.recipe_id,
        "algorithm_version": previous.algorithm_version,
        "adapter_versions": dict(previous.adapter_versions),
        "status": "complete",
    }
    compatible = (
        stored["status"] == "complete"
        and stored["algorithm_version"] == DEEP_RECIPE.algorithm_version
        and stored["adapter_versions"] == dict(DEEP_RECIPE.adapter_versions)
    )
    assert not compatible
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
@pytest.mark.parametrize("density", [1, 2])
def test_overlap_allowed_probes_coincide_with_audd_windows(
    tmp_path: Path, tone: Callable[[int], Path], density: int
) -> None:
    case = _fixture("overlap-allowed")
    code, audd, shazam, entry, _media_dir = _run(
        tmp_path, "overlap-allowed", tone(case["audio_s"]), density=density
    )
    assert code == 0 and entry["status"] == "complete"
    counts = entry["counts"]
    assert counts["paid_planned"] == {1: 7, 2: 4}[density]  # type: ignore[index]
    assert counts["secondary_capacity"] == 2 and counts["secondary_reserve"] == 0  # type: ignore[index]
    assert counts["secondary_allocated"] == counts["secondary_resolved"] == 2  # type: ignore[index]
    assert shazam.requests == 2
    # Shazam windows may and should coincide with AudD windows (E-C2): the only exclusion is a
    # window already sent to Shazam in this run.
    assert set(_shazam_indices(shazam)) & _audd_indices(audd)
    assert len(set(_shazam_indices(shazam))) == 2


def test_secondary_under_80_percent_is_degraded(
    tmp_path: Path, tone: Callable[[int], Path]
) -> None:
    case = _fixture("secondary-80pct")
    code, _audd, shazam, entry, media_dir = _run(tmp_path, "secondary-80pct", tone(case["audio_s"]))
    assert code == 0
    assert (entry["status"], entry["reason"], entry["achieved"]) == (
        "degraded",
        "secondary_not_achieved",
        "deep",
    )
    counts = entry["counts"]
    assert counts["secondary_allocated"] == 2 and counts["secondary_resolved"] == 1  # type: ignore[index]
    assert shazam.requests == 2
    assert entry["algorithm_version"] == "targeting:1,fusion:4"
    tracklist = json.loads(
        (shown_result_dir(media_dir) / "tracklist.json").read_text(encoding="utf-8")
    )
    assert tracklist["status"] == "degraded"  # shown with a banner, never served


def test_stopped_primary_leaves_a_blank_tail_the_secondary_probes(
    tmp_path: Path, tone: Callable[[int], Path]
) -> None:
    case = _fixture("stopped-primary-tail")
    code, audd, shazam, entry, _media_dir = _run(
        tmp_path, "stopped-primary-tail", tone(case["audio_s"])
    )
    assert code == 0
    assert (entry["status"], entry["reason"]) == ("partial", "provider_unavailable_midrun")
    assert _audd_indices(audd) == {0, 1, 2}  # two matches, then the quota refusal
    # The fuse records the 21-60 s tail as an unresolved boundary, not a gap; the secondary
    # treats it as blank and spends its second clip there (>= 27 s), the first on the A track.
    probed = _shazam_indices(shazam)
    assert len(probed) == 2 and min(probed) <= 1 and max(probed) >= 3
    counts = entry["counts"]
    assert counts["secondary_allocated"] == counts["secondary_resolved"] == 2  # type: ignore[index]
    # The tail probe found a track AudD never reported; with R = 0 it is listed uncorroborated.
    assert counts["secondary_discoveries"] == 1  # type: ignore[index]
    assert counts["secondary_confirmation_clips"] == 0  # type: ignore[index]
    assert counts["secondary_uncorroborated"] == 1  # type: ignore[index]


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


def test_reserve_confirms_a_new_identity_with_two_clips_30_s_apart(
    tmp_path: Path, tone: Callable[[int], Path]
) -> None:
    case = _fixture("reserve-confirmation")
    code, audd, shazam, entry, media_dir = _run(
        tmp_path, "reserve-confirmation", tone(case["audio_s"])
    )
    assert code == 0 and entry["status"] == "complete"
    counts = entry["counts"]
    # Six minutes: C = 12, R = 3, A = 9.  AudD listed A and B (0-48 s); the rest is blank.
    assert (counts["secondary_capacity"], counts["secondary_reserve"]) == (12, 3)  # type: ignore[index]
    assert counts["paid_resolved"] == 40 and _audd_indices(audd) == set(range(40))  # type: ignore[index]
    probed = _shazam_indices(shazam)
    first_pass, second_pass = probed[:9], probed[9:]
    assert len(probed) == 12 == len(set(probed))  # never the same Shazam window twice
    # Every blank probe found D, a track AudD never reported: one discovery (the same identity
    # again is not a second one), confirmed from the reserve with two clips >= 30 s apart, and
    # the third reserve clip handed back to the spans by step (ii).
    assert counts["secondary_discoveries"] == 1  # type: ignore[index]
    assert counts["secondary_confirmed"] == 1  # type: ignore[index]
    assert counts["secondary_confirmation_clips"] == 2  # type: ignore[index]
    assert counts["secondary_uncorroborated"] == 0  # type: ignore[index]
    assert counts["secondary_allocated"] == counts["secondary_resolved"] == 12  # type: ignore[index]
    assert len(second_pass) == 3
    probe = min(index for index in first_pass if index >= 5)  # the earliest blank probe
    expected = _expected_confirmations(probe, range(5, 40), set(first_pass))
    assert len(expected) == 2 and (expected[1] - expected[0]) * HOP_S >= 30
    assert set(expected) <= set(second_pass)
    (spare,) = set(second_pass) - set(expected)
    assert spare not in first_pass
    # The confirmations are their own Shazam invocation beside the first pass and AudD's.
    invocations = media_dir / "recognise" / "invocations"
    assert (
        sum(
            path_is_file(invocations / name / "observations.gen0.jsonl")
            for name in os.listdir(native_path(invocations))
        )
        == 3
    )
    # The re-fuse lists D inside the blank.
    assert _listed_inside(_episode_hulls(media_dir), (48_000, 360_000))


def test_reserve_exhausted_lists_the_third_discovery_uncorroborated(
    tmp_path: Path, tone: Callable[[int], Path]
) -> None:
    case = _fixture("reserve-exhausted")
    code, audd, shazam, entry, media_dir = _run(
        tmp_path, "reserve-exhausted", tone(case["audio_s"])
    )
    assert code == 0 and entry["status"] == "complete"
    counts = entry["counts"]
    assert (counts["secondary_capacity"], counts["secondary_reserve"]) == (12, 3)  # type: ignore[index]
    assert _audd_indices(audd) == set(range(40))
    probed = _shazam_indices(shazam)
    first_pass, second_pass = probed[:9], probed[9:]
    assert len(probed) == 12 == len(set(probed))
    # Three blanks (48-108 s, 138-198 s, 228-360 s) hide D, F and G.  Step (i) probes each blank
    # once; first-come, D takes two reserve clips, F the one left, G none.
    assert counts["secondary_discoveries"] == 3  # type: ignore[index]
    assert counts["secondary_confirmed"] == 2  # type: ignore[index]
    assert counts["secondary_confirmation_clips"] == 3  # type: ignore[index]
    assert counts["secondary_uncorroborated"] == 1  # type: ignore[index]
    assert counts["secondary_allocated"] == counts["secondary_resolved"] == 12  # type: ignore[index]
    d_probe = min(index for index in first_pass if 5 <= index <= 11)
    f_probe = min(index for index in first_pass if 15 <= index <= 21)
    assert any(25 <= index <= 39 for index in first_pass)  # G was found...
    d_pair = _expected_confirmations(d_probe, range(5, 12), set(first_pass))
    f_one = _expected_confirmations(f_probe, range(15, 22), set(first_pass) | set(d_pair), limit=1)
    assert len(d_pair) == 2 and (d_pair[1] - d_pair[0]) * HOP_S >= 30 and len(f_one) == 1
    assert sorted(second_pass) == sorted([*d_pair, *f_one])  # ...and got no reserve clip
    # G is still listed -- uncorroborated -- by the re-fuse, as are D and F.
    hulls = _episode_hulls(media_dir)
    for blank in ((48_000, 108_000), (138_000, 198_000), (228_000, 360_000)):
        assert _listed_inside(hulls, blank), (blank, hulls)
