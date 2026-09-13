"""Phase 0b-ii gate: effective config under a profile (H6), novelty guarded and hoisted (M2),
Shazam throttle handling (S1) and the true scanned window set (M10).  Every pipeline run is
offline over the 60 s fixture (seven frozen windows) with the scripted fakes."""

from __future__ import annotations

import asyncio
import json
import tomllib
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from id_detector import cli, orchestrate, pipeline
from id_detector.contracts import EpisodesFile
from id_detector.present.bundles import shown_result_dir
from id_detector.profiles import (
    PROFILE_FIXED_FIELDS,
    effective_app_config,
    load_profile,
    profile_app_config,
    profile_fixed_fields,
)
from id_detector.providers.base import AppConfig
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE, Recipe, get_recipe
from id_detector.semantics import interval_length
from id_detector.shazam import (
    CircuitBreaker,
    InjectedHTTPClient,
    ShazamHTTPError,
    TokenBucket,
)
from id_detector.webapp.runner import _resolve_settings
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff

ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "tests" / "fixtures" / "audio" / "tone-60s.wav"
SCRIPTS = ROOT / "tests" / "fakes" / "scripts"
runner = CliRunner()

#: The five knobs review H6 found dropped under a profile, with the values the tests set.
CARRIED = {
    "shazam_requests_per_minute": 7,
    "recognise_concurrency": 2,
    "collapse": False,
    "same_track_bridge_ms": 1_234,
    "present_min_track_ms": 5_000,
}
FILE_BODY = """\
max_requests = 321
lead_in_ms = 250
[recognise]
requests_per_minute = 7
concurrency = 2
[deep]
primary_density = 1
audd_requests_per_minute = 9
[schedule]
window_ms = 6000
hop_ms = 3000
[rescan]
max_generations = 1
[cache]
positive_max_age_days = 4
[hints]
mixesdb = false
[present]
collapse = false
same_track_bridge_ms = 1234
min_track_ms = 5000
"""


def _write_config(tmp_path: Path, body: str = FILE_BODY) -> Path:
    path = tmp_path / "idea.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _entry(work_root: Path) -> tuple[Path, dict[str, object]]:
    (path,) = work_root.rglob("invocations.jsonl")
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return path.parent, entries[-1]


def _episodes(media_dir: Path) -> EpisodesFile:
    return EpisodesFile.model_validate_json(
        (media_dir / "fuse" / "episodes.json").read_text(encoding="utf-8")
    )


