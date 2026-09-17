## A. Contract violations

- **P1 — cancellation leaves contradictory settlement metadata.** `_settle_as_cancelled()` changes only `status` and `exit_code` (`src/idea_web/jobs/worker.py:324-329`). A real successful pipeline settlement contains `reason`, `achieved`, `bundle_id`, `fuse_run`, and compatibility metadata (`src/id_detector/pipeline.py:1662-1684`). Therefore a last-detach race can produce a settlement and journal entry saying `status=cancelled` while still claiming `achieved=deep` and pointing to a completed bundle. This contradicts §3.5’s requirement that the run ends cancelled and the existing invariant that the durable status/reason/achievement agree.

- Hosted paid dispatch remains refused before intake and again inside `_commit_intake` (`src/idea_web/jobs/worker.py:2620-2624`, `2926-2929`). Recipe equality and the database trigger prevent Free/paid cross-attachment (`src/idea_web/jobs/worker.py:2938-2948`, `src/idea_web/migrations/0005_run_subscribers.up.sql:44-60`).

- Frozen/protected scope is clean: `git diff -- src/id_detector` is empty, and no second-session-owned file, profile, or frozen artefact changed.

## B. Correctness bugs

- **P1 — a crash or I/O failure after the cancellation transaction can permanently leave the journal reporting `complete`.** The database and job become terminal before reprojection; reprojection then runs outside the transaction under `suppress(Exception)` (`src/idea_web/jobs/worker.py:2378-2381`). A restarted worker only reconciles attached jobs and claims active jobs (`src/idea_web/jobs/worker.py:2603-2608`), so it never repairs this terminal settlement projection. SQLite remains financially authoritative, but the durable journal silently disagrees.

- No realistic P0 money failure found. The relevant writes are serialized and fenced:

  - Reservation insertion and current-payer attribution occur together (`src/idea_web/jobs/worker.py:567-580`).
  - Payer CAS, old-reservation release, re-attribution event, cap check, and audit share the detach transaction (`src/idea_web/jobs/worker.py:2156-2195`).
  - Terminal close/release events share the run-terminal transaction (`src/idea_web/jobs/worker.py:2349-2357`).
  - Crash recovery folds attempt events, authoritative dispatches, and the reservation, then uses the existing insert-if-absent settlement ledger (`src/idea_web/jobs/worker.py:3326-3350`).

- Concurrent detaches cannot transfer twice or select an already-detached subscriber: `BEGIN IMMEDIATE`, the live-subscriber query, payer CAS, and event uniqueness provide mutually reinforcing fences.

## C. Scope

- Migration 0005 is next-numbered; its down script refuses populated subscriber, payer-event, or audit tables (`src/idea_web/migrations/0005_run_subscribers.down.sql:4-12`) before removing the new schema.

- The opaque `user_id` and subscriber reservation-state model does not add accounts, credit lots, tenant balances, or hosted paid dispatch. The seam remains within 4b-iv scope.

- The cycle-size exception is the explicitly accepted decision and is not reported as a finding.

## D. Tests and round-1 fixes

1. **DONE — reservation after transfer.** Attribution reads the current payer inside the reservation transaction (`src/idea_web/jobs/worker.py:384-430`, `567-580`). The regression at `tests/idea_web/test_coalescing.py:1102-1167` asserts payer, cap lookup, event amount, and audit. Reverting the call necessarily breaks those assertions; the recorded reversion failed at `docs/reviews/build-4b-iv.md:365`.

2. **PARTIAL — last detach beats success.** `terminal()` overrides the result and analysis row, rewrites settlement status/exit code, and closes the payer as cancelled (`src/idea_web/jobs/worker.py:2298-2305`, `2349-2357`). However, the stale result metadata and unrecoverable reprojection window above remain. The test uses `interrupted_entry()` via its helper (`tests/idea_web/test_coalescing.py:207-220`, `1190`), which starts with no achieved result or bundle and therefore cannot expose the real-pipeline inconsistency. The status regressions themselves are causally covered and were reported failing at `docs/reviews/build-4b-iv.md:366-367`.

3. **DONE — crash after last detach.** The test starts a real child process, leaves a paid dispatch unresolved, terminates the driver, reclaims it, and asserts exactly one settlement and one dispatch (`tests/idea_web/test_coalescing.py:1252-1312`). Removing `_settle_if_absent()` breaks the row-count assertion; the recorded reversion failed at `docs/reviews/build-4b-iv.md:368`.

4. **DONE — abandon mirrors subscribers.** `_abandon_run()` closes subscriptions and calls the shared helper (`src/idea_web/jobs/worker.py:1713-1723`); the drain/dead-letter regression is at `tests/idea_web/test_coalescing.py:1315-1341`. Recorded reversion failed at `docs/reviews/build-4b-iv.md:369`.

5. **DONE — private race.** Two workers are barrier-started and must simultaneously drive distinct runs (`tests/idea_web/test_coalescing.py:1344-1391`). Recorded reversion failed at `docs/reviews/build-4b-iv.md:370`.

- The file collects 30 cases. Races use barriers/process events, not sleeps; tests are offline. No existing assertion was weakened or deleted.

- Exact `uv run` gates could not start because the sandbox cannot execute the WinGet `uv.exe` link. Direct pytest also cannot run because the read-only sandbox has no writable temporary directory. Direct collection succeeded for all 30 coalescing cases.

## E. Local mode

`uv run idea serve --no-open --port 8791` could not launch through `uv`. Direct `.venv\Scripts\idea.exe` reached local initialization but failed when it attempted to create `work/`, so `/` could not be served in this read-only environment. No serve entry point, `idea.cmd`, headers, theme, or `src/id_detector/**` file changed.

## F. Quality

- Direct Ruff passed; direct fixture audit passed over 514 files; `git diff --check` passed.
- `JobQueue.before_detach_commit` is unused in both production and tests (`src/idea_web/jobs/worker.py:1589`, `2057-2058`) and should not remain as dead test-seam code.
- The cancellation regression should use a pipeline-shaped complete settlement, including achievement and bundle metadata, rather than the metadata-empty interruption helper.

## Required fixes

### Realistic

1. **P1:** When last detach overrides a successful result, rewrite the complete settlement entry to cancelled truth—reason, achievement, bundle/fuse references, and compatibility included—and add a pipeline-shaped regression.
2. **P1:** Make post-terminal settlement reprojection recoverable after a crash or I/O failure, with a fail-once/restart regression proving the stale `complete` journal is repaired.
3. **P2:** Remove the unused `before_detach_commit` seam or exercise it in a necessary regression.
4. **P2:** Re-run the exact pytest/ruff/audit gates and live `/` smoke test in a writable environment.

### Adversarial

None.

VERDICT: FIX_FIRST