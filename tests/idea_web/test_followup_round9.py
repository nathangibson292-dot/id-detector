"""Round 9: atomic migration exclusion and a normalised local-database identity.

Run against the round-8 code first (see ``docs/reviews/followup-money-resume.md``).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from idea_web import database as database_module
from idea_web.database import Database, Migration, MigrationRefused
from tests.idea_web.test_followup_round8 import LOCK_HOLDER
from tests.idea_web.test_worker import ROOT

V2_CLAIM = (
    "INSERT INTO jobs(id, run_id, target, recipe_id, state, lease_owner, lease_until, claim_token, "
    "tenant_scope, progress, created_at, updated_at) VALUES ('old-job', NULL, "
    "'{\"kind\":\"local\",\"path\":\"x\"}', 'r', 'analysis', 'old-worker', ?, 'old-claim', "
    "'user:local', '{}', 1, 1)"
)


class _Holder:
    """A second process holding the supervisor lock (stopped only through its own handle)."""

    def __init__(self, lock_path: Path) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", LOCK_HOLDER, str(lock_path)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.process.stdout is not None
        assert self.process.stdout.readline().strip() == "ready"

    def release(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        try:
            self.process.wait(20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(20)


# ------------------------------------------------ item 1: the exclusion is one write transaction


def test_a_claim_racing_the_migration_check_cannot_commit_before_or_around_it(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / ".idea" / "app.db")
    assert database.migrate(2) == 2
    outcome: dict[str, str] = {}

    def racing_claim(_connection: sqlite3.Connection) -> None:
        # Fires after the supervisor lock is taken and before the claim check.
        other = sqlite3.connect(str(database.path), timeout=0, isolation_level=None)
        try:
            other.execute("PRAGMA busy_timeout = 0")
            other.execute(V2_CLAIM, (time.time() + 600,))
            outcome["claim"] = "committed"
        except sqlite3.OperationalError as exc:
            outcome["claim"] = str(exc)
        finally:
            other.close()

    database.before_claim_check = racing_claim
    assert database.migrate() == 6
    assert outcome == {"claim": "database is locked"}
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_every_pending_script_commits_once_or_not_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / ".idea" / "app.db")
    assert database.migrate(1) == 1
    real = database_module.migrations()
    broken = Migration(
        real[2].number, real[2].name, real[2].up + "\nSELECT no_such_function();\n", real[2].down
    )
    monkeypatch.setattr(database_module, "migrations", lambda: (real[0], real[1], broken))
    with pytest.raises(sqlite3.OperationalError):
        database.migrate()
    assert database.version() == 1  # 0002 was not committed on its own
    with database.read() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    assert "attached" not in columns and "money_authority" not in columns
    monkeypatch.undo()
    assert database.migrate() == 6


# ------------------------------------------------ item 2: one normalised local identity


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path aliases")
def test_case_relative_and_extended_aliases_of_a_local_database_share_one_supervisor_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upper = tmp_path / ".IDEA" / "APP.DB"
    assert Database(upper).migrate(2) == 2
    assert ".IDEA" in os.listdir(tmp_path) and "APP.DB" in os.listdir(tmp_path / ".IDEA")
    monkeypatch.chdir(tmp_path)
    aliases = {
        "casing": Database(upper),
        "relative": Database(Path(".IDEA") / "APP.DB"),
        "extended": Database(Path("\\\\?\\" + str(upper.resolve()))),
    }
    canonical = os.path.normcase(str((tmp_path / ".idea" / "worker-supervisor.lock").resolve()))
    for name, alias in aliases.items():
        assert alias.is_local_work_root, name
        assert os.path.normcase(str(alias.supervisor_lock_path)) == canonical, name

    holder = _Holder(tmp_path / ".idea" / "worker-supervisor.lock")
    try:
        for name, alias in aliases.items():
            with pytest.raises(MigrationRefused, match="stop the running ID'er first"):
                alias.migrate()
            assert alias.version() == 2, name
    finally:
        holder.release()
    assert aliases["extended"].migrate() == 6


# ------------------------------------------------ round 10: tests only (round-9 review)


def test_canonical_and_relative_local_aliases_share_one_supervisor_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = tmp_path / ".idea" / "app.db"
    assert Database(canonical).migrate(2) == 2
    monkeypatch.chdir(tmp_path)
    aliases = {
        "canonical": Database(canonical),
        "relative": Database(Path(".idea") / "app.db"),
    }
    expected = os.path.normcase(str((tmp_path / ".idea" / "worker-supervisor.lock").resolve()))
    for name, alias in aliases.items():
        assert alias.is_local_work_root, name
        assert os.path.normcase(str(alias.supervisor_lock_path)) == expected, name

    holder = _Holder(tmp_path / ".idea" / "worker-supervisor.lock")
    try:
        for name, alias in aliases.items():
            with pytest.raises(MigrationRefused, match="stop the running ID'er first"):
                alias.migrate()
            assert alias.version() == 2, name
    finally:
        holder.release()
    assert aliases["relative"].migrate() == 6


def test_the_in_transaction_reread_sees_a_migration_applied_after_the_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from idea_web.database import _statements

    database = Database(tmp_path / ".idea" / "app.db")
    assert database.migrate(1) == 1
    scripts = database_module.migrations()
    real = Database._applied
    seen: list[set[int]] = []

    def racing_applied(connection: sqlite3.Connection) -> dict[int, str]:
        result = real(connection)
        seen.append(set(result))
        if len(seen) == 1:
            # Between the preflight read and the migration's write transaction, another migrator
            # commits 0002 in full.
            other = sqlite3.connect(str(database.path), isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                for statement in _statements(scripts[1].up):
                    other.execute(statement)
                other.execute(
                    "INSERT INTO schema_migrations(number, name, applied_at) VALUES (?, ?, 'x')",
                    (scripts[1].number, scripts[1].name),
                )
                other.execute("COMMIT")
            finally:
                other.close()
        return result

    monkeypatch.setattr(Database, "_applied", staticmethod(racing_applied))
    assert database.migrate() == 6
    monkeypatch.undo()
    assert seen[0] == {1} and seen[-1] == {1, 2}
    with database.read() as connection:
        rows = connection.execute(
            "SELECT number, COUNT(*) FROM schema_migrations GROUP BY number ORDER BY number"
        ).fetchall()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    # none twice, none skipped
    assert [tuple(row) for row in rows] == [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (6, 1)]
    assert {"attached", "money_authority", "authority_token"} <= columns


def test_a_refused_migration_of_a_fresh_local_database_creates_no_schema_object(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".idea" / "app.db"
    holder = _Holder(tmp_path / ".idea" / "worker-supervisor.lock")
    try:
        with pytest.raises(MigrationRefused, match="stop the running ID'er first"):
            Database(path).migrate()
    finally:
        holder.release()
    raw = sqlite3.connect(str(path))
    try:
        objects = raw.execute("SELECT type, name FROM sqlite_master").fetchall()
    finally:
        raw.close()
    assert objects == []


@pytest.mark.skipif(sys.platform != "win32", reason="\\\\?\\UNC\\ aliases are Windows paths")
def test_a_unc_extended_alias_has_the_ordinary_unc_identity_and_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from idea_web.database import _local_identity

    # A pure identity test: resolving would reach for a network share, so resolution is the
    # identity here and nothing touches the filesystem.
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: self)
    extended = Path("\\\\?\\UNC\\server\\share\\work\\.IDEA\\APP.DB")
    ordinary = Path("\\\\server\\share\\work\\.idea\\app.db")
    assert _local_identity(extended) == "\\\\server\\share\\work\\.idea\\app.db"
    assert _local_identity(extended) == _local_identity(ordinary)
    extended_lock = Database(extended).supervisor_lock_path
    ordinary_lock = Database(ordinary).supervisor_lock_path
    assert str(extended_lock) == str(ordinary_lock)
    assert str(ordinary_lock) == "\\\\server\\share\\work\\.idea\\worker-supervisor.lock"
