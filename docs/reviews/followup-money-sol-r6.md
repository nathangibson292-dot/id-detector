## A — Audit of the fixer’s verdicts

- The original 4a-i/4b-i record marked no finding “already fixed.” That is accurate.
- The only “not reproducible” verdict—Shazam resend after loss of the per-media store—was wrongly broad. The fixer later withdrew it, reproduced the resend, and implemented per-run attempt recovery. The current tests delete `jobs.sqlite*`, so that correction is load-bearing.
- Never-rebill behavior is genuinely shared through `run_ledger.py`: dispatched-unresolved attempts become spent/ambiguous and are skipped; prepared-only and zero-cost retryable attempts receive fresh ordinals. Duplicate payload, ordinal, egress, and self-parent checks are substantive.
- Original reservation restoration is correct on the normal supervised-local path: `run_reservations` is read before the sidecar, stored under the claim fence, and exact recorded µUSD drives resumed admission. Repricing and lowered-cap tests would fail if this reverted.
- Settlement is one SQLite row per `run_id`, with monotonic monetary maxima. Cancellation, compatibility return, and pre-primary spend tests exercise the relevant behavior.
- Legacy adoption now uses the newest valid outcome while folding per-field maxima across every line and the durable fold. It neither sums duplicate settlements nor selects a lower monetary value.
- Migration 0003’s populated downgrade guard runs inside the migration transaction. `RAISE(ABORT)` prevents the table drops and the schema-version deletion, so version 3 and its rows survive.
- Round-5’s “every local terminal run gets a row” finding is not fully closed; see B1–B3.

Hosted refusal is complete in the current production call graph:

- Every claimed paid job—intake, waiting retry, analysis resume, or legacy row—passes the refusal at `Worker.run_once` before intake, reservation seams, run creation, or service execution.
- Paid detection covers a non-zero cap and paid primary/secondary engine names, including a zero-cap recipe naming AudD/ACRCloud.
- Attached jobs are not pipeline drivers; if corrupted into a claimable row they still hit the same refusal.
- `SQLiteAttemptJournal` defaults to hosted refusal and independently rejects hosted reservations and every non-Shazam event.
- A Free recipe strips paid engines even if paid adapters/options were injected.
- Hosted Free Shazam remains reachable and journalled; its regression test asserts a successful/degraded result and Shazam event rows.
- No production constructor of queue `Worker(local_mode=True)` exists. `idea serve`, `idea.cmd`, and the CLI use `LocalWorkerSupervisor → idea_web.jobs.local.main → LocalWorker`.

The record actually lists five residual risks:

- Hosted paid mode: accepted feature-gated risk; safe only while the refusal remains.
- Queue `Worker(local_mode=True)`: dormant/non-production, but unsafe if exposed.
- Pre-upgrade dispatch without a reservation: conservative P2 availability failure (`reservation_missing`).
- Pre-0003 job with missing media: realistic P1 because recovery is abandoned after three misses per process.
- Direct CLI sidecar authority: accepted local/adversarial risk, not widened here.

## B — Correctness bugs in the fix

1. **P0 — Exact SQLite spend is ignored when media lookup fails.**  
   [src/idea_web/jobs/local.py:459](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/local.py:459>) attempts `settlement_lines()` before reading `run_dispatches` or `run_reservations`; [service.py:633](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/id_detector/service.py:633>) likewise refuses to fold until media is located. Concrete scenario: a Deep local job durably reserves and dispatches AudD, its process dies, then its cache/index entry is lost before cancellation or dead-letter. SQLite still proves reservation and spent/ambiguous attempts, but no `run_settlements` row is ever created. This is a realistic loss of terminal spend accounting.

2. **P0 — “Provably unspent” relies on wall-clock ordering, not durable execution provenance.**  
   [src/idea_web/jobs/local.py:674](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/local.py:674>) treats `jobs.created_at >= migration-3 applied_at` plus absent authority rows as proof of zero spend. A clock correction can misclassify a pre-authority job. More concretely, a new process can migrate the shared database while an old supervised worker remains alive under the process lock; that old loaded code can claim a subsequently submitted job and dispatch without the new authority rows. If it dies and media is unavailable, the new sweep writes a permanent zero settlement. Once that row exists, [local.py:456](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/local.py:456>) returns without later ledger adoption.

