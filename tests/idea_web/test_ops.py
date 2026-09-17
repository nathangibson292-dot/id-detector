"""Cycle 4b-ii: progress, operations and the shared breaker (plan §2.3.5, §4.5, §4.6).

Everything here is offline and deterministic: the breaker is driven by rows written straight into
the append-only attempt ledger and by an injected clock, and the worker runs with the test seams
4b-i already ships (an intake resolver and a service runner), never a provider.

**U-F9's weighting is not touched by this cycle** — the owner has parked his re-weighting request —
so the tests below pin that the queue's progress document is the *shipped* page object, computed by
``Job.progress_percent``, and that nothing here re-implements a weight.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE
from id_detector.service import PlatformUrl
from id_detector.shazam_breaker import BreakerConfig, ShazamBlocked, ShazamBreaker
from id_detector.webapp.jobs import PHASE_EXPECTED_SECONDS
from id_detector.webapp.jobs import Job as PageJob
from idea_web import ops, progress
from idea_web.breaker import SHAZAM, SharedShazamBreaker
from idea_web.jobs import local
from idea_web.jobs.worker import JobQueue, Worker, _page_profile, _result_path
from idea_web.progress import PAGE_DOCUMENT_KEY
from tests.idea_web.test_worker import (
    _complete,
    _database,
    _insert_run,
    _intake,
    _publish_bundle,
    _resolver,
)

MIX = "https://soundcloud.com/example/ops-mix"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _event(
    database,
    *,
    run_id: str,
    attempt: str,
    state: str,
    at: datetime,
    outcome: str | None = None,
    provider: str = SHAZAM,
    egress: str = "default",
    usd_e6: int = 0,
    query_id: str | None = None,
) -> None:
    """One immutable ledger row, exactly as the attempt journal's projection writes it.

    The schema requires every AudD row to name the clip it asked about; only the Shazam seam is
    allowed a NULL ``query_id`` (it knows the egress and the outcome, never the clip).
    """

    seq = {"prepared": 1, "dispatched": 2, "resolved": 3}[state]
    if provider != SHAZAM and query_id is None:
        query_id = f"{attempt}-query"
    with database.write() as connection:
        connection.execute(
            "INSERT INTO provider_attempt_events(attempt_id, seq, run_id, provider, egress_id, "
            "query_id, parent_attempt_id, state, outcome, http_status, unit_usd_e6, at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, ?)",
            (
                attempt,
                seq,
                run_id,
                provider,
                egress,
                query_id,
                state,
                outcome,
                usd_e6,
                _stamp(at),
            ),
        )


def _seed_run(database, tmp_path: Path, run_id: str = "run-ops") -> str:
    _insert_run(database, _intake(tmp_path), run_id=run_id, status="analysis")
    return run_id


def _breaker_row(database):
    with database.read() as connection:
        return connection.execute("SELECT * FROM provider_breaker_state").fetchone()


class _Open(ShazamBreaker):
    """A breaker that is open for every reason a caller might ask about."""

    def __init__(self, reason: str = "shazam_breaker:a_failure_rate") -> None:
        self._reason = reason

    def reason(self) -> str | None:
        return self._reason


class _Clear(_Open):
    def __init__(self) -> None:
        super().__init__("")

    def reason(self) -> str | None:
        return None


# --------------------------------------------------------------------------------------------------
# The shared breaker (§2.3.5): one policy over the ledger, not one per process
# --------------------------------------------------------------------------------------------------
def test_the_breaker_is_shared_between_processes_not_owned_by_one(tmp_path: Path) -> None:
    """The denominator is "all resolved Shazam attempts on that egress" — so a second worker's
    failures must open the first worker's breaker. A per-process deque cannot do this."""

    database = _database(tmp_path)
    run_id = _seed_run(database, tmp_path)
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    config = BreakerConfig()
    one = SharedShazamBreaker(database, config=config, clock=lambda: now)
    two = SharedShazamBreaker(database, config=config, clock=lambda: now)
    assert one.reason() is None and two.reason() is None

    for index in range(20):
        outcome = "http_429" if index < 10 else "match"
        _event(
            database,
            run_id=run_id,
            attempt=f"attempt-{index}",
            state="resolved",
            outcome=outcome,
            at=now - timedelta(seconds=30),
        )
    assert two.reason() == "shazam_breaker:a_failure_rate"
    assert one.reason() == "shazam_breaker:a_failure_rate"  # the other process sees it at once
    with pytest.raises(ShazamBlocked, match="a_failure_rate"):
        one.dispatch(running_free=False)
    assert one.dispatch(running_free=True) == now.date()


