## A — Audit of the fixer’s verdicts

I read the round-2 review, the fixer’s complete record, both retros, background reviews, contract sections, the entire diff, and every untracked file.

1. **Round-2 P0-1 — partially fixed.** New submissions persist `jobs.run_id` at `src/idea_web/jobs/local.py:200-212`; abandonment folds the run at `src/idea_web/jobs/worker.py:908-966`; cancellation no longer depends on `started_at` at `src/idea_web/jobs/local.py:482-496`. Tests at `tests/idea_web/test_followup_round2.py:118`, `:128`, and `:147` would fail if those changes were reverted. However, pre-upgrade rows and post-commit settlement crashes remain open P0s below.

2. **Round-2 P0-2 — fixed.** Every override is rejected outside test mode at `src/id_detector/webapp/runner.py:210-243`; the parameterized test at `tests/idea_web/test_followup_round2.py:92-112` would fail immediately without that guard. The only production construction found is `src/idea_web/jobs/local.py:737-740`, with no overrides; custom runner loading is independently test-mode-gated at `local.py:730-736`.

3. **Round-2 P1-3 — core resend fix is load-bearing.** Local Shazam recovery uses the run journal at `src/id_detector/recognise.py:455-473`; hosted recovery consumes SQLite Shazam events at `src/id_detector/pipeline.py:850-854`. The local test really deletes every `jobs.sqlite*` at `tests/test_followup_money_resume.py:669-674`; the Worker test deletes both the job store and local Shazam journal at `tests/idea_web/test_followup_round2.py:249-258`. Reverting either ledger-consumption path would resend the killed query. The earlier “not reproducible” verdict was correctly withdrawn. The broader single-event implementation remains incomplete, however.

4. **Round-2 P1-4 — fixed for the stated migration defect.** Migration 0002 terminally detaches active attached-cancelled rows at `src/idea_web/migrations/0002_job_ownership.up.sql:9-18`. The populated-0001 test at `tests/idea_web/test_followup_round2.py:267-366` covers all requested row classes, logical transactional rollback, up migration, and down migration; removing the detach update leaves the cancelled subscriber waiting and fails line 356.

5. **Round-2 P1-5 — fixed for `SQLiteAttemptJournal`, not globally.** One `ProviderAttemptEvent` is built at `src/id_detector/attempts.py:124-135` and passed to the projection and JSONL append at `src/idea_web/jobs/worker.py:665-689`. Tests at `tests/idea_web/test_followup_round2.py:372-454` and `tests/idea_web/test_followup_review_fixes.py:255-287` are load-bearing for timestamp identity, durable duplicate refusal, ordinal validation, and egress conflicts. Shazam still creates a second independent event, described in B5.

6. **Round-2 P1-6 — fixed and load-bearing.** `tests/idea_web/test_followup_round2.py:174-213` uses the real `LocalWorkerSupervisor`, kills its subprocess, observes restart and lease reclamation, and checks unique dispatches and exact money. `tests/test_followup_money_resume.py:685-724` covers matching reservation/full spend and would return `failed` instead of `partial` if the fusion-resume branch were reverted. The fixer’s recorded targeted reversion failures are consistent with the test logic.

No original retro finding is currently dismissed as “not reproducible” or “already fixed”; the one earlier Shazam dismissal has been withdrawn. Static reinspection also confirms the previously verified same-run refresh, legacy checkpoint rebuild, full reservation equality, zero-money compatible replacement, AudD ambiguity handling, exact reservation restoration, and cumulative settlement logic remain present.

## B — Correctness bugs in the fix

1. **P0 [realistic] — upgraded local jobs can still lose terminal settlement.** Migration 0002 only adds/backfills `attached` (`0002_job_ownership.up.sql:4-18`); it does not backfill `jobs.run_id`. On first claim, an old row’s generated run ID is written only into progress JSON at `src/idea_web/jobs/local.py:406-415`. After three ordinary crash-loop deaths, quarantine reads the still-NULL normalized column at `src/idea_web/jobs/worker.py:1016-1018`; `_remember_abandoned` declines settlement at `worker.py:968-970`. Real AudD spend remains in the ledger without the required terminal settlement. The new-row test does not cover this live-0001 upgrade case.

