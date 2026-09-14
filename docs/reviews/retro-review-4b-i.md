## A. Findings both earlier reviews missed

- **P0 — A fresh paid-attempt journal is not namespace-durable.** Evidence: `8e937ab:src/idea_web/jobs/worker.py:1441-1452` creates a new per-run `.attempts/<run_id>.jsonl`; `8e937ab:src/id_detector/journal.py:56-61` fsyncs the file but never its parent directory. Scenario: on POSIX, AudD’s `dispatched` event and its SQLite projection commit, then power fails before the directory entry persists. Recovery treats the missing JSONL—not SQLite—as authoritative (`pipeline.py:690-694`), restores zero spend, and sends the paid clip again.

- **P0 — same-run recovery aliases a second AudD request onto the first attempt ID.** Evidence: `8e937ab:src/id_detector/attempts.py:39-42` derives IDs from `(run_id, query_id, ordinal)`; queue recovery preserves `run_id`, but `paid_clip.py:570-583` restarts every dangling query at `ordinal = 0`. `worker.py:376-383` then silently ignores the duplicate `(attempt_id, seq)`. Scenario: attempt 0 was dispatched and the worker died; the replacement prepares another request with the same ID and `parent_attempt_id` pointing to itself. Two provider calls collapse into one ledger attempt. A second crash before `primary` can therefore restore one charged unit although two were charged.

- **P1 — malformed `progress` JSON still stops the entire consumer.** Evidence: `8e937ab:src/idea_web/jobs/worker.py:683-688` and `:751-756` execute `json_extract(progress, ...)` before `_row()` and its quarantine path. Scenario: one waiting row contains `progress='{'`; SQLite raises `OperationalError: malformed JSON`, `run_forever()` catches it, then repeats the same failing query forever, never reaching a good row. I reproduced the SQL behavior with an in-memory stdlib `sqlite3` probe; the committed test only corrupts `recipe_id`.

- **P1 — cancellation of an attached job is ignored.** Evidence: `8e937ab:src/idea_web/jobs/worker.py:683-686` permanently excludes attached waiting jobs from claims; `:790-799` only sets `cancel_requested`; `:745-773` reconciles them to the run’s terminal status without checking that flag. Scenario: job B attaches to job A’s run, B is cancelled, A completes, and B becomes `complete` instead of `cancelled`; any future provisional reservation would remain attached until settlement.

- **P1 — hosted execution still publishes the forbidden global local-mode pointer.** Evidence: the worker creates a hosted store at `8e937ab:src/idea_web/jobs/worker.py:1464-1469`, but `pipeline.py:1420-1454` calls `publish_result()` without its `local` argument; `present/bundles.py:218-229,370-374` therefore defaults to `local=True`, updates `present/current`, and rebuilds the global index. Scenario: two hosted tenants analyse the same media and the later private bundle becomes the shared current pointer, contrary to §3.4’s “hosted mode has no global pointer”.

## B. Earlier findings whose fix is incomplete or wrong

- **P0 — report P0-1/P1-5 did not actually make lease expiry part of every fence.** `8e937ab:docs/reviews/build-4b-i.md:234-246,274-280` claims stale writes are closed, but checkpoint and attempt fences check only the token (`worker.py:295-309,352-360`), while `fail`, `wait`, `terminal`, and intake commit also omit `lease_until` (`:832-960,1276-1286`). Scenario: the paid sweep checks cancellation just before expiry (`paid_clip.py:574-583`), the machine sleeps, then wakes after expiry before another claimant; `prepare` and `dispatched` still pass and a new paid request leaves the process. A claimant arriving immediately afterwards can recover and repeat it.

- **P0 — report P0-3/P1-7 only preserves accounting after a `primary` checkpoint.** Run money and attempts are materialized only by the primary checkpoint (`worker.py:287-304`). `_recovered_money()` reads only the run row and that checkpoint (`:1504-1524`); retry/dead-letter handling does not fold the attempt journal or SQLite events (`:832-855`). Scenario: two AudD calls are durably dispatched, then the process dies before `primary`; cancelling the reclaimed job—or dead-lettering it after repeated pre-checkpoint failures—settles the run with zero reservation, zero spend and zero attempts.

- **P1 — report P0-2’s Windows rename gap can still produce an absent checkpoint artefact.** `decode.py:139` uses plain `os.replace`; `worker.py:106-111` subsequently fsyncs the destination file, while Windows directory syncing is a no-op. Microsoft documents `MOVEFILE_WRITE_THROUGH` as the operation that explicitly waits for the move to reach disk; plain file flushing does not provide that explicit prior-rename guarantee. [MoveFileEx documentation](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexa), [FlushFileBuffers documentation](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers). Scenario: the DB checkpoint survives a power loss but `audio.pcm`’s rename does not; resume sees `decode` complete, calls `load_decode()` (`pipeline.py:545-548`), and repeatedly fails rather than re-decoding, eventually dead-lettering.

