## A. Contract violations

Round-3 fix audit:

| # | Status | Evidence |
|---|---|---|
| 1. Corpus confinement | **DONE** | Ancestors are walked in `src/idea_web/backup.py:190-205`; mutation targets pass the guard at `:285-304`, `:500-521`, `:624-628`, and `:1043-1065`. Regressions: `tests/idea_web/test_backup.py:228-260`. |
| 2. Missing terminal media | **DONE** | Missing media is explicitly skipped with a reason at `backup.py:347-368`; regression at `test_backup.py:401-414`. |
| 3. Whole-root stale replacement | **PARTIAL** | The whole root is enumerated at `backup.py:908-942`, but only `present/bundles`, `fuse/runs`, `recognise`, and `hints` are removed (`:82-87`). Unlisted `ingest/source.json`, `present/current`, and legacy `present/index.html` survive. The regression checks only bundle files (`test_backup.py:562-585`). |
| 4. Durable recovery | **PARTIAL** | Renames call both parent fsyncs (`backup.py:285-291`), but that helper is a no-op on Windows (`src/id_detector/io.py:156-160`), while the rename itself is plain `os.replace`. Recovery restores only files found in `.replaced` (`backup.py:969-984`), so newly published files with no predecessor remain. Recovery runs before another restore (`:1024-1026`) but not at server startup (`src/idea_web/jobs/local.py:162-172,926-945`). |
| 5. Lock scope/stable capture/durability | **PARTIAL** | The lock is released before copying (`backup.py:546-565`), but captured `size`/`mtime_ns` (`:129-135,416-422`) are never checked by `_copy_view` (`:438-458`). Copy failures are silently skipped, and only leaf directories are fsynced (`:459-465`), not the full newly created hierarchy. |
| 6. Breaker cursor | **DONE** | `cursor_event_id` is persisted by migration 0004 (`0004_shared_breaker.up.sql:28-33`) and required for another trip (`breaker.py:230-249`). Regression: `test_ops.py:249-268`. |
| 7. Authoritative page spend | **DONE** | The page receives folded SQLite spend at `worker.py:2692-2699,2746-2765`; regression at `test_ops.py:799-830`. |
| 8. Sidecars | **PARTIAL** | Missing ordinary upstreams and schema versions are checked (`backup.py:831-857`), but a pruned hash is validated only by length (`:848-853`), unlike the frozen hexadecimal check at `src/id_detector/io.py:346-354`. |
| 9. Operations sidecars | **DONE narrowly** | Operations uses `mode=ro&immutable=1` (`ops.py:69-84`) and tests absence of both sidecars (`test_ops.py:766-796`). This introduces a separate correctness issue below. |
| 10. Real tests | **PARTIAL** | The concurrency test never asserts `gc_actions` or proves backup observed the committing medium (`test_backup.py:439-487`). Abrupt death is genuine, but recovery is called manually rather than by restarted server startup (`:645-687`). |
| 11. Junction roots | **DONE** | Root rejection uses `is_link` (`backup.py:214-224`); backup source, destination, and restore-root junctions are covered at `test_backup.py:311-330`. |
| 12. Exclusive staging/counts/dead accumulation | **DONE** | Exclusive creation is at `backup.py:519-521,1028-1032`; link counts increment only after successful `os.link` (`:447-456`); the obsolete `captured` accumulation is gone. Regression for counts: `test_backup.py:356-374`. |

Migration 0004 is additive and its down script genuinely drops only the breaker table (`0004_shared_breaker.down.sql:1`). An independent in-memory populated `0003 → 0004 → 0003` probe preserved every `run_dispatches`, `run_settlements`, and `run_reservations` row. Version-only assertions changed from 3 to 4; no existing substantive assertion was weakened.

`src/id_detector/`, profiles, corpus/golden data, and all second-session-owned paths have zero diff. U-F9 weighting is unchanged.

## B. Correctness bugs

1. **P0 — interrupted restore can destroy or mix durable owner state.** On Windows, journal and publication renames lack write-through durability because `_rename()` uses `os.replace` while directory fsync is a no-op. If the process dies after publishing a path that had no predecessor, `recover_interrupted()` cannot remove it because it only walks `.replaced`. Worse, `idea serve` can start on that tree before recovery; later recovery may overwrite work created after restart (`backup.py:285-291,945-985`; `local.py:162-172,926-945`).