2. **P0 [realistic] — dead-letter settlement has no durable retry.** The dead-letter/quarantine transaction commits before `_flush_abandoned` (`src/idea_web/jobs/worker.py:982-1003`, `:1232-1245`), and all callback failures are suppressed. A crash or power loss in that gap leaves the terminal job with no invocation settlement. Startup only cancels active work (`src/idea_web/jobs/local.py:623-627`), while dead-letter expiry merely changes database state (`worker.py:1404-1425`). The ledger is manually recoverable if its media and run ID remain, but nothing invokes that recovery on the next start; the spend is therefore not automatically reported.

3. **P0 [realistic] — cancellation does not fence dispatch admission.** A claimed job’s cancellation only sets `cancel_requested=1` (`src/idea_web/jobs/worker.py:1193-1199`), but `_RUN_FENCE_SQL` does not require `cancel_requested=0` (`worker.py:117-136`). Thus cancellation can commit after `_halt_requested()` at `src/id_detector/paid_clip.py:597-599`, yet a subsequent AudD `dispatched` event and provider request remain admissible at `paid_clip.py:498-513`. The supervised local path is wider: it relays cancellation only every progress interval (`src/idea_web/jobs/local.py:311-335`, explicitly described at `:243-245`) and uses an unfenced `LocalCheckpointStore`/plain journal (`src/id_detector/webapp/runner.py:289-328`). A quick ordinary browser cancellation can spend another real AudD unit.

4. **P1 [realistic] — the hosted invocation settlement itself is outside the claim fence.** Pipeline terminal branches directly replace `invocations.jsonl`, for example `src/id_detector/pipeline.py:1642`, before `Worker` reaches the fenced database terminal transition at `src/idea_web/jobs/worker.py:1937-1956`. A worker suspended past lease expiry can resume, write a terminal settlement, and only then discover that `queue.terminal` rejects its stale claim. This violates the explicit terminal-settlement fencing requirement and can temporarily overwrite the current run’s status.

5. **P1 [realistic] — Shazam still has two event identities and only partially consumes SQLite recovery.** `recognise` creates the deterministic JSONL attempt at `src/id_detector/recognise.py:324-347`; `LedgerShazamBreaker` independently creates a UUID SQLite attempt at `src/idea_web/jobs/worker.py:770-805`. They are not one immutable object. Moreover, pipeline imports only SQLite `.ambiguous_query_ids` (`pipeline.py:850-854`), not resolved terminal/reusable states. Under the same lost-job-store recovery condition used by the tests, loss of the local journal means a resolved terminal SQLite Shazam attempt can be treated as fresh and sent again. Shazam is free, so this is P1 rather than P0.

### Residual-risk classification

1. **Old local rows:** realistic, P0, unacceptable. A guarded `json_extract(progress, '$.local.run_id')` migration/startup backfill is cheap; minting must update both normalized state and snapshot atomically.

2. **Best-effort settlement:** realistic, P0, unacceptable. The ledger is recoverable manually, but no automatic next-start sweep exists.

3. **Pre-fix hosted Shazam rows without `query_id`:** `[adversarial]`, acceptable P2 for the current owner deployment. Since hosted mode was never deployed, it requires fabricated/imported legacy hosted state plus loss of the per-media store. No AudD money is involved.

4. **No Windows directory fsync:** realistic but acceptable P2 platform limitation. `MoveFileExW(...WRITE_THROUGH)` plus file flush/fsync at `src/id_detector/io.py:101-117` is an honestly bounded Windows durability mechanism.

5. **Out-of-pipeline recipe defaults to Free:** realistic, acceptable P2 for this cycle. Exact money is preserved, but `src/id_detector/service.py:583-597` produces inaccurate recipe metadata.