def test_rule_a_needs_the_minimum_sample_and_the_rate(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run_id = _seed_run(database, tmp_path)
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    breaker = SharedShazamBreaker(database, clock=lambda: now)
    for index in range(19):  # nineteen failures: below the minimum sample of twenty
        _event(
            database,
            run_id=run_id,
            attempt=f"small-{index}",
            state="resolved",
            outcome="timeout_post",
            at=now - timedelta(seconds=10),
        )
    assert breaker.reason() is None
    # Twenty resolved with only six failures is 30 %, which is not *greater than* 30 %.
    stale = SharedShazamBreaker(database, clock=lambda: now + timedelta(seconds=600))
    for index in range(20):
        outcome = "http_503" if index < 6 else "no_match"
        _event(
            database,
            run_id=run_id,
            attempt=f"edge-{index}",
            state="resolved",
            outcome=outcome,
            at=now + timedelta(seconds=590),
        )
    assert stale.reason() is None


def test_rule_b_counts_requests_that_were_sent_never_ones_merely_prepared(tmp_path: Path) -> None:
    """§2.3.3: prepared without dispatched was never sent. A cancelled or refused attempt must not
    spend the egress's daily budget and degrade somebody else's run without a single request."""

    database = _database(tmp_path)
    run_id = _seed_run(database, tmp_path)
    now = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
    config = BreakerConfig(shazam_daily_budget_per_egress=3)
    breaker = SharedShazamBreaker(database, config=config, clock=lambda: now)

    # Ten attempts that were prepared and never sent buy nothing at all.
    for index in range(10):
        _event(
            database,
            run_id=run_id,
            attempt=f"never-sent-{index}",
            state="prepared",
            at=now - timedelta(minutes=index),
        )
    assert breaker.reason() is None

    other = SharedShazamBreaker(database, config=config, egress_id="second", clock=lambda: now)
    for index in range(3):
        _event(
            database,
            run_id=run_id,
            attempt=f"sent-{index}",
            state="dispatched",
            at=now - timedelta(minutes=index),
        )
    assert breaker.reason() == "shazam_breaker:b_daily_budget"
    assert other.reason() is None  # the budget is *per egress*
    fresh = SharedShazamBreaker(database, config=config, clock=lambda: now + timedelta(days=1))
    assert fresh.reason() is None  # and per UTC day


def test_a_request_this_process_just_sent_counts_before_its_row_is_visible(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    _seed_run(database, tmp_path)
    now = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
    config = BreakerConfig(shazam_daily_budget_per_egress=1)
    breaker = SharedShazamBreaker(database, config=config, clock=lambda: now)
    assert breaker.reason() is None
    breaker.dispatch(running_free=True)
    assert breaker.reason() is None  # admission alone is not a request
    breaker.sent()
    assert breaker.reason() == "shazam_breaker:b_daily_budget"
    breaker.release_dispatch(now.date())
    assert breaker.reason() == "shazam_breaker:b_daily_budget"  # what left cannot be given back


def test_rule_c_latches_after_three_opens_and_only_an_operator_clears_it(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run_id = _seed_run(database, tmp_path)
    clock = {"now": datetime(2026, 9, 16, 8, 0, tzinfo=UTC)}
    config = BreakerConfig(minimum_sample=2, cooldown_seconds=1, latch_count=3)
    breaker = SharedShazamBreaker(database, config=config, clock=lambda: clock["now"])
    for index in range(2):
        _event(
            database,
            run_id=run_id,
            attempt=f"latch-{index}",
            state="resolved",
            outcome="malformed",
            at=clock["now"],
        )
    assert breaker.reason() == "shazam_breaker:a_failure_rate"  # first open

    # One incident trips ONCE. Its samples stay inside the five-minute window long after the
    # cooldown lapses, so re-tripping requires a resolved event newer than the one last judged —
    # the per-process breaker's guarantee, where only a fresh `resolved()` could move it.
    clock["now"] += timedelta(seconds=2)
    assert breaker.reason() is None
    clock["now"] += timedelta(seconds=2)
    assert breaker.reason() is None

    for generation in (2, 3):
        for index in range(2):
            _event(
                database,
                run_id=run_id,
                attempt=f"latch-{generation}-{index}",
                state="resolved",
                outcome="malformed",
                at=clock["now"],
            )
        expected = "shazam_breaker:a_failure_rate" if generation == 2 else "shazam_breaker:c_latch"
        assert breaker.reason() == expected
        clock["now"] += timedelta(seconds=2)
    with pytest.raises(ShazamBlocked, match="c_latch"):
        breaker.dispatch(running_free=False)

    clock["now"] += timedelta(days=1)
    assert breaker.reason() == "shazam_breaker:c_latch"  # a new day resets opens, never the latch
    state = ops.reenable_breaker(database)
    assert state["latched"] is False and state["reenable_generation"] >= 1
    assert breaker.reason() is None


def test_a_configured_reenable_survives_a_restart(tmp_path: Path) -> None:
    """The operator bumps the generation in config; a restart throws the in-memory config away, so
    it has to be compared with the PERSISTED generation or the latch outlives the re-enable."""

    database = _database(tmp_path)
    run_id = _seed_run(database, tmp_path)
    now = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
    latching = BreakerConfig(minimum_sample=1, cooldown_seconds=1, latch_count=1)
    first = SharedShazamBreaker(database, config=latching, clock=lambda: now)
    _event(database, run_id=run_id, attempt="one", state="resolved", outcome="http_5xx", at=now)
    assert first.reason() == "shazam_breaker:c_latch"
    assert bool(_breaker_row(database)["latched"]) is True

    # A brand-new object, as a restarted worker builds: same durable state, raised generation.
    later = now + timedelta(seconds=latching.window_seconds + 1)
    restarted = SharedShazamBreaker(
        database,
        config=BreakerConfig(
            minimum_sample=1, cooldown_seconds=1, latch_count=1, reenable_generation=1
        ),
        clock=lambda: later,
    )
    assert restarted.reason() is None
    row = _breaker_row(database)
    assert bool(row["latched"]) is False and int(row["reenable_generation"]) == 1


def test_the_manual_kill_switch_still_refuses_every_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    breaker = SharedShazamBreaker(database)
    monkeypatch.setenv("IDEA_ENGINE_SHAZAM", "off")
    with pytest.raises(ShazamBlocked, match="shazam_manual_off"):
        breaker.dispatch(running_free=True)


def test_the_breaker_state_table_migrates_down_again(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.read() as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_breaker_state'"
        ).fetchone()
    database.migrate(3)
    with database.read() as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_breaker_state'"
        ).fetchone()
    database.migrate()
    assert database.version() >= 4


# --------------------------------------------------------------------------------------------------
# The breaker and the queue
# --------------------------------------------------------------------------------------------------
def test_an_open_breaker_parks_a_new_free_job_and_costs_it_no_attempt(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    intake = _intake(tmp_path)
    resolved: list[str] = []
    ran: list[str] = []

    def resolver(job, store):
        resolved.append(job.id)
        return intake

    def service(request):
        ran.append(request.run_id)
        return _complete(request)

    worker = Worker(database, tmp_path / "work", intake_resolver=resolver, service_runner=service)
    worker.process_breaker = _Open()
    parked = worker.run_once()
    assert parked is not None and parked.state == "intake"
    assert parked.attempt == 0  # waiting is not a failed attempt (§4.6)
    assert resolved == [] and ran == []
    with database.read() as connection:
        row = connection.execute("SELECT progress FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert json.loads(row["progress"])["reason"] == "shazam_breaker:a_failure_rate"

    with database.write() as connection:
        connection.execute("UPDATE jobs SET lease_until=NULL WHERE id=?", (job_id,))
    worker.process_breaker = _Clear()
    done = worker.run_once()
    assert done is not None and done.state == "complete" and len(ran) == 1
    assert done.attempt == 1


def test_an_open_breaker_never_interrupts_a_run_already_under_way(tmp_path: Path) -> None:
    """§2.3.5: "running free primary continues". Only work that has not started waits."""

    database = _database(tmp_path)
    queue = JobQueue(database)
    intake = _intake(tmp_path)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, run_id="run-under-way")
    _insert_run(database, intake, run_id="run-under-way", status="analysis")
    with database.write() as connection:
        connection.execute("UPDATE jobs SET state='analysis' WHERE id=?", (job_id,))
    ran: list[str] = []

    def service(request):
        ran.append(request.run_id)
        return _complete(request)

    worker = Worker(
        database, tmp_path / "work", intake_resolver=_resolver(intake), service_runner=service
    )
    worker._intake_for_run = lambda job: intake  # the 4b-i resume seam, isolated
    worker.process_breaker = _Open()
    finished = worker.run_once()
    assert ran == ["run-under-way"]
    assert finished is not None and finished.state == "complete"


# --------------------------------------------------------------------------------------------------
# Progress (U-F9) and failed runs (U-F33) over the durable queue
# --------------------------------------------------------------------------------------------------
def test_the_queue_publishes_the_pages_own_progress_document(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    intake = _intake(tmp_path)

    def service(request):
        request.progress("ingest", 1, 1, "Resolved mix title")
        request.progress("recognise", 3, 10, "")
        return _complete(request)

    Worker(
        database, tmp_path / "work", intake_resolver=_resolver(intake), service_runner=service
    ).run_once()

    row = queue.get(job_id)
    document = row.progress[PAGE_DOCUMENT_KEY]
    assert isinstance(document, dict)
    view = local.job_view(row)
    assert view is not None
    assert view.status == "succeeded" and view.phase == "done"
    assert view.resolved_title == "Resolved mix title"
    assert view.windows_done == 3 and view.windows_total == 10
    assert "ingest" in view.phase_seconds
    status = view.status_dict()
    assert status["progress_pct"] == 100 and status["terminal"] is True

    rebuilt = PageJob(target=MIX, **{name: document[name] for name in progress.SNAPSHOT_FIELDS})
    assert rebuilt.progress_percent() == status["progress_pct"]
    assert not hasattr(progress, "PHASE_EXPECTED_SECONDS")
    assert PHASE_EXPECTED_SECONDS["recognise"] == 1_200.0  # untouched by this cycle


def test_the_hosted_progress_document_is_the_same_shape_local_mode_writes() -> None:
    assert PAGE_DOCUMENT_KEY == local._LOCAL
    assert progress.SNAPSHOT_FIELDS == local._SNAPSHOT_FIELDS


def test_a_job_that_fails_during_intake_is_still_visible(tmp_path: Path) -> None:
    """U-F33: the page document is published BEFORE intake, so a job that never reaches a phase
    still appears with its cause instead of vanishing from the library."""

    database = _database(tmp_path)
    queue = JobQueue(database)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)

    def refuse(job, store):
        raise RuntimeError("the platform would not hand over the audio")

    worker = Worker(database, tmp_path / "work", intake_resolver=refuse, service_runner=_complete)
    worker.run_once()
    row = queue.get(job_id)
    assert isinstance(row.progress.get(PAGE_DOCUMENT_KEY), dict)
    view = local.job_view(row)
    assert view is not None and view.status in ("failed", "queued", "running")

    for _ in range(2):
        with database.write() as connection:
            connection.execute("UPDATE jobs SET lease_until=NULL WHERE id=?", (job_id,))
        worker.run_once()
    row = queue.get(job_id)
    assert row.state == "dead_letter"
    view = local.job_view(row)
    assert view is not None and view.status == "failed"
    assert view.error and "would not hand over" in view.error


def test_a_cache_hit_carries_the_page_document_and_a_real_result_url(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    intake = _intake(tmp_path)
    identifier, directory = _publish_bundle(database, intake, run_id="run-cached")
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)

    def never(request):
        raise AssertionError("a cache hit must not run the pipeline")

    served = Worker(
        database, tmp_path / "work", intake_resolver=_resolver(intake), service_runner=never
    ).run_once()
    assert served is not None and served.state == "complete"
    row = queue.get(job_id)
    document = row.progress[PAGE_DOCUMENT_KEY]
    view = local.job_view(row)
    assert view is not None and view.status == "succeeded"
    # A real, served route — not a bare identifier, which would link to the site root.
    assert document["result_path"].endswith("/index.html")
    assert identifier in document["result_path"]
    assert document["result_path"] == _result_path(tmp_path / "work", directory)
    assert view.status_dict()["result_url"] == "/" + document["result_path"]


def test_an_attached_job_carries_the_page_document(tmp_path: Path) -> None:
    """A second submission of a run somebody else is already driving waits, and stays renderable.

    Its own database: with a compatible bundle already published the intake would be served from
    the cache instead, and the attachment path would never be reached.
    """

    database = _database(tmp_path)
    queue = JobQueue(database)
    intake = _intake(tmp_path)
    # The driver goes through intake, as every run does from 4b-iv: a run is attachable only while
    # it has a payer and a live subscriber (a bare run row has neither).
    queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, run_id="run-driven")
    driver = queue.claim("driver", lease_seconds=600)
    assert driver is not None
    assert Worker(database, tmp_path / "work")._commit_intake(driver, intake) == "analysis"

    def never(request):
        raise AssertionError("an attached job must not run the pipeline")

    waiting_id = queue.enqueue(PlatformUrl(MIX + "/again"), FREE_RECIPE)
    attached = Worker(
        database, tmp_path / "work", intake_resolver=_resolver(intake), service_runner=never
    ).run_once()
    assert attached is not None and attached.state == "waiting"
    row = queue.get(waiting_id)
    assert row.run_id == "run-driven"
    assert row.progress["attached"] is True
    assert isinstance(row.progress.get(PAGE_DOCUMENT_KEY), dict)
    assert local.job_view(row) is not None


def test_a_retry_continues_the_bar_instead_of_resetting_it(tmp_path: Path) -> None:
    """A reclaimed job that started a fresh tracker would throw away its measured time and its
    monotonic high-water mark, and the bar would snap backwards on every retry (U-F9)."""

    database = _database(tmp_path)
    queue = JobQueue(database)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    intake = _intake(tmp_path)
    attempts = {"n": 0}

    def service(request):
        attempts["n"] += 1
        request.progress("ingest", 1, 1, "Resolved mix title")
        request.progress("recognise", 5, 10, "")
        if attempts["n"] == 1:
            raise RuntimeError("deterministic failure")
        return _complete(request)

    worker = Worker(
        database, tmp_path / "work", intake_resolver=_resolver(intake), service_runner=service
    )
    worker.run_once()
    first = queue.get(job_id).progress[PAGE_DOCUMENT_KEY]
    # A fake run finishes in microseconds, so the *percentage* is honestly still 0; what must
    # survive the retry is the measured phase time and the high-water mark behind it.
    assert first["phase_seconds"] and "ingest" in first["phase_seconds"]

    with database.write() as connection:
        connection.execute("UPDATE jobs SET lease_until=NULL WHERE id=?", (job_id,))
    worker.run_once()
    second = queue.get(job_id).progress[PAGE_DOCUMENT_KEY]
    assert attempts["n"] == 2
    assert second["progress_max"] >= first["progress_max"]  # never backwards
    assert second["created_at"] == first["created_at"]  # the same job, not a new one
    assert set(first["phase_seconds"]) <= set(second["phase_seconds"])  # measurements carried
    for phase, spent in first["phase_seconds"].items():
        assert second["phase_seconds"][phase] >= spent  # and they accumulate, never reset


def test_a_deep_run_is_labelled_as_the_renderer_understands_paid(tmp_path: Path) -> None:
    """`present/server.py` is frozen and only knows ``max_accuracy`` as paid; a Deep run labelled
    "deep" renders as a Free scan and tells the owner nothing was spent."""

    assert _page_profile(DEEP_RECIPE) == "max_accuracy"
    assert _page_profile(FREE_RECIPE) == "free"

    database = _database(tmp_path)
    queue = JobQueue(database)
    intake = _intake(tmp_path, DEEP_RECIPE)
    job_id = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE)
    Worker(
        database,
        tmp_path / "work",
        local_mode=True,  # hosted paid is refused before intake; local mode is where Deep runs
        intake_resolver=_resolver(intake),
        service_runner=_complete,
    ).run_once()
    document = queue.get(job_id).progress[PAGE_DOCUMENT_KEY]
    assert document["profile"] == "max_accuracy"


# --------------------------------------------------------------------------------------------------
# The operator's view
# --------------------------------------------------------------------------------------------------
def test_looking_at_the_breaker_never_changes_it(tmp_path: Path) -> None:
    """Viewing operations must be read-only: a display that can open the breaker is a display that
    takes the service down. It must also judge with the worker's configured policy."""

    database = _database(tmp_path)
    run_id = _seed_run(database, tmp_path)
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    for index in range(2):
        _event(
            database,
            run_id=run_id,
            attempt=f"view-{index}",
            state="resolved",
            outcome="http_429",
            at=now,
        )
    config = BreakerConfig(minimum_sample=2)
    # Under the configured policy this WOULD trip; looking at it must not make it trip.
    states = ops.breaker_states(database, config=config, now=now.timestamp())
    assert [state["refusing"] for state in states] == ["shazam_breaker:a_failure_rate"]
    assert _breaker_row(database) is None  # nothing was written by looking

    # The default policy (minimum sample 20) would say nothing is wrong — so the display really is
    # using the worker's configuration rather than library defaults.
    assert ops.breaker_states(database, now=now.timestamp())[0]["refusing"] is None
    assert _breaker_row(database) is None

    # Only the breaker itself writes, and only when it is judging for real.
    SharedShazamBreaker(database, config=config, clock=lambda: now).refusal()
    assert _breaker_row(database) is not None


def test_the_operations_cost_counts_only_what_is_billable(tmp_path: Path) -> None:
    """§2.3.2: a 429, an auth error and a quota error cost nothing. Billing an operator's display
    for them would be false, even though it never touches the money authority."""

    database = _database(tmp_path)
    run_id = _seed_run(database, tmp_path)
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    for index, outcome in enumerate(("match", "http_429", "quota_error", "auth_error")):
        _event(
            database,
            run_id=run_id,
            attempt=f"cost-{index}",
            state="dispatched",
            at=now,
            provider="audd",
        )
        _event(
            database,
            run_id=run_id,
            attempt=f"cost-{index}",
            state="resolved",
            outcome=outcome,
            at=now,
            provider="audd",
            usd_e6=5_000,
        )
    usage = [
        item
        for item in ops.provider_usage(database, now=now.timestamp())
        if item["provider"] == "audd"
    ]
    assert usage and usage[0]["dispatched"] == 4 and usage[0]["resolved"] == 4
    assert usage[0]["usd_e6"] == 5_000  # the one match, and nothing for the three refusals


def test_the_operations_snapshot_shows_the_queue_the_waits_and_the_breaker(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    intake = _intake(tmp_path)
    run_id = _seed_run(database, tmp_path, "run-view")

    waiting_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    worker = Worker(
        database, tmp_path / "work", intake_resolver=_resolver(intake), service_runner=_complete
    )
    worker.process_breaker = _Open("shazam_breaker:b_daily_budget")
    worker.run_once()
    assert queue.get(waiting_id).state == "intake"

    dead_id = queue.enqueue(PlatformUrl(MIX + "/other"), FREE_RECIPE)

    def fail(_request):
        raise RuntimeError("deterministic failure")

    failing = Worker(
        database, tmp_path / "work", intake_resolver=_resolver(intake), service_runner=fail
    )
    for _ in range(3):
        with database.write() as connection:
            connection.execute("UPDATE jobs SET lease_until=NULL WHERE id=?", (dead_id,))
        failing.run_once()

    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    for index in range(4):
        _event(database, run_id=run_id, attempt=f"used-{index}", state="dispatched", at=now)
        _event(
            database,
            run_id=run_id,
            attempt=f"used-{index}",
            state="resolved",
            outcome="http_429" if index else "match",
            at=now,
            usd_e6=1_000,
        )

    snapshot = ops.operations_snapshot(
        # A negative threshold keeps the stale-run check independent of today's date.
        database,
        work_root=tmp_path,
        now=now.timestamp(),
        threshold=-1e12,
    )
    assert snapshot["queue"]["dead_letter"] == 1
    assert snapshot["queue"]["intake"] == 1
    parked = [item for item in snapshot["waiting"] if item["id"] == waiting_id]
    assert parked and parked[0]["reason"] == "shazam_breaker:b_daily_budget"
    assert [item["id"] for item in snapshot["dead_letters"]] == [dead_id]
    assert "deterministic failure" in snapshot["dead_letters"][0]["reason"]
    usage = [item for item in snapshot["providers"] if item["provider"] == SHAZAM]
    assert usage and usage[0]["dispatched"] == 4 and usage[0]["resolved"] == 4
    assert usage[0]["failures"] == 3 and usage[0]["usd_e6"] == 1_000  # one billable match
    assert [state["provider"] for state in snapshot["breakers"]] == [SHAZAM]
    assert snapshot["disk"]["total_bytes"] > 0


def test_the_operations_entry_point_prints_a_snapshot_and_reenables(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    assert ops.main([str(database.path), "show", "--work-root", str(tmp_path)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert set(shown) == {
        "at",
        "day",
        "queue",
        "waiting",
        "dead_letters",
        "providers",
        "breakers",
        "disk",
    }
    assert ops.main([str(database.path), "reenable"]) == 0
    assert json.loads(capsys.readouterr().out)["reenable_generation"] == 1


def test_a_reenable_takes_effect_while_the_failures_are_still_in_the_window(
    tmp_path: Path,
) -> None:
    """The process breaker cleared its sample deque. A ledger cannot be cleared, so a re-enable
    records a **cutoff** instead; without one the failures that caused the trip are still inside
    the five-minute window and re-open the breaker on the very next judgement."""

    database = _database(tmp_path)
    run_id = _seed_run(database, tmp_path)
    now = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
    config = BreakerConfig(minimum_sample=2, cooldown_seconds=1, latch_count=1)
    breaker = SharedShazamBreaker(database, config=config, clock=lambda: now)
    for index in range(2):
        _event(
            database,
            run_id=run_id,
            attempt=f"burn-{index}",
            state="resolved",
            outcome="http_429",
            at=now,
        )
    assert breaker.reason() == "shazam_breaker:c_latch"

    # No clock advance: the samples are still in the ledger and still inside the window.
    breaker.reenable()
    assert breaker.reason() is None
    assert _breaker_row(database)["reenabled_at"] is not None

    # A failure *after* the cutoff still counts, so the breaker is re-enabled, not disabled.
    for index in range(2):
        _event(
            database,
            run_id=run_id,
            attempt=f"after-{index}",
            state="resolved",
            outcome="http_429",
            at=now + timedelta(seconds=1),
        )
    later = SharedShazamBreaker(database, config=config, clock=lambda: now + timedelta(seconds=2))
    assert later.reason() == "shazam_breaker:c_latch"


def test_viewing_operations_creates_nothing_and_writes_nothing(tmp_path: Path) -> None:
    """`Database.connect()` creates a missing file and runs ``PRAGMA journal_mode = WAL`` — both
    writes. A typoed path must not bring a database into existence, and looking at a database must
    not modify it."""

    from idea_web.database import Database

    missing = Database(tmp_path / "absent" / "app.db")
    empty = ops.operations_snapshot(missing)
    assert empty["queue"]["intake"] == 0 and empty["breakers"] == []
    assert not (tmp_path / "absent" / "app.db").exists()
    assert not (tmp_path / "absent").exists()

    database = _database(tmp_path)
    before = database.path.read_bytes()
    beside = sorted(path.name for path in database.path.parent.iterdir())
    ops.operations_snapshot(database, work_root=tmp_path)
    # Not one byte, and not one file. A plain `mode=ro` reader of an idle WAL database materialises
    # a `-shm` beside it; operations must never write into a work root, so with nothing holding the
    # database open it reads a copy made outside the tree.
    assert database.path.read_bytes() == before
    assert sorted(path.name for path in database.path.parent.iterdir()) == beside
    for suffix in ("-wal", "-shm"):
        assert not Path(str(database.path) + suffix).exists(), suffix
    with ops.read_only(database) as connection:
        # The view is a private in-memory copy: the real tables, and no journal of its own.
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal"
    for suffix in ("-wal", "-shm"):
        assert not Path(str(database.path) + suffix).exists(), suffix


def test_the_page_states_the_spend_the_authority_holds_not_this_passs_belief(
    tmp_path: Path,
) -> None:
    """`RunResult` carries what this pass thinks it spent. Recovery can fold durable provider
    events it never saw, and SQLite is then correct while the result understates the cost."""

    from id_detector.money import ceil_e2
    from id_detector.service import RunResult

    database = _database(tmp_path)
    queue = JobQueue(database)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE)
    intake = _intake(tmp_path)

    def service(request):
        # More durable spend on the run than this pass believes: exactly what recovery finds.
        with database.write() as connection:
            connection.execute(
                "UPDATE analysis_runs SET usd_e6_spent=? WHERE run_id=?",
                (2_500_000, request.run_id),
            )
        return RunResult(
            request.run_id, "complete", None, request.recipe.name, None, 0, 1_000_000, 0
        )

    Worker(
        database, tmp_path / "work", intake_resolver=_resolver(intake), service_runner=service
    ).run_once()
    document = queue.get(job_id).progress[PAGE_DOCUMENT_KEY]
    assert document["spend_known"] is True
    assert document["usd_e2_spent"] == ceil_e2(2_500_000)
    assert document["usd_e2_spent"] > ceil_e2(1_000_000)


def test_a_cache_hit_across_two_source_aliases_links_to_the_stored_bundle(tmp_path: Path) -> None:
    """Compatibility selects by ``media_key``, and two source keys can share one audio. Rebuilding
    the URL under the new intake's media directory would hand the owner a link that 404s."""

    from idea_web.jobs.worker import PreparedIntake

    database = _database(tmp_path)
    queue = JobQueue(database)
    first = _intake(tmp_path)
    _identifier, directory = _publish_bundle(database, first, run_id="run-alias")

    # A second alias for the same audio: a different source key, the same media key.
    second_dir = tmp_path / "work" / "other-source" / first.inputs.media_key
    second_dir.mkdir(parents=True, exist_ok=True)
    second = PreparedIntake(first.inputs, second_dir, first.duration_ms, {})

    def never(request):
        raise AssertionError("a cache hit must not run the pipeline")

    job_id = queue.enqueue(PlatformUrl(MIX + "/alias"), FREE_RECIPE)
    served = Worker(
        database, tmp_path / "work", intake_resolver=_resolver(second), service_runner=never
    ).run_once()
    assert served is not None and served.state == "complete"
    document = queue.get(job_id).progress[PAGE_DOCUMENT_KEY]
    assert document["result_path"] == _result_path(tmp_path / "work", directory)
    assert "other-source" not in document["result_path"]
    assert local.job_view(queue.get(job_id)).status_dict()["result_url"] == (
        "/" + document["result_path"]
    )


def test_operations_reads_a_live_wal_writer_correctly_and_creates_no_sidecar(
    tmp_path: Path,
) -> None:
    """``immutable=1`` promised SQLite the file never changes. With the queue being written, a
    committed row can live only in the WAL — invisible to an immutable reader, which then reports
    an empty queue. The view must see it, and still create nothing beside the database."""

    import sqlite3

    database = _database(tmp_path)
    holder = sqlite3.connect(database.path)  # a live connection keeps the WAL and its index
    try:
        holder.execute("PRAGMA wal_autocheckpoint = 0")
        holder.execute("SELECT COUNT(*) FROM jobs").fetchone()
        JobQueue(database).enqueue(PlatformUrl(MIX), FREE_RECIPE)  # committed into the WAL only
        wal = Path(str(database.path) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0

        main_file_only = sqlite3.connect(f"{database.path.as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            # What the old reader saw: the main file, without the committed row.
            stale = main_file_only.execute(
                "SELECT COUNT(*) FROM jobs WHERE state='intake'"
            ).fetchone()[0]
        finally:
            main_file_only.close()
        assert stale == 0

        beside = sorted(path.name for path in database.path.parent.iterdir())
        assert ops.queue_depth(database)["intake"] == 1
        assert sorted(path.name for path in database.path.parent.iterdir()) == beside
    finally:
        holder.close()


def test_the_operations_snapshot_reads_one_copy_for_every_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One document, one moment: the database is copied once, not once per section."""

    database = _database(tmp_path)
    JobQueue(database).enqueue(PlatformUrl(MIX), FREE_RECIPE)
    real = ops._copy_into
    copies: list[Path] = []

    def counted(path, memory, **kwargs):
        copies.append(Path(path))
        return real(path, memory, **kwargs)

    monkeypatch.setattr(ops, "_copy_into", counted)
    snapshot = ops.operations_snapshot(database, work_root=tmp_path, threshold=-1e12)
    assert len(copies) == 1
    assert snapshot["queue"]["intake"] == 1
    assert [item["state"] for item in snapshot["waiting"]] == ["intake"]
    ops.queue_depth(database)  # outside the snapshot, a section takes its own copy again
    assert len(copies) == 2