def _run(
    tmp_path: Path,
    script_name: str | Mapping[str, object],
    *,
    recipe: Recipe = FREE_RECIPE,
    app_config: AppConfig | None = None,
    novelty: bool = False,
    max_generations: int = 0,
    max_requests: int = 100,
    progress_messages: list[str] | None = None,
) -> tuple[int, FakeAudD, FakeShazamHTTP, dict[str, object], Path]:
    script = SCRIPTS / script_name if isinstance(script_name, str) else script_name
    audd = FakeAudD(script)
    shazam = FakeShazamHTTP(script)
    work_root = tmp_path / "work"
    code = asyncio.run(
        cli._analyse(
            str(AUDIO),
            work_root=work_root,
            print_raw=False,
            refresh=False,
            max_requests=max_requests,
            tracklist=None,
            no_hints=True,
            app_config=app_config
            or AppConfig(
                transforms_policy="off",
                recognise_concurrency=1,
                shazam_requests_per_minute=1_000_000,
                audd_requests_per_minute=1_000_000,
            ),
            max_generations=max_generations,
            novelty=novelty,
            recipe=recipe,
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


def _capture_loop(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Record every ``run_generation_loop`` call ``_analyse`` makes (one per fuse)."""

    calls: list[dict[str, object]] = []
    real = pipeline.run_generation_loop

    async def wrapper(**kwargs: object) -> object:
        calls.append(kwargs)
        return await real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pipeline, "run_generation_loop", wrapper)
    return calls


def _scanned_starts(call: dict[str, object]) -> list[int]:
    return [window.support_ms[0] for window in call["scanned_windows"]]  # type: ignore[union-attr]


# --------------------------------------------------------------------------------------------------
# H6 — effective config: every file preference survives a profile, at both call sites
# --------------------------------------------------------------------------------------------------
def test_effective_config_carries_every_non_geometry_field_from_the_file(tmp_path: Path) -> None:
    file_config = AppConfig.load(_write_config(tmp_path))
    frozen = load_profile(ROOT, "free")
    effective = effective_app_config(file_config, frozen)
    from_profile = profile_app_config(frozen)
    for field in fields(AppConfig):
        name = field.name
        if name in PROFILE_FIXED_FIELDS:
            assert getattr(effective, name) == getattr(from_profile, name), name
            # The file tried to move the geometry; the profile wins.
            if name in {"window_ms", "hop_ms"}:
                assert getattr(effective, name) != getattr(file_config, name), name
        elif name == "rescan_max_generations":
            assert effective.rescan_max_generations == min(frozen.rescan.max_generations, 1) == 1
        elif name == "hints_enabled":
            assert effective.hints_enabled is (file_config.hints_enabled and frozen.hints_enabled)
        else:
            assert getattr(effective, name) == getattr(file_config, name), name
    for name, value in CARRIED.items():
        assert getattr(effective, name) == value, name
    assert effective.audd_requests_per_minute == 9 and effective.deep_primary_density == 1
    assert effective.disabled_hint_connectors == frozenset({"mixesdb", "tl1001"})


def test_the_rescan_ceiling_caps_the_profile_and_hints_off_in_the_profile_wins(
    tmp_path: Path,
) -> None:
    frozen = load_profile(ROOT, "free")
    assert frozen.rescan.max_generations == 3 and frozen.hints_enabled
    default_file = AppConfig.load(_write_config(tmp_path, "max_requests = 5\n"))
    assert effective_app_config(default_file, frozen).rescan_max_generations == 0  # default cap
    generous = AppConfig.load(_write_config(tmp_path, "[rescan]\nmax_generations = 9\n"))
    assert effective_app_config(generous, frozen).rescan_max_generations == 3  # profile's own
    quiet = frozen.model_copy(update={"hints_enabled": False})
    assert effective_app_config(default_file, quiet).hints_enabled is False
    hints_off = AppConfig.load(_write_config(tmp_path, "[hints]\nenabled = false\n"))
    assert effective_app_config(hints_off, frozen).hints_enabled is False


@pytest.mark.parametrize("via", ["--profile", "default_profile"])
def test_cli_analyse_and_the_web_runner_carry_the_five_knobs_and_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via: str
) -> None:
    body = FILE_BODY if via == "--profile" else 'default_profile = "free"\n' + FILE_BODY
    config = _write_config(tmp_path, body)
    captured: dict[str, object] = {}

    async def fake_analyse(_url: str, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(pipeline, "run_analysis", fake_analyse)
    args = ["analyse", "http://example/set", "--config", str(config)]
    if via == "--profile":
        args += ["--profile", "free"]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    cli_config = captured["app_config"]
    web = _resolve_settings(ROOT, config, "free" if via == "--profile" else None)
    for name, value in CARRIED.items():
        assert getattr(cli_config, name) == value, name  # type: ignore[arg-type]
        assert getattr(web.config, name) == value, name
    # The profile still owns the geometry at both sites, and the two sites agree exactly.
    assert cli_config.window_ms == web.config.window_ms == 12_000  # type: ignore[attr-defined]
    assert cli_config == web.config
    # The rescan cap flows through to the generation count the pipeline receives.
    assert captured["max_generations"] == web.max_generations == 1


def test_config_show_marks_what_the_profile_fixes_in_plain_english(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    result = runner.invoke(
        cli.app, ["config", "show", "--config", str(config), "--profile", "free"]
    )
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert '+ defaults + profile "free" (chosen by --profile)' in out
    assert 'Lines marked "fixed by profile" come from the frozen profile "free"' in out
    # Geometry lines carry the marker; the file's own values do not.
    for marked in (
        'window_ms = 12000  # fixed by profile "free"',
        'hop_ms = 9000  # fixed by profile "free"',
        'policy = "rescan_only"  # fixed by profile "free"',
        'hop_ms = 5000  # fixed by profile "free"',
        'max_generations = 1  # capped by this file (profile "free" allows 3)',
    ):
        assert marked in out, marked
    for own in (
        "requests_per_minute = 7\n",
        "concurrency = 2\n",
        "collapse = false\n",
        "same_track_bridge_ms = 1234\n",
        "min_track_ms = 5000\n",
        "audd_requests_per_minute = 9\n",
        "max_requests = 321\n",
    ):
        assert own in out, own
    assert "min_track_ms = 5000  #" not in out
    # The trailer names what no config line controls.
    assert "engines = shazam" in out and "novelty change points = on" in out
    # The whole thing is still TOML a reader (or the loader) can parse: the effective values.
    parsed = tomllib.loads(out)
    assert parsed["present"]["min_track_ms"] == 5_000
    assert parsed["schedule"] == {"window_ms": 12_000, "hop_ms": 9_000, "phase_ms": 0}
    assert parsed["rescan"]["max_generations"] == 1
    # No credential name or value leaks.
    for secret in ("AUDD_API_TOKEN", "SOUNDCLOUD_OAUTH_TOKEN", "DISCOGS_API_TOKEN"):
        assert secret not in out


def test_config_show_explains_the_rescan_cap_and_the_default_profile(tmp_path: Path) -> None:
    config = _write_config(tmp_path, 'default_profile = "free"\n[present]\nmin_track_ms = 0\n')
    result = runner.invoke(cli.app, ["config", "show", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert f"(chosen by default_profile in {config})" in result.stdout
    assert 'max_generations = 0  # capped by this file (profile "free" allows 3)' in result.stdout
    assert "min_track_ms = 0\n" in result.stdout
    assert profile_fixed_fields(AppConfig(), load_profile(ROOT, "free"))["hop_ms"] == (
        'fixed by profile "free"'
    )


def test_config_show_with_no_config_file_blames_the_built_in_default_not_a_file(
    tmp_path: Path,
) -> None:
    """The owner's very first run has no ``idea.toml``; the cap note must not invent one."""

    missing = tmp_path / "idea.toml"
    result = runner.invoke(
        cli.app, ["config", "show", "--config", str(missing), "--profile", "free"]
    )
    assert result.exit_code == 0, result.output
    assert f"built-in defaults ({missing} not found)" in result.stdout
    assert (
        'max_generations = 0  # capped by the built-in default (profile "free" allows 3)'
        in result.stdout
    )
    assert "capped by this file" not in result.stdout


def test_config_show_without_a_profile_marks_nothing_and_rejects_an_unknown_one(
    tmp_path: Path,
) -> None:
    config = _write_config(tmp_path)
    plain = runner.invoke(cli.app, ["config", "show", "--config", str(config)])
    assert plain.exit_code == 0, plain.output
    assert "fixed by profile" not in plain.stdout and "chosen by" not in plain.stdout
    assert "window_ms = 6000\n" in plain.stdout  # the file's geometry applies with no profile
    unknown = runner.invoke(cli.app, ["config", "show", "--config", str(config), "--profile", "x"])
    assert unknown.exit_code == 2
    assert "unknown profile" in unknown.output


# --------------------------------------------------------------------------------------------------
# M2 — novelty only with rescans on, and once per run
# --------------------------------------------------------------------------------------------------
def _count_novelty(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    def fake_points(pcm_path: Path, *, duration_ms: int) -> list[SimpleNamespace]:
        calls.append(duration_ms)
        return [SimpleNamespace(at_ms=21_000), SimpleNamespace(at_ms=42_000)]

    monkeypatch.setattr(orchestrate, "novelty_change_points", fake_points)
    return calls


def test_novelty_is_never_computed_when_rescans_are_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_novelty(monkeypatch)
    code, _audd, _shazam, entry, _media = _run(
        tmp_path, "gate0a-deep.json", novelty=True, max_generations=0
    )
    assert code == 0 and entry["status"] == "complete"
    assert calls == []
    assert entry["counts"]["novelty_change_points"] == 0  # type: ignore[index]


def test_novelty_is_computed_once_for_every_fuse_of_a_run_with_rescans_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_novelty(monkeypatch)
    loops = _capture_loop(monkeypatch)
    # Deep fuses twice (the first fuse, then the secondary's re-fuse) with rescans allowed; a
    # request budget equal to the window count keeps the loop from spending a rescan generation
    # (which would hand the secondary nothing to probe), so both fuses see the same gen-0 picture.
    code, _audd, shazam, entry, _media = _run(
        tmp_path,
        "gate0a-deep.json",
        recipe=DEEP_RECIPE,
        novelty=True,
        max_generations=1,
        max_requests=7,
    )
    assert code == 0 and entry["status"] == "complete"
    assert entry["counts"]["secondary_allocated"] == shazam.requests == 2  # type: ignore[index]
    assert len(loops) == 2  # the re-fuse happened
    assert calls == [60_000]  # ... and novelty was computed exactly once, before the first fuse
    assert [loop["novelty_change_points_ms"] for loop in loops] == [(21_000, 42_000)] * 2
    assert entry["counts"]["novelty_change_points"] == 2  # type: ignore[index]


def test_novelty_off_still_wins_and_the_loop_guards_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_novelty(monkeypatch)
    code, *_rest = _run(tmp_path, "gate0a-deep.json", novelty=False, max_generations=1)
    assert code == 0 and calls == []
    decoded = SimpleNamespace(
        pcm_path=Path("x.pcm"), record=SimpleNamespace(pcm=SimpleNamespace(duration_ms=5))
    )
    assert orchestrate.compute_novelty_change_points(decoded, enabled=False) == ()
    assert orchestrate.compute_novelty_change_points(decoded, enabled=True) == (21_000, 42_000)


# --------------------------------------------------------------------------------------------------
# S1 — Shazam decode/malformed replies under throttle
# --------------------------------------------------------------------------------------------------
def _client(
    body: bytes, status: int = 200, *, content_type: str = "application/json"
) -> tuple[InjectedHTTPClient, TokenBucket, CircuitBreaker, list[int]]:
    attempts: list[int] = []

    async def on_attempt() -> None:
        attempts.append(1)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    limiter = TokenBucket(rate_per_minute=60, capacity=1)
    breaker = CircuitBreaker()
    client = InjectedHTTPClient(
        on_attempt=on_attempt,
        limiter=limiter,
        breaker=breaker,
        transport=httpx.MockTransport(handler),
    )
    return client, limiter, breaker, attempts


@pytest.mark.parametrize(
    ("body", "status", "content_type", "message"),
    [
        (b"<html>Too many requests</html>", 200, "text/html", "not JSON"),
        (b"", 200, "application/json", "not JSON"),
        (b"[]", 200, "application/json", "no matches list"),
        (b'{"error": "rate limited", "retryms": 3000}', 200, "application/json", "no matches"),
        (b'"throttled"', 200, "application/json", "no matches list"),
        (b'{"matches": "nope"}', 200, "application/json", "no matches list"),
    ],
)
def test_malformed_shazam_bodies_penalise_the_limiter_and_fail_the_window(
    body: bytes, status: int, content_type: str, message: str
) -> None:
    client, limiter, breaker, attempts = _client(body, status, content_type=content_type)
    ceiling = limiter.rate_per_second
    with pytest.raises(ShazamHTTPError, match=message) as raised:
        asyncio.run(client.request("POST", "https://example.invalid/discovery", json={}))
    assert raised.value.status_code == status  # a 200 is not retryable: the window fails
    assert attempts == [1]  # the request was accounted before network I/O
    assert limiter.rate_per_second < ceiling  # the throttle slows the whole pool (like a 429)
    assert breaker.failures == 1


def test_a_recognition_body_is_not_malformed_and_a_429_still_penalises() -> None:
    client, limiter, breaker, _attempts = _client(b'{"matches": []}')
    assert asyncio.run(client.request("POST", "https://example.invalid/d", json={})) == {
        "matches": []
    }
    assert limiter.rate_per_second == limiter.max_rate_per_second and breaker.failures == 0
    throttled, limiter, breaker, _attempts = _client(b"slow down", 429, content_type="text/plain")
    with pytest.raises(ShazamHTTPError) as raised:
        asyncio.run(throttled.request("POST", "https://example.invalid/d", json={}))
    assert raised.value.status_code == 429
    assert limiter.rate_per_second < limiter.max_rate_per_second and breaker.failures == 1


def test_free_run_with_malformed_replies_is_partial_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages: list[str] = []
    loops = _capture_loop(monkeypatch)
    code, audd, shazam, entry, media_dir = _run(
        tmp_path, "free-thin.json", progress_messages=messages
    )
    assert code == 0 and audd.calls == 0 and shazam.requests == 7
    assert (entry["status"], entry["reason"]) == ("partial", "primary_not_achieved")
    assert entry["counts"]["failures"] == 2  # type: ignore[index]  # 5/7 resolved < 80 %
    assert any(
        "2 of 7 windows got no usable answer from Shazam" in message
        and "5 of 7 resolved, below the 80 % this recipe needs, so the run ends partial" in message
        for message in messages
    )
    # M10 through S1: the fuser is told about the five windows Shazam answered for; the two it
    # did not (the first two, per the script) are not scanned.
    (loop,) = loops
    assert _scanned_starts(loop) == [18_000, 27_000, 36_000, 45_000, 48_000]
    assert loop["novelty_change_points_ms"] == ()  # rescans off
    assert (shown_result_dir(media_dir) / "tracklist.json").is_file()


def test_one_unanswered_window_says_the_run_can_still_complete(tmp_path: Path) -> None:
    """The throttle line reports THIS run's verdict, not the rule: 6/7 is still above 80 %."""

    messages: list[str] = []
    code, _audd, _shazam, entry, _media = _run(
        tmp_path,
        {"shazam": {"default": "match", "windows": {"0": "malformed"}}},
        progress_messages=messages,
    )
    assert code == 0 and (entry["status"], entry["reason"]) == ("complete", None)
    assert entry["counts"]["failures"] == 1  # type: ignore[index]
    assert any(
        "1 of 7 windows got no usable answer from Shazam" in message
        and "6 of 7 resolved, still at or above the 80 % this recipe needs" in message
        for message in messages
    )
    assert not any("ends partial" in message for message in messages)


def test_no_throttle_line_when_every_planned_window_was_answered(tmp_path: Path) -> None:
    messages: list[str] = []
    code, _audd, _shazam, entry, _media = _run(
        tmp_path, "gate0a-deep.json", progress_messages=messages
    )
    assert code == 0 and entry["status"] == "complete"
    assert not any("no usable answer" in message for message in messages)


# --------------------------------------------------------------------------------------------------
# M10 — the fuser receives the windows an engine actually answered for
# --------------------------------------------------------------------------------------------------
def test_scanned_windows_are_the_ones_named_by_a_resolved_observation() -> None:
    windows = [SimpleNamespace(id=f"w{index}") for index in range(4)]
    observations = [
        SimpleNamespace(status="match", source_ids=["query:q0", "window:w0"]),
        SimpleNamespace(status="no_match", source_ids=["query:q2", "window:w2"]),
        SimpleNamespace(status="error", source_ids=["query:q3", "window:w3"]),  # nobody answered
        SimpleNamespace(status="no_match", source_ids=["query:q9"]),  # a span query (Panako)
    ]
    scanned = orchestrate.scanned_windows(windows, observations)
    assert [window.id for window in scanned] == ["w0", "w2"]  # order preserved, w3 unscanned
    assert orchestrate.scanned_windows(windows, []) == []


def test_deep_density_two_reports_the_windows_the_paid_sweep_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig(
        transforms_policy="off",
        recognise_concurrency=1,
        shazam_requests_per_minute=1_000_000,
        audd_requests_per_minute=1_000_000,
        deep_primary_density=2,
    )
    loops = _capture_loop(monkeypatch)
    code, audd, shazam, entry, media_dir = _run(
        tmp_path,
        "gate0a-deep.json",
        recipe=get_recipe("deep", primary_density=2),
        app_config=config,
    )
    assert code == 0 and entry["status"] == "complete"
    assert audd.calls == 4 and entry["counts"]["paid_planned"] == 4  # type: ignore[index]
    first, second = loops
    # The first fuse sees exactly the even-indexed windows the density-2 sweep sent to AudD;
    # every window is still handed over for the sidecars.
    assert _scanned_starts(first) == [0, 18_000, 36_000, 48_000]
    assert len(first["windows"].records) == 7  # type: ignore[attr-defined]
    # The re-fuse adds whatever the Shazam secondary probed (it may coincide with AudD windows).
    assert entry["counts"]["secondary_allocated"] == shazam.requests == 2  # type: ignore[index]
    probed = set(_scanned_starts(second))
    assert {0, 18_000, 36_000, 48_000} <= probed and len(probed) <= 6
    scanned = [tuple(window.support_ms) for window in second["scanned_windows"]]  # type: ignore[union-attr]
    # Proved evidence never exceeds the time an engine actually listened to (here the two
    # unanswered holes sit against episode edges, so the partition reports them as unresolved
    # boundaries rather than unscanned time), and gap evidence counts only answered windows.
    episodes = _episodes(media_dir)
    listened = interval_length(scanned, 60_000)
    assert listened < 60_000
    assert episodes.durations.evidence_supported_ms + episodes.durations.unclear_ms <= listened
    assert episodes.durations.unscanned_ms <= 60_000 - listened
    for gap in episodes.gaps:
        expected = sum(start < gap.end_ms and end > gap.start_ms for start, end in scanned)
        assert gap.evidence.n_windows == expected


def test_the_scanned_subset_does_not_widen_the_rescan_request_budget(tmp_path: Path) -> None:
    """M10 changes what the fuser SEES; it must not change what ``--max-requests`` allows.

    The budget is spent per window generated.  With a ceiling equal to the window count nothing
    is left for a rescan — and telling the fuser two windows went unanswered must not hand the
    loop two more requests than the owner allowed.
    """

    from id_detector.windows import generate_windows_async
    from tests.test_stage4c_generations import _Harness

    async def scenario() -> object:
        harness = _Harness(tmp_path, 120_000)
        windows = await generate_windows_async(harness.decoded, harness.media_dir)
        recognised = await harness.recognise(windows=windows, generation=0)
        assert len(windows.records) >= 13
        return await harness.loop(
            windows,
            recognised,
            max_generations=3,
            request_budget=len(windows.records),
            # Only the first three windows were answered: ten windows' worth of budget the loop
            # must NOT hand back to itself.
            scanned_windows=list(windows.records)[:3],
            gen0_requests=recognised.requests,
            gen0_physical_attempts=recognised.physical_attempts,
        )

    result = asyncio.run(scenario())
    assert result.stop_reason == orchestrate.STOP_BUDGET_EXHAUSTED  # type: ignore[attr-defined]
    assert result.final_generation == 0  # type: ignore[attr-defined]
    generation = result.generations[0]  # type: ignore[attr-defined]
    assert generation.emitted_requests > 0 and generation.accepted_requests == 0


def test_a_complete_free_run_still_scans_the_whole_mix(tmp_path: Path) -> None:
    code, _audd, shazam, entry, media_dir = _run(tmp_path, "gate0a-deep.json")
    assert code == 0 and entry["status"] == "complete" and shazam.requests == 7
    assert _episodes(media_dir).durations.unscanned_ms == 0