6. **Pre-first-flush test reconstructs the snapshot:** realistic test limitation, acceptable P2. It uses a real child death and reconstructs the relevant durable state, although it does not time the race itself.

### Adversarial residual risks

- **P0 `[adversarial, money]`:** Two hostile owner processes that deliberately bypass the media lock can race the cached check at `src/id_detector/attempts.py:57-78`, append identical deterministic attempt IDs, and dispatch twice while the fold deduplicates the charge. Preconditions: deliberately ignoring the application lock and concurrent access to one run.

- **P0 `[adversarial, money]`:** An owner-level hostile process can delete or coherently alter both the JSONL attempt ledger and SQLite projection/reservation before resume, causing recorded spend to disappear and paid work to be resent. Preventing this requires an external or tamper-evident authority; it needs an explicit owner decision under the stated money exception.

## C — Regressions

No 4a-ii regression found. `idea serve` constructs `LocalWorkerSupervisor` and the HTTP server separately at `src/id_detector/cli.py:884-910`; the worker subprocess owns pipeline execution. GET-facing `LocalJobs.get/recent` use read transactions at `src/idea_web/jobs/local.py:218-233`, and bounded-body handling remains unchanged. `idea.cmd` is untouched.

The fixer reports 1,341 passes, one skip, and four known Windows-path retention failures. I could not independently run `uv` or pytest in this read-only sandbox. The affected retention test and implementation are untouched, so the recorded failures do not appear introduced by this diff.

## D — Test quality

The six round-2 fixes have meaningful, generally load-bearing tests, including both required per-media-store deletion cases.

Missing regressions correspond directly to B1–B5: populated-0001 local `run_id` upgrade, crash after terminal DB commit, cancellation committing immediately before AudD admission, stale-claim invocation settlement, and one-object/full-state Shazam projection.

The record overstates migration crash safety as “byte-for-byte unchanged.” `tests/idea_web/test_followup_round2.py:332-342` proves schema version and logical row equality after rollback, not byte identity of the SQLite file. That is adequate crash-safety evidence, but the record should say “logically unchanged.”

No existing assertion was weakened: tracked test changes update expected migration count, construct genuinely live fences, or replace obsolete exception expectations with explicit failed-`RunResult` assertions.

## E — Scope

`git diff --check` is clean. No dependency or lockfile changed. No protected retention, jobs, truth, playlist, security, README, `idea.cmd`, theme, `data/`, or `work/` path was touched.

No FastAPI, Starlette, or uvicorn import was added under `src/id_detector/` or `src/idea_web/jobs/`; matches are comments only. The `cli.py` change is the minimal two-line rendering of a failed result’s reason.

## Required fixes

### Realistic

1. **P0:** Backfill `jobs.run_id` from valid local progress during migration/startup, and atomically persist both the normalized column and snapshot when an old row needs a newly minted ID; add a populated-0001 three-hard-kill regression.

2. **P0:** Replace the post-commit best-effort abandonment callback with a durable idempotent outbox or startup sweep for terminal jobs lacking settlement; test a process death immediately after the dead-letter commit.

3. **P0:** Include `cancel_requested=0` in dispatch fencing and give the supervised local runner a queue-aware atomic dispatch-admission guard; test cancellation committed between the last cancellation check and AudD’s callback, asserting no additional request.

4. **P1:** Fence invocation-journal settlement before writing it, using the current job token, active state, and unexpired lease; test lease expiry/reclamation immediately before settlement.

5. **P1:** Give each Shazam request one event identity/object projected to both stores and consume every relevant SQLite resume state, not only unresolved dispatches; add an actual-writer recovery test with the per-media store and local journal absent.

6. **P2:** Correct the record’s “byte-for-byte unchanged” migration claim or add an actual file-level assertion.

### Adversarial

7. **P0:** Put the lock-bypass/double-dispatch and dual-ledger-deletion money risks to the owner; if not explicitly accepted, require cross-process exclusive attempt creation and an external/tamper-evident money ledger.

VERDICT: FIX_FIRST