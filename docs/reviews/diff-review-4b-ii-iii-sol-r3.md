## A. Contract violations

1. **P0 — corpus write backstop is bypassable.** Backup validates only `destination/snapshot.json` (`src/idea_web/backup.py:391`), but the frozen helper checks only two ancestor levels (`src/id_detector/io.py:105`). A destination such as `data/corpus/dev-1/backups/snapshot` passed that check in a read-only probe, after which staging/database writes occur at `src/idea_web/backup.py:395-402`. Restore likewise stages safely but publishes with raw `os.replace` calls without checking final targets (`src/idea_web/backup.py:891-906`). An accidental nested corpus destination can therefore modify a frozen corpus.

2. **P0 — terminal-run completeness is still false.** `_referenced()` records terminal runs only inside `for media_dir in media_dirs` (`src/idea_web/backup.py:273-291`). If the copied DB contains a terminal run but its media directory is wholly absent, the run is neither listed nor skipped.

3. **P0 — restore still overlays media absent from the snapshot.** `_stale_artefacts()` derives the media to inspect solely from listed snapshot paths (`src/idea_web/backup.py:774-785`). A newer media tree, or a medium recorded only in `skipped`, is never inspected and survives restore, potentially resurrecting a deleted or newer mix.

4. **P0 — restore is not a power-loss-recoverable boundary.** Renames occur throughout `src/idea_web/backup.py:878-906`, but directory fsync happens only after every rename and covers neither `.replaced` parents nor staging source parents (`src/idea_web/backup.py:877-910`). Power loss mid-loop can lose rollback entries before they become durable. No startup or subsequent-restore recovery consumes an old journal; each restore simply creates a fresh staging directory (`src/idea_web/backup.py:827-830`). Worse, if exception rollback itself fails, errors are suppressed and `finally` deletes the remaining recovery tree (`src/idea_web/backup.py:911-923`).

5. **P1 — the medium lock is held across bulk copying.** `_capture()` performs `shutil.copy2()` while the lock is held (`src/idea_web/backup.py:313-347,421-438`). This contradicts the directed requirement that no medium lock span copying, hashing, or verification and can delay the next scan behind a large fuse/journal copy.

Positive confirmations: `src/id_detector/`, profiles, golden artefacts, and all second-session-owned files have zero diff. U-F9 weighting is unchanged. The attempt ledger remains append-only, hosted paid dispatch remains refused, and the money-authority tables/fences/sweep are unchanged in substance.

## B. Correctness bugs

1. **P1 — old breaker samples can generate multiple trips.** Once cooldown expires, `_judge()` re-trips from the same ledger rows without requiring a newer resolved event (`src/idea_web/breaker.py:224-238`). With `cooldown_seconds < window_seconds`, repeated `reason()` calls can latch after one incident; the test positively expects this at `tests/idea_web/test_ops.py:247-251`, unlike the original process breaker which requires another `resolved()` call.

2. **P1 — terminal page spend is not read from the SQLite authority.** `queue.terminal()` folds durable events and preserves the maximum spend (`src/idea_web/jobs/worker.py:1885-1905`), but the page document is computed beforehand from `RunResult.usd_e6_spent` (`src/idea_web/jobs/worker.py:2687-2699`). If recovery finds more durable spend than the result reports, SQLite is correct while the page understates cost.

3. **P1 — completion-sidecar verification silently accepts missing ordinary upstreams.** `_verify_sidecar()` reports a mismatch only when an upstream exists with different bytes (`src/idea_web/backup.py:700-705`). Missing plain-hash upstreams pass, even though the frozen verifier requires an explicit `pruned_upstream` marker; schema version is also unchecked.

4. **P1 — copied snapshot bytes are published without durability.** `shutil.copy2()` is not followed by a file or nested-directory fsync (`src/idea_web/backup.py:346`); only the staging root is fsynced before publication (`src/idea_web/backup.py:495-501`). A power loss after a successful return can leave a sealed snapshot missing copied fuse/journal bytes.

5. **P1 — operations can create SQLite sidecars.** `mode=ro` at `src/idea_web/ops.py:62-70` prevents database creation and journal-mode changes, but a WAL reader can materialise `-shm`. The test explicitly accepts that write (`tests/idea_web/test_ops.py:761-769`), contrary to the Round 3 requirement of no WAL sidecar creation.