3. **P1 — Settlement misses cease being retried after three sweeps.**  
   [src/idea_web/jobs/local.py:433](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/local.py:433>) skips a run permanently for the lifetime of the server after `MAX_RECOVERY_MISSES`. A temporary media/index outage lasting three sweeps leaves the terminal job without a settlement even after its media becomes available. Restarting `idea serve` is the only retry.

4. **P1 — Supervised-local Shazam dispatch drops the job fence.**  
   [src/id_detector/attempts.py:108](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/id_detector/attempts.py:108>) creates provider journals without propagating the parent journal’s admission; [pipeline.py:896](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/id_detector/pipeline.py:896>) uses that unfenced journal for Shazam. After lease expiry/reclamation or a committed cancellation between polling and dispatch, the stale worker can still journal and send a Shazam request. Two overlapping workers can consequently send the same query under the same deterministic attempt identity. It is free, so not P0, but violates the required claim fence and resume identity contract.

5. **P2 — Dispatch and settlement fences do not themselves bind the written run ID to the job.**  
   [src/idea_web/jobs/worker.py:210](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:210>) checks the job claim but does not assert `record.run_id = jobs.run_id`; [worker.py:348](</C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:348>) similarly trusts `entry.invocation_id`. Current production plumbing supplies matching IDs, so I found no realistic bypass, but the atomic fence is incomplete.

## C — Regressions

- No 4a-ii architecture regression found: `idea serve` still supervises a separate `LocalWorker` process, and the web process does not execute the pipeline.
- HTTP server/handler code governing GET purity and request-body limits is untouched.
- `idea.cmd` is unchanged and still invokes `uv run idea serve --open`.
- Hosted Free Shazam remains operational.
- The material regression is B4: the supervised-local Shazam provider journal loses the queue admission fence.

## D — Test quality

- The six tests changed to `hosted=False` retain their original assertions; only their intended execution mode changed.
- Reservation repricing, sidecar deletion/forgery, missing-reservation refusal, legacy maxima, populated downgrade, compatibility settlement, and reprojection tests are load-bearing.
- Three Round-6 cases intentionally passed before the fix, as the record admits. The module header claiming every case failed is inaccurate.
- There is no regression for SQLite-only settlement when paid media is missing, the unsafe timestamp proof, retry exhaustion, or supervised-local Shazam reclamation.
- Hosted refusal lacks direct tests for analysis-state resume, waiting retry, attached/legacy rows, and a zero-cap recipe naming a paid engine. Static inspection shows them refused, but removal of individual detection branches could escape the current suite.
- I did not run `uv` or pytest: the supplied sandbox is read-only, as anticipated by the request. Test execution claims above are therefore based on source/reversion reasoning and the fixer’s recorded outputs, not an independent run.

## E — Scope

- Every tracked change and untracked file reported by `git status --short` was read.
- No dependency manifest changed.
- No FastAPI, Starlette, or uvicorn import was added under `src/id_detector/` or `src/idea_web/jobs/`.
- No changes exist under `data/` or `work/`.
- Playlists, `tests/test_playlists.py`, `README.md`, `idea.cmd`, `present/theme.py`, `tests/test_phase0a_security.py`, `retention.py`, and `tests/test_phase2b_retention.py` are untouched.
- `git diff --check` reported no whitespace errors.

## Required fixes

### Realistic

1. **P0:** On media lookup failure, fold `run_dispatches` and `run_reservations` first and write the exact/conservative SQLite-derived terminal settlement with no projection path; reserve media-dependent retry only for genuinely pre-authority runs.
2. **P0:** Replace the migration timestamp heuristic with durable authority-version provenance that an old worker cannot satisfy; never create a zero row merely from wall-clock ordering and absent rows.
3. **P1:** Keep unresolved settlement recovery durably scheduled until success; do not permanently suppress it after three in-process misses.
4. **P1:** Propagate a claim-and-cancellation admission fence to supervised-local Shazam journals and atomically reserve each attempt identity before its network call.
5. **P1:** Add regressions for the two P0 scenarios, retry recovery after more than three misses, the Shazam reclaim race, and the hosted refusal state/zero-cap-paid matrix.

### Adversarial

6. **P2:** In the same transaction as dispatch or settlement, assert that the event/entry `run_id` equals the claimed job’s `run_id`; for paid dispatch also assert that the matching reservation row exists.
7. **P2:** Correct stale comments saying ambiguous attempts are re-run, SQLite JSONL is authoritative, and every Round-6 test failed before the pass.

VERDICT: FIX_FIRST