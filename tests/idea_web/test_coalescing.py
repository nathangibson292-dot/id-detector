"""Cycle 4b-iv gate: coalescing, subscribers, payer transfer, cancel and drain (plan §3.4, §3.5).

Users and credit reservations do not exist yet (4c-i, 4d-i). A "user" is the opaque ``user_id`` a
job is submitted with, and a subscriber's reservation is its ``run_subscribers`` row. The USD money
is the real 0003 authority: reservations, dispatch rows, attempt events and settlements written by
the same journal, admission and ledger a paid run uses. Paid runs are driven by the queue worker in
local mode because hosted paid dispatch stays refused; every race uses separate SQLite handles on
threads or processes, started together by a barrier.
"""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from id_detector.attempts import DispatchRefused
from id_detector.compat import AnalysisInputs
from id_detector.compat import RunRequest as CompatibilityRequest
from id_detector.io import atomic_write_json, sha256_file
from id_detector.money import reserve_usd
from id_detector.present.bundles import bundle_id
from id_detector.providers.base import AppConfig
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE
from id_detector.run_ledger import fold_run_ledger, recovered_money
from id_detector.service import PlatformUrl, RunResult, interrupted_entry
from idea_web.database import Database
from idea_web.jobs.worker import (
    ALL_SUBSCRIBERS_DETACHED,
    HOSTED_PAID_REFUSAL,
    USD_REATTRIBUTION_OVERAGE,
    JobQueue,
    PreparedIntake,
    QuotaExceeded,
    Worker,
)
from tests.idea_web.test_worker import MIX, _database, _intake, _job_row, _resolver, _run_row

UNIT = AppConfig().audd_usd_e6_per_request
BARRIER_SECONDS = 60
#: A paid run's real reservation: 10 planned requests at the configured price, plus 5 %.
RESERVATION = reserve_usd(
    planned=10, unit_usd_e6=UNIT, recipe_max_usd_e2=900, configured_max_usd_e2=None
)


# ------------------------------------------------------------------------------------ helpers


def _subscribers(database: Database, run_id: str) -> list[dict[str, Any]]:
    with database.read() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM run_subscribers WHERE run_id=? ORDER BY seq", (run_id,)
            )
        ]


def _events(database: Database, run_id: str) -> list[dict[str, Any]]:
    with database.read() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM run_payer_events WHERE run_id=? ORDER BY event_id", (run_id,)
            )
        ]


def _kinds(database: Database, run_id: str) -> list[tuple[str, str | None, str | None]]:
    return [
        (event["kind"], event["user_id"], event["to_user_id"])
        for event in _events(database, run_id)
    ]


def _count(database: Database, sql: str, *params: object) -> int:
    with database.read() as connection:
        return int(connection.execute(sql, params).fetchone()[0])


def _audit(database: Database) -> list[dict[str, Any]]:
    with database.read() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM admin_audit ORDER BY id")]


def _nothing_may_run(_request) -> RunResult:
    raise AssertionError("an attached subscriber started a pipeline")


def _complete(request) -> RunResult:
    return RunResult(request.run_id, "complete", None, request.recipe.name, None, 0, 0, 0)


def _subscribe(
    database: Database,
    tmp_path: Path,
    users: list[str],
    *,
    intake: PreparedIntake | None = None,
    recipe=FREE_RECIPE,
    local_mode: bool = False,
    scope: str | None = None,
) -> tuple[JobQueue, list[str], str]:
    """Commit one intake per user, in order: the first drives a new run, the rest attach to it.

    Claims are taken with a long lease and no pipeline is run, so the run stays live for the test.
    """

    intake = intake or _intake(tmp_path, recipe)
    queue = JobQueue(database, local_mode=local_mode)
    worker = Worker(database, tmp_path / "work", local_mode=local_mode)
    job_ids: list[str] = []
    for user in users:
        job_id = queue.enqueue(PlatformUrl(MIX), recipe, user_id=user, tenant_scope=scope)
        job = queue.claim(f"intake-{user}", lease_seconds=600)
        assert job is not None and job.id == job_id
        expected = "analysis" if not job_ids else "waiting"
        assert worker._commit_intake(job, intake) == expected
        job_ids.append(job_id)
    run_id = queue.get(job_ids[0]).run_id
    assert run_id is not None
    return queue, job_ids, run_id


def _payer(database: Database, run_id: str) -> tuple[str | None, str | None]:
    run = _run_row(database, run_id)
    return run["payer_user"], run["payer_reservation_id"]


def _reservation_of(database: Database, job_id: str) -> str:
    with database.read() as connection:
        return connection.execute(
            "SELECT reservation_id FROM run_subscribers WHERE job_id=?", (job_id,)
        ).fetchone()[0]


class RecordingSeam:
    """A 4d-i stand-in: records each reservation, writes a probe row, refuses listed users."""

    def __init__(self, database: Database, refuse: frozenset[str] = frozenset()) -> None:
        self.refuse = refuse
        self.calls: list[tuple[str, str | None, bool]] = []
        with database.write() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS seam_probe (job_id TEXT NOT NULL, kind TEXT NOT NULL)"
            )

    def _reserve(self, kind: str, connection: Any, job: Any) -> None:
        self.calls.append((kind, job.user_id, connection.in_transaction))
        connection.execute("INSERT INTO seam_probe VALUES (?, ?)", (job.id, kind))
        if job.user_id in self.refuse:
            raise QuotaExceeded(f"{job.user_id} has no minutes left")

    def reserve_for_new_run(self, connection: Any, job: Any, intake: Any) -> None:
        self._reserve("new", connection, job)

    def reserve_for_subscriber(self, connection: Any, job: Any, intake: Any, run_id: str) -> None:
        assert connection.execute(
            "SELECT 1 FROM analysis_runs WHERE run_id=? AND status='analysis'", (run_id,)
        ).fetchone()
        self._reserve("subscriber", connection, job)


class MonthCaps:
    """A 4d-iv stand-in: each listed account's remaining USD this month (others uncapped)."""

    def __init__(self, remaining: dict[str, int]) -> None:
        self.remaining = remaining
        self.asked: list[tuple[str | None, bool]] = []

    def account_month_remaining_e6(self, connection: Any, user_id: str | None) -> int | None:
        self.asked.append((user_id, connection.in_transaction))
        return self.remaining.get(user_id or "")


def _reserve_and_dispatch(request, outcomes: list[str | None]) -> list[str]:
    """Record the run's real reservation, then prepare and dispatch one AudD attempt per outcome.

    ``None`` leaves the attempt dispatched and unresolved (an ambiguous, in-flight request).
    """

    journal = request.attempt_journal
    journal.record_reservation(RESERVATION)
    attempts = []
    for outcome in outcomes:
        attempt = _dispatch(request)
        if outcome is not None:
            journal.resolved(attempt, outcome)
        attempts.append(attempt)
    return attempts


def _dispatch(request) -> str:
    journal = request.attempt_journal
    ordinal = len(journal.events_for("audd"))
    attempt = journal.prepare(
        query_id=f"{ordinal:064x}", window_id="f" * 40, ordinal=0, parent_attempt_id=None
    )
    journal.dispatched(attempt)
    return attempt


def _settle(request, tmp_path: Path, status: str) -> RunResult:
    """What the service does at a terminal status: fold the durable money, settle it ONCE."""

    journal = request.attempt_journal
    money = recovered_money(
        fold_run_ledger(request.run_id, journal.durable_events()), journal.durable_reservation()
    )
    writer = request.checkpoint_store.settlement_writer(request.run_id)
    writer(
        tmp_path / "invocations.jsonl", interrupted_entry(request.run_id, MIX, status, money, [])
    )
    return RunResult(
        request.run_id,
        status,
        None,
        None,
        None,
        money.usd_e6_reserved,
        money.usd_e6_spent,
        money.attempts,
    )