- **P1 — the report’s SQLite-discipline conclusion overlooks potentially long write transactions.** Contrary to `build-4b-i.md:361-365`, `worker.py:437-461` reads and parses the entire JSONL while holding `BEGIN IMMEDIATE`, and `:463-481` performs append plus filesystem fsync inside another write transaction. Scenario: a large journal or slow disk holds both SQLite’s writer lock and the process-local writer mutex for over 30 seconds; heartbeat renewal blocks, the lease expires, and another process can reclaim as soon as the transaction releases.

- **P1 — report P1-9’s Shazam ledger fix breaks breaker lifetime and crash ordering.** `worker.py:1453-1463` creates a fresh `LedgerShazamBreaker` for every job, resetting its samples, daily count and latch; supplying the intended shared breaker bypasses ledger wrapping entirely. Additionally, `worker.py:545-550` records `dispatched` only from `resolved()`, after network I/O (`shazam.py:398-407`). Scenario: thirty sequential jobs each return one 429 but never reach the 20-sample process threshold; killing a worker after request transmission leaves only `prepared`, understating actual egress.

## C. Test gaps

- No expired-but-not-yet-reclaimed claim attempts a checkpoint, terminal transition, or paid dispatch; the fencing test rotates the token first.
- No worker dies after AudD `dispatched` but before `primary`, followed by cancellation, dead-lettering, or another crash.
- No same-`run_id` ambiguous retry asserts a fresh attempt ID, non-self parent, two ledger attempts, and cumulative spend.
- No test simulates loss of a newly created JSONL directory entry while its SQLite projection survives.
- Malformed-row coverage changes `recipe_id`, not malformed waiting-state `progress` used inside SQL.
- No coalesced subscriber is cancelled, and no cancellation arrives after the final progress callback but before `terminal()`.
- Breaker tests do not process sequential queue jobs or exercise a caller-supplied shared breaker.
- Durability tests observe calls to `fsync`; they do not model Windows rename persistence or verify recovery from a missing checkpointed PCM.
- Migration concurrency uses threads (`test_worker.py:193-217`), not separate processes.
- No hosted worker assertion forbids creation or mutation of `present/current`.
- No slow-journal test proves heartbeat remains live while backfill and projection execute.

## D. What you verified is correct

- Claim selection and lease installation occur within one `BEGIN IMMEDIATE`; a fresh UUID token is minted per successful claim and rotated onto an existing run.
- Once another claim has rotated the token, the old claim cannot checkpoint, project events, publish progress, or settle.
- Heartbeat defaults to exactly 10 seconds; heartbeat, progress, `begin_analysis`, and cancellation polling reject an already-expired lease.
- The breaker-waiting predicate is NULL-safe when `attached` is merely absent.
- A third failed/crashed claim dead-letters the job; expiry after 24 hours changes job and run to `failed`; attachment lookup requires an active run with an active driving job.
- POSIX checkpoint handling fsyncs each named file and its immediate directory before committing.
- Every queue-managed connection sets WAL, foreign keys, and a 5-second busy timeout. The complete service run itself is outside a database transaction.
- The current migration’s `down` drops triggers and tables in dependency order; migration application is guarded by OS-level file locking.
- Append-only triggers reject UPDATE and DELETE, and no production `src/` path issues either operation against `provider_attempt_events`.
- The reviewed paths are byte-identical between `8e937ab` and committed `HEAD`; excluded working-tree and separately owned files were not reviewed.
- I could not run `uv` or pytest in this read-only sandbox, so test conclusions are inspection-only. I started no server, provider pipeline, or GC and made no filesystem changes.

## Required follow-ups

1. **[P0]** Bind every run-side fence to the driving job and atomically require its matching token, active state, and unexpired lease immediately before checkpointing, dispatch admission, wait/fail, intake commit, and terminal settlement.
2. **[P0]** Make creation of `.attempts/<run_id>.jsonl` namespace-durable before provider I/O, and recover paid state from validated SQLite events if the JSONL is missing.
3. **[P0]** Allocate a strictly new per-query ordinal/attempt ID when resuming an ambiguous same-run attempt; reject self-parenting and verify duplicate event payloads instead of blindly ignoring conflicts.
4. **[P0]** Before cancellation or dead-letter settlement, fold durable provider events into cumulative reservation, spend, and attempt counts even when `primary` was never checkpointed.
5. **[P1]** Guard SQL JSON extraction with `json_valid`/`CASE`, quarantine malformed waiting rows transactionally, and continue to the next claim.
6. **[P1]** Give attached jobs an explicit cancellation/detachment transition and ensure reconciliation cannot overwrite it with the driver’s result.
7. **[P1]** Keep one breaker state per worker process while decorating it with per-run ledger context; record Shazam `dispatched` before network I/O.
8. **[P1]** Publish decoder output with the repository’s Windows write-through atomic move and add recovery behavior that invalidates a checkpoint whose artefact is absent.
9. **[P1]** Parse journal backfill outside the write transaction and apply short fenced batches; do not hold the writer lock across unbounded reads or fsyncs.
10. **[P1]** Propagate hosted/local mode into presentation publication and assert hosted workers never write `present/current` or the local index.

VERDICT: FOLLOW_UP_REQUIRED