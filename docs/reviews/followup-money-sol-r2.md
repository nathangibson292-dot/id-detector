## A. Audit of the fixer’s verdicts

All paths below are relative to the specified worktree.

| # | First-round finding | Round-2 verification |
|---|---|---|
| P0-1 | Supervised local restart loses run/money | **Partial.** `run_id` is minted in `src/idea_web/jobs/local.py:191-210`, restored at `:401-430`, and consumed by `src/id_detector/webapp/runner.py:293`. Tests: `tests/idea_web/test_followup_review_fixes.py:129`, `:141`, `:153`. Reverting run reuse makes the first two fail at the cross-pass run/reservation assertions; reverting interrupted settlement removes the invocation expected at `:167`. Repeated-death and early-cancellation paths still lose settlement; see B. |
| P0-2 | Attached/corrupt job can seize another driver’s run | **Fixed for newly written rows.** Durable attachment is added by `0002_job_ownership.up.sql:5-7`; claims exclude it at `worker.py:952-955`; rotation is guarded at `:995-1016`; abandonment checks for another live driver at `:908-924`. Tests: `test_followup_review_fixes.py:198-225`. Reverting progress-independent ownership or the rotation guard makes the run-token assertions fail. The tests do not pin migration backfill behavior. |
| P1-3 | Shazam ambiguity/query identity | **Open.** Hosted `query_id` is now supplied at `recognise.py:305-310` and recorded at `worker.py:728-743`; `test_followup_review_fixes.py:231-249` would fail if that part were reverted. However `test_followup_money_resume.py:654-676` is local-only and passed before the ledger fix. It creates `submission_started`, reaches the send hook, receives no raw answer, and resumes with the breaker closed—but it neither creates a local Shazam attempt ledger nor exercises queue recovery. |
| P1-4 | Intake checkpoints suppress fresh refresh | **Fixed.** `pipeline.py:835-848` distinguishes intake-only checkpoints from same-run evidence. Test: `test_followup_money_resume.py:609-634`; reverting to “any completed phase” makes the fresh Shazam request count remain zero. |
| P1-5 | Unhashed legacy checkpoints trusted | **Fixed.** `service.py:276-290` rejects entries without `artefact_records`; paid rebuilding still consults `RunLedger.resume` at `paid_clip.py:551-577`. Test: `test_followup_money_resume.py:504-522`; restoring existence-only trust makes `decode` appear complete. |
| P1-6 | Injected admitter differs from durable reservation | **Fixed.** Full reservation equality is enforced at `run_ledger.py:518-535`. Test: `test_followup_money_resume.py:525-543`; reverting to unit-price-only validation makes the expected `failed` result and zero-call assertion fail. |
| P1-7 | Duplicate ordinal/egress omitted | **Partial.** Shared comparison is at `run_ledger.py:93-109`; SQLite verification is at `worker.py:508-529`. Tests: `test_followup_money_resume.py:589-606` and `test_followup_review_fixes.py:255-287`; each fails if its field check is reverted. The writer still constructs separate JSONL and SQLite payloads; see B/D. |
| P1-8 | Zero-money compatible resume retains cancelled terminal | **Fixed.** `pipeline.py:662-681` settles when an invocation exists even if recovered money is zero. Test: `test_followup_money_resume.py:546-586`; reverting to `recovered.any` alone leaves `cancelled`. |
| P1-9 | Ambiguity test not load-bearing/missing paths | **Partial.** The strengthened test at `test_followup_money_resume.py:151-197` asserts exact query identities and would fail if the ambiguous skip at `paid_clip.py:554` were reverted. The paid-process test manually expires the lease and invokes `LocalWorker`; it does not drive `LocalWorkerSupervisor`, and there is still no hosted Shazam-ambiguity recovery test. |

The fixer’s “Shazam resend not reproducible” verdict is too broad. The current per-media Shazam job store prevents resend while it survives, but the claimed durable ledger recovery is neither implemented nor tested.

## B. Correctness bugs in the fix

- **[P0] Supervised-local dead-letter and fast-cancellation paths can lose real spend.** `LocalJobs.submit` persists the service run only inside the progress snapshot and does not pass it to `JobQueue.enqueue` (`src/idea_web/jobs/local.py:191-210`), leaving `jobs.run_id=NULL`. After three worker-process deaths, `JobQueue.claim` quarantines the row before `LocalWorker` sees it (`src/idea_web/jobs/worker.py:959-960`); `_quarantine` then calls `_abandon_run` with that NULL (`:938-945`), which returns without settlement (`:906-907`). Three paid ambiguous dispatches can therefore end in `dead_letter` with no terminal invocation. Separately, if the first worker dies before the 0.5-second publisher persists `started_at`, `_run_money` refuses recovery at `local.py:465-469`; cancellation then reports zero despite the durable attempt ledger.

- **[P0] Production accepts the new fake-provider inputs without test-mode authorization.** `make_pipeline_runner` accepts `paid_scan_adapters`, `shazam_http_client`, `paid_sleep`, and arbitrary `configure` at `src/id_detector/webapp/runner.py:212-248`, then injects them at `:283-288`. There is no `IDEA_TEST_MODE` check. The hidden worker `--runner` option is gated at `src/idea_web/jobs/local.py:711-717`, but direct construction of the production runner bypasses that gate and can silently run a real job against substitute providers.