def _settlement(database: Database, run_id: str) -> dict[str, Any] | None:
    with database.read() as connection:
        row = connection.execute(
            "SELECT status, entry FROM run_settlements WHERE run_id=?", (run_id,)
        ).fetchone()
    return None if row is None else {"status": row["status"], **json.loads(row["entry"])}


# ------------------------------------------------------- gate 1: one run, two reserved subscribers


def test_two_users_with_the_same_key_get_one_run_and_two_reserved_subscribers(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    intake = _intake(tmp_path)
    seam = RecordingSeam(database)
    alice = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id="alice")
    bob = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id="bob")
    # A second consumer on its own SQLite handle, as a second worker process would have.
    other = Worker(
        Database(database.path),
        tmp_path / "work",
        intake_resolver=_resolver(intake),
        service_runner=_nothing_may_run,
        reservation_seam=seam,
    )
    seen: dict[str, Any] = {}

    def service(request) -> RunResult:
        attached = other.run_once()
        assert attached is not None and attached.id == bob and attached.state == "waiting"
        seen["run"] = dict(_run_row(database, request.run_id))
        seen["subscribers"] = _subscribers(database, request.run_id)
        seen["bob"] = dict(_job_row(database, bob))
        seen["runs"] = _count(database, "SELECT COUNT(*) FROM analysis_runs")
        return _complete(request)

    driver = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(intake),
        service_runner=service,
        reservation_seam=seam,
    ).run_once()
    assert driver is not None and driver.id == alice and driver.state == "complete"
    run_id = driver.run_id
    assert run_id is not None

    # While the run was live: ONE run, TWO subscribers, BOTH holding a reservation of one size.
    assert seen["runs"] == 1
    assert seen["bob"]["run_id"] == run_id and seen["bob"]["attached"] == 1
    subscribers = seen["subscribers"]
    assert [row["user_id"] for row in subscribers] == ["alice", "bob"]
    assert [row["reservation_state"] for row in subscribers] == ["held", "held"]
    assert [row["minutes_reserved"] for row in subscribers] == [1, 1]  # a 60 s mix
    assert {row["recipe_id"] for row in subscribers} == {FREE_RECIPE.recipe_id}
    # The initiator's reservation is the settling one; bob's is provisional.
    assert (seen["run"]["payer_user"], seen["run"]["payer_reservation_id"]) == (
        "alice",
        subscribers[0]["reservation_id"],
    )
    # The seam reserved for both, each inside the deciding transaction.
    assert seam.calls == [("new", "alice", True), ("subscriber", "bob", True)]

    # At the terminal status: the payer settles, the provisional reservation is released, and the
    # attached job has its result in the same transaction (no reconciliation pass ran).
    assert _job_row(database, bob)["state"] == "complete"
    assert [row["reservation_state"] for row in _subscribers(database, run_id)] == [
        "settled",
        "released",
    ]
    assert _kinds(database, run_id) == [
        ("attach", "alice", None),
        ("attach", "bob", None),
        ("close", "alice", None),
        ("release", "bob", None),
    ]
    assert _events(database, run_id)[2]["status"] == "complete"


def test_two_intakes_racing_for_one_new_key_make_exactly_one_run(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    intake = _intake(tmp_path)
    ids = [queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id=user) for user in ("alice", "bob")]
    together = threading.Barrier(2)
    first_done = threading.Event()
    results: list[Any] = []
    errors: list[BaseException] = []

    def resolve(_job, _store) -> PreparedIntake:
        together.wait(
            BARRIER_SECONDS
        )  # both intakes are claimed and prepared before either commits
        return intake

    def service(request) -> RunResult:
        # The driver finishes only once the other intake has committed.
        assert first_done.wait(BARRIER_SECONDS)
        return _complete(request)

    def consume(name: str) -> None:
        try:
            worker = Worker(
                Database(database.path),
                tmp_path / "work",
                worker_id=name,
                intake_resolver=resolve,
                service_runner=service,
            )
            results.append(worker.run_once())
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)
        finally:
            first_done.set()

    threads = [threading.Thread(target=consume, args=(f"worker-{n}",)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert not errors
    assert sorted(result.id for result in results) == sorted(ids)
    assert _count(database, "SELECT COUNT(*) FROM analysis_runs") == 1
    run_id = _job_row(database, ids[0])["run_id"]
    assert _job_row(database, ids[1])["run_id"] == run_id
    assert sorted(_job_row(database, job_id)["attached"] for job_id in ids) == [0, 1]
    assert {_job_row(database, job_id)["state"] for job_id in ids} == {"complete"}
    assert _count(database, "SELECT COUNT(*) FROM run_subscribers WHERE run_id=?", run_id) == 2


def test_a_refused_reservation_is_quota_exceeded_and_never_attached(tmp_path: Path) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path)
    seam = RecordingSeam(database, refuse=frozenset({"bob", "carol"}))
    queue, (alice,), run_id = _subscribe(database, tmp_path, ["alice"], intake=intake)
    worker = Worker(database, tmp_path / "work", reservation_seam=seam)

    bob = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id="bob")
    job = queue.claim("intake-bob", lease_seconds=600)
    assert job is not None and job.id == bob
    assert worker._commit_intake(job, intake) == "quota_exceeded"
    row = _job_row(database, bob)
    assert (row["state"], row["run_id"], row["attached"], row["claim_token"]) == (
        "quota_exceeded",
        None,
        0,
        None,
    )
    assert [sub["user_id"] for sub in _subscribers(database, run_id)] == ["alice"]
    # The seam's own writes were rolled back with the refusal.
    assert _count(database, "SELECT COUNT(*) FROM seam_probe") == 0

    # A refused NEW run leaves no run at all.
    carol = queue.enqueue(
        PlatformUrl("https://soundcloud.com/example/other"), FREE_RECIPE, user_id="carol"
    )
    job = queue.claim("intake-carol", lease_seconds=600)
    assert job is not None and job.id == carol
    other = PreparedIntake(
        AnalysisInputs("b" * 8, FREE_RECIPE.recipe_id, "platform", "public", "hints"),
        intake.media_dir,
        intake.duration_ms,
        {},
    )
    assert worker._commit_intake(job, other) == "quota_exceeded"
    assert _count(database, "SELECT COUNT(*) FROM analysis_runs") == 1
    assert _job_row(database, alice)["state"] == "analysis"


# ------------------------------------------------------------- gate 2: initiator detach → transfer


