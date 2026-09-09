"""Phase 0b-i gate: the AudD clip adapter v2 — the anchor validity rule and the ``audd`` trial
source, the recipe's four-way concurrency behind a token bucket, and the bounded retry policy
with ``auth_error`` / ``quota_error`` classification.  Everything is offline: the fakes script AudD
and Shazam over the 60 s fixture (seven frozen windows)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import pytest
from typer.testing import CliRunner

from id_detector import cli
from id_detector import paid_clip as paid_clip_module
from id_detector.attempts import AttemptState, attempts_path, load_attempt_ledger
from id_detector.config_template import CONFIG_TEMPLATE, render_effective_config
from id_detector.contracts import QueryRecord, WindowRecord
from id_detector.fuse.alignment import select_logical_trial_points
from id_detector.paid_clip import _error_body_outcome, _protocol_outcome
from id_detector.providers.audd import (
    CLIP_ANCHOR_METHOD,
    CLIP_ANCHOR_UNCERTAINTY_MS,
    CLIP_SIMULTANEOUS_SOURCE,
    DEFAULT_ANCHOR_MAX_MS,
    DEFAULT_ANCHOR_SLACK_MS,
    clip_anchor,
    clip_response_to_observation,
)
from id_detector.providers.base import AppConfig, ProviderProtocolError
from id_detector.recipes import DEEP_RECIPE
from id_detector.shazam import TokenBucket
from id_detector.webapp.runner import _resolve_settings
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff

ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "tests" / "fixtures" / "audio" / "tone-60s.wav"
SCRIPTS = ROOT / "tests" / "fakes" / "scripts"
GOLDEN = ROOT / "tests" / "golden"
CONFIG = AppConfig(
    transforms_policy="off",
    recognise_concurrency=1,
    shazam_requests_per_minute=1_000_000,
    audd_requests_per_minute=1_000_000,
)


def _entry(work_root: Path) -> tuple[Path, dict[str, object]]:
    (path,) = work_root.rglob("invocations.jsonl")
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return path.parent, entries[-1]


def _run(
    tmp_path: Path,
    script_name: str,
    *,
    audd: object | None = None,
    sleep=no_backoff,
    progress_messages: list[str] | None = None,
) -> tuple[int, FakeAudD, FakeShazamHTTP, dict[str, object], Path]:
    script = SCRIPTS / script_name
    fake = audd if audd is not None else FakeAudD(script)
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
            app_config=CONFIG,
            max_generations=0,
            novelty=False,
            recipe=DEEP_RECIPE,
            paid_scan_adapters={"audd": fake},
            shazam_http_client=shazam,
            paid_sleep=sleep,
            progress=(
                (lambda _phase, _done, _total, message: progress_messages.append(message))
                if progress_messages is not None
                else None
            ),
        )
    )
    media_dir, entry = _entry(work_root)
    return code, fake, shazam, entry, media_dir  # type: ignore[return-value]


def _chains(media_dir: Path) -> dict[str, list[AttemptState]]:
    """Every query's attempts in journal order (a retry chain reads ordinal 0, 1, 2 ...)."""

    chains: dict[str, list[AttemptState]] = defaultdict(list)
    for attempt in load_attempt_ledger(attempts_path(media_dir)).attempts:
        chains[attempt.query_id].append(attempt)
    return chains


def _window_and_query() -> tuple[WindowRecord, QueryRecord]:
    window = WindowRecord.model_validate(
        json.loads((GOLDEN / "window.json").read_text(encoding="utf-8"))
    )
    query = QueryRecord.model_validate(
        json.loads((GOLDEN / "query.json").read_text(encoding="utf-8"))
    )
    return window, query


def _observation(result: dict[str, object] | None, **anchor_bounds: int):
    window, query = _window_and_query()
    return clip_response_to_observation(
        {"status": "success", "result": result},
        query=query,
        window=window,
        media_key="a" * 64,
        raw_response_ref="recognise/x.json",
        **anchor_bounds,
    )


