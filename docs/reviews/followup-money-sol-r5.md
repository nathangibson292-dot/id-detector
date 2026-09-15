## A — Audit of the fixer's verdicts

This was a static, read-only review. I did not run `uv`, pytest, the pipeline, a server, GC, or any provider call.

- Every original 4a-i and 4b-i finding is marked reproduced; none was dismissed as “not reproducible” or “already fixed.” The reported fixes for ambiguous attempts, ordinals, exact attempt prices, compatible-result recovery, retries, degradation, checkpoints, lease fences, duplicate verification, Shazam recovery, and hosted presentation are present.
- Hosted AudD dispatch follows `run_paid_clip_recognition → AttemptJournal.dispatched → SQLiteAttemptJournal._persist`: fence, `run_dispatches`, and `provider_attempt_events` commit together before JSONL and network I/O.
- Supervised-local AudD dispatch uses the same `DispatchAdmission`: `run_dispatches` commits before JSONL and network I/O. A committed row missing its JSONL projection is folded as ambiguous/spent.
- Direct CLI analysis remains JSONL-based and is outside the hosted/supervised-local SQLite authority. The legacy `providers.audd.execute_job` path is reachable from development/benchmark code, not the service or queue workers.
- The attempt/resume implementation is genuinely shared through `id_detector.run_ledger`. Dispatch admission and invocation settlement are also shared classes. Reservation storage, however, is still split; see B1.
- Normal `SettlementLedger.settle` upserts keep reservation/spend maxima, and the primary key prevents duplicate database rows. Not every terminal path uses it, and legacy adoption bypasses those guarantees.
- The local sweep covers all listed terminal states, including `complete`, and retries `SettlementMiss`. The hosted worker has no corresponding sweep.
- The overlapping-window heuristic alone is P2: it can misattribute `job_id`, but the unique `run_id` row prevents a second database settlement. Separate legacy-adoption defects can still under-settle or leave duplicate projection lines; see B3.
- Job-driven runner and pipeline paths now fail closed without a run ID. All remaining code-generated service run IDs use `new_run_id()`; migration 0002’s SQL backfill is the documented exception. Adding run IDs to the five duck-typed test contexts was necessary, not a weakening.
- Migration application is transactional through `Database.migrate`, schema version is 3, and upgrading from 0001 applies 0002 then 0003. The migration shape is valid, but its populated downgrade is unsafe; see B4.
- A missing settlement projection is not always brief as claimed; see B5.

## B — Correctness bugs in the fix

1. **P0 — Supervised-local reservation is neither SQLite-authoritative nor fenced.**  
   `src/id_detector/attempts.py:131-144` reads/writes only the reservation sidecar, while `src/idea_web/jobs/local.py:585-599` attaches SQLite admission only to dispatch and settlement.  
   Scenario: worker A pauses past lease expiry immediately before persisting its reservation; worker B reclaims under changed pricing/caps. A can still write its unfenced stale sidecar, which B trusts and uses for new paid dispatches. Alternatively, if the sidecar is unavailable while `run_dispatches` survives, `src/id_detector/pipeline.py:789-824` recomputes the reservation from current configuration. This violates both the fence and the single-authority design and can authorize spend beyond the replacement’s effective cap. Hosted recovery also incorrectly prefers the sidecar over SQLite at `src/idea_web/jobs/worker.py:860-865`.

2. **P0 — Hosted cancellation and dead-letter paths can terminate paid runs without a `run_settlements` row.**  
   `src/idea_web/jobs/worker.py:2268-2278` backfills events and calls `JobQueue.terminal` directly; `JobQueue.terminal` only folds and updates `analysis_runs/jobs` at `:1555-1617`. Dead-lettering similarly calls `_abandon_run` at `:1527-1535`, which only updates `analysis_runs` at `:1121-1129`. The hosted queue is constructed without an abandonment settlement callback at `:1731`, and its run loop has no settlement sweep at `:1745-1765`.  
   Scenario: a hosted worker dies after paid dispatches but before `primary`; the replacement observes cancellation, or the third claim dead-letters it. Spend is raised on `analysis_runs`, but no authoritative terminal settlement or invocation projection is ever created.

3. **P0 — Pre-upgrade JSONL adoption can freeze an older, lower settlement and preserve duplicate lines.**  
   `src/id_detector/service.py:563-568` returns the first matching invocation line. `src/idea_web/jobs/local.py:452-455` adopts it without folding later lines or durable attempts, and `SettlementLedger.adopt` stores it unchanged at `src/idea_web/jobs/worker.py:306-327`. `reproject` then treats any matching line as sufficient at `:329-341`.  
   Scenario: the documented pre-fix cancel/resume behavior left `cancelled` then `complete` lines for one run, with increasing spend. Upgrade adopts the first, lower line as SQLite authority, leaves both JSONL lines in place, and never corrects either. The row under-reports spend while readers that sum the old projection can still double-count it.

