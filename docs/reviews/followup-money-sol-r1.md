## A. Audit of the fixer’s verdicts

No finding was marked “not reproducible” or “already fixed”; the reproduction verdicts are consistent with the pre-fix code and recorded evidence. Static audit of the claimed fixes:

| Retro finding | Audit | Reversion-test quality |
|---|---|---|
| 4a ambiguous AudD resume/ordinal | **Partial**: correct when the same `run_id` survives; supervised local restart does not preserve it | **Insufficient** for the no-resend clause; see D |
| 4a reservation/current pricing | **Partial**: durable record works in the service/hosted worker, but local restart creates a new run; mismatched injected reservations are accepted | Price/cap tests are load-bearing |
| 4a compatible-result money | Fixed for runs with durable money | Load-bearing |
| 4a cancel/resume settlement | **Partial**: paid service case works; supervised local crash/cancel and zero-money compatible resume do not | Paid-case test is load-bearing |
| 4a failed `RunResult` | Fixed | Load-bearing |
| 4a C0/DEL URL validation | Fixed | Load-bearing |
| 4a retryable zero-cost outcome | Fixed for AudD | Load-bearing |
| 4a `allow_degrade` cumulative spend | Fixed | Load-bearing |
| 4a interrupted secondary | **Partial**: resolved cached answers are restored; dispatched-unresolved Shazam attempts are not | Tests cover resolved `no_match`, not ambiguity |
| 4a checkpoint durability/revalidation | **Partial**: new entries are hashed; legacy entries retain existence-only trust | New-entry test is load-bearing |
| 4b JSONL namespace/SQLite recovery | Fixed for the hosted `Worker` | Load-bearing |
| 4b attempt IDs/conflicting duplicates | **Partial**: principal outcome conflict is caught, but not the full event identity | Existing test is load-bearing only for `outcome` |
| 4b malformed progress | Fixed for isolated rows; unsafe when the malformed row is attached to a live run | Load-bearing for the isolated case |
| 4b attached cancellation | Fixed | Load-bearing |
| 4b hosted pointer/index | Fixed | Load-bearing |
| 4b lease fencing | **Partial**: direct expired-claim paths are fenced; corrupt attached jobs can still seize or terminate another driver’s run | Direct expiry tests are load-bearing |
| 4b pre-primary accounting | Fixed in `Worker`; absent from the supervised `LocalWorker` path | Worker tests are load-bearing |
| 4b Windows write-through rename | Fixed | Exact revert would fail the test |
| 4b backfill transaction length | Fixed | Load-bearing |
| 4b breaker lifetime/send ordering | Fixed for the stated lifetime/order defect; ambiguous resume remains open | Exact revert would fail |

The two acknowledged P2 findings remain unchanged, matching the record.

## B. Correctness bugs in the fix

- **P0 — The supervised local worker can re-bill after a hard worker-process death and lose the first pass’s spend.** The legacy runner generates a new UUID on every execution at [webapp/runner.py:276](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/id_detector/webapp/runner.py:276). A reclaimed local job is merely recreated and run again at [local.py:396](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/local.py:396) and [local.py:444](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/local.py:444). Consequently, `RunLedger.resume()` sees a fresh run; [paid_clip.py:583](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/id_detector/paid_clip.py:583) treats the old dispatched attempt merely as a cross-run parent and sends the clip again. Resolved matches from the first pass are also omitted from the new run’s spend, while cached `no_match` and ambiguous clips may be billed again. A pricing change is likewise treated as a fresh reservation. Cancellation before restart settles the outer job from a snapshot with `run_id=""` and usually zero money at [local.py:451](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/local.py:451). This defeats the claimed shared implementation on the owner’s `idea serve` path.

- **P0 — A malformed or de-marked attached job can terminate or take over a different worker’s actively leased run.** Attachment is inferred from mutable `progress` JSON at [worker.py:95](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:95). Invalid JSON makes the row claimable; `_row()` then fails and `_quarantine()` calls `_abandon_run()` at [worker.py:889](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:889). That method folds money and clears `analysis_runs.claim_token` using only `run_id` and status at [worker.py:866](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:866), without proving this job is the unexpired driver. Valid JSON that merely loses `attached=true` instead reaches [worker.py:937](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:937) and rotates the live run’s token. The malformed-progress regression uses a job with no shared run and misses both cases.

- **P1 — Dispatched-unresolved Shazam requests are journalled but not recoverable by query.** Shazam rows deliberately store `query_id=NULL` at [worker.py:707](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:707), and the shared resume fold is consumed only by the AudD sweep. If a worker dies after `sent()` and before a response, there is no raw answer; with the breaker closed, `recognise_generation()` can submit that query again. Local service runs do not persist Shazam attempt events at all. This violates §2.3.3’s state machine for every provider request.

- **P1 — Legacy checkpoints preserve the exact existence-only defect the retro required removing.** [service.py:280](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/id_detector/service.py:280) accepts any pre-fix checkpoint lacking `artefact_records` when its paths merely exist. A truncated or changed legacy PCM/window artefact is therefore still marked complete. Existing interrupted runs—the main consumers of this fix—do not receive the new hashes.