# --------------------------------------------------------------------------------------------------
# Retry policy (plan §2.3.1): connect_error / timeout_pre / http_429 / http_503 only; <= 3; 1/2/4 s
# --------------------------------------------------------------------------------------------------
def test_http_429_twice_then_match_is_three_attempts_and_one_unit_billed(tmp_path: Path) -> None:
    messages: list[str] = []
    code, audd, _shazam, entry, media_dir = _run(
        tmp_path, "retry-429-then-match.json", progress_messages=messages
    )
    assert code == 0 and entry["status"] == "complete"
    # Window 0: 429, 429, match = three attempts for one billed unit; the other six windows match.
    assert audd.calls == 9 and audd.billed_units == 7
    counts = entry["counts"]
    assert counts["paid_requests"] == 7 and counts["paid_attempts"] == 9  # type: ignore[index]
    assert counts["paid_resolved"] == 7 and counts["paid_billable_units"] == 7  # type: ignore[index]
    assert entry["usd_e6_spent"] == 35_000 and entry["usd_e2_spent"] == 4
    (chain,) = [chain for chain in _chains(media_dir).values() if len(chain) == 3]
    assert [attempt.ordinal for attempt in chain] == [0, 1, 2]
    assert [attempt.outcome for attempt in chain] == ["http_429", "http_429", "match"]
    assert chain[0].parent_attempt_id is None
    assert chain[1].parent_attempt_id == chain[0].attempt_id
    assert chain[2].parent_attempt_id == chain[1].attempt_id
    assert all(attempt.dispatched and attempt.run_id == entry["invocation_id"] for attempt in chain)
    assert any("retry 1/3" in message and "http_429" in message for message in messages)
    assert any("2 retries" in message for message in messages)
    # The journal assertion helper reads the new counts like any other.
    check = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "assert_journal.py"),
            "--work-root",
            str(tmp_path / "work"),
            "--expect",
            "status=complete",
            "paid_attempts=9",
            "paid_requests=7",
            "usd_e6_spent=35000",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stdout + check.stderr


def test_timeout_pre_is_retried_and_the_retry_resolves(tmp_path: Path) -> None:
    code, audd, _shazam, entry, media_dir = _run(tmp_path, "retry-timeout-pre.json")
    assert code == 0 and entry["status"] == "complete"
    assert audd.calls == 8 and audd.billed_units == 7
    assert entry["usd_e6_spent"] == 35_000
    (chain,) = [chain for chain in _chains(media_dir).values() if len(chain) == 2]
    assert [attempt.outcome for attempt in chain] == ["timeout_pre", "match"]


def test_retries_are_bounded_by_the_recipe_and_sleep_its_backoff(tmp_path: Path) -> None:
    delays: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        delays.append(seconds)
        await asyncio.sleep(0)

    code, audd, _shazam, entry, media_dir = _run(
        tmp_path, "retry-503-exhausted.json", sleep=recording_sleep
    )
    # Window 0 never stops answering 503: one attempt plus the recipe's three retries, each
    # refunded, then the window is given up; the six others match.  6/7 < 95 % -> partial.
    assert code == 0
    assert (entry["status"], entry["reason"]) == ("partial", "primary_not_achieved")
    assert audd.calls == 10 and audd.billed_units == 6
    assert entry["usd_e6_spent"] == 30_000
    assert entry["counts"]["paid_attempts"] == 10  # type: ignore[index]
    assert entry["counts"]["paid_failures"] == 1  # type: ignore[index]
    policy = DEEP_RECIPE.retry_policy["audd"]
    assert delays == list(policy.backoff_seconds) == [1, 2, 4]
    (chain,) = [chain for chain in _chains(media_dir).values() if len(chain) == 4]
    assert [attempt.outcome for attempt in chain] == ["http_503"] * 4
    assert (
        [attempt.ordinal for attempt in chain]
        == [0, 1, 2, 3]
        == list(range(policy.max_retries + 1))
    )


def test_billable_ambiguous_outcomes_are_never_retried(tmp_path: Path) -> None:
    # http_500 is spent and ambiguous (plan §2.3.3): exactly one attempt, no backoff.
    delays: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        delays.append(seconds)

    code, audd, _shazam, entry, media_dir = _run(
        tmp_path, "primary-thin.json", sleep=recording_sleep
    )
    assert code == 0 and audd.calls == audd.billed_units == 7
    assert delays == [] and entry["counts"]["paid_attempts"] == 7  # type: ignore[index]
    assert all(len(chain) == 1 for chain in _chains(media_dir).values())