def test_the_initiator_detaching_moves_the_payer_to_the_earliest_subscriber_with_its_usd(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path, DEEP_RECIPE)
    queue = JobQueue(database, local_mode=True)
    # Attachment order, not name order, decides who pays next.
    alice = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, user_id="alice")
    zoe = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, user_id="zoe")
    adam = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, user_id="adam")
    caps = MonthCaps({"zoe": 10_000})
    attacher = Worker(
        Database(database.path),
        tmp_path / "work",
        local_mode=True,
        intake_resolver=_resolver(intake),
        service_runner=_nothing_may_run,
    )
    detacher = JobQueue(Database(database.path), local_mode=True, usd_caps=caps)
    seen: dict[str, Any] = {}

    def service(request) -> RunResult:
        _reserve_and_dispatch(request, ["match"])
        assert attacher.run_once().id == zoe
        assert attacher.run_once().id == adam
        reserved = _run_row(database, request.run_id)["usd_e6_reserved"]
        assert reserved == RESERVATION.usd_e6_reserved > 10_000  # the run's own reservation

        assert detacher.request_cancel(alice)
        seen["reserved"] = reserved
        seen["payer"] = _payer(database, request.run_id)
        seen["subscribers"] = _subscribers(database, request.run_id)
        seen["events"] = _events(database, request.run_id)
        seen["audit"] = _audit(database)
        # The run continues: its token did not fire, and the next paid request is admitted.
        seen["cancelled"] = request.cancel_token.is_set()
        journal = request.attempt_journal
        journal.resolved(_dispatch(request), "no_match")
        return _settle(request, tmp_path, "complete")

    driver = Worker(
        database,
        tmp_path / "work",
        local_mode=True,
        intake_resolver=_resolver(intake),
        service_runner=service,
    ).run_once()
    assert driver is not None and driver.id == alice
    run_id = driver.run_id
    assert run_id is not None

    assert seen["cancelled"] is False
    zoe_reservation = _reservation_of(database, zoe)
    assert seen["payer"] == ("zoe", zoe_reservation)
    subscribers = {row["user_id"]: row for row in seen["subscribers"]}
    assert subscribers["alice"]["reservation_state"] == "released"
    assert subscribers["alice"]["detached_at"] is not None
    assert subscribers["zoe"]["reservation_state"] == "held"
    assert subscribers["adam"]["reservation_state"] == "held"
    transfer = [event for event in seen["events"] if event["kind"] == "transfer"]
    assert len(transfer) == 1
    assert (transfer[0]["user_id"], transfer[0]["to_user_id"]) == ("alice", "zoe")
    assert transfer[0]["to_reservation_id"] == zoe_reservation
    # USD re-attribution: the run's whole reservation now counts against zoe's account...
    assert transfer[0]["usd_e6_reattributed"] == seen["reserved"]
    # ...and the part her month cap cannot cover is charged to the global pool and audited.
    assert transfer[0]["usd_e6_overage"] == seen["reserved"] - 10_000
    assert caps.asked == [("zoe", True)]
    (audit,) = seen["audit"]
    assert (audit["actor"], audit["action"], audit["target"]) == (
        "system",
        USD_REATTRIBUTION_OVERAGE,
        f"run:{run_id}",
    )
    assert json.loads(audit["detail"]) == {
        "run_id": run_id,
        "trigger": "payer_transfer",
        "from_user": "alice",
        "from_reservation": subscribers["alice"]["reservation_id"],
        "to_user": "zoe",
        "to_reservation": zoe_reservation,
        "usd_e6_reattributed": seen["reserved"],
        "usd_e6_overage": seen["reserved"] - 10_000,
        "charged_to": "global_pool",
    }

    # The run finished for the subscribers; alice, who detached, sees her request cancelled.
    run = _run_row(database, run_id)
    assert run["status"] == "complete"
    assert (run["payer_user"], run["payer_reservation_id"]) == ("zoe", zoe_reservation)
    assert _job_row(database, alice)["state"] == "cancelled"
    assert _job_row(database, zoe)["state"] == "complete"
    assert _job_row(database, adam)["state"] == "complete"
    # ONE settlement, through the existing authority, with both paid requests in it.
    settlement = _settlement(database, run_id)
    assert settlement is not None and settlement["status"] == "complete"
    assert settlement["usd_e6_spent"] == 2 * UNIT
    assert run["usd_e6_spent"] == 2 * UNIT
    assert _count(database, "SELECT COUNT(*) FROM run_dispatches WHERE run_id=?", run_id) == 2
    assert _kinds(database, run_id) == [
        ("attach", "alice", None),
        ("reserve", "alice", None),  # made before anyone else attached: the initiator's
        ("attach", "zoe", None),
        ("attach", "adam", None),
        ("detach", "alice", None),
        ("transfer", "alice", "zoe"),
        ("close", "zoe", None),
        ("release", "adam", None),
    ]


def test_a_transfer_within_the_new_payers_cap_is_not_audited(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue, (alice, bob), run_id = _subscribe(
        database, tmp_path, ["alice", "bob"], recipe=DEEP_RECIPE, local_mode=True
    )
    with database.write() as connection:  # the run's one reservation row, as admission writes it
        connection.execute(
            "INSERT INTO run_reservations(run_id, job_id, reservation, usd_e6_reserved, "
            "created_at) VALUES (?, ?, '{}', 52500, 1)",
            (run_id, alice),
        )
    caps = MonthCaps({"bob": 52_500})
    assert JobQueue(database, local_mode=True, usd_caps=caps).request_cancel(alice)
    (transfer,) = [event for event in _events(database, run_id) if event["kind"] == "transfer"]
    assert (transfer["usd_e6_reattributed"], transfer["usd_e6_overage"]) == (52_500, 0)
    assert _audit(database) == []
    assert _payer(database, run_id)[0] == "bob"
    assert _job_row(database, alice)["cancel_requested"] == 0


def test_a_hosted_free_run_keeps_running_for_its_subscriber_after_the_initiator_detaches(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path)
    queue = JobQueue(database)
    alice = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id="alice")
    bob = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id="bob")
    caps = MonthCaps({"bob": 0})
    attacher = Worker(
        Database(database.path),
        tmp_path / "work",
        intake_resolver=_resolver(intake),
        service_runner=_nothing_may_run,
    )
    seen: dict[str, Any] = {}

    def service(request) -> RunResult:
        assert attacher.run_once().id == bob
        assert JobQueue(Database(database.path), usd_caps=caps).request_cancel(alice)
        seen["cancelled"] = request.cancel_token.is_set()
        request.progress("recognise", 1, 2, "still running")  # would raise if cancelled
        return _complete(request)

    driver = Worker(
        database, tmp_path / "work", intake_resolver=_resolver(intake), service_runner=service
    ).run_once()
    assert driver is not None and driver.id == alice
    assert seen["cancelled"] is False
    assert _job_row(database, alice)["state"] == "cancelled"
    assert _job_row(database, bob)["state"] == "complete"
    (transfer,) = [e for e in _events(database, driver.run_id) if e["kind"] == "transfer"]
    assert (transfer["usd_e6_reattributed"], transfer["usd_e6_overage"]) == (0, 0)
    assert caps.asked == []  # a Free run re-attributes no USD, so no cap is consulted
    assert _audit(database) == []


def test_a_reclaimed_driver_whose_user_detached_still_finishes_for_the_subscribers(
    tmp_path: Path,
) -> None:
    now = [1_000.0]
    database = _database(tmp_path)
    intake = _intake(tmp_path)
    queue = JobQueue(database, clock=lambda: now[0])
    worker = Worker(database, tmp_path / "work", clock=lambda: now[0])
    ids = []
    for user in ("alice", "bob"):
        job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id=user)
        job = queue.claim(f"intake-{user}", lease_seconds=30)
        assert job is not None
        worker._commit_intake(job, intake)
        ids.append(job_id)
    alice, bob = ids
    assert queue.request_cancel(alice)
    now[0] += 31  # the driving worker died: its lease lapses
    ran: list[str] = []

    def service(request) -> RunResult:
        ran.append(request.run_id)
        return _complete(request)

    resumed = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(intake),
        service_runner=service,
        clock=lambda: now[0],
    ).run_once()
    assert resumed is not None and resumed.id == alice
    assert len(ran) == 1  # the detached initiator's job was NOT cancelled before it started
    assert _job_row(database, alice)["state"] == "cancelled"
    assert _job_row(database, bob)["state"] == "complete"
    assert _payer(database, ran[0])[0] == "bob"


