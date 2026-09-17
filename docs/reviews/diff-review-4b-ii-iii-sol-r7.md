## A. Contract violations

| Round-6 item | Status | Evidence |
|---|---|---|
| 1. Continuous restore exclusion through migration | **DONE** | `local_database()` constructs and migrates the database inside `_restore_excluded()` (`src/idea_web/jobs/local.py:174-178`). The underlying lock is acquired before recovery and released in `finally` after the context returns (`src/idea_web/backup.py:1420-1440`). A restore takes the same lock before the supervisor lock (`src/idea_web/backup.py:1477-1497`). The inverse-race regression starts a real restore before SQLite opens, requires an initial refusal, and permits publication only after the migration-return marker (`tests/idea_web/test_backup.py:1650-1737`). |
| 2. Preserve skipped runs within captured media | **DONE** | Restore derives skipped run and owned-bundle prefixes (`src/idea_web/backup.py:1241-1268`), excludes them from stale removal (`src/idea_web/backup.py:1275-1278,1540-1546`), and records them in the journal and result (`src/idea_web/backup.py:1550-1563,1626-1633`). The regression changes both subtrees after backup, verifies exact bytes after restore, checks both report entries, and would fail if preservation were removed (`tests/idea_web/test_backup.py:1743-1805`). |

Additional contract checks:

- `git diff 08ec7ab -- src/id_detector` is exactly zero lines.
- No protected file or `data/corpus/**` changed.
- The current pass does not modify the worker, migrations 0001–0003, paid-dispatch authority, settlement sweep, or claim/cancel fence.
- Migration 0004 only creates `provider_breaker_state`; its down script drops that table (`src/idea_web/migrations/0004_shared_breaker.up.sql:13-36`, `0004_shared_breaker.down.sql:1`). An independent in-memory populated 0003→0004→0003 probe retained one row in each money-authority table.
- Version assertions changed from 3 to 4 without removing the surrounding row-preservation assertions (`tests/idea_web/test_followup_round6.py:419-440`).
- U-F9 weighting remains unchanged.

No contract violation found under the Round-7 stop rule.

## B. Correctness bugs

No realistic correctness regression introduced by this pass.

The restore/startup lock ordering now prevents concurrent SQLite restore and migration. Skipped-run preservation covers both `fuse/runs/<run_id>` and bundles identified by the skip record or their manifest, without exempting unrelated stale files.

No new route can re-dispatch AudD, lose settlement state, weaken cancellation fencing, or change breaker/status behavior.

## C. Scope

No out-of-scope or second-session-owned file changed. The accepted CLI deferral remains respected; backup, verification, and restore are reachable through `python -m idea_web.backup` (`src/idea_web/backup.py:1640-1679`). Its help command exposes all three operations.

Nightly scheduling, off-box upload, and progress re-weighting remain correctly deferred.

## D. Tests

The new regressions are offline and assert behavior rather than merely successful return:

- Restore/migration inverse race: `tests/idea_web/test_backup.py:1682-1737`.
- Skipped-run and owned-bundle byte preservation/reporting: `tests/idea_web/test_backup.py:1743-1805`.

Collection succeeded directly with capture disabled:

- `test_ops.py`: 27 tests.
- `test_backup.py`: 49 tests.
- `tests/idea_web`: 302 tests.
- Full suite: 1803 collected, 97 deselected.

The exact `uv run ...` commands could not execute because the installed WinGet `uv.exe` alias is unusable in this sandbox. Direct pytest execution and `check_page_js.py` were also prevented by the absence of any writable temporary directory. No existing assertion was weakened by this pass.

## E. Local mode

The exact `uv run idea serve --no-open --port 8791` command stopped at the broken `uv.exe` alias, so `/` could not be probed dynamically.

Static review found no changes to `idea.cmd`, the serve entry point, cached-mix handling, headers, CSP, templates, or static assets. The only local-startup change is the now-correct restore exclusion described above.

## F. Quality

- Direct Ruff check: passed.
- Direct fixture audit: passed, 506 files.
- `git diff --check`: passed.
- No dead code or duplicated money/restore authority introduced.
- Both fixed defects have focused regressions.

## Required fixes

### Realistic

None.

### Adversarial

None.

## Follow-ups, not blockers

- Rerun the exact pytest gates, full suite, `check_page_js.py`, and live `/` probe in a writable environment with a functioning `uv.exe`.

VERDICT: OK_TO_COMMIT