2. **P0 — backup’s “stable captured view” is not stable.** After releasing the medium lock, GC or a scan can replace/delete a captured journal. `_copy_view()` neither compares the stored identity nor fails on copy errors; it silently omits the file (`backup.py:438-458`). The snapshot can therefore verify despite missing `recognise`, `hints`, or ingest evidence, and a subsequent restore removes the owner’s surviving copy.

3. **P1 — stale media can still survive restore.** A medium absent from the snapshot can retain ingest metadata, `present/current`, and a legacy flat result because `_OWNED_SUBTREES` covers only `present/bundles` (`backup.py:82-87,924-942`). This does not meet the directed “newer medium cannot survive” requirement.

4. **P1 — corrupted pruned markers can verify.** An absent upstream with a 64-character non-hex marker passes `backup.py:848-853`; the frozen verifier requires lowercase hexadecimal (`io.py:346-354`).

5. **P1 — live operations uses SQLite’s immutable contract on a changing database.** `ops.py:64-74` opens the actively written service database with `immutable=1`. SQLite specifies that this flag asserts the file cannot change and warns that changes can produce incorrect results or corruption errors; therefore queue, breaker, and dead-letter output can be false while the service runs. [SQLite URI documentation](https://www.sqlite.org/uri.html#recognized_query_parameters)

Money did not regress: the authority tables, claim/lease/cancel fence, settlement sweep, and hosted-paid refusal remain intact; hosted paid work is still refused before breaker handling at `worker.py:2161-2165`.

## C. Scope

The accepted `python -m idea_web.backup` interface and deferred nightly/off-box copy are correctly scoped. No frozen engine, profile, golden, playlist, README, launcher, theme, or corpus file changed.

Required scope remains incomplete only where the Round-3 restore/capture fixes above are partial.

## D. Tests

The named gate files exist and are offline in design, but the critical regressions are incomplete:

- No startup invokes recovery, and the death test manually calls it.
- No death case publishes a path/database that had no predecessor.
- No test mutates or removes a captured non-bundle file after lock release.
- No test proves every nested directory is durably flushed.
- The concurrency test does not assert GC skipped or ensure the committed medium is referenced by the copied DB.
- The sidecar test uses a valid hexadecimal marker and misses malformed 64-character values.
- No regression covers stale ingest, current pointers, or legacy flat results.

All exact `uv run` commands—including the full suite and both phase gates—failed before execution because the sandbox cannot launch the WinGet `uv.exe` shim. Direct fallback Ruff and fixture audit passed. Direct pytest and `check_page_js.py` could not start because the read-only sandbox has no writable temporary directory. `git diff --check` passed.

## E. Local mode

`uv run idea serve --no-open --port 8791` failed at the same inaccessible `uv.exe` shim, so `/` could not be probed. `idea.cmd`, static assets, headers, CSP, cached-open code, and protected local presentation files are unchanged.

The missing startup recovery is nevertheless a local-mode blocker: `LocalWorkerSupervisor.start()` proceeds directly to cancellation/spawn without consuming an interrupted restore (`local.py:926-945`).

## F. Quality

`_Member.size` and `mtime_ns` are currently dead assurances: recorded at `backup.py:416-422` and never consumed. `gc_actions` is likewise collected but never asserted at `test_backup.py:451,468`.

The working tree remained clean throughout the review.

## Required fixes

### Realistic

1. **P0** Use a Windows write-through/durable rename boundary, recover before any database migration or server use, and make recovery remove every journal-listed publication that had no predecessor.
2. **P0** Create a genuinely stable post-lock capture, fail rather than silently omit changed members, and durably flush every copied file and newly created directory ancestor before publication.
3. **P1** Remove the complete restore-owned footprint across every medium, including ingest metadata, current pointers, and legacy flat presentation files.
4. **P1** Validate `pruned_upstream` exactly as the frozen verifier does, including `[0-9a-f]{64}`.
5. **P1** Replace `immutable=1` live inspection with a no-sidecar method that remains correct under concurrent WAL writers.
6. **P1** Add deterministic regressions for GC skip/commit overlap, server-start recovery, publications without predecessors, capture mutation, nested durability, and stale non-bundle media.

### Adversarial

1. **P2** Reject duplicate snapshot entry paths and confine every recovery-journal target before creating parents; a tampered snapshot/journal can otherwise overwrite rollback material or paths outside the work root.

## Follow-ups, not blockers

The parked-job page document, CLI spelling, nightly schedule, and off-box upload remain accepted deferrals. A committed populated-0003 migration regression would be useful, although the independent migration probe passed.

VERDICT: FIX_FIRST