# ------------------------------------------------------ gate 3: last detach → cancelled + settled


def test_the_last_detach_cancels_the_run_and_settles_its_in_flight_attempts(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path, DEEP_RECIPE)
    queue = JobQueue(database, local_mode=True)
    alice = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, user_id="alice")
    bob = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, user_id="bob")
    attacher = Worker(
        Database(database.path),
        tmp_path / "work",
        local_mode=True,
        intake_resolver=_resolver(intake),
        service_runner=_nothing_may_run,
    )
    seen: dict[str, Any] = {}

    def service(request) -> RunResult:
        journal = request.attempt_journal
        # Two requests are on the wire: one will answer, one never will (ambiguous).
        in_flight, lost = _reserve_and_dispatch(request, [None, None])
        assert attacher.run_once().id == bob
        seen["before"] = request.cancel_token.is_set()

        # Both users detach at the same moment, each through its own SQLite handle.
        together = threading.Barrier(2)
        errors: list[BaseException] = []

        def detach(job_id: str) -> None:
            try:
                handle = JobQueue(Database(database.path), local_mode=True)
                together.wait(BARRIER_SECONDS)
                assert handle.request_cancel(job_id)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=detach, args=(job,)) for job in (alice, bob)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        assert not errors
        seen["payer"] = _payer(database, request.run_id)
        seen["after"] = request.cancel_token.is_set()
        # No new paid request may leave once nobody is subscribed...
        with pytest.raises(DispatchRefused):
            _dispatch(request)
        # ...but the one already on the wire resolves and is settled as spent.
        journal.resolved(in_flight, "match")
        del lost
        return _settle(request, tmp_path, "cancelled")

    driver = Worker(
        database,
        tmp_path / "work",
        local_mode=True,
        intake_resolver=_resolver(intake),
        service_runner=service,
    ).run_once()
    assert driver is not None and driver.id == alice
    run_id = driver.run_id
    assert run_id is not None
    assert seen["before"] is False and seen["after"] is True
    # Never a run without a payer while attempts were in flight.
    assert seen["payer"][0] in {"alice", "bob"} and seen["payer"][1] is not None

    run = _run_row(database, run_id)
    assert run["status"] == "cancelled"
    assert _job_row(database, alice)["state"] == "cancelled"
    assert _job_row(database, bob)["state"] == "cancelled"
    # The in-flight answer and the unanswered request are both spent, settled exactly once.
    settlement = _settlement(database, run_id)
    assert settlement is not None and settlement["status"] == "cancelled"
    assert settlement["usd_e6_spent"] == 2 * UNIT
    assert run["usd_e6_spent"] == 2 * UNIT
    assert _count(database, "SELECT COUNT(*) FROM run_settlements WHERE run_id=?", run_id) == 1
    # The refused third request never left.
    assert _count(database, "SELECT COUNT(*) FROM run_dispatches WHERE run_id=?", run_id) == 2
    # The payer at cancellation settles; everyone else's reservation was released.
    subscribers = _subscribers(database, run_id)
    settled = [row for row in subscribers if row["reservation_state"] == "settled"]
    assert [row["reservation_id"] for row in settled] == [run["payer_reservation_id"]]
    assert all(row["detached_at"] is not None for row in subscribers)
    transfers = [event for event in _events(database, run_id) if event["kind"] == "transfer"]
    # Either order is valid: alice first hands the payer to bob; bob first leaves alice paying.
    assert [(e["user_id"], e["to_user_id"]) for e in transfers] in ([], [("alice", "bob")])
    closes = [event for event in _events(database, run_id) if event["kind"] == "close"]
    assert [event["status"] for event in closes] == ["cancelled"]


def test_a_non_payer_detaching_releases_only_its_own_reservation(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue, (alice, bob, carol), run_id = _subscribe(database, tmp_path, ["alice", "bob", "carol"])
    assert queue.request_cancel(bob)
    assert _job_row(database, bob)["state"] == "cancelled"
    assert _payer(database, run_id)[0] == "alice"
    assert [row["reservation_state"] for row in _subscribers(database, run_id)] == [
        "held",
        "released",
        "held",
    ]
    assert _job_row(database, alice)["cancel_requested"] == 0
    assert _kinds(database, run_id)[-2:] == [("detach", "bob", None), ("release", "bob", None)]
    # Detaching again changes nothing.
    assert queue.request_cancel(bob) is False
    assert len(_events(database, run_id)) == 5
    assert carol


# ------------------------------------------------------------- races: exactly-once payer transfer


def _race(database: Database, job_ids: list[str]) -> list[bool]:
    together = threading.Barrier(len(job_ids))
    results: list[bool] = []
    errors: list[BaseException] = []

    def detach(job_id: str) -> None:
        try:
            handle = JobQueue(Database(database.path))
            together.wait(BARRIER_SECONDS)
            results.append(handle.request_cancel(job_id))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=detach, args=(job_id,)) for job_id in job_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert not errors
    return results


def _assert_transfer_chain(database: Database, run_id: str, final_payer: str) -> None:
    """Replay the event log: every transfer leaves a live payer and targets a live subscriber."""

    detached: set[str] = set()
    payer = None
    for event in _events(database, run_id):
        if event["kind"] == "attach" and payer is None:
            payer = event["reservation_id"]
        elif event["kind"] == "detach":
            assert event["reservation_id"] not in detached, "detached twice"
            detached.add(event["reservation_id"])
        elif event["kind"] == "transfer":
            assert event["reservation_id"] == payer, "a transfer from someone who was not paying"
            assert event["to_reservation_id"] not in detached, "a transfer to a detached subscriber"
            payer = event["to_reservation_id"]
    user, reservation = _payer(database, run_id)
    assert reservation == payer
    assert user == final_payer


@pytest.mark.parametrize("round_", range(4))
def test_two_concurrent_detaches_of_the_initiator_transfer_the_payer_once(
    tmp_path: Path, round_: int
) -> None:
    database = _database(tmp_path / str(round_))
    queue, (alice, _bob, _carol), run_id = _subscribe(
        database, tmp_path / str(round_), ["alice", "bob", "carol"]
    )
    assert _race(database, [alice, alice]) == [True, True]
    kinds = [event["kind"] for event in _events(database, run_id)]
    assert kinds.count("detach") == 1
    assert kinds.count("transfer") == 1
    _assert_transfer_chain(database, run_id, "bob")
    assert _job_row(database, alice)["cancel_requested"] == 0
    assert queue.get(alice).state == "analysis"


@pytest.mark.parametrize("round_", range(4))
def test_the_payer_and_its_successor_detaching_together_never_orphan_the_run(
    tmp_path: Path, round_: int
) -> None:
    database = _database(tmp_path / str(round_))
    _queue, (alice, bob, _carol), run_id = _subscribe(
        database, tmp_path / str(round_), ["alice", "bob", "carol"]
    )
    assert _race(database, [alice, bob]) == [True, True]
    _assert_transfer_chain(database, run_id, "carol")
    states = {row["user_id"]: row["reservation_state"] for row in _subscribers(database, run_id)}
    assert states == {"alice": "released", "bob": "released", "carol": "held"}
    assert _job_row(database, alice)["cancel_requested"] == 0  # carol is still subscribed


def _detach_in_child(database_path: str, job_id: str, barrier, results) -> None:
    barrier.wait(BARRIER_SECONDS)
    results.put(JobQueue(Database(Path(database_path))).request_cancel(job_id))