- **P1 — An injected admitter is not restored to the exact durable reservation.** [run_ledger.py:507](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/id_detector/run_ledger.py:507) validates only unit price and then retains the caller’s reservation. An injected admitter with the same unit price but a smaller reservation makes a resume stop early even though the durable reservation permits it; a different planned count, reserved amount, or cap is silently accepted.

- **P1 — Duplicate verification excludes contract fields.** `AttemptEvent.payload()` omits `ordinal` at [run_ledger.py:91](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/id_detector/run_ledger.py:91), while SQLite duplicate comparison omits `egress_id` at [worker.py:502](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:502). A duplicate `(attempt_id, seq)` attributed to another egress is silently accepted, corrupting breaker attribution; conflicting JSONL ordinals are likewise merged.

- **P1 — A zero-money compatible resume can leave the old terminal status in the invocation journal.** The compatible branch rewrites the settlement only when `recovered.any` at [pipeline.py:654](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/id_detector/pipeline.py:654). If a run was cancelled before reserving, another run later publishes a compatible bundle, and the first `run_id` resumes, it returns `complete` but leaves its sole invocation entry as `cancelled`.

## C. Regressions

- **P1 — Fresh durable-queue jobs suppress configured Shazam `no_match` refresh.** [pipeline.py:823](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/id_detector/pipeline.py:823) treats any completed phase as proof of a same-run resume. Queue intake pre-populates `ingest` and often `decode` checkpoints before the first pipeline execution at [worker.py:1503](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/src/idea_web/jobs/worker.py:1503). Thus a genuinely fresh hosted run reuses another run’s cached `no_match` even when the configured default requires refreshing it.

- Static inspection found no regression to separate-process supervision, the web/pipeline boundary, GET write behavior, or body limits. `idea.cmd` and the protected second-session files are unchanged. The fixer did not run this cycle’s two real PowerShell/browser gates, and this read-only review could not run them either, so the double-click experience is not independently verified.

## D. Test quality

- **P1 — The headline ambiguous-attempt regression can pass with the core no-resend fix reverted.** [test_followup_money_resume.py:169](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-ac7462893536c29b5/tests/test_followup_money_resume.py:169) checks only the number of resumed calls. Re-sending the ambiguous clip while leaving one untouched clip unsent produces the same count because restored spend exhausts the reservation. With fresh ordinals retained, all remaining assertions also pass; the test even permits `partial`. It must assert query/window identity and that every never-dispatched window—not the ambiguous one—was sent.

- No regression covers a supervised `LocalWorker` process death, lease reclamation, cancellation-before-restart, or price change with the same local job.

- The malformed-progress test has no attached live run, duplicate verification changes only `outcome`, and the Shazam test proves ordering only—not ambiguous recovery.

- By inspection, the remaining retro tests would fail under their principal production changes being reverted.

- I did not run `uv`, pytest, a server, the pipeline, GC, or any provider call; runtime claims are inspection-only.

## E. Scope

- Status contains 16 modified tracked files and four reviewed untracked files. `git diff --check` reports no whitespace errors.

- No dependency or lockfile change; no framework import in `src/id_detector/` or `src/idea_web/jobs/`; no `data/` or `work/` changes.

- `src/id_detector/playlists/**`, `tests/test_playlists.py`, `README.md`, `idea.cmd`, `src/id_detector/present/theme.py`, and `tests/test_phase0a_security.py` are untouched.

- Existing test edits encode the intended failed-result and durable-reservation behavior; I found no existing assertion weakened. The new ambiguous regression is independently inadequate as described above.

## Required fixes

1. **[P0]** Give each supervised local job one durable service `run_id` reused across worker-process reclamation; route restart and cancellation through the shared ledger/reservation/settlement path, and add hard-kill plus price-change coverage.

2. **[P0]** Normalize driver/attachment ownership and require the matching driving job, token, active state, and unexpired lease before rotating or abandoning a run; quarantining a corrupt attached row must never mutate its driver’s run.

3. **[P1]** Persist Shazam `query_id` and consume its prepared/dispatched/resolved ledger during resume so an unresolved dispatch is never automatically resent, locally or through the queue.

4. **[P1]** Base Shazam refresh suppression on actual same-run evidence, not merely nonempty intake checkpoints; add a fresh hosted-run cached-`no_match` regression.

5. **[P1]** Treat unhashed legacy checkpoints as incomplete and rebuild them, rather than preserving existence-only trust.

6. **[P1]** Require an injected admitter’s complete reservation to equal the durable record, or replace it with an admitter constructed from that record.

7. **[P1]** Verify every immutable duplicate field, including ordinal and egress attribution, using one event payload for JSONL and SQLite projection.

8. **[P1]** Always replace an existing run’s terminal invocation on compatible resume, even when reservation, spend, and attempts are all zero.

9. **[P1]** Strengthen the ambiguous-attempt test to assert exact query identities and add the missing supervised-local, corrupt-attached-run, and Shazam-ambiguity regressions; then run the two deferred local-mode gates.

VERDICT: FIX_FIRST