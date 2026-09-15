## A — Audit of the fixer’s verdicts

- **Round 8 item 1 is not closed.** `Database.migrate()` now owns the supervisor-lock check, and `ProcessLock.acquire()` is non-blocking. Exact-path upgrades and downgrades are correctly refused while the supervisor lock is held. However, two bypasses remain:
  - the old-claim check is not atomic with the migration;
  - local-path detection is case-sensitive.
- The no-pending fast path is sound: [`database.py:200`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/database.py:200>) skips the supervisor lock, and because `pending=False` implies neither migration loop predicate can match, it applies no packaged migration. This lets a supervised worker open an already-current database.
- No other production code applies the `idea_web` numbered migrations. `local_database()` delegates to `Database.migrate()`. The other `executescript` found is `AsyncJobStore`’s unrelated per-media `jobs.sqlite` initialization.
- **Round 8 item 2 is closed.** [`worker.py:430`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:430>) validates an explicit job’s existence and exact `run_id` within the settlement transaction. The SQLite service writer’s run fence independently proves an active, unexpired job owns that run and token in the same transaction.
- [`LegacyRecoveryLedger`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:573>) validates a stopped null-run job, rejects an already-owned run and conflicting settlement, and its only production caller is `_recover_unidentified` through [`local.py:586`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/local.py:586>).
- **Round 8 item 3 is closed.** The pathless-settlement docstring at [`worker.py:388`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:388>) now correctly describes conservative paid spend and later projection.
- The record marks no original retro finding “not reproducible” or “already fixed”; all were reproduced. Nothing in the narrow Round 8 change invalidates the money/resume design confirmed in Rounds 4–7.

## B — Correctness bugs in the fix

1. **P0 — the live old-claim exclusion has a check-then-act race.**  
   [`database.py:256`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/database.py:256>) examines claims while the SQLite connection is in autocommit mode. `_exclude_local_workers()` then returns, and only [`database.py:204`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/database.py:204>) starts `BEGIN IMMEDIATE` through `executescript`.

   Concrete failure: an unsupervised/orphaned pre-v3 worker claims a v2 job after the `SELECT` reports no claim but before 0003 obtains SQLite’s write lock. Its claim commits without `authority_token`; 0003 then adds the authority columns and trigger, but the already-completed claim never fires that trigger. Old code can dispatch without `run_dispatches` or durable reservation authority. A crash plus loss of its old JSONL can therefore make the replacement re-bill.

   Multiple migrations are also committed separately, providing further gaps between scripts. Additionally, [`database.py:185`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/database.py:185>) can create `schema_migrations` before the supervisor exclusion is acquired, so the lock is not held across all schema DDL.

2. **P0 — a Windows case-preserved local path is classified as hosted.**  
   [`database.py:232`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/database.py:232>) compares `.idea` and `app.db` case-sensitively. Windows preserves physical casing even though lookup is case-insensitive. A restored or previously created `.IDEA/APP.DB` resolves with that casing, making `is_local_work_root=False`.

   Concrete failure: an old supervisor holds the case-folded `worker-supervisor.lock`, but a new direct `migrate()` sees the same local database as hosted and skips `_exclude_local_workers()` entirely. It can perform the mixed-version migration that Round 7 identified as capable of losing spend or enabling a re-bill. Relative paths and `\\?\` paths with canonical casing work; casing remains the bypass.

## C — Regressions

- No separate 4a-ii regression found. `idea serve` still constructs `LocalJobs`, starts `LocalWorkerSupervisor`, and supervises a separate worker process. The web process still does not execute the pipeline.
- Worker restart remains viable because an already-current database takes the no-pending path while its supervisor owns the lock.
- The fail-closed schema check in `JobQueue._claim()` is unchanged.
- HTTP routes and body readers were untouched by Round 8, so GET-write and bounded-body behavior are unchanged.
- `idea.cmd`, `retention.py`, and `tests/test_phase2b_retention.py` are untouched.

## D — Test quality

I could not run `uv` or pytest in this read-only sandbox. This is static/reversion-path analysis only; no provider or GC command was invoked.

The Round 8 tests are load-bearing for the branches they cover:

- Removing direct migration locking makes the exact-path upgrade/downgrade test fail.
- Removing the pre-existing unproven-claim check makes its claim test fail.
- Reverting exact settlement ownership makes both missing/null-owner cases pass unexpectedly.
- Removing `LegacyRecoveryLedger` or its validations fails the legacy test.

However, the migration tests remain insufficient:

- **P1 test gap:** the claim test inserts the old claim before `migrate()` begins. It passes despite the P0 window between [`database.py:266`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/database.py:266>) and the later `BEGIN IMMEDIATE`.
- **P1 test gap:** every local migration test uses canonical lowercase names. None covers Windows case-preserved `.IDEA/APP.DB`, combined case/`\\?\` spelling, or verifies that all aliases contend on the same supervisor lock.
- The no-pending path lacks a narrow dedicated test, but existing real-supervisor tests exercise it indirectly, and its loop predicates are statically consistent with `pending`.

## E — Scope

- `git diff --check` is clean.
- All untracked files and the complete diff/status were read.
- No dependency file changed.
- No FastAPI, Starlette, or uvicorn import was added under `src/id_detector/` or `src/idea_web/jobs/`.
- Nothing under `data/` or `work/`.
- Playlists, `tests/test_playlists.py`, `README.md`, `idea.cmd`, `present/theme.py`, `tests/test_phase0a_security.py`, `retention.py`, and `tests/test_phase2b_retention.py` are untouched.
- No Round 8 existing test was edited, and no existing assertion appears weakened by the accumulated follow-up diff.

## Required fixes

### Realistic

1. **P0:** Make migration exclusion atomic: acquire the local supervisor lock before any schema DDL, obtain and retain SQLite’s write exclusion before checking claims, and hold both through every up/down script without `executescript` introducing commits or gaps. Add a deterministic regression that inserts a v2 old-code claim after the current precheck point and proves migration cannot proceed past it.
2. **P0:** Normalize local database identity using Windows-aware case normalization as well as resolved relative/extended-path handling, and derive the supervisor lock from that same identity. Add Windows tests for physically case-preserved `.IDEA/APP.DB`, canonical casing, relative paths, and `\\?\` aliases.

### Adversarial

No newly widened adversarial issue or required adversarial fix.

VERDICT: FIX_FIRST