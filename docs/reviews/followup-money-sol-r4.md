## A — Audit of the fixer’s verdicts

This was a static, read-only review. I read the prescribed records, Round 3 review, complete diff, status, and every untracked file. I could not run `uv` or pytest in the read-only sandbox.

- All original 4a-i findings were marked reproduced. The fixes for ambiguous attempts, fresh ordinals, persisted reservation/unit price, compatible-result settlement, retryable outcomes, cumulative-spend degradation, failure results, secondary restoration, URL controls, and checkpoint validation are present and use the shared `run_ledger` fold.
- All original 4b-i findings were marked reproduced. Namespace durability, SQLite recovery, duplicate verification, malformed-progress handling, attachment cancellation, hosted presentation isolation, lease fencing, pre-primary money folding, artefact validation, bounded backfill, and process-wide Shazam breaker state are present.
- The earlier “Shazam resend not reproducible” verdict was correctly withdrawn once the per-media store-loss case was tested. Current recovery consumes the SQLite attempt states.
- Reservation is restored from durable per-attempt µUSD and reservation records rather than current pricing or caps. The pricing-change and lowered-cap tests directly depend on that behavior.
- The shared monetary implementation is genuine: both service and queue recovery use `src/id_detector/run_ledger.py`; hosted SQLite events and JSONL records are converted to the same event/fold model.
- Hosted settlement has an equivalent guarantee without the local sweep: terminal `analysis_runs` updates and abandonment fold provider events inside the authoritative SQLite transaction.

Round-4 design audit:

1. **Run identity — incomplete.** Local submission and adoption do store `jobs.run_id` and `progress.local.run_id` together, and normal readers no longer fall back to progress. Migration 0002 backfills valid active local snapshots. However, `new_run_id()` is not the only service-run mint; see B4.
2. **Active jobs that may have `NULL run_id`:**
   - A generic `JobQueue.enqueue(..., run_id=None)` row remains active and NULL until hosted intake or local adoption.
   - Migrated active rows with malformed progress or no `$.local` object are not backfilled; local adoption repairs them after claim.
   - New `LocalJobs.submit` rows and valid migrated local rows are non-NULL.
   - Terminal 0001 rows are deliberately excluded and never repaired or swept; see B1.
3. **Dispatch paths:** hosted primary and secondary AudD requests both reach `paid_clip.py:516`, then the hosted `SQLiteAttemptJournal._persist`, whose cancellation/claim check is within its SQLite write transaction. Supervised-local AudD uses the same `DispatchAdmission`, but closes its transaction before the JSONL write. Direct CLI analysis has no driving job, so a queue claim fence is inapplicable.
4. **Settlement fencing:** all eight pipeline outcomes route through `_append_settlement`, and cancellation is allowed by `require_not_cancelled=False`. The fences are nevertheless check-then-act with respect to `invocations.jsonl`; see B3.
5. **Shazam identity — closed.** One journal event object is projected into both stores. `LedgerShazamBreaker` is policy-only, and `recognise.py:468-478` folds all states. The four rewritten tests retain stronger identity, timing, query-ID, and stale-claim assertions; they were not weakened.
6. **Record correction — correct.** The migration claim now says logical schema/row equality, not byte-for-byte database equality.

Residual-risk classification:

- Pre-upgrade stopped jobs without `jobs.run_id`: **P0 realistic** when their paid JSONL contains identifiable run IDs but no settlement. The sweep ignores recoverable real spend.
- Missing Shazam response body after store loss: **P2 realistic**. It can reduce answer quality, but the never-resend bias neither re-bills nor loses paid spend.
- The two owner-held adversarial money risks were not widened.

## B — Correctness bugs in the fix

1. **P0 — Existing stopped jobs with recoverable spend remain permanently unsettled.**  
   `src/idea_web/migrations/0002_job_ownership.up.sql:20-36` backfills only active states. `src/idea_web/jobs/worker.py:1224-1239` then selects settlement candidates only when `jobs.run_id IS NOT NULL`.  
   Scenario: a pre-upgrade local worker dispatched AudD, died, and the job became cancelled/dead-lettered without an invocation settlement. Its attempt JSONL still names the charged run ID, but migration skips the row and every startup sweep skips it. The owner’s spend is never settled. Random per-execution IDs make normalization harder, but do not make the durable ledger’s individual run IDs unrecoverable.

2. **P0 — Ordinary cancellation or lease expiry can still admit a billed local request.**  
   `src/idea_web/jobs/worker.py:188-190` checks and commits the claim transaction. Only afterward does `src/id_detector/attempts.py:167-172` append and fsync `dispatched`; the AudD call follows at `src/id_detector/paid_clip.py:525`.  
   Scenario: the lease has milliseconds remaining. Admission succeeds, the transaction releases, then file creation/fsync crosses the expiry—or another tab commits cancellation—before the dispatched append. The request still leaves. This is an ordinary scheduling/disk-latency race, not an adversarial lock bypass.