def test_two_processes_detaching_the_initiator_transfer_the_payer_once(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _queue, (alice, _bob), run_id = _subscribe(database, tmp_path, ["alice", "bob"])
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    children = [
        context.Process(target=_detach_in_child, args=(str(database.path), alice, barrier, results))
        for _ in range(2)
    ]
    for child in children:
        child.start()
    for child in children:
        child.join(180)
    assert [child.exitcode for child in children] == [0, 0]
    assert sorted(results.get(timeout=10) for _ in children) == [True, True]
    kinds = [event["kind"] for event in _events(database, run_id)]
    assert (kinds.count("detach"), kinds.count("transfer")) == (1, 1)
    _assert_transfer_chain(database, run_id, "bob")


def test_the_event_log_itself_refuses_a_second_transfer_from_one_reservation(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    _queue, (alice, _bob, _carol), run_id = _subscribe(
        database, tmp_path, ["alice", "bob", "carol"]
    )
    reservation = _reservation_of(database, alice)
    others = [row["reservation_id"] for row in _subscribers(database, run_id)][1:]
    with database.write() as connection:
        connection.execute(
            "INSERT INTO run_payer_events(run_id, kind, reservation_id, to_reservation_id, at) "
            "VALUES (?, 'transfer', ?, ?, 1)",
            (run_id, reservation, others[0]),
        )
    with pytest.raises(sqlite3.IntegrityError), database.write() as connection:
        connection.execute(
            "INSERT INTO run_payer_events(run_id, kind, reservation_id, to_reservation_id, at) "
            "VALUES (?, 'transfer', ?, ?, 2)",
            (run_id, reservation, others[1]),
        )
    for statement in ("UPDATE run_payer_events SET at=3", "DELETE FROM run_payer_events"):
        with pytest.raises(sqlite3.IntegrityError), database.write() as connection:
            connection.execute(statement)
    with pytest.raises(sqlite3.IntegrityError), database.write() as connection:
        connection.execute("DELETE FROM run_subscribers")
    with database.write() as connection:
        connection.execute(
            "INSERT INTO admin_audit(actor, action, target, at) VALUES ('a', 'b', 'c', 1)"
        )
    for statement in ("UPDATE admin_audit SET at=3", "DELETE FROM admin_audit"):
        with pytest.raises(sqlite3.IntegrityError), database.write() as connection:
            connection.execute(statement)


# ------------------------------------------------------ gate 4: private scope never coalesces


def test_private_scope_never_coalesces(tmp_path: Path) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path, scope="user:alice")
    queue = JobQueue(database)
    worker = Worker(database, tmp_path / "work")
    ids = []
    for _ in range(2):  # the same private request twice, from the same user
        job_id = queue.enqueue(
            PlatformUrl(MIX), FREE_RECIPE, user_id="alice", tenant_scope="user:alice"
        )
        job = queue.claim("intake", lease_seconds=600)
        assert job is not None and job.id == job_id
        assert worker._commit_intake(job, intake) == "analysis"
        ids.append(job_id)
    runs = {_job_row(database, job_id)["run_id"] for job_id in ids}
    assert len(runs) == 2
    assert {_job_row(database, job_id)["attached"] for job_id in ids} == {0}
    for run_id in runs:
        assert _run_row(database, run_id)["analysis_key"] == intake.analysis_key
        assert [row["user_id"] for row in _subscribers(database, run_id)] == ["alice"]

    # A public request for the same media gets its own public run, never the private one.
    public = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id="bob")
    job = queue.claim("intake", lease_seconds=600)
    assert job is not None and job.id == public
    assert worker._commit_intake(job, _intake(tmp_path)) == "analysis"
    assert _job_row(database, public)["run_id"] not in runs

    # And the database itself refuses a second subscriber on a private run.
    run_id = next(iter(runs))
    refused = pytest.raises(sqlite3.IntegrityError, match="private run never coalesces")
    with refused, database.write() as connection:
        connection.execute(
            "INSERT INTO run_subscribers(reservation_id, run_id, job_id, user_id, recipe_id, "
            "minutes_reserved, attached_at) VALUES ('forged', ?, ?, 'bob', ?, 1, 1)",
            (run_id, public, FREE_RECIPE.recipe_id),
        )


# ---------------------------------------------------------------- tiers and the hosted refusal


def _forge_run(database: Database, intake: PreparedIntake, run_id: str, recipe) -> str:
    """A live run for ``intake``'s key under ``recipe``, driven by a claimed job."""

    queue = JobQueue(database, local_mode=True)
    job_id = queue.enqueue(PlatformUrl(MIX), recipe, run_id=run_id, user_id="owner")
    job = queue.claim("forger", lease_seconds=600)
    assert job is not None and job.id == job_id
    with database.write() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO media(media_key, duration_ms, first_seen) VALUES (?, ?, 1)",
            (intake.inputs.media_key, intake.duration_ms),
        )
        connection.execute(
            "INSERT INTO analysis_runs(run_id, analysis_key, media_key, requested_recipe_id, "
            "algorithm_version, adapter_versions, status, tenant_scope, started_at, "
            "analysis_inputs, non_recipe_key, claim_token, payer_user, payer_reservation_id) "
            "VALUES (?, ?, ?, ?, ?, '{}', 'analysis', 'public', 1, '{}', ?, ?, 'owner', ?)",
            (
                run_id,
                intake.analysis_key,
                intake.inputs.media_key,
                recipe.recipe_id,
                recipe.algorithm_version,
                intake.inputs.non_recipe_key,
                job.token,
                f"res-{run_id}",
            ),
        )
        connection.execute(
            "INSERT INTO run_subscribers(reservation_id, run_id, job_id, user_id, recipe_id, "
            "minutes_reserved, attached_at) VALUES (?, ?, ?, 'owner', ?, 1, 1)",
            (f"res-{run_id}", run_id, job_id, recipe.recipe_id),
        )
    return job_id


def test_a_free_request_never_attaches_to_a_paid_run_even_under_the_same_key(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path)  # a FREE key...
    _forge_run(database, intake, "paid-run", DEEP_RECIPE)  # ...on which a paid run is live
    queue = JobQueue(database)
    job_id = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id="bob")
    job = queue.claim("intake", lease_seconds=600)
    assert job is not None and job.id == job_id
    assert Worker(database, tmp_path / "work")._commit_intake(job, intake) == "analysis"
    assert _job_row(database, job_id)["run_id"] != "paid-run"
    assert [row["user_id"] for row in _subscribers(database, "paid-run")] == ["owner"]
    # The database refuses the cross-tier subscriber outright.
    refused = pytest.raises(sqlite3.IntegrityError, match="recipe differs")
    with refused, database.write() as connection:
        connection.execute(
            "INSERT INTO run_subscribers(reservation_id, run_id, job_id, user_id, recipe_id, "
            "minutes_reserved, attached_at) VALUES ('forged', 'paid-run', ?, 'bob', ?, 1, 1)",
            (job_id, FREE_RECIPE.recipe_id),
        )