# --------------------------------------------------------------------------------------------------
# auth_error / quota_error classification (plan §2.3.3)
# --------------------------------------------------------------------------------------------------
def test_http_403_is_auth_error_cost_zero_and_stops_the_sweep(tmp_path: Path) -> None:
    code, audd, shazam, entry, media_dir = _run(tmp_path, "all-http-403.json")
    assert code == 3
    assert (entry["status"], entry["reason"], entry["achieved"]) == (
        "provider_unavailable",
        "auth_error",
        None,
    )
    assert audd.calls == 1 and audd.billed_units == 0
    assert entry["usd_e6_spent"] == 0 and entry["costs"] == {"usd_e2": 0}
    assert entry["counts"]["paid_attempts"] == 1  # type: ignore[index]
    assert shazam.requests == 0
    ((attempt,),) = _chains(media_dir).values()
    assert attempt.outcome == "auth_error" and attempt.unit_usd_e6 == 5_000


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProviderProtocolError("AudD HTTP 401"), "auth_error"),
        (ProviderProtocolError("AudD HTTP 403"), "auth_error"),
        (ProviderProtocolError("AudD HTTP 402"), "quota_error"),
        (ProviderProtocolError("AudD HTTP 429"), "http_429"),
        (ProviderProtocolError("AudD HTTP 503"), "http_503"),
        (ProviderProtocolError("AudD HTTP 502"), "http_5xx"),
        (ProviderProtocolError("AudD returned a non-JSON response"), "malformed"),
    ],
)
def test_transport_status_codes_classify_into_the_frozen_outcomes(
    error: ProviderProtocolError, expected: str
) -> None:
    assert _protocol_outcome(error) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"status": "error", "error": {"error_code": 403, "error_message": "x"}}, "auth_error"),
        # AudD's own codes inside an HTTP 200 body: 900 wrong token, 901 limit reached.
        (
            {"status": "error", "error": {"error_code": 900, "error_message": "api_token wrong"}},
            "auth_error",
        ),
        (
            {"status": "error", "error": {"error_code": 901, "error_message": "limit reached"}},
            "quota_error",
        ),
        ({"status": "error", "error": {"error_code": 402, "error_message": "x"}}, "quota_error"),
        (
            {"status": "error", "error": {"error_code": 300, "error_message": "no credits"}},
            "quota_error",
        ),
        ({"status": "error", "error": {"error_code": 429, "error_message": "x"}}, "http_429"),
        ({"status": "error", "error": {"error_code": 503, "error_message": "x"}}, "http_503"),
        ({"status": "error", "error": {"error_code": 500, "error_message": "x"}}, "http_5xx"),
        ({"status": "success", "result": {}}, "malformed"),
    ],
)
def test_error_bodies_classify_into_the_frozen_outcomes(body: dict, expected: str) -> None:
    assert _error_body_outcome(body) == expected


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        # AudD words a throttle "…limit reached"; reading the words first turned a retryable
        # http_429 into a terminal quota_error that stopped the whole primary sweep.
        (429, "Too many requests, your rate limit reached", "http_429"),
        (503, "temporarily unavailable: no credits are being served", "http_503"),
        (500, "author lookup failed", "http_5xx"),
        (403, "forbidden", "auth_error"),
        (901, "limit reached", "quota_error"),
    ],
)
def test_an_explicit_error_code_outranks_the_message_words(
    code: int, message: str, expected: str
) -> None:
    body = {"status": "error", "error": {"error_code": code, "error_message": message}}
    assert _error_body_outcome(body) == expected
    # The transport classifier keeps the same precedence.
    if code < 900:
        assert _protocol_outcome(ProviderProtocolError(f"AudD HTTP {code} {message}")) == expected
    # With no code at all the words still decide, so a genuine quota body is still terminal.
    assert _error_body_outcome({"status": "error", "error": {"error_message": "quota"}}) == (
        "quota_error"
    )


