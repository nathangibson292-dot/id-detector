## A. Contract violations

1. **Backup can seal an incomplete snapshot.** DB-referenced bundles outside the work root are silently ignored (`src/idea_web/backup.py:148-152`), and missing referenced directories are silently skipped (`src/idea_web/backup.py:263-268`). `verify_snapshot()` checks only files that were listed (`src/idea_web/backup.py:390-408`), so a complete run whose bundle disappeared produces a “valid” backup whose restored DB points nowhere. Missing bundle/fuse manifests are also accepted: `directories` is calculated then discarded without checking completeness (`src/idea_web/backup.py:417-433`).

2. **The snapshot is not immutable.** `_link()` hard-links every file (`src/idea_web/backup.py:181-190`), including `recognise/*attempts.jsonl`; those journals are subsequently appended in place (`src/id_detector/journal.py:134-147`). A later scan of the same media therefore changes the already-sealed backup and invalidates `snapshot.json`.

3. **The shipped verification command is incomplete and returns the wrong exit code.** `python -m idea_web.backup verify` calls only `verify_snapshot()`, never bundle/fuse/sidecar verification, and `main()` always returns zero even when `"ok": false` (`src/idea_web/backup.py:562-569`). This does not provide the deferred `verify-artefacts` behavior required to be reachable through the module entry point.

4. **Rule (b) does not preserve the original breaker semantics.** It counts `prepared` rows (`src/idea_web/breaker.py:40-42,127-133`), although the attempt contract explicitly says prepared-without-dispatched was never sent (`src/id_detector/attempts.py:3-11`). Cancellation, signature failure, or a breaker-refused Deep secondary can therefore consume the daily request budget and degrade another paid run without a provider request.

5. **Operations “show” is not read-only.** `breaker_states()` constructs a default-config breaker and calls `refusal()` (`src/idea_web/ops.py:177-183`); `refusal()` may write/open the breaker (`src/idea_web/breaker.py:188-215`). Merely viewing operations can therefore change service state using thresholds that differ from the worker’s configured policy.

6. HEAD is the requested `08ec7ab`. Profiles, the golden/frozen engine tree, all second-session-owned files, and all of `src/id_detector/` have zero diff. Migration 0004 is additive; an independent populated in-memory 0003→0004→down drill preserved `run_reservations`, `run_dispatches`, and `run_settlements`. Existing migration-test edits only change expected version/count values.

## B. Correctness bugs

1. **Restore is destructive before it knows the restore is valid.** It copies directly over `app.db` and live artefacts (`src/idea_web/backup.py:515-523`), releases the supervisor lock, then performs semantic verification (`src/idea_web/backup.py:524-528`). A power loss or copy failure leaves a partial database/tree; a semantically corrupt but hash-consistent snapshot overwrites good data before rejection; and `idea serve` can acquire the released lock while verification is still running. There is no staging, rollback, atomic publication, or handling of existing SQLite `-wal`/`-shm` files.

2. **Backups normally fail during a real scan.** Backup waits at most 30 seconds for each media lock (`src/idea_web/backup.py:65-67,220-224`), while the pipeline holds that lock across the whole analysis (`src/id_detector/pipeline.py:548-560,1749-1753`). A typical scan lasting minutes or hours makes the nightly snapshot refuse rather than safely coexist with it.

3. **Configured breaker re-enable is lost across restart.** `SharedShazamBreaker.__init__` accepts the already-incremented generation without comparing it with persisted state (`src/idea_web/breaker.py:60-80`); `configure()` only reacts to an increase relative to that in-memory config (`src/idea_web/breaker.py:89-93`). Restarting after bumping the config therefore leaves the durable latch set.

4. **Breaker opening is delayed and state updates are non-atomic.** `SharedShazamBreaker.resolved()` queries the ledger before the caller writes the resolved event (`src/idea_web/breaker.py:272-275`, `src/id_detector/shazam.py:400-409`, `src/id_detector/recognise.py:352-356`). Multiple processes also read `opens`, calculate, and later overwrite it via `_store`, allowing lost trips (`src/idea_web/breaker.py:170-206`).

5. **Progress omits important job paths.** Intake runs before `PageProgress` exists (`src/idea_web/jobs/worker.py:2149-2159,2508-2523`), while cache hits and attachments return earlier with ad-hoc documents (`src/idea_web/jobs/worker.py:2363-2402`). Intake failures, cache hits, and attached jobs therefore lack the page document. Every retry creates a fresh tracker, resetting measured time and the monotonic progress clamp.

