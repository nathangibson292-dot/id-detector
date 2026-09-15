## A — Audit of the fixer’s verdicts

- The two Round 8 P0 production defects are now closed.
- [`Database.migrate()`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/database.py:213>) acquires the migration lock, performs a read-only preflight, acquires the local supervisor lock before schema DDL, opens one `BEGIN IMMEDIATE`, checks claims, creates `schema_migrations`, re-reads applied migrations inside the transaction, applies all pending scripts and records, and commits once. Errors roll back and the normal path releases the supervisor lock in `finally`.
- The transactional re-read at [`database.py:242`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/database.py:242>) prevents a stale preflight snapshot from deciding which scripts run.
- All six scripts contain no `PRAGMA`, `VACUUM`, transaction-control statement, or other statement that requires an implicit commit. I also ran a read-only in-memory SQLite probe: all six split and executed, all four trigger bodies remained whole, and `connection.in_transaction` stayed true after every statement.
- Hosted databases follow the same single-transaction loop; only supervisor acquisition and the old-claim check are local-only. No hosted migration regression is apparent.
- [`_local_identity()`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/database.py:31>) handles resolved relative paths, Windows case normalization, `\\?\` and `\\?\UNC\` stripping. Both locality and supervisor-lock derivation consume that identity.
- The no-pending path no longer creates `schema_migrations`. The only production consumer that directly queries that table is [`JobQueue._claim()`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:1482>), which receives a database migrated to the current schema by [`local_database()`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/local.py:162>). A target-0 database has no `jobs` table either, so no valid production queue depends on an otherwise-empty `schema_migrations` table.
- The Round 9 record dismisses none of the Round 8 findings.

## B — Correctness bugs in the fix

No production correctness defect found in the narrow Round 9 code.

## C — Regressions

1. **P1 — the new alias test fails on supported POSIX environments.**  
   [`test_followup_round9.py:106`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/tests/idea_web/test_followup_round9.py:106>) is unconditional, although uppercase and `\\?\` aliases are Windows-specific. On POSIX, `os.path.normcase()` does not lowercase paths, so the uppercase supervisor path at [`test_followup_round9.py:121`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/tests/idea_web/test_followup_round9.py:121>) differs from the lowercase canonical path at line 118. The constructed `\\?\` path is also not an extended-path alias on POSIX. The package is not declared Windows-only and implements POSIX `fcntl` locking at [`database.py:71`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/database.py:71>).

- No production regression to hosted migration behavior or the existing up/down migration tests was found.
- Round 9 did not touch serving, HTTP, worker supervision, or `idea.cmd`; no 4a-ii regression is introduced.

## D — Test quality

I could not run `uv` or pytest in this read-only sandbox. No provider, pipeline, or GC command was invoked.

The three tests broadly detect the advertised fixes:

- Moving `BEGIN IMMEDIATE` after the racing claim makes the first test fail.
- Restoring per-script commits makes the rollback test leave schema version 2 instead of 1.
- Restoring Round 8’s case-sensitive identity makes the Windows alias test fail.
- Removing supervisor acquisition or prefix normalization also makes the alias test fail on Windows.

Two required atomicity details are not load-bearing:

- **P1:** All tests observe the same applied set at preflight and at [`database.py:242`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/database.py:242>). Reusing the stale preflight snapshot instead of re-reading inside the transaction would still pass all three tests.
- **P1:** The held-supervisor tests start from schema 2, where `schema_migrations` already exists. Moving its creation back before supervisor acquisition would therefore pass those tests. No fresh pending database proves that refusal leaves every schema object absent.
- **P2:** The alias test covers a drive-style `\\?\` spelling but not the separate `\\?\UNC\` normalization branch.

## E — Scope

- Round 9 adds only [`test_followup_round9.py`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/tests/idea_web/test_followup_round9.py:1>) and changes production code only in [`database.py`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/database.py:1>).
- `git diff --check` is clean.
- No dependency file, prohibited second-session file, `tests/test_phase0a_security.py`, `data/`, or `work/` change is attributable to Round 9.
- No FastAPI, Starlette, or uvicorn import was added in the restricted packages.
- No existing test was weakened in Round 9.

## Required fixes

### Realistic

1. **P1:** Mark the case/extended-path integration test Windows-only, and add a separate cross-platform test for canonical and relative `.idea/app.db` aliases.
2. **P1:** Add a deterministic test that changes the applied schema after the preflight read and proves the in-transaction `_applied()` re-read prevents a duplicate or skipped migration.
3. **P1:** Add a fresh local-database test with the supervisor lock held that verifies a refused pending migration creates no `schema_migrations` table or other schema object.

### Adversarial

1. **P2:** Add coverage for `\\?\UNC\server\share\...\.idea\app.db`, verifying both normalized identity and contention with the supervisor’s ordinary UNC lock path.

VERDICT: FIX_FIRST