# --------------------------------------------------------------------------------------------------
# Anchor validity rule (plan §2.3.4 step 2; review E-C3)
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("result", "expected_ref_ms"),
    [
        ({"timecode": 23}, 23_000),
        ({"timecode": "00:23"}, 23_000),
        ({"timecode": "01:02:03"}, 3_723_000),
        ({"timecode": 23.5}, 23_500),
        ({"timecode": 0}, 0),
        ({"timecode": 86_400}, 86_400_000),  # exactly anchor_max_ms
        ({"timecode": 210, "duration_ms": 200_000}, 210_000),  # within duration + slack
        ({"timecode": 210, "spotify": {"duration_ms": 200_000}}, 210_000),
        ({"timecode": 212, "apple_music": {"durationInMillis": 200_000}}, 212_000),
    ],
)
def test_a_valid_timecode_becomes_a_reliable_anchor(result: dict, expected_ref_ms: int) -> None:
    window, _query = _window_and_query()
    anchor = clip_anchor(result, mix_anchor_ms=window.support_ms[0])
    assert anchor is not None
    assert anchor.mix_anchor_ms == window.support_ms[0] == 18_000
    assert anchor.ref_anchor_ms == expected_ref_ms
    assert anchor.reliable is True
    assert anchor.uncertainty_ms == CLIP_ANCHOR_UNCERTAINTY_MS == 1_000
    assert anchor.method == CLIP_ANCHOR_METHOD and anchor.bias_applied_ms == 0
    observation = _observation({"artist": "A", "title": "B", **result})
    assert observation.anchor == anchor and observation.status == "match"


@pytest.mark.parametrize(
    "result",
    [
        {},  # absent
        {"timecode": None},
        {"timecode": -5},
        {"timecode": "abc"},
        {"timecode": True},
        {"timecode": 86_401},  # beyond anchor_max_ms (24 h)
        {"timecode": 213, "duration_ms": 200_000},  # beyond duration + 12 s slack
        {"timecode": 213, "apple_music": {"durationInMillis": 200_000}},
    ],
)
def test_an_invalid_timecode_leaves_the_match_anchorless(result: dict) -> None:
    window, _query = _window_and_query()
    assert clip_anchor(result, mix_anchor_ms=window.support_ms[0]) is None
    observation = _observation({"artist": "A", "title": "B", **result})
    assert observation.status == "match" and observation.anchor is None


def test_anchor_bounds_come_from_the_caller_and_default_to_the_recipe_values() -> None:
    assert DEFAULT_ANCHOR_MAX_MS == DEEP_RECIPE.anchor_max_ms == 86_400_000
    assert DEFAULT_ANCHOR_SLACK_MS == DEEP_RECIPE.anchor_slack_ms == 12_000
    tight = _observation({"artist": "A", "title": "B", "timecode": 23}, anchor_max_ms=10_000)
    assert tight.anchor is None
    slack = _observation(
        {"artist": "A", "title": "B", "timecode": 213, "duration_ms": 200_000},
        anchor_slack_ms=13_000,
    )
    assert slack.anchor is not None and slack.anchor.ref_anchor_ms == 213_000
    # An unknown duration never blocks an otherwise valid timecode.
    unknown = _observation({"artist": "A", "title": "B", "timecode": 9_000, "duration_ms": "x"})
    assert unknown.anchor is not None


def test_clip_observations_carry_the_audd_trial_source_so_shazam_is_not_evicted() -> None:
    audd = _observation({"artist": "A", "title": "B", "timecode": 18})
    assert audd.native["simultaneous_source"] == CLIP_SIMULTANEOUS_SOURCE == "audd"
    no_match = _observation(None)
    assert no_match.native["simultaneous_source"] == "audd" and no_match.anchor is None
    # A Shazam match on the same window (the primary trial source) for the same candidate.
    shazam = audd.model_copy(
        update={
            "id": "1" * 40,
            "provider": "shazam",
            "native": {"matches": [{"frequencyskew_e6": 5, "timeskew_e6": 0}]},
        }
    )
    candidates = {audd.id: "cand", shazam.id: "cand"}
    selection = select_logical_trial_points([audd, shazam], candidates)
    assert set(selection.selected_observation_ids) == {audd.id, shazam.id}
    assert selection.hypothesis_rejected == ()
    assert len(selection.points) == 2  # both anchored: AudD's timecode now aligns globally too
    # Without its own source the anchorless-vs-skew tie-break evicted Shazam (review C3).
    legacy = audd.model_copy(update={"native": {"result": audd.native["result"]}})
    evicted = select_logical_trial_points([legacy, shazam], {legacy.id: "cand", shazam.id: "cand"})
    assert len(evicted.selected_observation_ids) == 1 and len(evicted.hypothesis_rejected) == 1


