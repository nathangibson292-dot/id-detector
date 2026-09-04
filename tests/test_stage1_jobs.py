from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from id_detector.jobs import AsyncJobStore, BudgetExhausted, JobStoreLocked
from id_detector.submission import execute_durable_submission

MEDIA_KEY = "a" * 64
QUERY_ID = "b" * 40


def test_lease_reclaim_and_submission_started_recovery(tmp_path: Path) -> None:
    async def prepare() -> tuple[str, str]:
        async with AsyncJobStore(tmp_path / "jobs.sqlite", lease_seconds=1) as store:
            await store.ensure_budget(MEDIA_KEY, "shazam", max_requests=10)
            first = await store.ensure_job(MEDIA_KEY, QUERY_ID, "shazam")
            leased = await store.lease_next("dead-owner")
            assert leased and leased.id == first.id
            second = await store.ensure_job(MEDIA_KEY, "c" * 40, "shazam")
            leased_second = await store.lease_next("dead-owner")
            assert leased_second and leased_second.id == second.id
            await store.submission_started(second.id, "dead-owner")
            return first.id, second.id

    first_id, second_id = asyncio.run(prepare())
    with sqlite3.connect(tmp_path / "jobs.sqlite") as connection:
        connection.execute(
            "UPDATE jobs SET heartbeat_at='2000-01-01T00:00:00Z' WHERE id IN (?, ?)",
            (first_id, second_id),
        )
        connection.commit()

    async def recover() -> tuple[str, str]:
        async with AsyncJobStore(tmp_path / "jobs.sqlite", lease_seconds=1) as store:
            first = await store.get_job(first_id)
            second = await store.get_job(second_id)
            assert first and second
            return first.state, second.state

    assert asyncio.run(recover()) == ("pending", "outcome_unknown")


