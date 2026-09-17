## A. Contract violations

Round-4 closure:

| # | Status | Evidence |
|---|---|---|
| 1. Durable publication/recovery | **PARTIAL** | Restore renames now use `durable_replace` (`src/idea_web/backup.py:301-310,1237-1266`), and startup invokes recovery (`src/idea_web/jobs/local.py:162-174,962-968`). However snapshot publication still uses plain `os.replace` (`backup.py:675-683`), and recovery establishes absence with `os.remove` plus a Windows no-op directory fsync (`backup.py:313-316,1083-1088`; `src/id_detector/io.py:156-160`). |
| 2. Stable capture | **PARTIAL** | Size/mtime checks, copy failure, file flushes and directory flushes exist (`backup.py:452-502,675-678`). A file that vanishes permanently after attempt one is silently absent from attempt two because `_view` skips missing members and no prior membership is compared (`backup.py:423-449,585-622`). |
| 3. Stale replacement | **DONE** | Owned scope includes `ingest/source.json`, flat `present/*`, and recursive trees (`backup.py:92-97,395-416,1016-1042`); regression at `tests/idea_web/test_backup.py:1192-1218`. |
| 4. `pruned_upstream` validation | **DONE** | Lowercase 64-hex is enforced (`backup.py:100,939-944`) and malformed values are tested (`test_backup.py:963-974`). |
| 5. Operations/WAL inspection | **DONE** | Production no longer uses `immutable=1`; live WAL uses `mode=ro` plus SQLite backup, idle files are copied outside the work root (`src/idea_web/ops.py:67-119`). Regression: `tests/idea_web/test_ops.py:868-898`. |
| 6. Deterministic regressions | **PARTIAL** | Most requested regressions exist, but the vanish test restores the file before retry (`test_backup.py:1093-1108`), and there is no inverse concurrency test starting `idea serve` during a live restore or test detecting the plain snapshot `os.replace`. |
| 7. Duplicate/confined paths | **DONE** | Case-folded duplicate rejection is at `backup.py:794-810`; journal paths are preflighted before parent creation at `backup.py:1056-1081`. |

The accepted sidecar exception is implemented too broadly. Anything outside a whitelist is allowed to be missing (`backup.py:884-893,945-949`), not only decoded audio, windows, and original media. Real production sidecars name `local-cache/...` (`src/id_detector/hints/pipeline.py:610-618`) and `provider_configs/...` whose bytes live beside the recognition invocation (`src/id_detector/recognise.py:435-440`). These can evade semantic verification.

`git diff 08ec7ab -- src/id_detector` is empty. Profiles, corpus/golden artefacts, and second-session-owned paths are unchanged. U-F9 weighting remains unchanged.

Migration 0004 advances the schema to 4 and its down script removes only `provider_breaker_state`. An independent populated in-memory `0003 → 0004 → 0003` probe preserved all rows in `run_dispatches`, `run_settlements`, and `run_reservations`. Version-only assertions changed; no substantive money assertion was weakened.

## B. Correctness bugs

1. **P0 — `idea serve` can race a live restore and lose durable writes.** When restore owns the supervisor lock, `recover_if_unowned` catches `JobStoreLocked` and returns success (`backup.py:1123-1135`). `local_database` then continues (`jobs/local.py:162-174`); on an already-current schema, migration returns before acquiring the supervisor lock (`idea_web/database.py:227-233`). The frozen serve path ignores `supervisor.start() == False` and still serves (`src/id_detector/cli.py:919-930`). A submit or cancellation can therefore write the database that restore subsequently displaces and deletes.

2. **P0 — Windows durability remains incomplete.** Snapshot publication is a plain directory `os.replace` (`backup.py:682`) with no effective Windows parent flush. Recovery of publications with no predecessor uses non-write-through deletion (`backup.py:313-316,1083-1088`). A power loss can leave a reported snapshot unpublished or resurrect a removed publication after its recovery journal is cleaned.