6. **Deep jobs are represented as Free scans.** The tracker stores `job.recipe.name`, i.e. `"deep"` (`src/idea_web/jobs/worker.py:2508-2513`), but the unchanged renderer recognizes only `"max_accuracy"` as paid and otherwise says nothing was spent (`src/id_detector/present/server.py:798`). It also stores a bare bundle ID as `result_path` (`src/idea_web/progress.py:161-162`), yielding a root-relative `/<bundle-id>` link rather than a proven result route.

7. `provider_usage()` sums `unit_usd_e6` for every resolved outcome (`src/idea_web/ops.py:141-146`), including §2.3.2 zero-cost outcomes such as 429/auth/quota errors. It does not mutate the money authority, but the operator cost display is false.

## C. Scope

No protected or frozen path was touched. Deferring the `idea backup`/`restore` CLI spellings and off-box scheduling is within the two accepted scope decisions.

Required scope is nevertheless incomplete: a stable, complete snapshot; a safe restore; a full standalone artefact verifier; and durable progress for intake/cache/attach paths are not delivered.

## D. Tests

Both named gate files are present, offline, and intended to be deterministic, but they miss the failures above.

- The bundle test first expects backup to refuse while the lock is held, joins the commit thread, and only then takes the successful backup (`tests/idea_web/test_backup.py:130-160`).
- The GC test uses a manually held lock and likewise backs up only after release (`tests/idea_web/test_backup.py:163-201`); it does not run GC while backup owns the lock.
- No test covers a missing DB-referenced bundle, a missing manifest, mutation of a hard-linked journal after sealing, interrupted restore, existing WAL files, or invalid verification’s exit status.
- Removing restore’s internal `verify_artefacts()` would not fail the tests: the drill calls the verifier separately after restore (`tests/idea_web/test_backup.py:221-235`), and the damage test invokes the helper directly (`tests/idea_web/test_backup.py:284-301`).
- Progress tests cover only an analysis-phase success and analysis-phase dead letter, not intake failure, cache hit, attachment, retry monotonicity, Deep labeling, or result URLs.
- The breaker tests encode the incorrect prepared-row budget and test config re-enable only on a live object, not after restart.
- No unrelated assertion was weakened; the one existing breaker test was changed consistently with the new parking behavior.

Independent execution was restricted: `uv.exe` points outside the readable sandbox, and pytest cannot create a temporary directory. Direct `.venv` Ruff passed, fixture audit passed (`506 files`), while pytest and the JS checker stopped before execution for lack of writable temp space.

## E. Local mode

Static local-mode surfaces are unchanged: `idea.cmd`, CLI code, theme, templates, headers, assets, and `src/id_detector/` have no diff.

The live smoke test could not be completed in the read-only sandbox: `.venv\Scripts\idea.exe serve --no-open --port 8791` reached local database setup but failed with `PermissionError` creating `work/.idea`. Consequently `/` serving is unverified here.

## F. Quality

Ruff passes and fixture audit passes. `git diff --check` is clean.

`verify_artefacts()` contains a dead `directories` calculation (`src/idea_web/backup.py:417-433`), apparently the missing completeness check. Progress snapshot field-selection logic is duplicated from `jobs.local`, and the operations cost calculation duplicates money-outcome logic incorrectly.

## Required fixes

### Realistic

1. **P0** Refuse backup unless every copied-DB bundle/run reference is inside the work root, present, manifested, and semantically verified before sealing.
2. **P0** Make snapshots immutable after lock release by copying mutable journals or introducing copy-on-write/versioned artefacts, with a same-media post-backup append regression test.
3. **P0** Stage and fsync restore output, publish it atomically with SQLite WAL/SHM handling, retain the supervisor lock through verification, and preserve or roll back the previous tree on failure.
4. **P1** Use locking that lets backup safely complete while a scan, bundle commit, and GC overlap instead of timing out on the whole-run media lock.
5. **P1** Count durable dispatched Shazam requests for rule (b), evaluate newly resolved events after commit, and update open/latch state atomically across processes.
6. **P1** Make operations display strictly read-only and policy-aware, and apply configured re-enable generations against persisted state after restart.
7. **P1** Publish one durable page document across intake, cache-hit, attachment, analysis, retry, and terminal paths while preserving prior monotonic progress.
8. **P1** Map Deep runs to the renderer’s paid profile and publish a valid authorized result URL rather than a bare bundle ID.
9. **P1** Make module verification run snapshot plus bundle/fuse/sidecar checks and return nonzero on any failure.
10. **P1** Add gate regressions for all realistic cases above, including a test that fails if restore’s internal verification call is removed.
11. **P2** Derive the operations cost display from billable outcomes and remove the dead verifier variable.

### Adversarial

12. **P2** Validate every snapshot entry as a normalized relative path confined beneath both `artefacts/` and `work_root`, rejecting absolute paths, `..`, symlinks, and reparse-point escapes.

VERDICT: FIX_FIRST