def test_coalescing_never_admits_a_hosted_paid_request(tmp_path: Path) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path, DEEP_RECIPE)
    _forge_run(database, intake, "paid-run", DEEP_RECIPE)
    queue = JobQueue(database)  # hosted
    job_id = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, user_id="bob")

    # The intake transaction itself refuses it, even though a run for its key is live...
    job = queue.get(job_id)
    with database.write() as connection:
        connection.execute(
            "UPDATE jobs SET claim_token='t', lease_owner='w', lease_until=9e12 WHERE id=?",
            (job_id,),
        )
    job = queue.get(job_id)
    with pytest.raises(DispatchRefused, match=HOSTED_PAID_REFUSAL):
        Worker(database, tmp_path / "work")._commit_intake(job, intake)
    assert _job_row(database, job_id)["attached"] == 0
    assert [row["user_id"] for row in _subscribers(database, "paid-run")] == ["owner"]
    with database.write() as connection:
        connection.execute(
            "UPDATE jobs SET claim_token=NULL, lease_owner=NULL, lease_until=NULL WHERE id=?",
            (job_id,),
        )

    # ...and a hosted worker dead-letters it before intake, never touching the paid run.
    result = Worker(
        database,
        tmp_path / "work",
        intake_resolver=_resolver(intake),
        service_runner=_nothing_may_run,
    ).run_once()
    assert result is not None and result.id == job_id and result.state == "dead_letter"
    assert HOSTED_PAID_REFUSAL in (result.dead_letter_reason or "")
    assert _count(database, "SELECT COUNT(*) FROM run_subscribers") == 1
    assert _run_row(database, "paid-run")["status"] == "analysis"


def test_a_run_whose_last_subscriber_left_takes_no_new_subscribers(tmp_path: Path) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path)
    queue, (alice,), run_id = _subscribe(database, tmp_path, ["alice"], intake=intake)
    assert queue.request_cancel(alice)
    assert _job_row(database, alice)["cancel_requested"] == 1  # the token fires
    bob = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id="bob")
    job = queue.claim("intake-bob", lease_seconds=600)
    assert job is not None and job.id == bob
    assert Worker(database, tmp_path / "work")._commit_intake(job, intake) == "analysis"
    assert _job_row(database, bob)["run_id"] != run_id


# --------------------------------------------------------------------------------- cancel/drain


def test_a_draining_worker_finishes_its_run_and_its_subscribers_get_the_result(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path)
    queue = JobQueue(database)
    alice = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id="alice")
    bob = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id="bob")
    carol = queue.enqueue(
        PlatformUrl("https://soundcloud.com/example/other"), FREE_RECIPE, user_id="carol"
    )
    attacher = Worker(
        Database(database.path),
        tmp_path / "work",
        intake_resolver=_resolver(intake),
        service_runner=_nothing_may_run,
    )
    worker = Worker(database, tmp_path / "work", intake_resolver=_resolver(intake))

    def service(request) -> RunResult:
        assert attacher.run_once().id == bob
        worker.drain()  # mid-run: the current job keeps its claim and finishes
        request.progress("recognise", 1, 2, "draining")
        return _complete(request)

    worker.service_runner = service
    finished = worker.run_once()
    assert finished is not None and finished.id == alice and finished.state == "complete"
    # No reconciliation pass will run on a draining worker: the result reached bob in the same
    # transaction that finished the run.
    assert _job_row(database, bob)["state"] == "complete"
    assert worker.run_once() is None
    assert _job_row(database, carol)["state"] == "intake"


# ------------------------------------------------------------------------------------ migration


def test_migration_0005_goes_down_only_while_its_tables_are_empty(tmp_path: Path) -> None:
    database = _database(tmp_path)
    assert database.version() == 5
    assert database.migrate(4) == 4
    with database.read() as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    assert not names & {"run_subscribers", "run_payer_events", "admin_audit"}
    assert "user_id" not in columns
    assert database.migrate() == 5

    _subscribe(database, tmp_path, ["alice"])
    with pytest.raises(sqlite3.DatabaseError, match="subscriber and payer tables hold rows"):
        database.migrate(4)
    assert database.version() == 5
    assert _count(database, "SELECT COUNT(*) FROM run_subscribers") == 1
    assert _count(database, "SELECT COUNT(*) FROM run_payer_events") == 1


# ----------------------------------------------------------------- fix pass (review FIX_FIRST)


def _detach_in_thread(database: Database, job_ids: list[str], caps: Any = None) -> list[bool]:
    """Detach ``job_ids`` in order on another thread and handle; return once they committed."""

    together = threading.Barrier(2)
    results: list[bool] = []
    errors: list[BaseException] = []

    def detach() -> None:
        try:
            handle = JobQueue(Database(database.path), local_mode=True, usd_caps=caps)
            together.wait(BARRIER_SECONDS)
            results.extend(handle.request_cancel(job_id) for job_id in job_ids)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    thread = threading.Thread(target=detach)
    thread.start()
    together.wait(BARRIER_SECONDS)
    thread.join(120)
    assert not errors and not thread.is_alive()
    return results


def test_a_reservation_made_after_the_transfer_is_attributed_to_the_new_payer_and_cap_checked(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path, DEEP_RECIPE)
    queue = JobQueue(database, local_mode=True)
    alice = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, user_id="alice")
    bob = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, user_id="bob")
    caps = MonthCaps({"bob": 10_000})
    attacher = Worker(
        Database(database.path),
        tmp_path / "work",
        local_mode=True,
        intake_resolver=_resolver(intake),
        service_runner=_nothing_may_run,
    )
    seen: dict[str, Any] = {}

    def service(request) -> RunResult:
        # Ordered: alice starts, bob attaches, alice detaches, and only THEN is money reserved.
        assert attacher.run_once().id == bob
        assert _detach_in_thread(database, [alice], caps) == [True]
        events = _events(database, request.run_id)
        seen["transfer"] = [event for event in events if event["kind"] == "transfer"]
        seen["asked_before"] = list(caps.asked)
        _reserve_and_dispatch(request, ["match"])
        return _settle(request, tmp_path, "complete")

    driver = Worker(
        database,
        tmp_path / "work",
        local_mode=True,
        intake_resolver=_resolver(intake),
        service_runner=service,
        usd_caps=caps,
    ).run_once()
    assert driver is not None and driver.id == alice
    run_id = driver.run_id
    reserved = RESERVATION.usd_e6_reserved
    bob_reservation = _reservation_of(database, bob)

    # The transfer happened while nothing was reserved: it moved nothing and checked no cap...
    (transfer,) = seen["transfer"]
    assert (transfer["usd_e6_reattributed"], transfer["usd_e6_overage"]) == (0, 0)
    assert seen["asked_before"] == []
    # ...and the reservation, made afterwards, belongs to bob and was checked against HIS cap.
    (reserve,) = [event for event in _events(database, run_id) if event["kind"] == "reserve"]
    assert (reserve["reservation_id"], reserve["user_id"]) == (bob_reservation, "bob")
    assert (reserve["usd_e6_reattributed"], reserve["usd_e6_overage"]) == (
        reserved,
        reserved - 10_000,
    )
    assert caps.asked == [("bob", True)]
    (audit,) = _audit(database)
    detail = json.loads(audit["detail"])
    assert audit["action"] == USD_REATTRIBUTION_OVERAGE
    assert (detail["trigger"], detail["from_user"], detail["to_user"]) == (
        "reservation_after_transfer",
        "alice",
        "bob",
    )
    assert detail["usd_e6_overage"] == reserved - 10_000
    assert _payer(database, run_id) == ("bob", bob_reservation)
    assert _job_row(database, bob)["state"] == "complete"
    assert _job_row(database, alice)["state"] == "cancelled"


