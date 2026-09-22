"""Phase 0a-iv gate: the plan §2.3.5 status matrix, ``--allow-degrade`` and the secondary
scheduler's candidate classes.  Every run is offline: the fakes script both providers over the
60 s fixture (seven frozen windows, so the Deep secondary capacity is ``C = ceil(1 min x 2) = 2``
and the reserve ``R = floor(0.25 x 2) = 0``)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from id_detector import cli, pipeline
from id_detector.cli import _achieved, _run_status
from id_detector.contracts import EpisodeRecord, EpisodesFile, Transform, WindowRecord
from id_detector.present.bundles import shown_result_dir
from id_detector.providers.base import AppConfig
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE, Recipe
from id_detector.secondary_targeting import (
    PRIORITY,
    allocate_secondary_windows,
    blank_spans,
    secondary_capacity,
    select_secondary_candidates,
)
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff

ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "tests" / "fixtures" / "audio" / "tone-60s.wav"
SCRIPTS = ROOT / "tests" / "fakes" / "scripts"
GOLDEN = ROOT / "tests" / "golden"


def _entry(work_root: Path) -> tuple[Path, dict[str, object]]:
    (path,) = work_root.rglob("invocations.jsonl")
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return path.parent, entries[-1]


def _tracklist(media_dir: Path) -> dict[str, object]:
    return json.loads((shown_result_dir(media_dir) / "tracklist.json").read_text(encoding="utf-8"))


def _run(
    tmp_path: Path,
    script_name: str,
    *,
    recipe: Recipe = DEEP_RECIPE,
    allow_degrade: bool = False,
    app_config: AppConfig | None = None,
    progress_messages: list[str] | None = None,
) -> tuple[int, FakeAudD, FakeShazamHTTP, dict[str, object], Path]:
    script = SCRIPTS / script_name
    audd = FakeAudD(script)
    shazam = FakeShazamHTTP(script)
    work_root = tmp_path / "work"
    code = asyncio.run(
        cli._analyse(
            str(AUDIO),
            work_root=work_root,
            print_raw=False,
            refresh=False,
            max_requests=100,
            tracklist=None,
            no_hints=True,
            app_config=app_config
            or AppConfig(
                transforms_policy="off",
                recognise_concurrency=1,
                shazam_requests_per_minute=1_000_000,
                audd_requests_per_minute=1_000_000,
            ),
            max_generations=0,
            novelty=False,
            recipe=recipe,
            allow_degrade=allow_degrade,
            paid_scan_adapters={"audd": audd},
            shazam_http_client=shazam,
            paid_sleep=no_backoff,
            progress=(
                (lambda _phase, _done, _total, message: progress_messages.append(message))
                if progress_messages is not None
                else None
            ),
        )
    )
    media_dir, entry = _entry(work_root)
    return code, audd, shazam, entry, media_dir


# --------------------------------------------------------------------------------------------------
# Row 1 — terminal-provider / unreachable before any resolved attempt
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("script", "reason", "audd_calls"),
    [
        ("all-http-401.json", "auth_error", 1),
        ("quota-first.json", "quota_error", 1),
        # 0b-i: an unreachable provider is retried per window (1 + 3 bounded retries).
        ("all-timeout-pre.json", "timeout_pre", 28),
    ],
)
def test_provider_unavailable_before_any_resolved_attempt_is_exit_3_unspent_and_unstored(
    tmp_path: Path, script: str, reason: str, audd_calls: int
) -> None:
    code, audd, shazam, entry, media_dir = _run(tmp_path, script)
    assert code == 3
    assert entry["status"] == "provider_unavailable"
    assert entry["reason"] == reason
    assert entry["achieved"] is None
    assert entry["exit_code"] == 3
    # A terminal-provider outcome stops the sweep at once; an unreachable provider is retried per
    # window (the 0b-i bounded retries) but resolves nothing either way.
    assert audd.calls == audd_calls and audd.billed_units == 0
    assert entry["usd_e6_reserved"] == 36_750 and entry["usd_e6_spent"] == 0
    assert entry["costs"] == {"usd_e2": 0}
    assert shazam.requests == 0  # never a silent Free run
    assert not (media_dir / "present").exists()  # no result stored, nothing served later


# --------------------------------------------------------------------------------------------------
# Row 2 — terminal-provider after at least one resolved attempt
# --------------------------------------------------------------------------------------------------
def test_quota_error_after_two_matches_is_partial_provider_unavailable_midrun(
    tmp_path: Path,
) -> None:
    code, audd, shazam, entry, media_dir = _run(tmp_path, "quota-midrun.json")
    assert code == 0
    assert entry["status"] == "partial"
    assert entry["reason"] == "provider_unavailable_midrun"
    assert entry["achieved"] == "deep"
    assert audd.calls == 3  # two matches, then the quota refusal stops the primary
    assert entry["counts"]["paid_resolved"] == 2  # type: ignore[index]
    assert entry["usd_e6_spent"] == 10_000 and entry["usd_e2_spent"] == 1
    # The Deep secondary still runs over the two-track hull and the blank remainder.
    assert entry["counts"]["secondary_allocated"] == shazam.requests == 2  # type: ignore[index]
    assert (shown_result_dir(media_dir) / "index.html").is_file()
    tracklist = _tracklist(media_dir)
    assert (tracklist["status"], tracklist["reason"], tracklist["achieved"]) == (
        "partial",
        "provider_unavailable_midrun",
        "deep",
    )


# --------------------------------------------------------------------------------------------------
# Row 3 — primary achieved fraction not met
# --------------------------------------------------------------------------------------------------
def test_deep_primary_below_95_percent_is_partial_and_billed(tmp_path: Path) -> None:
    code, audd, _shazam, entry, _media_dir = _run(tmp_path, "primary-thin.json")
    assert code == 0
    assert entry["status"] == "partial"
    assert entry["reason"] == "primary_not_achieved"
    assert entry["counts"]["paid_resolved"] == 6  # type: ignore[index]  # 6/7 < 95 %
    assert audd.calls == 7 and audd.billed_units == 7  # an http_5xx unit is spent (§2.3.2)
    assert entry["usd_e6_spent"] == 35_000


def test_deep_primary_resolving_nothing_without_a_terminal_outcome_is_partial_unspent(
    tmp_path: Path,
) -> None:
    code, audd, shazam, entry, _media_dir = _run(tmp_path, "all-http-429.json")
    assert code == 0
    assert (entry["status"], entry["reason"]) == ("partial", "primary_not_achieved")
    # Every window is retried to the recipe's bound (1 + 3) and refunded each time.
    assert audd.calls == 28 and audd.billed_units == 0
    assert entry["usd_e6_spent"] == 0
    assert shazam.requests == 2  # the whole mix is blank: the secondary probes its capacity


def test_free_primary_below_80_percent_is_partial(tmp_path: Path) -> None:
    code, audd, shazam, entry, media_dir = _run(tmp_path, "free-thin.json", recipe=FREE_RECIPE)
    assert code == 0
    assert (entry["status"], entry["reason"]) == ("partial", "primary_not_achieved")
    assert entry["achieved"] == "free"
    assert shazam.requests == 7 and audd.calls == 0  # 5/7 resolved < 80 %
    assert entry["counts"]["failures"] == 2  # type: ignore[index]
    assert entry["usd_e6_reserved"] == entry["usd_e6_spent"] == 0
    assert _tracklist(media_dir)["status"] == "partial"


def test_free_primary_meeting_80_percent_is_complete(tmp_path: Path) -> None:
    code, _audd, shazam, entry, media_dir = _run(tmp_path, "gate0a-deep.json", recipe=FREE_RECIPE)
    assert code == 0
    assert (entry["status"], entry["reason"], entry["achieved"]) == ("complete", None, "free")
    assert shazam.requests == 7
    assert "secondary_allocated" not in entry["counts"]  # type: ignore[operator]
    assert _tracklist(media_dir)["achieved"] == "free"


# --------------------------------------------------------------------------------------------------
# Row 4 — primary met, secondary < 80 %
# --------------------------------------------------------------------------------------------------
def test_secondary_below_80_percent_is_degraded(tmp_path: Path) -> None:
    code, audd, shazam, entry, media_dir = _run(tmp_path, "secondary-thin.json")
    assert code == 0
    assert entry["status"] == "degraded"
    assert entry["reason"] == "secondary_not_achieved"
    assert entry["achieved"] == "deep"
    assert audd.calls == audd.billed_units == 7  # the primary was complete and is paid for
    assert entry["usd_e6_spent"] == 35_000
    assert shazam.requests == entry["counts"]["secondary_allocated"] == 2  # type: ignore[index]
    assert entry["counts"]["secondary_resolved"] == 0  # type: ignore[index]
    assert (shown_result_dir(media_dir) / "index.html").is_file()  # shown, with a banner
    assert _tracklist(media_dir)["status"] == "degraded"


# --------------------------------------------------------------------------------------------------
# Row 5 — all requirements met
# --------------------------------------------------------------------------------------------------
def test_all_requirements_met_is_complete_with_the_gate_money_figures(tmp_path: Path) -> None:
    code, audd, shazam, entry, media_dir = _run(tmp_path, "gate0a-deep.json")
    assert code == 0
    assert (entry["status"], entry["reason"], entry["achieved"]) == ("complete", None, "deep")
    assert entry["algorithm_version"] == "targeting:1,fusion:3"  # bumped in 1b-i
    assert entry["usd_e6_reserved"] == 36_750
    assert entry["usd_e6_spent"] == 35_000 and entry["usd_e2_spent"] == 4
    assert audd.calls == 7
    counts = entry["counts"]
    assert counts["secondary_capacity"] == 2  # type: ignore[index]
    assert counts["secondary_allocated"] == counts["secondary_resolved"] == 2  # type: ignore[index]
    assert shazam.requests == 2
    tracklist = _tracklist(media_dir)
    assert (tracklist["status"], tracklist["reason"], tracklist["achieved"]) == (
        "complete",
        None,
        "deep",
    )


# --------------------------------------------------------------------------------------------------
# Row 6 — reservation over the cap
# --------------------------------------------------------------------------------------------------
def test_budget_exhausted_is_exit_4_with_nothing_spent_and_no_result(tmp_path: Path) -> None:
    config = AppConfig(
        transforms_policy="off",
        recognise_concurrency=1,
        shazam_requests_per_minute=1_000_000,
        max_usd_e2=0,
    )
    code, audd, shazam, entry, media_dir = _run(tmp_path, "gate0a-deep.json", app_config=config)
    assert code == 4
    assert (entry["status"], entry["achieved"]) == ("budget_exhausted", None)
    assert entry["reason"] == "reservation_exceeds_cap"
    assert audd.calls == 0 and shazam.requests == 0
    assert entry["usd_e6_reserved"] == entry["usd_e6_spent"] == 0
    assert not (media_dir / "present").exists()


# --------------------------------------------------------------------------------------------------
# --allow-degrade
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("script", ["all-http-401.json", "all-timeout-pre.json"])
def test_allow_degrade_restarts_as_the_free_recipe_before_any_paid_work(
    tmp_path: Path, script: str
) -> None:
    messages: list[str] = []
    code, audd, shazam, entry, media_dir = _run(
        tmp_path, script, allow_degrade=True, progress_messages=messages
    )
    assert code == 0
    assert entry["status"] == "degraded"
    assert entry["reason"] == "provider_unavailable"
    assert entry["achieved"] == "free"
    # Requested stays Deep; the result was produced by the Free pipeline.
    assert entry["requested_recipe_id"] == DEEP_RECIPE.recipe_id
    assert entry["algorithm_version"] == FREE_RECIPE.algorithm_version == "fusion:3"
    # Zero further AudD attempts after the refusal; the free sweep covers every frozen window.
    assert audd.billed_units == 0
    assert shazam.requests == 7
    assert entry["counts"]["paid_attempts"] == audd.calls  # type: ignore[index]
    assert entry["usd_e6_spent"] == 0 and entry["costs"] == {"usd_e2": 0}
    assert entry["usd_e6_reserved"] == 36_750  # reserved, then released in full
    assert any("restarting as the free recipe" in message for message in messages)
    assert (shown_result_dir(media_dir) / "index.html").is_file()
    tracklist = _tracklist(media_dir)
    assert (tracklist["status"], tracklist["reason"], tracklist["achieved"]) == (
        "degraded",
        "provider_unavailable",
        "free",
    )


def test_allow_degrade_never_restarts_after_a_billable_outcome_was_already_spent(
    tmp_path: Path,
) -> None:
    """Plan §2.3.5 row 1: the restart happens *before any paid work*.

    An ambiguous-but-billable outcome (``http_5xx``) charges a unit even though it resolves
    nothing, so a later ``auth_error`` must not be answered by re-running the whole mix as the
    Free recipe and reporting ``degraded`` — that would settle a run at 100 % after real spend.
    """

    messages: list[str] = []
    code, audd, shazam, entry, media_dir = _run(
        tmp_path, "spend-then-401.json", allow_degrade=True, progress_messages=messages
    )
    # The 500 billed a unit; the 401 stopped the sweep at the second window.
    assert audd.calls == 2 and audd.billed_units == 1
    assert code == 3
    assert (entry["status"], entry["reason"], entry["achieved"]) == (
        "provider_unavailable",
        "auth_error",
        None,
    )
    assert entry["usd_e6_spent"] == 5_000 and entry["usd_e2_spent"] == 1
    assert shazam.requests == 0  # never a second, free sweep on top of the spend
    assert not (media_dir / "present").exists()
    assert not any("restarting as the free recipe" in message for message in messages)
    assert any("--allow-degrade not applied" in message for message in messages)


def test_allow_degrade_never_applies_after_paid_work_or_to_a_free_request(tmp_path: Path) -> None:
    # A terminal-provider outcome after resolved attempts is partial, degrade or not.
    code, audd, _shazam, entry, _media_dir = _run(
        tmp_path / "midrun", "quota-midrun.json", allow_degrade=True
    )
    assert code == 0 and audd.calls == 3
    assert (entry["status"], entry["reason"], entry["achieved"]) == (
        "partial",
        "provider_unavailable_midrun",
        "deep",
    )
    # A Free request has nothing to degrade from.
    code, _audd, _shazam, entry, _media_dir = _run(
        tmp_path / "free", "gate0a-deep.json", recipe=FREE_RECIPE, allow_degrade=True
    )
    assert code == 0 and entry["status"] == "complete" and entry["achieved"] == "free"


def test_allow_degrade_cli_flag_is_threaded_and_off_by_default(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_analyse(_url: str, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(pipeline, "run_analysis", fake_analyse)
    runner = CliRunner()
    default = runner.invoke(cli.app, ["analyse", "http://example/set", "--recipe", "deep"])
    assert default.exit_code == 0, default.output
    assert captured["allow_degrade"] is False
    flagged = runner.invoke(
        cli.app, ["analyse", "http://example/set", "--recipe", "deep", "--allow-degrade"]
    )
    assert flagged.exit_code == 0, flagged.output
    assert captured["allow_degrade"] is True
    assert "--allow-degrade" in runner.invoke(cli.app, ["analyse", "--help"]).output


# --------------------------------------------------------------------------------------------------
# Status arithmetic
# --------------------------------------------------------------------------------------------------
def test_achieved_fraction_is_exact_integer_arithmetic() -> None:
    assert _achieved(7, 7, 0.95) and not _achieved(6, 7, 0.95)
    assert _achieved(19, 20, 0.95) and not _achieved(18, 20, 0.95)
    assert _achieved(4, 5, 0.80) and not _achieved(3, 5, 0.80)
    assert _achieved(0, 0, 0.80)  # nothing planned is trivially achieved


def test_run_status_precedence_follows_the_matrix() -> None:
    def status(**overrides: object) -> tuple[str, str | None]:
        base: dict[str, object] = {
            "primary_resolved": 7,
            "primary_planned": 7,
            "primary_fraction": 0.95,
            "secondary_resolved": 2,
            "secondary_allocated": 2,
            "secondary_fraction": 0.80,
            "provider_stopped": None,
            "reservation_exhausted": False,
        }
        return _run_status(**{**base, **overrides})  # type: ignore[arg-type]

    assert status() == ("complete", None)
    assert status(secondary_resolved=1) == ("degraded", "secondary_not_achieved")
    assert status(primary_resolved=6) == ("partial", "primary_not_achieved")
    assert status(primary_resolved=6, secondary_resolved=0) == ("partial", "primary_not_achieved")
    assert status(reservation_exhausted=True) == ("partial", "reservation_exhausted")
    assert status(provider_stopped="quota_error") == ("partial", "provider_unavailable_midrun")
    assert status(secondary_fraction=None, secondary_resolved=0) == ("complete", None)  # free
    assert status(secondary_allocated=0, secondary_resolved=0) == ("complete", None)


# --------------------------------------------------------------------------------------------------
# The secondary scheduler (targeting:1 since 1b-i; the class ranking is unchanged from 0a-iv)
# --------------------------------------------------------------------------------------------------
def _window(start_ms: int, *, generation: int = 0, transform: Transform | None = None):
    base = WindowRecord.model_validate(
        json.loads((GOLDEN / "window.json").read_text(encoding="utf-8"))
    )
    return base.model_copy(
        update={
            "id": f"{start_ms + generation * 7 + (1 if transform else 0):040x}",
            "generation": generation,
            "start_ms": start_ms,
            "support_ms": (start_ms, start_ms + 12_000),
            "wav_sha256": f"{start_ms:064x}",
            "transform": transform or base.transform,
        }
    )


def _episode(
    span: tuple[int, int],
    *,
    badge: str = "possible",
    flags: list[str] | None = None,
    suppressed: str | None = None,
    evidence: list[str] | None = None,
) -> EpisodeRecord:
    base = EpisodeRecord.model_validate(
        json.loads((GOLDEN / "episode.json").read_text(encoding="utf-8"))
    )
    return base.model_copy(
        update={
            "id": f"{span[0]:040x}",
            "best_start_ms": span[1],  # crossed on purpose: the hull, not the estimate, counts
            "best_end_ms": span[0],
            "evidence_support_ms": [span],
            "badge": badge,
            "flags": flags or [],
            "suppressed": suppressed,
            "evidence": evidence if evidence is not None else ["a" * 40, "b" * 40],
        }
    )


def _episodes_file(*episodes: EpisodeRecord) -> EpisodesFile:
    base = EpisodesFile.model_validate(
        json.loads((GOLDEN / "episodes.json").read_text(encoding="utf-8"))
    )
    return base.model_copy(update={"episodes": list(episodes), "gaps": []})


def test_secondary_capacity_is_ceil_of_minutes_times_clips_per_minute() -> None:
    assert secondary_capacity(60_000, 2) == 2
    assert secondary_capacity(61_000, 2) == 3
    assert secondary_capacity(3_600_000, 2) == 120
    assert secondary_capacity(0, 2) == 0 and secondary_capacity(60_000, 0) == 0


def test_candidates_rank_by_class_and_the_allocation_never_repeats_a_window() -> None:
    hint_id = "c" * 40
    episodes = _episodes_file(
        _episode((0, 15_000), badge="possible"),  # listed, not confident
        _episode((15_000, 30_000), badge="likely"),  # confident: never probed
        _episode((20_000, 35_000), suppressed="scatter"),  # 2 selected votes: challengeable
        _episode((30_000, 40_000), suppressed="buried", evidence=["d" * 40, hint_id]),  # 1 vote
        _episode((40_000, 52_000), flags=["hint_only", "hint_supported"]),
    )
    candidates = select_secondary_candidates(
        episodes,
        duration_ms=60_000,
        suppressed_min_votes=2,
        min_intersection_ms=4_000,
        hint_ids={hint_id},
    )
    # Suppressed episodes are not coverage, so 30-40 s (under the two hidden ones) is blank too.
    assert [(item.priority, item.span) for item in candidates] == [
        ("hint_only", (40_000, 52_000)),
        ("listed_not_confident", (0, 15_000)),
        ("suppressed_challengeable", (20_000, 35_000)),
        ("blank", (30_000, 40_000)),
        ("blank", (52_000, 60_000)),
    ]
    assert [item.priority for item in candidates] == sorted(
        (item.priority for item in candidates), key=PRIORITY.index
    )
    windows = [_window(start) for start in (0, 9_000, 18_000, 27_000, 36_000, 45_000, 48_000)]
    # Eligibility is >= 4 s of overlap: the hint-only span reaches windows 36, 45 and 48 (exactly
    # 4 s for 48-60); the listed span reaches 0 and 9 (18-30 overlaps only 0 s); the suppressed
    # span reaches 18 and 27; the blanks reach 27, 36, 45 and 48 again.  targeting:1 (1b-i) gives
    # every candidate its best window first, in class order, then shares the rest out; a window
    # is queued at most once whatever the allocation.
    picks = allocate_secondary_windows(
        windows, candidates, allocation=100, min_intersection_ms=4_000
    )
    starts = [item.window.support_ms[0] for item in picks]
    assert sorted(starts) == [0, 9_000, 18_000, 27_000, 36_000, 45_000, 48_000]
    # Every pick still clears the >= 4 s bar for the span that chose it — the eligibility rule
    # the targeting:0 queue order used to carry implicitly.
    for item in picks:
        span = item.candidate.span
        window = item.window.support_ms
        assert min(window[1], span[1]) - max(window[0], span[0]) >= 4_000, (span, window)
    assert starts[:5] == [36_000, 0, 18_000, 27_000, 48_000]
    assert [item.round for item in picks[:5]] == ["first"] * 5
    capped = allocate_secondary_windows(
        windows, candidates, allocation=4, min_intersection_ms=4_000
    )
    assert [item.window.support_ms[0] for item in capped] == [36_000, 0, 18_000, 27_000]
    assert (
        allocate_secondary_windows(windows, candidates, allocation=0, min_intersection_ms=4_000)
        == ()
    )


def test_secondary_never_queues_rescan_or_transformed_windows() -> None:
    episodes = _episodes_file(_episode((0, 15_000)))
    candidates = select_secondary_candidates(
        episodes, duration_ms=60_000, suppressed_min_votes=2, min_intersection_ms=4_000
    )
    frozen = _window(0)
    rescan = _window(0, generation=1)
    transformed = _window(0, transform=Transform(type="resample", rate_e4=9_600, semitones=0))
    picks = allocate_secondary_windows(
        [transformed, rescan, frozen], candidates, allocation=10, min_intersection_ms=4_000
    )
    assert [item.window for item in picks] == [frozen]


def test_blank_spans_cover_the_unresolved_tail_after_a_stopped_primary() -> None:
    # Two matched windows at the start and nothing after: the fuse records an unresolved boundary,
    # not a gap, yet the 39 s tail is exactly where the secondary must look.
    episodes = _episodes_file(_episode((0, 21_000), badge="unclear"))
    assert blank_spans(episodes, 60_000, min_ms=4_000) == [(21_000, 60_000)]
    assert blank_spans(_episodes_file(), 60_000, min_ms=4_000) == [(0, 60_000)]
    # A suppressed episode does not count as coverage; a sliver below min_ms is dropped.
    hidden = _episode((0, 58_000), suppressed="scatter")
    assert blank_spans(_episodes_file(hidden), 60_000, min_ms=4_000) == [(0, 60_000)]
    listed = _episode((0, 58_000))
    assert blank_spans(_episodes_file(listed), 60_000, min_ms=4_000) == []


# --------------------------------------------------------------------------------------------------
# The web runner surfaces every non-zero exit as a failed job
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("code", "meaning"), [(4, "budget exhausted"), (5, "source changed")])
def test_web_runner_fails_every_non_zero_exit_without_attaching_a_result(
    tmp_path: Path, monkeypatch, code: int, meaning: str
) -> None:
    from id_detector.webapp.jobs import JobManager
    from id_detector.webapp.runner import make_pipeline_runner

    async def refused_analyse(_target: str, **_kwargs: object) -> int:
        return code

    def never_cached(_work_root: Path, _target: str):
        raise AssertionError("a refused run must not attach a cached result")

    monkeypatch.setattr(pipeline, "run_analysis", refused_analyse)
    monkeypatch.setattr(cli, "_load_cached", never_cached)
    monkeypatch.setattr(pipeline, "_load_cached", never_cached)
    runner = make_pipeline_runner(
        tmp_path, project_root=ROOT, config_path=tmp_path / "missing-idea.toml"
    )
    manager = JobManager(tmp_path, runner)
    try:
        job_id = manager.submit(str(AUDIO), "max_accuracy")
        deadline = time.monotonic() + 5
        while manager.get(job_id).status not in {"failed", "succeeded"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        job = manager.get(job_id)
        assert job.status == "failed"
        assert job.error and meaning in job.error and f"exit code {code}" in job.error
        assert job.result_path is None
    finally:
        manager.shutdown()