3. **P0 — permanent disappearance is silently omitted.** After a first-copy `FileNotFoundError`, capture retries from a fresh listing. If the member remains absent, `_view` simply excludes it and the snapshot seals without a skip reason (`backup.py:442-449,610-622`). The regression only covers disappearance followed by reappearance.

4. **P1 — sidecar verification can certify missing or mismatched evidence.** Missing `local-cache` and logical `provider_configs` upstreams fall outside `_in_scope`, so they pass without matching their recorded digest. A malformed ordinary digest also passes when such a path is absent.

The shared breaker remains append-only and scoped by provider/egress (`breaker.py:155-196`); cursor fencing prevents repeated trips (`:230-249`), the kill-switch is checked first (`:377-385`), and running free work is not interrupted. Hosted paid dispatch remains refused before breaker handling (`jobs/worker.py:2161-2165`). The core claim/cancel fence and settlement sweep are unchanged.

## C. Scope

No frozen engine, playlist, launcher, README, theme, profile, corpus, or golden artefact changed. The module entry point `python -m idea_web.backup` exposes backup, verify, and restore successfully.

The deferred `idea backup` spelling, nightly schedule, and off-box upload are accepted scope decisions and are not gaps.

## D. Tests

The named files collect successfully: 26 operations tests and 39 backup tests; all `tests/idea_web` collect 291 tests. They are offline in design.

Missing effective regressions:

- Permanent member disappearance across the retry.
- Starting `idea serve` while restore owns the supervisor lock.
- Windows write-through snapshot publication and durable removal of predecessor-less publications.
- Real `local-cache`/`provider_configs` sidecar mappings and malformed hashes under the allowed-missing exception.

The semantic-restore test at `test_backup.py:673-690` does fail if semantic verification is removed. No existing assertion was deleted or weakened beyond expected schema-version updates and the intentional breaker behaviour update.

## E. Local mode

`uv run idea serve --no-open --port 8791` could not execute because the sandbox cannot launch the WinGet `uv.exe` shim. Direct invocation reached startup but failed when the read-only sandbox denied creation of `work/.idea`, so `/` could not be probed.

Static assets, headers, CSP, `idea.cmd`, cached-result serving, and presentation code have zero diff. The live-restore race above is nevertheless a local-mode blocker.

## F. Quality

All exact `uv` gates failed before execution for the same shim restriction. Direct fallbacks produced:

- `ruff check --no-cache .`: passed.
- Fixture audit: passed, 506 files.
- Python parsing of all changed files: passed.
- `git diff --check`: passed.
- Pytest collection: passed.
- Test execution and `check_page_js.py`: blocked by the absence of any writable temporary directory.

`operations_snapshot` independently copies the whole database for each of its five sections (`ops.py:362-371`), and the journal’s `removing` list is written but never read (`backup.py:1229`). These are quality follow-ups, not blockers.

## Required fixes

### Realistic

1. **P0** Make live restore ownership explicit and make `local_database()` fail closed while a restore is active; add a concurrent restore-versus-serve regression proving no server can accept writes.
2. **P0** Replace the snapshot’s plain directory `os.replace` and predecessor-less recovery deletions with Windows write-through durable boundaries.
3. **P0** Carry capture membership across retries so a previously observed member that remains vanished refuses the backup or skips the whole medium with a recorded reason.
4. **P1** Restrict allowed-missing sidecar upstreams exactly to decode, windows, and original media; correctly resolve present `provider_configs` evidence and reject missing `local-cache` evidence or seal it explicitly.
5. **P1** Add regressions for all four cases above, including tests that fail when write-through publication or semantic verification is removed.

### Adversarial

None.

## Follow-ups, not blockers

- Reuse one in-memory operations snapshot rather than copying the database separately for every subsection.
- Remove or consume the unused recovery-journal `removing` field.
- The CLI aliases, nightly schedule, off-box upload, and parked U-F9 weighting remain accepted deferrals.

VERDICT: FIX_FIRST