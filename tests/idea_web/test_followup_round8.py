"""Round 8: migration exclusion inside ``Database.migrate``; exact settlement ownership.

Run against the round-7 code first (see ``docs/reviews/followup-money-resume.md``). Round-8 names
are imported inside each test so a reverted fix fails that test alone.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from id_detector.compat import LOCAL_OWNER_SCOPE
from id_detector.recipes import FREE_RECIPE
from id_detector.run_ledger import LedgerConflict, RecoveredMoney, new_run_id
from idea_web.database import Database
from idea_web.jobs.worker import SettlementLedger
from tests.idea_web.test_followup_round6 import _count
from tests.idea_web.test_worker import AUDIO, ROOT

LOCK_HOLDER = """
import sys
from pathlib import Path
from id_detector.jobs import ProcessLock
lock = ProcessLock(Path(sys.argv[1]))
lock.acquire()
print("ready", flush=True)
sys.stdin.read()
lock.release()
"""


def _local(root: Path, version: int) -> Database:
    database = Database(root / ".idea" / "app.db")
    assert database.migrate(version) == version
    return database


def _insert_job(
    database: Database,
    *,
    run_id: str | None,
    state: str = "analysis",
    claim_token: str | None = None,
    lease_until: float | None = None,
) -> str:
    job_id = uuid.uuid4().hex
    with database.write() as connection:
        connection.execute(
            "INSERT INTO jobs(id, run_id, target, recipe_id, state, lease_owner, lease_until, "
            "claim_token, tenant_scope, progress, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', 1, 1)",
            (
                job_id,
                run_id,
                json.dumps({"kind": "local", "path": str(AUDIO)}),
                FREE_RECIPE.recipe_id,
                state,
                "old-worker" if claim_token else None,
                lease_until,
                claim_token,
                LOCAL_OWNER_SCOPE,
            ),
        )
    return job_id


# ------------------------------------------------ item 1: migrations cannot bypass the lock


def test_a_direct_migrate_is_refused_while_another_process_holds_the_supervisor_lock(
    tmp_path: Path,
) -> None:
    from idea_web.database import MigrationRefused

    database = _local(tmp_path, 2)
    holder = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, str(tmp_path / ".idea" / "worker-supervisor.lock")],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "ready"
        with pytest.raises(MigrationRefused, match="stop the running ID'er first"):
            Database(tmp_path / ".idea" / "app.db").migrate()
        assert database.version() == 2
        with pytest.raises(MigrationRefused):
            database.migrate(1)  # a downgrade is a migration too
        assert database.version() == 2
        # A hosted database is unaffected by any local supervisor lock.
        assert Database(tmp_path / "hosted" / "app.db").migrate() == 5
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        try:
            holder.wait(20)
        except subprocess.TimeoutExpired:
            holder.kill()  # only the lock holder this test started, by its handle (PID)
            holder.wait(20)
    assert database.migrate() == 5


def test_a_migration_is_refused_while_an_unstamped_unexpired_claim_exists(tmp_path: Path) -> None:
    from idea_web.database import MigrationRefused

    database = _local(tmp_path, 2)
    job_id = _insert_job(
        database, run_id=None, claim_token="old-code-claim", lease_until=time.time() + 600
    )
    with pytest.raises(MigrationRefused, match="stop the running ID'er first"):
        database.migrate()
    assert database.version() == 2

    # The same claim in a hosted database does not block its migration.
    hosted = Database(tmp_path / "hosted.db")
    assert hosted.migrate(2) == 2
    _insert_job(hosted, run_id=None, claim_token="old-code-claim", lease_until=time.time() + 600)
    assert hosted.migrate() == 5

    # Once the old claim's lease has expired nothing can still be running it.
    with database.write() as connection:
        connection.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job_id,))
    assert database.migrate() == 5


# ------------------------------------------------ item 2: settlement ownership is exact


def _entry(run_id: str):
    from id_detector.service import interrupted_entry

    return interrupted_entry(run_id, str(AUDIO), "cancelled", RecoveredMoney(), [])


@pytest.mark.parametrize("owner", ["missing", "null"])
def test_a_settlement_needs_an_existing_job_that_owns_the_run(tmp_path: Path, owner: str) -> None:
    database = _local(tmp_path, 3)
    run_id = new_run_id()
    job_id = (
        "no-such-job" if owner == "missing" else _insert_job(database, run_id=None, state="failed")
    )
    with pytest.raises(LedgerConflict):
        SettlementLedger(database, job_id=job_id).settle(None, _entry(run_id))
    assert _count(database, "run_settlements") == 0


def test_legacy_null_run_adoption_is_an_explicit_validated_recovery(tmp_path: Path) -> None:
    from idea_web.jobs.worker import LegacyRecoveryLedger

    database = _local(tmp_path, 3)
    legacy = _insert_job(database, run_id=None, state="cancelled")
    run_id = new_run_id()
    assert LegacyRecoveryLedger(database, job_id=legacy).settle(None, _entry(run_id))
    assert _count(database, "run_settlements", run_id) == 1

    # A run another job owns is never attributed to a legacy row.
    owned = new_run_id()
    _insert_job(database, run_id=owned, state="complete")
    with pytest.raises(LedgerConflict):
        LegacyRecoveryLedger(database, job_id=legacy).settle(None, _entry(owned))
    # Nor may the recovery write for a job that has a run id, or one still active.
    stamped = _insert_job(database, run_id=new_run_id(), state="cancelled")
    active = _insert_job(database, run_id=None, state="analysis")
    for job_id in ("no-such-job", stamped, active):
        with pytest.raises(LedgerConflict):
            LegacyRecoveryLedger(database, job_id=job_id).settle(None, _entry(new_run_id()))
    assert _count(database, "run_settlements") == 1