# --------------------------------------------------------------------------------------------------
# Concurrency 4 + token bucket (plan §2.3.1)
# --------------------------------------------------------------------------------------------------
class _Timed:
    """Wraps FakeAudD and records when the first call started and the last one returned."""

    def __init__(self, inner: FakeAudD) -> None:
        self.inner = inner
        self.first_started: float | None = None
        self.last_finished: float | None = None

    async def recognize_clip(self, path: Path, on_attempt):
        if self.first_started is None:
            self.first_started = time.monotonic()
        try:
            return await self.inner.recognize_clip(path, on_attempt)
        finally:
            self.last_finished = time.monotonic()


def test_the_recipe_runs_four_clips_at_once(tmp_path: Path) -> None:
    inner = FakeAudD(SCRIPTS / "latency-match.json")  # 200 ms per clip
    timed = _Timed(inner)
    code, _audd, _shazam, entry, _media_dir = _run(tmp_path, "latency-match.json", audd=timed)
    assert code == 0 and entry["status"] == "complete"
    assert inner.calls == 7 and inner.max_in_flight == DEEP_RECIPE.audd_concurrency == 4
    assert timed.first_started is not None and timed.last_finished is not None
    # Seven 200 ms clips four at a time take two rounds (~0.4 s), not seven in a row (1.4 s).
    assert timed.last_finished - timed.first_started < 1.0


class _RecordingBucket(TokenBucket):
    instances: list[_RecordingBucket] = []

    def __init__(self, rate_per_minute: int = 18, capacity: int = 1, **kwargs: object) -> None:
        super().__init__(rate_per_minute, capacity, **kwargs)  # type: ignore[arg-type]
        self.args = (rate_per_minute, capacity)
        self.acquired = 0
        self.penalties = 0
        self.recoveries = 0
        _RecordingBucket.instances.append(self)

    async def acquire(self) -> None:
        self.acquired += 1
        await super().acquire()

    def penalize(self) -> None:
        self.penalties += 1
        super().penalize()

    def recover(self) -> None:
        self.recoveries += 1
        super().recover()


def test_every_dispatch_takes_a_token_and_throttles_slow_the_bucket(
    tmp_path: Path, monkeypatch
) -> None:
    _RecordingBucket.instances.clear()
    monkeypatch.setattr(paid_clip_module, "TokenBucket", _RecordingBucket)
    code, audd, _shazam, _entry, _media_dir = _run(tmp_path, "retry-429-then-match.json")
    assert code == 0 and audd.calls == 9
    (bucket,) = _RecordingBucket.instances
    # Ceiling from config, capacity = the recipe's concurrency; one token per attempt (retries
    # included); each 429 cuts the rate, each resolved clip nudges it back.
    assert bucket.args == (CONFIG.audd_requests_per_minute, DEEP_RECIPE.audd_concurrency)
    assert bucket.acquired == 9
    assert bucket.penalties == 2 and bucket.recoveries == 7


def test_audd_requests_per_minute_is_a_deep_config_knob_carried_under_a_profile(
    tmp_path: Path, monkeypatch
) -> None:
    assert AppConfig().audd_requests_per_minute == 120
    assert "audd_requests_per_minute = 120" in CONFIG_TEMPLATE
    config = tmp_path / "idea.toml"
    config.write_text(
        "[deep]\nprimary_density = 2\naudd_requests_per_minute = 7\n", encoding="utf-8"
    )
    loaded = AppConfig.load(config)
    assert loaded.audd_requests_per_minute == 7 and loaded.deep_primary_density == 2
    assert "audd_requests_per_minute = 7" in render_effective_config(loaded)
    for bad in ("0", "-1", "true", "'x'"):
        config.write_text(f"[deep]\naudd_requests_per_minute = {bad}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="deep.audd_requests_per_minute"):
            AppConfig.load(config)
    config.write_text("[deep]\naudd_requests_per_minute = 7\n", encoding="utf-8")
    # Both profile call sites carry the file's value over the profile's defaults.
    assert _resolve_settings(ROOT, config, "free").config.audd_requests_per_minute == 7
    captured: dict[str, object] = {}

    async def fake_analyse(_url: str, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_analyse", fake_analyse)
    result = CliRunner().invoke(
        cli.app,
        ["analyse", "http://example/set", "--profile", "free", "--config", str(config)],
    )
    assert result.exit_code == 0, result.output
    assert captured["app_config"].audd_requests_per_minute == 7  # type: ignore[attr-defined]