- **[P1] Shazam’s hosted ledger is written but never consumed during resume.** Local `ShazamBreaker.sent` is a no-op (`src/id_detector/shazam_breaker.py:109-115`); hosted rows are written by `worker.py:728-791`, but pipeline recovery folds only the AudD journal. If a power loss preserves `app.db`’s dispatched Shazam row but loses the per-media `jobs.sqlite` namespace entry, `recognise.py:418-453` recreates pending jobs and resends the query. The local path has no Shazam attempt ledger from which to recover at all.

- **[P1] Migration 0002 strands previously cancelled attached jobs.** The migration only backfills `attached` (`0002_job_ownership.up.sql:5-7`). An existing 0001 row with `state='waiting'`, `cancel_requested=1`, and `progress.attached=true` becomes `attached=1`; claims exclude it at `worker.py:952-955`, while reconciliation excludes cancelled requests at `:1047-1051`. It remains waiting forever unless the user cancels it a second time.

- **[P1] JSONL and SQLite are still produced from two event payloads.** `SQLiteAttemptJournal._write` first asks the base writer to construct and append a `ProviderAttemptEvent`, then separately reconstructs the SQLite fields and timestamp (`worker.py:657-675`). This does not satisfy the stipulated single-payload invariant; the ordinal and egress tests exercise synthetic duplicates independently, not the actual dual writer.

## C. Regressions

Static inspection found no new violation of 4a-ii’s web/process boundary: `idea serve` still creates `LocalWorkerSupervisor` in `src/id_detector/cli.py:885-910`; the web application does not import the runner or pipeline; GET/body-limit code is untouched. `idea.cmd` is unchanged.

The fresh-run refresh regression is fixed as described in A. The pre-review PowerShell gates are accepted as passed per the correction, but the final-code gates are being run separately and were not independently executed here.

## D. Test quality

- **[P1]** The Shazam ambiguity guard would still pass with all new Shazam ledger/query-id work reverted. It proves only the older per-media job-store behavior and never removes that store to force ledger recovery.

- **[P1]** No test exercises a paid job through the actual supervisor kill/restart loop. The new test launches one worker process, manually expires its lease, then calls `LocalWorker.run_once()` in-process.

- **[P1]** No populated-0001 migration test covers in-flight, breaker-waiting, attached-cancelled, terminal, and dead-letter rows, nor proves backfill and down migration together. A small in-memory stdlib SQLite probe confirmed that malformed progress survives, ordinary attachment backfills, and `down` removes the column; it is not the required populated-app or crash test. `Database.migrate` does at least wrap 0002 in explicit `BEGIN IMMEDIATE`/`COMMIT` at `src/idea_web/database.py:185-203`.

- **[P1]** The “reservation already filled” fusion fix is not load-bearing. The corrected branch at `pipeline.py:960-994` does run the sweep, admits no further network request, and `_run_status` returns honest `partial` at `:190-217`; settlement remains monotonic through `_settle` at `:464`. But `test_an_injected_admitter_must_hold_the_durable_reservation` exits earlier on a mismatched reservation, so reverting this fusion fix would not fail that test.

- The exact AudD ambiguity, reservation/price, refresh, legacy-checkpoint, injected-admitter, and zero-money-compatible tests are otherwise load-bearing as detailed in A.

I did not run `uv`, pytest, a server, the pipeline, GC, or any provider call; runtime conclusions are inspection-only.

## E. Scope

- Status contains 19 modified tracked files and seven reviewed untracked files. `git diff --check` reports no whitespace errors.

- No dependency or lockfile change; no data/work content; no FastAPI/Starlette/uvicorn import under `src/id_detector/` or `src/idea_web/jobs/`.

- All prohibited playlist, retention, truth, security, `README.md`, `idea.cmd`, and theme files are untouched. `src/id_detector/cli.py` has only the two-line failed-result message change.

- Existing test edits are necessary consequences of failed results becoming `RunResult` and durable reservations surviving cap changes; I found no weakened existing assertion.

## Required fixes

1. **[P0]** Persist the supervised job’s service `run_id` in normalized queue state and settle its shared ledger on max-attempt quarantine/dead-letter and cancellation even when `started_at` was never snapshotted; add repeated hard-kill and pre-first-flush cancellation tests.

2. **[P0]** Reject every provider/config/sleep override to `make_pipeline_runner` unless `IDEA_TEST_MODE=1`, with a regression proving production construction cannot enable them.

3. **[P1]** Journal local Shazam attempts with `query_id` and consume local/SQLite Shazam ledgers on resume; test dispatched-without-resolved recovery locally and through `Worker`, including loss of the per-media job store.

4. **[P1]** Make migration 0002 terminally detach pre-existing cancelled subscribers and add a populated-0001 up/down/crash-safety migration test covering every relevant job state.

5. **[P1]** Construct one immutable attempt-event object and use it for both JSONL append and SQLite projection, including ordinal and egress verification in actual-writer tests.

6. **[P1]** Add an end-to-end paid `LocalWorkerSupervisor` kill/restart test and a separate matching-reservation/full-spend test that fails if the fusion-resume branch is reverted.

VERDICT: FIX_FIRST