def test_a_last_detach_after_the_pipeline_settled_complete_still_ends_the_run_cancelled(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path, DEEP_RECIPE)
    queue = JobQueue(database, local_mode=True)
    alice = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, user_id="alice")
    bob = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, user_id="bob")
    attacher = Worker(
        Database(database.path),
        tmp_path / "work",
        local_mode=True,
        intake_resolver=_resolver(intake),
        service_runner=_nothing_may_run,
    )

    def service(request) -> RunResult:
        assert attacher.run_once().id == bob
        _reserve_and_dispatch(request, ["match"])
        # Ordered: the pipeline settles `complete`, THEN the last subscriber leaves, THEN
        # terminal() records the result the pipeline returned.
        finished = _settle(request, tmp_path, "complete")
        assert _settlement(database, request.run_id)["status"] == "complete"
        assert _detach_in_thread(database, [alice, bob]) == [True, True]
        return finished

    driver = Worker(
        database,
        tmp_path / "work",
        local_mode=True,
        intake_resolver=_resolver(intake),
        service_runner=service,
    ).run_once()
    assert driver is not None and driver.id == alice
    run_id = driver.run_id
    run = _run_row(database, run_id)
    assert (run["status"], run["reason"], run["achieved"]) == (
        "cancelled",
        ALL_SUBSCRIBERS_DETACHED,
        None,
    )
    assert run["usd_e6_spent"] == UNIT  # what was dispatched stays spent
    settlement = _settlement(database, run_id)
    assert settlement is not None
    assert (settlement["status"], settlement["exit_code"]) == ("cancelled", 130)
    assert settlement["usd_e6_spent"] == UNIT
    assert _count(database, "SELECT COUNT(*) FROM run_settlements WHERE run_id=?", run_id) == 1
    # The projection follows the row.
    lines = [
        json.loads(line)
        for line in (tmp_path / "invocations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [line["status"] for line in lines if line["invocation_id"] == run_id] == ["cancelled"]
    # The payer's reservation closes under `cancelled` (0 % credits under §3.5).
    closes = [event for event in _events(database, run_id) if event["kind"] == "close"]
    assert [event["status"] for event in closes] == ["cancelled"]
    assert _job_row(database, alice)["state"] == "cancelled"
    assert _job_row(database, bob)["state"] == "cancelled"


def _publish_result_bundle(request, intake: PreparedIntake) -> tuple[str, dict[str, Any]]:
    """A durable Deep result bundle for ``request``'s run, exactly as a pipeline publishes one."""

    identifier = bundle_id(request.run_id, 1)
    directory = intake.media_dir / "present" / "bundles" / identifier
    directory.mkdir(parents=True, exist_ok=True)
    names = ("index.html", "tracklist.json", "source.json")
    for name in names:
        (directory / name).write_text("{}", encoding="utf-8")
    compatibility = {
        **CompatibilityRequest(intake.inputs, DEEP_RECIPE, local=True).metadata(),
        "analysis_key": intake.analysis_key,
    }
    manifest = {
        "run_id": request.run_id,
        "presentation_version": 1,
        "status": "complete",
        "achieved": "deep",
        "duration_ms": intake.duration_ms,
        "fuse_run": f"fuse/runs/{request.run_id}",
        "compatibility": compatibility,
        "files": {
            name: {
                "sha256": sha256_file(directory / name),
                "size": (directory / name).stat().st_size,
            }
            for name in names
        },
    }
    atomic_write_json(directory / "manifest.json", manifest)
    return identifier, compatibility


def _settle_like_the_pipeline(request, tmp_path: Path, intake: PreparedIntake) -> RunResult:
    """A successful Deep settlement with every outcome field the real pipeline writes."""

    identifier, compatibility = _publish_result_bundle(request, intake)
    journal = request.attempt_journal
    money = recovered_money(
        fold_run_ledger(request.run_id, journal.durable_events()), journal.durable_reservation()
    )
    entry = interrupted_entry(request.run_id, MIX, "complete", money, []).model_copy(
        update={
            "status": "complete",
            "exit_code": 0,
            "reason": None,
            "achieved": "deep",
            "bundle_id": identifier,
            "fuse_run": f"fuse/runs/{request.run_id}",
            "analysis_key": intake.analysis_key,
            "compatibility": compatibility,
        }
    )
    request.checkpoint_store.settlement_writer(request.run_id)(
        tmp_path / "invocations.jsonl", entry
    )
    return RunResult(
        request.run_id,
        "complete",
        None,
        "deep",
        identifier,
        money.usd_e6_reserved,
        money.usd_e6_spent,
        money.attempts,
    )


def _journal_lines(tmp_path: Path, run_id: str) -> list[dict[str, Any]]:
    path = tmp_path / "invocations.jsonl"
    return [
        line
        for line in (json.loads(text) for text in path.read_text(encoding="utf-8").splitlines())
        if line["invocation_id"] == run_id
    ]


def _last_detach_over_a_pipeline_success(tmp_path: Path, database: Database) -> tuple[str, Any]:
    """alice drives a Deep run, bob attaches; the pipeline publishes and settles a real success;
    then both detach; then terminal() records the result. Returns (run id, the intake)."""

    intake = _intake(tmp_path, DEEP_RECIPE)
    queue = JobQueue(database, local_mode=True)
    alice = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, user_id="alice")
    bob = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, user_id="bob")
    attacher = Worker(
        Database(database.path),
        tmp_path / "work",
        local_mode=True,
        intake_resolver=_resolver(intake),
        service_runner=_nothing_may_run,
    )

    def service(request) -> RunResult:
        assert attacher.run_once().id == bob
        _reserve_and_dispatch(request, ["match"])
        finished = _settle_like_the_pipeline(request, tmp_path, intake)
        assert _journal_lines(tmp_path, request.run_id)[0]["achieved"] == "deep"
        assert _detach_in_thread(database, [alice, bob]) == [True, True]
        return finished

    driver = Worker(
        database,
        tmp_path / "work",
        local_mode=True,
        intake_resolver=_resolver(intake),
        service_runner=service,
    ).run_once()
    assert driver is not None and driver.id == alice
    return driver.run_id, intake