Migration 0004 passed an independent populated in-memory `0003 → 0004 → 0003` drill: versions changed cleanly, the breaker table was added/removed, and all three money-authority rows survived.

## C. Scope

The accepted `python -m idea_web.backup` spelling and deferred off-box/nightly work are correctly scoped and reachable. No frozen engine, playlist, README, launcher, theme, profile, corpus, or golden file was changed.

Required scope remains incomplete because snapshot completeness, corpus confinement, and crash-recoverable replacement are not achieved.

## D. Tests

The named gate files are present and offline, but several Round 3 regressions are ineffective or missing:

- The “concurrency” test runs GC inside the lock-holder callback and waits for it to finish before starting backup (`tests/idea_web/test_backup.py:393-403`); GC and backup are not simultaneous, and no production bundle-commit operation runs.
- The “power loss” test raises a catchable `OSError`, allowing ordinary rollback and `finally` cleanup (`tests/idea_web/test_backup.py:550-576`). The journal test merely copies its document into memory (`tests/idea_web/test_backup.py:579-599`); neither test exercises recovery after abrupt process death.
- Stale-removal coverage adds a bundle beneath media that is present in the snapshot (`tests/idea_web/test_backup.py:502-514`); it misses an entirely absent or skipped medium.
- No test covers a terminal DB run with no media directory.
- No regression covers authoritative folded spend exceeding `RunResult` spend or a cache hit across distinct source aliases.
- The read-only operations test explicitly permits `-shm` creation.
- Junction coverage tests only `contained()` with an inner junction (`tests/idea_web/test_backup.py:684-702`), not backup source roots, destination aliases, or restore roots.
- Existing committed assertions were not materially weakened beyond expected schema-version updates; the new operations test itself asserts a weaker contract.

Exact `uv` commands could not execute because the sandbox cannot launch the WinGet `uv.exe` shim. Direct Ruff and fixture audit passed; direct pytest and the JS checker could not start because no writable temporary directory exists.

## E. Local mode

`uv run idea serve --no-open --port 8791` was blocked by the same `uv.exe` shim. Direct `idea.exe serve` reached local database setup, then the read-only sandbox denied creation of `work/.idea`, so `/` could not be probed. Static routing, headers, CSP, assets, `idea.cmd`, and the cached-open implementation are unchanged.

## F. Quality

`git diff --check`, direct Ruff, and direct fixture audit pass.

`captured` is accumulated and then discarded without use (`src/idea_web/backup.py:415-435,484`), while the reported `linked` count is based on entry kind rather than whether `os.link()` actually succeeded (`src/idea_web/backup.py:339-346,519`). Both are misleading dead/reporting logic.

## Required fixes

### Realistic

1. **P0** Reject any backup or restore location anywhere beneath `data/corpus` and apply the corpus backstop to every final write, rename, and deletion target.
2. **P0** Record every terminal DB run with no discoverable media directory as explicitly skipped, with a regression for the zero-directory case.
3. **P0** Replace stale owned subtrees across the entire locked work root, including media absent or skipped in the snapshot.
4. **P0** Make restore recovery durable per rename by fsyncing both parents, retain rollback data when rollback is incomplete, and recover or resume an interrupted journal before any new restore/server use.
5. **P1** Move fuse/journal copying outside the medium lock while preserving a stable captured view, and fsync every copied file and nested directory before snapshot publication.
6. **P1** Persist a last-evaluated event/cursor so one set of breaker samples cannot create multiple opens without a new resolution.
7. **P1** Publish page spend from the folded SQLite settlement authority, not directly from `RunResult`.
8. **P1** Validate completion-sidecar schema, missing upstreams, and explicit `pruned_upstream` markers with the frozen verifier’s semantics.
9. **P1** Make operations inspection create no `-wal` or `-shm` files and assert their absence.
10. **P1** Replace the concurrency and power-loss simulations with regressions that genuinely overlap production commit/GC/backup and restart after an interrupted publication.

### Adversarial

11. **P1** Reject symlink/junction/reparse roots and source directories themselves, not only components beneath a resolved root.
12. **P2** Create staging directories exclusively and report actual hard-link successes rather than classifying every bundle entry as linked.

VERDICT: FIX_FIRST