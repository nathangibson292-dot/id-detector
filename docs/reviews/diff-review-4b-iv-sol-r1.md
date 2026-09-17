## A. Contract violations

- **P0 — payer transfer can precede the USD reservation.** `_transfer_payer` records the current amount and skips the cap seam when that amount is zero (`src/idea_web/jobs/worker.py:2026-2041`), while the real reservation may be created later (`src/idea_web/jobs/worker.py:1284-1312`). If Alice detaches after Bob attaches but before `record_reservation()`, the immutable transfer event records `$0`; the later paid reservation is never re-attributed or cap-checked. This contradicts `docs/PLAN-v2.md:368-376`.

- **P0 — last detach can still produce a `complete` run.** Detach only sets the driver’s cancellation flag (`src/idea_web/jobs/worker.py:1987-1993`). If cancellation commits after the service has formed/settled a `complete` result but before `terminal()`, that method does not read `cancel_requested` or live subscribers and writes `result.status` unchanged (`src/idea_web/jobs/worker.py:2162-2192`), then closes the payer under that status (`src/idea_web/jobs/worker.py:2212-2216`). The run becomes `complete`, implying 100% credit settlement, although the last subscriber detached first; §3.5 requires `cancelled` and 0% credits (`docs/PLAN-v2.md:377-384`).

- Frozen/protected scope is clean: no `src/id_detector/**`, profiles, `README.md`, `idea.cmd`, playlist, or theme changes.

## B. Correctness bugs

- **P0 — crash after last detach can leave no authoritative settlement.** A replacement worker seeing `cancel_requested` takes `_cancel_before_start` (`src/idea_web/jobs/worker.py:2459-2461`). That path backfills/folds and calls `JobQueue.terminal()` (`src/idea_web/jobs/worker.py:3157-3167`), but `terminal()` never inserts `run_settlements` (`src/idea_web/jobs/worker.py:2171-2216`). The base worker has no settlement sweep. Thus a crash between detach and service settlement can close subscriptions and terminalize the run while the single settlement authority has no row.

- **P1 — dead-letter during drain strands subscribers in `waiting`.** `_abandon_run()` terminalizes the run and closes reservations but never mirrors attached jobs (`src/idea_web/jobs/worker.py:1562-1569`); mirroring occurs only in the normal terminal path (`src/idea_web/jobs/worker.py:2212-2216`). A draining worker exits before another reconciliation pass (`src/idea_web/jobs/worker.py:2446-2449`), leaving subscriber pages waiting until another worker starts.

- Concurrent detach serialization and earliest-live-subscriber selection otherwise look sound: all relevant writes occur under one `BEGIN IMMEDIATE`, and `_LIVE_SUBSCRIBERS_SQL` excludes detached/released/terminal subscribers.

## C. Scope

- **P1 — cycle exceeds the build-contract size limit.** The plan requires splitting a cycle above approximately 1,500 changed lines (`docs/PLAN-v2.md:8-9`). This change is approximately 1,687 implementation/test/migration lines excluding the 296-line report, or 1,983 including it.

- No second-session-owned files were touched. Migration 0005 is correctly numbered, uses the existing migration path, and its down script refuses populated subscriber/payer/audit tables.

- The opaque `user_id` and reservation-row model does not create accounts, lots, or tenant-credit tables; that seam is appropriately limited for this phase.

## D. Tests

- The named gate coverage exists: 19 functions yielding 25 cases, offline, with barriers rather than sleeps. Existing assertions were not weakened or deleted.

- **P0 coverage gap:** the last-detach test manually settles and returns `cancelled` (`tests/idea_web/test_coalescing.py:652-658`), so it cannot expose either the terminal-status race or crash-before-settlement bug.

- **P0 coverage gap:** the payer-transfer test creates the USD reservation before subscribers attach and cancellation occurs (`tests/idea_web/test_coalescing.py:424-431`), missing transfer-before-reservation.

- **P1 coverage gap:** `test_private_scope_never_coalesces` submits private requests sequentially (`tests/idea_web/test_coalescing.py:864-871`); the explicitly required two-submission race is not tested.

- Exact `uv run …` gates could not execute because the sandbox cannot launch the WinGet `uv.exe` link. Direct pytest also aborted before collection because no writable temporary directory exists. Direct `.venv` checks did run: Ruff passed, and fixture audit passed over 514 files.

## E. Local mode

`uv run idea serve --no-open --port 8791` could not launch through `uv`. Direct `.venv/Scripts/idea.exe` reached local initialization but failed before binding because the read-only sandbox denied creation of `work/`. Therefore `/` could not be runtime-verified. No serve entry point, headers, `idea.cmd`, or theme files changed.

## F. Quality

- Direct Ruff and `git diff --check` passed.
- The new tests are generally strong, but their controlled service callbacks bypass precisely the cancellation/recovery transitions that carry the highest financial risk.
- `_mirror_attached` and `reconcile_attached` duplicate result-mirroring logic, contributing to the dead-letter path omission.

## Required fixes

### Realistic

1. **P0:** Make USD attribution/cap handling correct when payer transfer occurs before reservation creation, and add a barrier-based regression test for that ordering.
2. **P0:** In the terminal transaction, make a committed last detach win over a concurrent successful result and reconcile `analysis_runs`, `run_settlements`, payer-close status, and credit semantics to `cancelled`.
3. **P0:** Add base-worker crash recovery that creates exactly one `run_settlements` row from durable dispatch/attempt state after last detach, with a crash-and-reclaim regression test.
4. **P1:** Mirror attached jobs when `_abandon_run()` terminalizes a run so draining cannot leave subscribers waiting.
5. **P1:** Add a real barrier-based race test for two simultaneous private submissions.
6. **P1:** Split or reduce this cycle to comply with the approximately 1,500-line build-contract limit.
7. **P2:** Re-run every requested `uv` gate and the local `/` smoke test in a writable environment.

### Adversarial

None; hostile direct database/API forgery was not treated as release-blocking.

VERDICT: FIX_FIRST