def test_a_cancelled_run_keeps_no_trace_of_the_success_it_overrode(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run_id, intake = _last_detach_over_a_pipeline_success(tmp_path, database)

    settlement = _settlement(database, run_id)
    assert settlement is not None
    outcome = {
        name: settlement[name]
        for name in (
            "status",
            "exit_code",
            "reason",
            "achieved",
            "bundle_id",
            "fuse_run",
            "compatibility",
        )
    }
    assert outcome == {
        "status": "cancelled",
        "exit_code": 130,
        "reason": ALL_SUBSCRIBERS_DETACHED,
        "achieved": None,
        "bundle_id": None,
        "fuse_run": None,
        "compatibility": None,
    }
    assert settlement["usd_e6_spent"] == UNIT  # the dispatched request stays spent
    (line,) = _journal_lines(tmp_path, run_id)
    assert {name: line[name] for name in outcome} == outcome
    assert _count(database, "SELECT COUNT(*) FROM result_bundles WHERE run_id=?", run_id) == 0

    # A cancelled run is never a compatible result, even if a bundle row names it.
    worker = Worker(database, tmp_path / "work", local_mode=True)
    identifier = bundle_id(run_id, 1)
    directory = intake.media_dir / "present" / "bundles" / identifier
    with database.write() as connection:
        connection.execute(
            "INSERT INTO result_bundles VALUES (?, ?, 1, ?, ?, 1)",
            (identifier, run_id, str(directory), sha256_file(directory / "manifest.json")),
        )
    with database.read() as connection:
        assert worker._compatible_bundle(connection, intake, DEEP_RECIPE) is None
    newcomer = JobQueue(database, local_mode=True).enqueue(
        PlatformUrl(MIX), DEEP_RECIPE, user_id="carol"
    )
    job = worker.queue.claim("intake-carol", lease_seconds=600)
    assert job is not None and job.id == newcomer
    assert worker._commit_intake(job, intake) == "analysis"  # a new run, not a cache hit
    # Control: the same bundle IS servable once its run's own outcome is a result.
    with database.write() as connection:
        connection.execute("UPDATE analysis_runs SET status='complete' WHERE run_id=?", (run_id,))
    with database.read() as connection:
        assert worker._compatible_bundle(connection, intake, DEEP_RECIPE) is not None


def test_a_failed_reprojection_after_the_cancel_is_repaired_on_the_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from idea_web.jobs import worker as worker_module

    real = worker_module._project_settlement
    failures: list[str] = []

    def fail_once(path: Path, entry: Any) -> None:
        if entry.status == "cancelled" and not failures:
            failures.append(entry.invocation_id)
            raise OSError("disk went away after the terminal commit")
        real(path, entry)

    monkeypatch.setattr(worker_module, "_project_settlement", fail_once)
    database = _database(tmp_path)
    run_id, _intake_ = _last_detach_over_a_pipeline_success(tmp_path, database)
    assert failures == [run_id]
    # SQLite says cancelled; the journal still says complete.
    assert _settlement(database, run_id)["status"] == "cancelled"
    (stale,) = _journal_lines(tmp_path, run_id)
    assert (stale["status"], stale["achieved"]) == ("complete", "deep")

    # A worker restarts: its first pass repairs the journal from the row, with nothing to claim.
    restarted = Worker(Database(database.path), tmp_path / "work", local_mode=True)
    assert restarted.run_once() is None
    (line,) = _journal_lines(tmp_path, run_id)
    row = _settlement(database, run_id)
    assert (line["status"], line["achieved"], line["bundle_id"]) == ("cancelled", None, None)
    assert {name: line[name] for name in line} == {
        name: row[name] for name in line
    }  # the projection is exactly the row
    assert restarted.reproject_cancelled_settlements() == 0  # verified once, nothing left


def _child_drive_until_dispatched(database_path: str, work_root: str, tmp: str, ready) -> None:
    database = Database(Path(database_path))
    intake = _intake(Path(tmp), DEEP_RECIPE)

    def service(request) -> RunResult:
        _reserve_and_dispatch(request, [None])  # one paid request on the wire, never answered
        ready.set()
        threading.Event().wait()  # killed here, before anything settles
        raise AssertionError("unreachable")

    Worker(
        database,
        Path(work_root),
        worker_id="doomed-driver",
        local_mode=True,
        intake_resolver=_resolver(intake),
        service_runner=service,
        heartbeat_seconds=0.1,
        lease_seconds=2.0,
    ).run_once()


def test_a_driver_killed_after_the_last_detach_is_settled_once_by_its_replacement(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    work = tmp_path / "work"
    intake = _intake(tmp_path, DEEP_RECIPE)
    queue = JobQueue(database, local_mode=True)
    alice = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, user_id="alice")
    bob = queue.enqueue(PlatformUrl(MIX), DEEP_RECIPE, user_id="bob")
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    child = context.Process(
        target=_child_drive_until_dispatched,
        args=(str(database.path), str(work), str(tmp_path), ready),
    )
    child.start()
    try:
        assert ready.wait(180), "the driver never dispatched"
        attached = Worker(
            Database(database.path),
            work,
            local_mode=True,
            intake_resolver=_resolver(intake),
            service_runner=_nothing_may_run,
        ).run_once()
        assert attached is not None and attached.id == bob
        # The last detach commits while the driver is alive; then the driver dies unsettled.
        assert queue.request_cancel(alice)
        assert queue.request_cancel(bob)
        assert _job_row(database, alice)["cancel_requested"] == 1
    finally:
        child.terminate()
        child.join(30)
    assert child.exitcode is not None
    run_id = _job_row(database, alice)["run_id"]
    assert _count(database, "SELECT COUNT(*) FROM run_settlements") == 0

    # The replacement sees the dead driver's lease as expired (its clock is a minute ahead).
    replacement = Worker(
        database,
        work,
        worker_id="replacement",
        local_mode=True,
        service_runner=_nothing_may_run,
        clock=lambda: time.time() + 60,
    )
    reclaimed = replacement.run_once()
    assert reclaimed is not None and reclaimed.id == alice and reclaimed.state == "cancelled"
    assert replacement.run_once() is None

    settlement = _settlement(database, run_id)
    assert settlement is not None and settlement["status"] == "cancelled"
    assert settlement["usd_e6_spent"] == UNIT  # the unanswered request is spent, never resent
    assert settlement["usd_e6_reserved"] == RESERVATION.usd_e6_reserved
    assert _count(database, "SELECT COUNT(*) FROM run_settlements") == 1
    assert _count(database, "SELECT COUNT(*) FROM run_dispatches WHERE run_id=?", run_id) == 1
    run = _run_row(database, run_id)
    assert (run["status"], run["usd_e6_spent"]) == ("cancelled", UNIT)
    assert _job_row(database, bob)["state"] == "cancelled"
    closes = [event for event in _events(database, run_id) if event["kind"] == "close"]
    assert [event["status"] for event in closes] == ["cancelled"]


def test_a_dead_letter_during_drain_gives_its_subscribers_the_result(tmp_path: Path) -> None:
    database = _database(tmp_path)
    intake = _intake(tmp_path)
    queue = JobQueue(database)
    alice = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id="alice")
    bob = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id="bob")
    with database.write() as connection:  # two failed attempts already: this one is the last
        connection.execute("UPDATE jobs SET attempt=2 WHERE id=?", (alice,))
    attacher = Worker(
        Database(database.path),
        tmp_path / "work",
        intake_resolver=_resolver(intake),
        service_runner=_nothing_may_run,
    )
    worker = Worker(database, tmp_path / "work", intake_resolver=_resolver(intake))

    def service(request) -> RunResult:
        assert attacher.run_once().id == bob
        worker.drain()  # no reconciliation pass will run on this worker again
        raise RuntimeError("deterministic failure")

    worker.service_runner = service
    dead = worker.run_once()
    assert dead is not None and dead.id == alice and dead.state == "dead_letter"
    assert _run_row(database, dead.run_id)["status"] == "dead_letter"
    assert _job_row(database, bob)["state"] == "dead_letter"
    assert worker.run_once() is None


def test_two_private_submissions_racing_for_one_key_never_coalesce(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = JobQueue(database)
    intake = _intake(tmp_path, scope="user:alice")
    ids = [
        queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id="alice", tenant_scope="user:alice")
        for _ in range(2)
    ]
    committed = threading.Barrier(2)
    running = threading.Barrier(2)
    results: list[Any] = []
    errors: list[BaseException] = []

    def resolve(_job, _store) -> PreparedIntake:
        committed.wait(BARRIER_SECONDS)  # both claimed and prepared before either commits
        return intake

    def service(request) -> RunResult:
        running.wait(30)  # both are driving their OWN run at the same time
        return _complete(request)

    def consume(name: str) -> None:
        try:
            worker = Worker(
                Database(database.path),
                tmp_path / "work",
                worker_id=name,
                intake_resolver=resolve,
                service_runner=service,
            )
            results.append(worker.run_once())
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=consume, args=(f"private-{n}",)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert not errors
    assert sorted(result.id for result in results) == sorted(ids)
    assert {_job_row(database, job_id)["state"] for job_id in ids} == {"complete"}
    assert {_job_row(database, job_id)["attached"] for job_id in ids} == {0}
    runs = {_job_row(database, job_id)["run_id"] for job_id in ids}
    assert len(runs) == 2
    for run_id in runs:
        assert _run_row(database, run_id)["tenant_scope"] == "user:alice"
        assert [row["user_id"] for row in _subscribers(database, run_id)] == ["alice"]