4. **P0 — Downgrading populated 0003 can erase the only dispatch evidence and permit re-billing.**  
   `src/idea_web/migrations/0003_money_authority.down.sql:1-2` unconditionally drops both authoritative tables.  
   Scenario: `run_dispatches` committed, the process died before its JSONL projection, and an operator rolls back to schema 0002. The down migration deletes the only durable dispatched record; the older recovery sees no dispatch and may send the clip again. The same issue loses a settlement row whose projection was pending.

5. **P1 — Zero-money terminal paths and hosted projection recovery are incomplete.**  
   The compatible-result branch writes a settlement only when recovered money or an existing JSONL line is present (`src/id_detector/pipeline.py:677-698`). `settle_interrupted_run` similarly declines to write when money is zero (`src/id_detector/service.py:618-637`).  
   Consequences include supervised-local cancellation before analysis and fresh compatible returns having no `run_settlements` row. More importantly, if a hosted free run crashes after its settlement row commits but before projection, its compatible resume has zero recovered money and no line, so it never invokes `SettlementLedger`; because the hosted worker has no sweep, the supposedly “brief” missing projection can be permanent.

## C — Regressions

- No static regression to 4a-ii was found: `idea serve` still supervises a separate worker process, while the web-side `LocalJobs` implementation only manipulates the queue.
- No FastAPI/Starlette/uvicorn dependency was introduced into the pipeline or job worker packages.
- GET/write separation and bounded request-body code are outside the diff.
- `idea.cmd` is untouched, preserving the double-click entry point.
- Hosted execution still suppresses `present/current` and the local index.
- `retention.py` and `tests/test_phase2b_retention.py` are untouched.

## D — Test quality

- The original retro regression tests are load-bearing by inspection: reverting the corresponding ambiguity, ordinal, pricing, retry, compatibility, fencing, duplicate, durability, breaker, or presentation changes would violate their exact assertions.
- The Round 5 admission test proves committed-before-return behavior. Its “inside transaction” phase always races cancellation, even for the `reclaim` parameter, though the earlier phases do exercise reclamation.
- The price-change tests retain the JSON reservation sidecar, so they pass while supervised-local SQLite reservation authority and reservation fencing remain absent.
- Hosted cancellation/dead-letter tests assert only `analysis_runs` money; they never require a `run_settlements` row or projection.
- The pre-upgrade test starts with no invocation line and one run, so it cannot expose first-line adoption, historical duplicate lines, or monotonic reconciliation against the attempt ledger.
- The complete-run projection test is local and paid; it does not cover the zero-money hosted compatible-resume case.
- Migration tests downgrade only an empty authority and do not model a populated `run_dispatches` row whose projection is missing.
- The five newly supplied test run IDs preserve existing assertions and correctly model real jobs.
- I could not execute the reported 1,350-test gate in this read-only sandbox.

## E — Scope

- No dependency or packaging file changed.
- Nothing under `data/` or `work/` appears in status.
- `src/id_detector/playlists/**`, `tests/test_playlists.py`, `README.md`, `idea.cmd`, `src/id_detector/present/theme.py`, and `tests/test_phase0a_security.py` are untouched.
- `src/id_detector/retention.py` and `tests/test_phase2b_retention.py` are untouched.
- No FastAPI, Starlette, or uvicorn import was added beneath `src/id_detector/` or `src/idea_web/jobs/`.
- All untracked implementation, migration, test, and review files were inspected.
- `git diff --check` reported no whitespace errors.

## Required fixes

### Realistic

1. **[P0]** Add an SQLite-authoritative, unique reservation record for supervised-local runs; write it under the active job/lease/run fence before dispatch, read SQLite before any sidecar, and make the sidecar projection-only.
2. **[P0]** Route hosted cancel-before-start, dead-letter, quarantine, and expiry through `SettlementLedger`, with a periodic hosted backstop that creates/reprojects every missing terminal settlement.
3. **[P0]** Replace legacy first-line adoption with validated reconciliation: read every line for the run, fold durable attempts/reservation, take monetary maxima, insert that canonical row, and rewrite the projection to exactly one matching line.
4. **[P0]** Make 0003 downgrade fail closed when authority rows exist unless all dispatch and settlement projections have first been durably materialized and verified.
5. **[P1]** Create a settlement row for every job-owned terminal run, including zero-money cancellation and compatible returns, and always reproject an existing row when its line is missing or differs from the row.
6. **[P1]** Add regressions for the four cases above: fenced local reservation with changed pricing, hosted paid cancel/dead-letter rows, legacy duplicate/lower settlement adoption, and populated-0003 downgrade with missing projections.

### Adversarial

None. The two owner-held adversarial risks were not widened and are not re-raised.

VERDICT: FIX_FIRST