## A — Audit of the fixer’s verdicts

- **Round-6 #1 — closed.** When media is unavailable, `_settle_run` folds paid `run_dispatches` and the persisted `run_reservations` row, treating unresolved dispatches as spent. It creates one pathless settlement; `attach` later merges money monotonically and projects exactly one invocation line. This also works without a `primary` checkpoint.
- **Round-6 #2 — partially closed.** Normal claims made by unaware code cannot retain `money_authority=3`: the trigger changes it permanently to `0`, and later current-code claims preserve `0`. The timestamp heuristic is gone. Supported `idea serve` migration correctly refuses while the supervisor lock is held. However, the claim that an old worker necessarily exits against schema 3 is false; see B1.
- **Round-6 #3 — closed.** Settlement misses remain scheduled indefinitely with exponential backoff capped at 1,800 seconds. A new worker starts with an empty retry schedule and retries immediately.
- **Round-6 #4 — closed for the required supervised-local path.** `AttemptJournal.for_provider("shazam")` inherits `DispatchAdmission`; each Shazam dispatch inserts its unique fenced row before network I/O. Stale, duplicate and canceled dispatches are refused, and `recognise` converts refusal into cancellation. Paid folds filter out Shazam rows.
- **Round-6 #5 — closed.** Hosted paid recipes are rejected immediately after claim and before intake, run creation, reservation, attempt recording or service execution. The matrix covers analysis resume, waiting retry, legacy/demarked rows and paid engine names under a zero cap.
- **Round-6 #6 — only partly closed.** Paid dispatch now requires exact job/run ownership and a durable reservation in the admission transaction. Settlement rejects a different non-null owner run, but still accepts a missing owner or a job whose `run_id` is null; see B2.
- **Round-6 #7 — misclassified.** Migration outside `idea serve` is not merely a delayed-settlement residual: it can leave old paid execution running without the new authority and resume protections.
- The record’s earlier “Shazam resend not reproducible” verdict was explicitly withdrawn in round 2. The present shared ledger does suppress an unresolved Shazam dispatch even after loss of the per-media job database. No other retro finding was marked “not reproducible” or “already fixed.”
- Reservation recovery uses the stored per-run record, including its original unit price and cap calculation. The shared `run_ledger.py` fold drives both service and queue recovery; compatible-result, cancellation, dead-letter, reclamation and open-breaker paths preserve cumulative money and ambiguous attempts.
- Migration from populated 0001 is covered and inspection shows 0002→0003 is additive. The 0003 down script aborts if any dispatch, reservation or settlement authority table contains a row.

## B — Correctness bugs

1. **P0 — migration exclusion is bypassable, and the old-worker guard cannot protect old code.**  
   [`local.py:165`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/local.py:165>) acquires the supervisor lock only through `local_database`, while [`database.py:164`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/database.py:164>) allows direct migration without it. An already-held old claim survives the migration untouched; a later old claim is merely marked unproven by [`0003_money_authority.up.sql:51`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/migrations/0003_money_authority.up.sql:51>) and is not rejected. The schema check at [`worker.py:1453`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:1453>) exists only in the new worker; an already-running previous-version worker cannot execute it.

   Concrete failure: an old `idea serve` owns the supervisor lock; another process invokes `Database.migrate()` directly; the old worker retains or obtains a claim and dispatches paid requests without `run_reservations`/`run_dispatches`. If it dies and its old supervisor restarts old code, the pre-fix fresh-run behavior can re-bill. If media and its JSONL disappear, new recovery cannot settle the spend. The record’s “worst case is a waiting settlement” understates this P0 class.

2. **P2 — settlement ownership is not exact for missing/null owners.**  
   [`worker.py:399`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:399>) rejects only when the job exists and has a different non-null `run_id`. A writer with a nonexistent `job_id`, or a real legacy job with `run_id=NULL`, may settle any entry. The latter is reachable through unidentified pre-upgrade recovery and can associate an overlapping neighboring run with the wrong job. The run remains uniquely settled, so this is attribution/fence weakness rather than a re-bill.

