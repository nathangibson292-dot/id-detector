"""Phase 1b-iii gate: process breaker and the existing corpus scorer's fixture."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from typer.testing import CliRunner

from id_detector import cli
from id_detector import pipeline as service_pipeline
from id_detector.jobs import BudgetExhausted
from id_detector.profiles import effective_app_config, load_profile
from id_detector.providers.base import AppConfig
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE
from id_detector.recognise import load_provider_config
from id_detector.shazam import ShazamAdapter, ShazamHTTPError, TokenBucket
from id_detector.shazam_breaker import BreakerConfig, ShazamBlocked, ShazamBreaker
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff
from tests.test_phase0a_status import AUDIO, ROOT, SCRIPTS, _entry
from tests.test_score_corpus import EXPECTED
from tests.test_score_corpus import _run as score_corpus


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 11, 12, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    monkeypatch.setenv("AUDD_API_TOKEN", "")
    monkeypatch.delenv("IDEA_ENGINE_SHAZAM", raising=False)


def trip(breaker):
    for _ in range(20):
        breaker.resolved("http_429")


def test_rate_minimum_cooldown_and_rolling_window():
    clock = Clock()
    breaker = ShazamBreaker(clock=clock)
    for _ in range(19):
        breaker.resolved("http_429")
    assert breaker.reason() is None
    breaker.resolved("http_429")
    assert breaker.reason() == "shazam_breaker:a_failure_rate"
    clock.advance(1799)
    assert breaker.reason() == "shazam_breaker:a_failure_rate"
    clock.advance(1)
    assert breaker.reason() is None
    breaker.resolved("http_429")
    assert breaker.reason() is None  # old samples expired, no immediate re-trip


def test_threshold_is_strict_and_all_resolved_outcomes_are_denominator():
    breaker = ShazamBreaker()
    for outcome in [
        "match",
        "no_match",
        "timeout_pre",
        "connect_error",
        "auth_error",
        "quota_error",
    ]:
        breaker.resolved(outcome)
    for _ in range(8):
        breaker.resolved("no_match")
    for _ in range(6):
        breaker.resolved("http_503")
    assert breaker.reason() is None  # exactly 30%
    breaker.resolved("timeout_post")
    assert breaker.reason() == "shazam_breaker:a_failure_rate"


@pytest.mark.parametrize(
    "outcome", ["http_429", "http_503", "http_5xx", "malformed", "timeout_post"]
)
def test_each_qualifying_outcome_opens(outcome):
    breaker = ShazamBreaker()
    for _ in range(20):
        breaker.resolved(outcome)
    assert breaker.reason() == "shazam_breaker:a_failure_rate"


def test_sample_expires_at_five_minutes():
    clock = Clock()
    breaker = ShazamBreaker(clock=clock)
    for _ in range(19):
        breaker.resolved("malformed")
    clock.advance(300)
    breaker.resolved("malformed")
    assert breaker.reason() is None


def test_daily_budget_counts_dispatches_and_expires_at_utc_midnight():
    clock = Clock()
    clock.now = datetime(2026, 9, 11, 23, 59, 59, tzinfo=UTC)
    breaker = ShazamBreaker(BreakerConfig(shazam_daily_budget_per_egress=2), clock=clock)
    breaker.dispatch(running_free=False)
    breaker.resolved("no_match")
    assert breaker.reason() is None
    breaker.dispatch(running_free=False)  # includes unresolved in-flight attempts
    assert breaker.reason() == "shazam_breaker:b_daily_budget"
    with pytest.raises(ShazamBlocked, match="b_daily_budget"):
        breaker.dispatch(running_free=False)
    breaker.dispatch(running_free=True)  # running Free is explicitly allowed to continue
    clock.advance(1)
    assert breaker.reason() is None
    breaker.dispatch(running_free=False)


def test_third_rate_open_latches_across_cooldown_and_midnight_until_explicit_reenable():
    clock = Clock()
    breaker = ShazamBreaker(clock=clock)
    for count in range(3):
        trip(breaker)
        expected = "c_latch" if count == 2 else "a_failure_rate"
        assert breaker.reason() == f"shazam_breaker:{expected}"
        clock.advance(1800)
    assert breaker.reason() == "shazam_breaker:c_latch"
    clock.advance(86400)
    assert breaker.reason() == "shazam_breaker:c_latch"
    breaker.reenable()
    assert breaker.reason() is None
    trip(breaker)
    assert breaker.reason() == "shazam_breaker:a_failure_rate"


def test_trip_counter_resets_on_utc_day_and_budget_survives_reenable():
    clock = Clock()
    breaker = ShazamBreaker(BreakerConfig(shazam_daily_budget_per_egress=1), clock=clock)
    for _ in range(2):
        trip(breaker)
        clock.advance(1800)
    clock.advance(86400)
    trip(breaker)
    assert breaker.reason() == "shazam_breaker:a_failure_rate"
    breaker.dispatch(running_free=True)
    breaker.reenable()
    assert breaker.reason() == "shazam_breaker:b_daily_budget"


def test_config_reset_is_explicit_and_consumed_once():
    breaker = ShazamBreaker(BreakerConfig(latch_count=1))
    trip(breaker)
    breaker.configure(breaker.config)
    assert breaker.reason() == "shazam_breaker:c_latch"
    reset = replace(breaker.config, reenable_generation=1)
    breaker.configure(reset)
    assert breaker.reason() is None
    trip(breaker)
    breaker.configure(reset)
    assert breaker.reason() == "shazam_breaker:c_latch"
    assert ShazamBreaker().reason() is None  # no mutable singleton


def test_config_show_load_and_profile_carry(tmp_path):
    config_path = tmp_path / "idea.toml"
    settings = BreakerConfig(2500, 200, 600, 10, 123, 4, 2)
    config_path.write_text(
        "[shazam_breaker]\n"
        + "\n".join(f"{name} = {value}" for name, value in vars(settings).items())
    )
    config = AppConfig.load(config_path)
    assert config.shazam_breaker == settings
    assert effective_app_config(config, load_profile(ROOT, "free")).shazam_breaker == settings
    result = CliRunner().invoke(cli.app, ["config", "show", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    for name, value in vars(settings).items():
        assert f"{name} = {value}" in result.output
    default = CliRunner().invoke(cli.app, ["config", "show", "--config", str(tmp_path / "absent")])
    assert default.exit_code == 0
    for name, value in vars(BreakerConfig()).items():
        assert f"{name} = {value}" in default.output


@pytest.mark.parametrize(
    "setting",
    [
        "minimum_sample = 0",
        "failure_rate_e4 = 10001",
        "window_seconds = true",
        "shazam_daily_budget_per_egress = -1",
        "latch_count = 0",
        "reenable_generation = -1",
        "typo = 2",
    ],
)
def test_invalid_config_is_rejected(tmp_path, setting):
    path = tmp_path / "idea.toml"
    path.write_text("[shazam_breaker]\n" + setting)
    with pytest.raises(ValueError, match="shazam_breaker"):
        AppConfig.load(path)


def pipeline(tmp_path, breaker, *, free=False, shazam=None, script="gate0a-deep.json", **kwargs):
    audd = FakeAudD(SCRIPTS / script)
    shazam = shazam or FakeShazamHTTP(SCRIPTS / script)
    code = asyncio.run(
        cli._analyse(
            str(AUDIO),
            work_root=tmp_path / "work",
            print_raw=False,
            refresh=False,
            max_requests=100,
            tracklist=None,
            no_hints=True,
            max_generations=0,
            novelty=False,
            recipe=FREE_RECIPE if free else DEEP_RECIPE,
            **kwargs,
            app_config=AppConfig(
                transforms_policy="off",
                recognise_concurrency=1,
                shazam_requests_per_minute=1_000_000,
                audd_requests_per_minute=1_000_000,
                shazam_breaker=breaker.config,
            ),
            shazam_breaker=breaker,
            shazam_http_client=shazam,
            paid_scan_adapters={"audd": audd},
            paid_sleep=no_backoff,
        )
    )
    _, entry = _entry(tmp_path / "work")
    return code, entry, shazam, audd


@pytest.mark.parametrize("rule", ["a", "b", "c"])
def test_open_secondary_is_skipped_and_degraded_with_rule_in_journal(tmp_path, rule):
    breaker = ShazamBreaker(
        BreakerConfig(
            latch_count=1 if rule == "c" else 3,
            shazam_daily_budget_per_egress=1 if rule == "b" else 2000,
        )
    )
    if rule == "b":
        breaker.dispatch(running_free=True)
    else:
        trip(breaker)
    reason = breaker.reason()
    code, entry, shazam, audd = pipeline(tmp_path, breaker)
    assert code == 0 and entry["status"] == "degraded"
    assert entry["reason"] == reason and entry["achieved"] == "deep"
    assert audd.calls == 7 and shazam.requests == 0


def test_mid_secondary_daily_open_skips_remaining_and_is_degraded(tmp_path):
    breaker = ShazamBreaker(BreakerConfig(shazam_daily_budget_per_egress=1))
    code, entry, shazam, audd = pipeline(tmp_path, breaker)
    assert code == 0 and entry["status"] == "degraded"
    assert entry["reason"] == "shazam_breaker:b_daily_budget"
    assert shazam.requests == 1 and audd.calls == 7


def test_admitted_free_primary_finishes_despite_opening(tmp_path):
    breaker = ShazamBreaker(BreakerConfig(shazam_daily_budget_per_egress=1))
    code, entry, shazam, audd = pipeline(tmp_path, breaker, free=True)
    assert code == 0 and entry["status"] == "complete"
    assert shazam.requests == 7 and audd.calls == 0
    assert breaker.reason() == "shazam_breaker:b_daily_budget"
    code, entry, shazam, audd = pipeline(tmp_path / "next", breaker, free=True)
    assert code == 6 and entry["status"] == "waiting"
    assert entry["reason"] == "shazam_breaker:b_daily_budget"
    assert shazam.requests == audd.calls == 0
    # Intake runs first (§3.4/§4.2 submission order) but no analysis starts: no result, no spend.
    assert not list((tmp_path / "next").rglob("tracklist.json"))
    assert entry["usd_e6_reserved"] == entry["usd_e6_spent"] == 0


def test_manual_off_short_circuits_before_bookkeeping(tmp_path, monkeypatch):
    breaker = ShazamBreaker()

    def forbidden(*args, **kwargs):
        pytest.fail("manual off must precede breaker bookkeeping")

    monkeypatch.setenv("IDEA_ENGINE_SHAZAM", "off")
    monkeypatch.setattr(breaker, "configure", forbidden)
    monkeypatch.setattr(breaker, "reason", forbidden)
    code, entry, shazam, audd = pipeline(tmp_path, breaker, free=True)
    assert code == 6 and entry["reason"] == "shazam_manual_off"
    assert shazam.requests == audd.calls == 0
    breaker.reenable()
    with pytest.raises(ShazamBlocked, match="manual_off"):
        breaker.dispatch(running_free=True)
    adapter = ShazamAdapter(load_provider_config(ROOT)[0], process_breaker=breaker)
    with pytest.raises(ShazamBlocked, match="manual_off"):
        asyncio.run(adapter.recognize_once(AUDIO, forbidden))
    assert breaker._daily_attempts == 0 and not breaker._samples


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (httpx.Response(429), "http_429"),
        (httpx.Response(503), "http_503"),
        (httpx.Response(500), "http_5xx"),
        (httpx.Response(401), "auth_error"),
        (httpx.Response(402), "quota_error"),
        (httpx.Response(200, text="html"), "malformed"),
        (httpx.Response(200, json={"error": "bad"}), "malformed"),
        (httpx.Response(200, json={"matches": []}), "no_match"),
        (httpx.ReadTimeout("fixture"), "timeout_post"),
        (httpx.ConnectTimeout("fixture"), "timeout_pre"),
        (httpx.ConnectError("fixture"), "connect_error"),
    ],
)
def test_transport_resolves_each_physical_attempt_once(response, outcome):
    breaker = ShazamBreaker()

    def handle(request):
        if isinstance(response, Exception):
            raise response
        return response

    adapter = ShazamAdapter(
        load_provider_config(ROOT)[0],
        process_breaker=breaker,
        transport=httpx.MockTransport(handle),
        limiter=TokenBucket(1_000_000),
    )

    async def attempted():
        pass

    async def run():
        with suppress(ShazamHTTPError):
            await adapter.recognize_once(AUDIO, attempted)

    asyncio.run(run())
    assert breaker._daily_attempts == 1
    assert len(breaker._samples) == 1
    from id_detector.shazam_breaker import FAILURES

    assert breaker._samples[0][1] == (outcome in FAILURES)


def test_local_budget_refusal_is_not_a_dispatch_or_resolution():
    breaker = ShazamBreaker()
    fake = FakeShazamHTTP(SCRIPTS / "gate0a-deep.json")
    adapter = ShazamAdapter(
        load_provider_config(ROOT)[0], process_breaker=breaker, http_client=fake
    )

    async def refused():
        raise BudgetExhausted("local cap")

    with pytest.raises(BudgetExhausted):
        asyncio.run(adapter.recognize_once(AUDIO, refused))
    assert breaker._daily_attempts == 0 and not breaker._samples and fake.requests == 0


def test_fake_pre_timeout_is_not_a_qualifying_failure():
    breaker = ShazamBreaker()
    fake = FakeShazamHTTP({"shazam": {"default": "timeout_pre"}})
    adapter = ShazamAdapter(
        load_provider_config(ROOT)[0], process_breaker=breaker, http_client=fake
    )

    async def attempted():
        pass

    with pytest.raises(ShazamHTTPError):
        asyncio.run(adapter.recognize_once(AUDIO, attempted))
    assert len(breaker._samples) == 1 and breaker._samples[0][1] is False


def test_corpus_mini_reproduces_expected_using_existing_helper(tmp_path):
    code, result = score_corpus(tmp_path)
    assert code == 0 and result == EXPECTED


def test_free_primary_continues_after_rate_trip(tmp_path):
    breaker = ShazamBreaker()
    for _ in range(19):
        breaker.resolved("malformed")
    fake = FakeShazamHTTP({"shazam": {"default": "match", "windows": {"0": "malformed"}}})
    code, entry, shazam, audd = pipeline(tmp_path, breaker, free=True, shazam=fake)
    assert code == 0 and entry["status"] == "complete"
    assert shazam.requests == 7 and audd.calls == 0
    assert breaker.reason() == "shazam_breaker:a_failure_rate"


def test_web_runner_owns_one_breaker_across_jobs_and_reports_waiting(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from id_detector.webapp.runner import make_pipeline_runner

    seen = []

    async def refused(*args, **kwargs):
        seen.append(kwargs["shazam_breaker"])
        return 6

    monkeypatch.setattr(service_pipeline, "run_analysis", refused)
    context = SimpleNamespace(
        run_id="breaker-web-job",  # the job's durable run id (the runner fails closed without)
        target=str(AUDIO),
        build_index=False,
        profile="free",
        known_tracklist=None,
        cancel_token=None,
        progress=lambda *args: None,
    )
    runner = make_pipeline_runner(tmp_path, config_path=tmp_path / "missing.toml")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="waiting.*not queued locally"):
            runner(context)
    assert seen[0] is seen[1]
    other = make_pipeline_runner(tmp_path, config_path=tmp_path / "missing.toml")
    with pytest.raises(RuntimeError, match="waiting"):
        other(context)
    assert seen[2] is not seen[0]


def test_daily_budget_exhaustion_is_never_itself_a_rate_trip():
    clock = Clock()
    breaker = ShazamBreaker(BreakerConfig(shazam_daily_budget_per_egress=1), clock=clock)
    breaker.dispatch(running_free=True)
    for _ in range(40):
        breaker.resolved("no_match")  # the admitted Free primary runs on, healthily
    assert breaker.reason() == "shazam_breaker:b_daily_budget"
    clock.advance(86400)
    assert breaker.reason() is None  # rule (b) expires at 00:00 UTC: no trip, no latch


def test_qualifying_failures_still_trip_and_latch_while_the_budget_is_exhausted():
    """Rule (b) must not disable rule (a) or the latch (§2.3.5, D8).

    The running Free primary is explicitly allowed past the daily budget, so its qualifying
    failures are exactly the ones that would otherwise get the owner's IP blocked.
    """

    clock = Clock()
    breaker = ShazamBreaker(BreakerConfig(shazam_daily_budget_per_egress=1), clock=clock)
    breaker.dispatch(running_free=True)
    assert breaker.reason() == "shazam_breaker:b_daily_budget"
    for count in range(3):
        trip(breaker)
        assert breaker._opens == count + 1
        clock.advance(1800)
    assert breaker.reason() == "shazam_breaker:c_latch"
    clock.advance(86400)  # the budget resets; the latch does not
    assert breaker.reason() == "shazam_breaker:c_latch"
    breaker.reenable()
    assert breaker.reason() is None


def test_direct_http_boundary_obeys_manual_off_before_limiter(monkeypatch):
    from id_detector.shazam import CircuitBreaker, InjectedHTTPClient

    monkeypatch.setenv("IDEA_ENGINE_SHAZAM", "off")

    async def forbidden():
        pytest.fail("hard off must precede limiter and attempt bookkeeping")

    limiter = TokenBucket()
    monkeypatch.setattr(limiter, "acquire", forbidden)
    client = InjectedHTTPClient(on_attempt=forbidden, limiter=limiter, breaker=CircuitBreaker())
    with pytest.raises(ShazamBlocked, match="manual_off"):
        asyncio.run(client.request("POST", "https://fixture.invalid"))


def test_open_secondary_skips_signature_generation_and_limiter(monkeypatch):
    breaker = ShazamBreaker()
    trip(breaker)
    adapter = ShazamAdapter(
        load_provider_config(ROOT)[0], process_breaker=breaker, running_free=False
    )

    async def forbidden(*args):
        pytest.fail("an already-open secondary must skip work before the adapter/limiter")

    monkeypatch.setattr(adapter, "_recognize_once", forbidden)
    with pytest.raises(ShazamBlocked, match="a_failure_rate"):
        asyncio.run(adapter.recognize_once(AUDIO, forbidden))
    assert breaker._daily_attempts == 0 and len(breaker._samples) == 20


def test_open_breaker_still_serves_a_compatible_cached_result(tmp_path):
    """§3.4: the compatible-result lookup comes first; a cache hit needs no Shazam request.

    Only a request that would start a *new* analysis waits, so an open breaker (here the latch,
    the strongest state) must not refuse a stored ``free complete`` result.
    """

    breaker = ShazamBreaker(BreakerConfig(latch_count=1))
    code, entry, _shazam, _audd = pipeline(tmp_path, breaker, free=True)
    assert code == 0 and entry["status"] == "complete"
    trip(breaker)
    assert breaker.reason() == "shazam_breaker:c_latch"
    code, entry, shazam, audd = pipeline(tmp_path, breaker, free=True)
    assert code == 0 and entry["status"] == "complete"  # served, not waiting
    assert shazam.requests == audd.calls == 0


def test_degrade_restart_refusal_settles_the_deep_reservation(tmp_path):
    """§2.3.2: a Deep run refused at its ``--allow-degrade`` restart already reserved USD.

    The waiting journal must report that reservation and its release, not a run that never
    reserved — nothing is re-billed and nothing is hidden.
    """

    breaker = ShazamBreaker(BreakerConfig(latch_count=1))
    trip(breaker)
    code, entry, shazam, audd = pipeline(
        tmp_path, breaker, script="all-http-401.json", allow_degrade=True
    )
    assert code == 6 and entry["status"] == "waiting"
    assert entry["reason"] == "shazam_breaker:c_latch"
    assert entry["usd_e6_reserved"] == 36_750  # reserved by Deep, released in full here
    assert entry["usd_e6_spent"] == 0 and entry["costs"] == {"usd_e2": 0}
    assert audd.billed_units == 0 and shazam.requests == 0
    assert not list((tmp_path / "work").rglob("tracklist.json"))


def test_web_job_of_a_refused_request_is_waiting_not_failed(tmp_path, monkeypatch):
    """Plan §2.3.5: an open breaker makes new Free jobs ``waiting`` — never ``failed``."""

    from id_detector.webapp import jobs as jobs_module
    from id_detector.webapp.runner import make_pipeline_runner

    async def refused(*args, **kwargs):
        return 6

    monkeypatch.setattr(service_pipeline, "run_analysis", refused)
    runner = make_pipeline_runner(tmp_path, config_path=tmp_path / "missing.toml")
    manager = jobs_module.JobManager(tmp_path, runner)
    try:
        job_id = manager.submit(str(AUDIO), profile="free")
        for _ in range(400):
            job = manager.get(job_id)
            if job is not None and job.status in jobs_module.TERMINAL_STATES:
                break
            time.sleep(0.02)
    finally:
        manager.shutdown()
    job = manager.get(job_id)
    assert job is not None
    assert job.status == jobs_module.WAITING and job.error is None
    assert "not queued locally" in job.message
    assert job.status_dict()["terminal"] is True
    assert job.result_path is None


def test_cached_free_result_does_not_add_breaker_attempts(tmp_path):
    breaker = ShazamBreaker()
    pipeline(tmp_path, breaker, free=True)
    attempts = breaker._daily_attempts
    samples = len(breaker._samples)
    code, _, shazam, audd = pipeline(tmp_path, breaker, free=True)
    assert code == 0 and shazam.requests == audd.calls == 0
    assert breaker._daily_attempts == attempts and len(breaker._samples) == samples
