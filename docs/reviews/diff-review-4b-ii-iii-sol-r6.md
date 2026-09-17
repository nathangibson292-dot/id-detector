## A. Contract violations

Round-5 closure:

| # | Status | Evidence |
|---|---|---|
| 1. Restore excludes live database use | **PARTIAL** | An already-active restore is refused (`src/idea_web/backup.py:1364-1383`; `src/idea_web/jobs/local.py:192-198`), but the restore lock is released before `Database.migrate()` (`src/idea_web/jobs/local.py:172-174`). Migration then creates/opens files and enables WAL (`src/idea_web/database.py:165-188`). |
| 2. Durable Windows publication/recovery | **DONE** | Real `CreateFileW`/`FlushFileBuffers`, write-through directory move, and flushed deletion exist (`src/idea_web/durable.py:50-107`). Snapshot publication uses the durable move (`src/idea_web/backup.py:830-839`); recovery uses durable deletion and closes the journal first (`src/idea_web/backup.py:1296-1310`). No publication/recovery `os.replace` bypass remains. |
| 3. Capture membership across retries | **DONE** | Prior membership is retained and disappearance skips the whole medium (`src/idea_web/backup.py:725-763`); the regression keeps the file permanently absent (`tests/idea_web/test_backup.py:1227-1256`). |
| 4. Exact sidecar handling | **DONE** | Allowed-missing paths are exact (`src/idea_web/backup.py:113-128`); provider configs resolve beside the invocation and malformed hashes fail (`src/idea_web/backup.py:1092-1113`). Real hint-cache and provider-config shapes are tested at `tests/idea_web/test_backup.py:1469-1578`. |

There is one additional mandatory restore-contract violation:

- **P0 — a skipped run inside an otherwise captured medium is destroyed.** Non-terminal runs are recorded with their run IDs under `skipped` (`src/idea_web/backup.py:398-409`), while another terminal run can cause the same medium to be captured (`:420-425`). Restore explicitly declines to preserve any skipped item once the medium has any captured artefact (`:1184-1216`), recursively marks unlisted `fuse/` and bundle files stale (`:1229-1254`), then moves them into disposable recovery staging (`:1473-1508`). Thus `fuse/runs/<skipped-run>/...` is deleted after commit. The build report acknowledges this unsafe rule at `docs/reviews/build-4b-ii-iii.md:765-766`.

Other contract checks:

- `git diff 08ec7ab -- src/id_detector` is zero lines. Profiles, corpus, golden artefacts, playlists, README, `idea.cmd`, and theme are untouched.
- No code writes beneath `data/`; hint evidence is only read (`src/idea_web/backup.py:472-485`). Corpus destinations remain guarded (`:238-259`).
- U-F9 weighting remains unchanged, as required.
- Migration 0004 adds/drops only `provider_breaker_state`. My in-memory populated `0003 → 0004 → 0003` probe retained one row in each of `run_dispatches`, `run_settlements`, and `run_reservations`. Version-only assertions were updated without weakening their surrounding money assertions.
- The shared breaker remains ledger-backed and append-only, preserves the kill-switch, and allows an already-running free primary to continue (`src/idea_web/breaker.py:155-196,377-409`).

## B. Correctness bugs

1. **P0 — restore can start between the fail-closed probe and SQLite open.**  
   State: `local_database()` acquires/releases the restore lock with no active restore; immediately afterward restore acquires its lock and supervisor lock; `local_database()` then executes `Database.migrate()`. That creates the migration-lock file and opens/enables WAL while restore is publishing (`src/idea_web/jobs/local.py:170-174`; `src/idea_web/backup.py:1364-1383`; `src/idea_web/database.py:165-188`). This can leave or race WAL sidecars against the replaced database. The regression starts restore first and therefore misses this inverse interleaving (`tests/idea_web/test_backup.py:1292-1349`).

2. **P0 — mixed captured/skipped media lose skipped-run data.**  
   Input: medium M contains captured run A and skipped/in-flight run B. Snapshot holds A, records B under `skipped`, and omits `fuse/runs/B`. Restore regards M as captured, marks B’s files stale, and permanently discards them with the recovery tree. This is precisely the durable-owner-data loss prohibited by the round-6 instructions.

No paid-dispatch, reservation, claim/cancel fence, or settlement-sweep regression was found in this pass.

## C. Scope

No frozen-engine or second-session-owned file changed. The widened `fuse/`, `enrich/`, and sealed hint-evidence footprint is within the requested fix.

`python -m idea_web.backup backup|verify|restore` is reachable; the accepted CLI aliases, off-box upload, nightly schedule, and parked progress weighting remain correctly deferred.

## D. Tests

The requested `uv` commands could not start because the installed WinGet `uv.exe` shim reports “No application is associated with the specified file.” Direct pytest execution was also blocked before tests ran because the read-only sandbox provides no writable temporary directory.

Read-only collection succeeded:

- `test_ops.py`: 27 tests.
- `test_backup.py`: 47 tests.
- `tests/idea_web`: 300 tests.
- Full suite: 1801 collected, 97 deselected.

The tests are offline by inspection and make substantive assertions, including semantic verification and Windows publication boundaries. However:

- There is no inverse race test where restore begins after `local_database()`’s probe but before migration/open.
- The skipped-media regression requires that the skipped medium have zero captured artefacts (`tests/idea_web/test_backup.py:1616-1620`); there is no mixed captured-medium/skipped-run restore regression.
- No existing assertion was weakened beyond the accepted skipped-media decision, expected schema-version updates, and intentional breaker behavior.

## E. Local mode

`uv run idea serve --no-open --port 8791` failed at the `uv.exe` shim before launch, so `/` could not be probed. The serve entry point, `idea.cmd`, static assets, headers, CSP, and cached-result serving have zero diff. The restore-startup race above nevertheless affects local mode.

## F. Quality

- Direct Ruff check: passed.
- Fixture audit: passed, 506 files.
- `git diff --check`: passed.
- `check_page_js.py`: blocked by the absence of a writable temporary directory.
- The new one-copy operations snapshot removes the previous duplicated database-copy work.
- The report should not describe a captured medium with a skipped run as safely replaceable; that statement encodes the P0 behavior.

## Required fixes

### Realistic

1. **P0** Hold restore exclusion continuously through `local_database()` recovery, database construction, and migration; add a deterministic inverse restore/startup race proving SQLite is never opened concurrently.
2. **P0** Preserve every skipped run’s uncaptured subtree within a captured medium—including `fuse/runs/<run_id>` and any run-owned bundle—and add a restore regression proving those bytes remain identical.

### Adversarial

None.

## Follow-ups, not blockers

- Rerun all exact `uv` gates, `check_page_js.py`, and the live `/` probe in a writable environment with a functioning `uv.exe`.
- CLI aliases, nightly scheduling, off-box upload, and progress re-weighting remain accepted deferrals.

VERDICT: FIX_FIRST