## C — Regressions

- No functional regression found in 4a-ii: `idea serve` still starts and supervises a separate `LocalWorker` process; the HTTP process does not run the pipeline. HTTP routes/body readers were untouched, so GET-write and bounded-body behavior are unchanged. `idea.cmd` is untouched.
- **P2 documentation regression:** [`worker.py:388`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:388>) still says a pathless settlement “provably spent nothing.” Round 7 deliberately creates pathless settlements containing paid spend. This contradicts the record’s claim that stale comments were fixed.
- No regression found in ambiguous-attempt handling, exact reservation restoration, cumulative settlement, lease fencing, JSONL/SQLite recovery or checkpoint artefact validation.

## D — Test quality

I could not run `uv`, pytest, browser/PowerShell gates or live-process tests in this read-only sandbox. I performed static test and reversion-path analysis only; no provider or `idea gc` command was invoked.

The round-7 tests are load-bearing for their implemented branches:

- Removing the SQLite authority fold leaves R1 without a settlement.
- Removing the trigger/stamp lets the simulated old claim retain `3` and receive a false zero row.
- Removing `local_database` locking allows R3’s migration.
- Restoring permanent miss suppression prevents R5’s eventual recovery.
- Dropping admission propagation lets R6’s stale, duplicate and canceled Shazam dispatches pass.
- Removing hosted refusal reaches the guarded intake/service callbacks.
- Removing dispatch ownership/reservation checks or the current settlement mismatch check makes R9/R10 insert rows.

However:

- **P1 test gap:** [`test_followup_round7.py:185`](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/tests/idea_web/test_followup_round7.py:185>) runs current code against artificial schema 99. It proves a v3 worker rejects a future schema, not that a pre-v3 worker exits after migration to v3. The simulated old claim test updates one row but never lets old execution reach paid dispatch. Both tests pass while B1 remains exploitable.
- The settlement-binding test covers a foreign run owned by an existing non-null job, but not the missing-owner or null-owner cases accepted by the implementation.
- Earlier core regressions remain meaningfully branch-sensitive: reverting ambiguous classification re-sends the unresolved clip; removing reservation authority re-prices under current configuration; bypassing early recovery zeros compatible-result spend; removing invocation replacement creates duplicate terminal lines; removing SQLite merge loses deleted-JSONL spend.

## E — Scope

- `git diff --check` reported no whitespace errors.
- No new dependency or dependency-file change.
- No FastAPI, Starlette or uvicorn import was added under `src/id_detector/` or `src/idea_web/jobs/`.
- Nothing under `data/` or `work/`.
- Playlists, `tests/test_playlists.py`, `README.md`, `idea.cmd`, `theme.py`, `tests/test_phase0a_security.py`, `retention.py`, and `tests/test_phase2b_retention.py` are untouched.
- I read all untracked files listed by `git status --short`; no existing assertion appears weakened. Existing test adjustments align with the new failure-as-`RunResult`, durable run-id and hosted-refusal contracts.

## Required fixes

### Realistic

1. **P0:** Make migration/worker exclusion non-bypassable for the local database: direct migration must acquire the same supervisor lock or otherwise atomically refuse while an old supervisor or unexpired claim can exist. Add a mixed-version process regression using the previous worker implementation and prove it cannot claim or reach paid dispatch after upgrade.
2. **P2:** Require `SettlementLedger` job ownership to exist and equal the settlement run inside the transaction. Put legacy null-run adoption behind a separate, explicit recovery operation with its own validated association.
3. **P2:** Correct the pathless-settlement docstring to state that such a row may contain conservatively accounted paid spend.

### Adversarial

No newly widened adversarial risk. The two owner-held hostile same-user ledger/lock-bypass risks remain unchanged and are not re-raised.

VERDICT: FIX_FIRST