def test_live_store_is_locked_and_fresh_submission_is_not_recovered(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "jobs.sqlite"
        first = await AsyncJobStore(database, lease_seconds=60).start()
        try:
            await first.ensure_budget(MEDIA_KEY, "shazam", max_requests=10)
            job = await first.ensure_job(MEDIA_KEY, QUERY_ID, "shazam")
            leased = await first.lease_next("live-owner")
            assert leased and leased.id == job.id
            await first.submission_started(job.id, "live-owner")
            with pytest.raises(JobStoreLocked):
                await AsyncJobStore(database, lease_seconds=60).start()
        finally:
            await first.close()

        async with AsyncJobStore(database, lease_seconds=60) as reopened:
            fresh = await reopened.get_job(job.id)
            assert fresh and fresh.state == "submission_started"

    asyncio.run(scenario())


def test_transactional_budget_reservation_and_reconciliation(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with AsyncJobStore(tmp_path / "jobs.sqlite") as store:
            await store.ensure_budget(MEDIA_KEY, "shazam", max_requests=2)
            job = await store.ensure_job(MEDIA_KEY, QUERY_ID, "shazam")
            leased = await store.lease_next("worker")
            assert leased and leased.id == job.id
            await store.submission_started(job.id, "worker")
            assert await store.begin_physical_attempt(job.id) == 1
            assert await store.begin_physical_attempt(job.id) == 2
            with pytest.raises(BudgetExhausted):
                await store.begin_physical_attempt(job.id)
            await store.submitted(job.id)
            final = await store.finish(job.id, "succeeded", result_path="result.json")
            budget = await store.budget(MEDIA_KEY, "shazam")
            assert final.physical_attempts == final.actual_units == 2
            assert budget and budget["reserved_requests"] == 0
            assert budget["used_requests"] == 2

    asyncio.run(scenario())


def test_refresh_charges_only_new_physical_attempts(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with AsyncJobStore(tmp_path / "jobs.sqlite") as store:
            await store.ensure_budget(MEDIA_KEY, "shazam", max_requests=10)
            job = await store.ensure_job(MEDIA_KEY, QUERY_ID, "shazam")

            for owner in ("first", "refresh"):
                leased = await store.lease_next(owner)
                assert leased and leased.id == job.id
                await store.submission_started(job.id, owner)
                await store.begin_physical_attempt(job.id)
                await store.submitted(job.id)
                await store.finish(job.id, "succeeded", result_path=f"{owner}.json")
                if owner == "first":
                    await store.reset_for_refresh(job.id)

            budget = await store.budget(MEDIA_KEY, "shazam")
            final = await store.get_job(job.id)
            assert budget and budget["used_requests"] == 2
            assert final and final.physical_attempts == final.actual_units == 2

            await store.reset_for_refresh(job.id)
            leased = await store.lease_next("no-network-refresh")
            assert leased and leased.id == job.id
            await store.submission_started(job.id, "no-network-refresh")
            await store.finish(job.id, "permanent_failure", result_path="error.json")
            budget = await store.budget(MEDIA_KEY, "shazam")
            assert budget and budget["used_requests"] == 2

    asyncio.run(scenario())


class SimulatedCrash(BaseException):
    pass


class FakeDurableAdapter:
    def __init__(self, failure_point: str) -> None:
        self.failure_point = failure_point
        self.submissions = 0
        self.remote_by_key: dict[str, str] = {}
        self.poll_crashed = False

    async def reconcile(self, cache_key: str, on_attempt: object) -> str | None:
        await on_attempt()  # type: ignore[operator]
        return self.remote_by_key.get(cache_key)

    async def submit(self, cache_key: str, on_attempt: object) -> str:
        await on_attempt()  # type: ignore[operator]
        self.submissions += 1
        remote = "remote-1"
        self.remote_by_key[cache_key] = remote
        if self.failure_point == "during_upload":
            self.failure_point = "done"
            raise SimulatedCrash
        return remote

    async def poll(self, remote_ref: str, on_attempt: object) -> str:
        del remote_ref
        await on_attempt()  # type: ignore[operator]
        if self.failure_point == "during_polling" and not self.poll_crashed:
            self.poll_crashed = True
            raise SimulatedCrash
        return "succeeded"


@pytest.mark.parametrize(
    "failure_point",
    [
        "before_network",
        "during_upload",
        "after_acceptance",
        "after_remote_id_persistence",
        "during_polling",
    ],
)
def test_failure_injection_yields_exactly_one_submission(
    tmp_path: Path, failure_point: str
) -> None:
    database = tmp_path / f"{failure_point}.sqlite"
    adapter = FakeDurableAdapter(failure_point)

    def hook(point: str) -> None:
        if point == failure_point:
            adapter.failure_point = "done"
            raise SimulatedCrash

    async def first_run() -> str:
        store = await AsyncJobStore(database, lease_seconds=0).start()
        await store.ensure_budget(MEDIA_KEY, "scanner", max_requests=20)
        original = await store.ensure_job(MEDIA_KEY, QUERY_ID, "scanner")
        job = await store.lease_next("crashed-worker")
        assert job and job.id == original.id
        with pytest.raises(SimulatedCrash):
            await execute_durable_submission(
                store=store,
                job=job,
                owner="crashed-worker",
                cache_key="cache-key",
                adapter=adapter,
                failure_hook=hook,
            )
        await store.close()
        return job.id

    job_id = asyncio.run(first_run())

    async def resumed_run() -> str:
        async with AsyncJobStore(database, lease_seconds=0) as store:
            recovered = await store.get_job(job_id)
            assert recovered
            if recovered.state == "outcome_unknown":
                await store.acknowledge_retry(job_id)
            job = await store.lease_next("resumed-worker")
            assert job and job.id == job_id
            await execute_durable_submission(
                store=store,
                job=job,
                owner="resumed-worker",
                cache_key="cache-key",
                adapter=adapter,
            )
            final = await store.get_job(job_id)
            assert final
            return final.state

    assert asyncio.run(resumed_run()) == "succeeded"
    assert adapter.submissions == 1


def test_abrupt_process_kill_recovers_without_double_submission(tmp_path: Path) -> None:
    database = tmp_path / "killed.sqlite"
    ready = tmp_path / "remote-accepted.txt"
    program = """
import asyncio, os, sys, time
from pathlib import Path
from id_detector.jobs import AsyncJobStore
async def main():
    store = await AsyncJobStore(Path(sys.argv[1]), lease_seconds=0).start()
    await store.ensure_budget('a' * 64, 'scanner', max_requests=20)
    original = await store.ensure_job('a' * 64, 'b' * 40, 'scanner')
    job = await store.lease_next('killed-worker')
    await store.submission_started(original.id, 'killed-worker')
    await store.begin_physical_attempt(original.id)
    Path(sys.argv[2]).write_text(original.id, encoding='utf-8')
    time.sleep(60)
asyncio.run(main())
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(database), str(ready)],
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.is_file()
        job_id = ready.read_text(encoding="utf-8")
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
    adapter = FakeDurableAdapter("done")
    adapter.remote_by_key["cache-key"] = "remote-accepted-before-crash"

    async def resume() -> tuple[str, int]:
        async with AsyncJobStore(database, lease_seconds=0) as store:
            recovered = await store.get_job(job_id)
            assert recovered and recovered.state == "outcome_unknown"
            await store.acknowledge_retry(job_id)
            job = await store.lease_next("restart")
            assert job
            await execute_durable_submission(
                store=store,
                job=job,
                owner="restart",
                cache_key="cache-key",
                adapter=adapter,
            )
            final = await store.get_job(job_id)
            assert final
            return final.state, final.physical_attempts

    state, physical_attempts = asyncio.run(resume())
    assert state == "succeeded"
    assert adapter.submissions == 0  # the accepted pre-crash submission was reconciled
    assert physical_attempts >= 3