3. **P1 — Invocation settlement is not atomically fenced.**  
   `src/id_detector/pipeline.py:468-473` calls a fence and then separately rewrites the journal. Hosted `fence_settlement` at `src/idea_web/jobs/worker.py:473-478` and local `DispatchAdmission.__call__` both release the SQLite transaction first.  
   Scenario: a lease expires or a replacement reclaims immediately after the check. The stale worker can still execute `append_invocation`, including its full-file read and atomic replacement. Monetary maxima prevent reduction, but terminal status/reason and the required stale-write fence are not protected atomically.

4. **P1 — “One run-id mint” is false and job-driven fallbacks still fail open.**  
   Service run IDs are also minted at `src/id_detector/cli.py:697`, `src/id_detector/pipeline.py:441`, `src/id_detector/webapp/jobs.py:572`, `src/id_detector/webapp/runner.py:315`, and `src/idea_web/jobs/worker.py:1845`; migration has another SQL mint.  
   The dangerous cases are the pipeline and runner fallbacks: if a job context loses its ID, they silently create an ID not first bound to that job and snapshot. A crash can therefore cause another fallback ID on restart. The normal LocalWorker wiring avoids this, but the claimed invariant is not enforced by design.

5. **P1 — The derived sweep does not cover every terminal state or prove concurrent idempotence.**  
   `src/idea_web/jobs/local.py:79-84` covers only `dead_letter`, `failed`, `cancelled`, and `provider_unavailable`; a `complete` or other terminal row with spend and a missing line is ignored.  
   Additionally, `src/id_detector/service.py:588-609` performs `has_invocation` and `append_invocation` without a shared lock or database uniqueness claim. Two concurrent sweep/callback paths can both pass the test and rewrite the file. `local.py:419` also permanently marks a run swept even when recovery returned no money after a cache miss.

## C — Regressions

- No static regression to 4a-ii was found: `idea serve` still separates web serving from the supervised worker process; the web process does not execute the pipeline.
- GET/write separation and bounded-body code are outside the diff.
- `idea.cmd` is untouched, so the double-click entry point is unchanged.
- Hosted publication still suppresses the local global pointer/index.
- The current `io.py` durability diff predates Round 4 and was already examined in Round 3. Because there is no intermediate Round-3 tree, “untouched in Round 4” cannot be proven byte-for-byte from this aggregate uncommitted diff.
- `retention.py` and `tests/test_phase2b_retention.py` are untouched.

## D — Test quality

- The original money/resume regressions are materially load-bearing: reverting ambiguous suppression, durable pricing, reservation restoration, duplicate comparison, or compatible settlement changes would violate their exact request, identity, or µUSD assertions.
- `test_an_upgraded_0001_local_row_gets_one_run_id...` covers only an active, valid local snapshot. It passes while terminal rows remain unbackfilled and while other production run-ID mints remain.
- The settlement-sweep test covers one `dead_letter` row and two sequential starts. It does not cover every terminal state, concurrent writers, or temporary recovery failure.
- The local admission test cancels before `DispatchAdmission` runs. It cannot expose cancellation or expiry after the SQL check but before the JSONL append.
- The settlement-fence test reclaims before the fence. It proves the fence exists, but not atomicity between the fence and journal rewrite.
- The Shazam recovery and four rewritten identity tests are load-bearing by inspection and preserve their substantive assertions.
- No existing test assertion appears weakened.
- I did not execute the fixer’s reported 1,347-test gate or its four known retention failures.

## E — Scope

- No dependency or packaging file changed.
- Nothing under `data/` or `work/` is present in status.
- `src/id_detector/playlists/**`, `tests/test_playlists.py`, `README.md`, `idea.cmd`, `src/id_detector/present/theme.py`, and `tests/test_phase0a_security.py` are untouched.
- No FastAPI, Starlette, or uvicorn import was introduced under `src/id_detector/` or `src/idea_web/jobs/`.
- All untracked migration, implementation, test, and review files were inspected.
- `git diff --check` is clean.

## Required fixes

### Realistic

1. **[P0]** Recover and settle every identifiable run in durable attempt/reservation records for pre-upgrade terminal local jobs, even when `jobs.run_id` is NULL; emit exactly one missing settlement per recovered run.
2. **[P0]** Hold the local SQLite claim/admission transaction across the durable `dispatched` JSONL append, so cancellation and reclamation cannot commit between authorization and the event that permits network I/O.
3. **[P1]** Make settlement authorization and emission one fenced durable operation—prefer an SQLite settlement/outbox row written under the claim fence, followed by an idempotent journal projection.
4. **[P1]** Remove job-driven fallback run-ID mints: require the normalized job run ID, route legitimate new IDs through one helper, and fail closed if a worker context lacks it.
5. **[P1]** Sweep all terminal states and serialize the “missing settlement” decision with emission; retry cache/recovery misses instead of adding them permanently to `_swept`.

### Adversarial

None beyond the two owner-held risks, which the Round-4 changes did not widen.

VERDICT: FIX_FIRST