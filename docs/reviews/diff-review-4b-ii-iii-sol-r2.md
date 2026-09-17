## A. Contract violations

1. Backup completeness is still incomplete. `_referenced()` only records a run when its fuse directory already exists (`src/idea_web/backup.py:224-234`), and `_publication()` silently omits missing or unsealed run directories (`src/idea_web/backup.py:285-293`). A terminal DB run with no bundle/fuse artefact—or an analysis row between intake commit and media-lock acquisition—can therefore be sealed without either the artefact or a `skipped` record. This does not satisfy the required “every DB-referenced bundle/run is present, verified, or explicitly skipped” invariant.

2. Sealed snapshots are not immutable as destinations. `backup()` reuses the caller’s directory, deletes its existing `app.db` (`src/idea_web/backup.py:329-338`), overwrites listed files, and rewrites `snapshot.json` directly (`src/idea_web/backup.py:464-467`). Re-running backup into a sealed snapshot mutates or corrupts it; a crash during sealing can truncate the only manifest.

3. The hard-link classification violates the directed design. `fuse_run` is declared linkable (`src/idea_web/backup.py:73-74,409`) although it is addressed by caller-supplied `run_id`, not a content address (`src/idea_web/backup.py:231-234`). Its manifest makes it immutable in normal application code, but it does not meet “only immutable, content-addressed artefacts may be hard-linked.” Classification:

   - `app.db`: SQLite copy.
   - Bundles: hard-linked.
   - Fuse runs: hard-linked incorrectly.
   - `recognise/`, `hints/`, `ingest/source.json`: copied.
   - `snapshot.json`: newly written, non-atomically.

4. Backup does wait behind scans: once per referenced medium it retries `.media.lock` for up to one second (`src/idea_web/backup.py:69-72,364-374`). With multiple busy media this accumulates serially. Once acquired, the lock is held through manifest verification, hashing, and copying every mutable artefact (`src/idea_web/backup.py:385-423`), so a backup can also delay a scan or bundle commit; GC correctly skips if backup wins. If GC wins first, backup waits and then skips.

5. Restore does not hold the supervisor lock across the whole operation: snapshot/hash verification occurs before acquisition (`src/idea_web/backup.py:663-683`). A custom `--database` is not confined to the supplied work root, while the lock is always derived from that work root (`src/idea_web/backup.py:670-681`), allowing an unrelated live database to be overwritten without its supervisor lock.

6. Positive confirmations: `src/id_detector/`, profiles, all second-session-owned files, and the frozen page/theme surfaces have zero diff. U-F9 weighting is unchanged. `provider_attempt_events` remains append-only. Rule (b) now counts `dispatched`, and hosted paid refusal remains ahead of the new breaker logic.

## B. Correctness bugs

1. A valid CLI argument can delete the live money-authority database. With `--into work/.idea`, destination `app.db` equals the source database; line 338 unlinks it before `database.read()` opens it (`src/idea_web/backup.py:335-340`). This is a realistic path-selection accident and a P0 data-loss/money-authority failure.

2. Restore publication is not crash/power-loss atomic. It replaces artefacts one file at a time, then the database (`src/idea_web/backup.py:717-732`), fsyncs only the database parent (`src/idea_web/backup.py:733`), and deletes the rollback tree in `finally` (`src/idea_web/backup.py:734-748`). A power loss can leave a partial tree or non-durable directory entries; the OSError regression tests exception rollback, not power loss.

3. Restore overlays rather than replaces the durable tree. Files absent from the snapshot are never removed (`src/idea_web/backup.py:717-732`). Local discovery scans the filesystem (`src/idea_web/application.py:190-197`), so a newer or previously deleted bundle left in `work/` can remain visible after restoring an older snapshot.

4. Production bundle paths are not portable across restore. The worker stores absolute paths (`src/idea_web/jobs/worker.py:1917-1919`); restore copies the DB unchanged into a potentially different work root (`src/idea_web/backup.py:695-696,731`). The restored DB then points at the old machine/tree, making compatibility lookup and subsequent backups fail. The text fixture uses a relative path and misses this production case.

5. Manual/configured breaker re-enable is ineffective while qualifying failures remain in the five-minute ledger window. Re-enable clears only persisted latch/open fields (`src/idea_web/breaker.py:185-205,290-309`); it cannot clear or cutoff the samples read at `src/idea_web/breaker.py:135-153`. The next `reason()` immediately reopens, unlike the original process breaker, which clears its sample deque.

6. Cache-hit result URLs can be wrong across aliases. Compatibility selects a stored bundle by `media_key` and validates its actual DB path, but returns only IDs (`src/idea_web/jobs/worker.py:2387-2413`). The cache-hit branch reconstructs the URL beneath the new intake’s media directory (`src/idea_web/jobs/worker.py:2438-2447`). Two source keys producing identical audio can therefore return a successful job whose link 404s.

7. Durable terminal progress does not carry exact spend. `PageProgress.settle()` records status/reason but never `usd_e2_spent` or `spend_known` (`src/idea_web/progress.py:175-213`), and the worker does not pass `RunResult.usd_e6_spent` (`src/idea_web/jobs/worker.py:2679-2693`). A Deep terminal page consequently says its cost record could not be read despite SQLite holding the exact settlement.

8. Operations “show” is not strictly read-only at the SQLite boundary. It constructs ordinary `Database` connections (`src/idea_web/ops.py:297-305`), whose `connect()` creates missing database files and executes writable `PRAGMA journal_mode=WAL` (`src/idea_web/database.py:178-189`). A typoed path creates a database; a non-WAL database is modified merely by viewing it.

9. The parked-job deferral is as described but user-visible behavior is harsher than “no card”: `wait()` replaces the page document (`src/idea_web/jobs/worker.py:1969-1978`), and `job_view()` rejects the row (`src/idea_web/jobs/local.py:192-205`). The owner gets `404 unknown job`, it disappears from activity, and cannot be cancelled through the UI. It is a Free job parked before intake/reservation, so no money is at risk; the queue row and reason remain durable and visible in operations. This is safe to carry as an explicit UX follow-up, not a commit blocker.

## C. Scope

The accepted CLI spelling and off-box/nightly deferrals are correctly recorded. No frozen or second-session-owned path was touched, and no out-of-phase upload/scheduler work was added.

Required scope remains incomplete because snapshot sealing, portable restore, crash-safe publication, and immediate breaker re-enable do not yet meet the directed designs.

## D. Tests

The named gate files are present and offline, but important assertions are missing:

- The supposed overlap invokes GC and backup sequentially (`tests/idea_web/test_backup.py:247-251`); no bundle commit runs, and `gc_actions is not None` is vacuous (`tests/idea_web/test_backup.py:264-267`).
- No timing assertion catches the one-second-per-medium scan wait.
- No regression covers source/destination DB aliasing, reuse of a sealed destination, power loss, stale unlisted artefacts, portable absolute DB paths, or terminal runs missing fuse artefacts.
- Breaker tests avoid immediate re-enable by aging failures beyond the window (`tests/idea_web/test_ops.py:262-286`) and do not concurrently test cross-process trip updates.
- The Deep regression checks only the profile (`tests/idea_web/test_ops.py:538-557`); the URL test is a same-directory Free cache hit.
- Path tests exercise hostile strings but no actual symlink or Windows junction (`tests/idea_web/test_backup.py:504-530`).
- Existing committed assertions were not weakened beyond expected schema-version changes; the prior breaker test now appropriately asserts queue parking.

The exact `uv` commands could not start because the sandbox cannot execute the WinGet `uv.exe` shim. Direct pytest and the JS checker also stopped before collection because no writable temporary directory exists. Direct Ruff check passed, and the fixture audit passed (`506 files`). Ruff format could not create its temporary file.

An independent in-memory populated `0003 → 0004 → 0003` SQL drill passed and preserved rows in `run_reservations`, `run_dispatches`, and `run_settlements`; 0004’s `down` removed only breaker state. The version-only test edits are sound.

## E. Local mode

`idea serve --no-open --port 8791` reached local database setup but the sandbox denied creation of `work/.idea`, so `/` could not be probed. Static local surfaces, `idea.cmd`, cached-result routing, headers, CSP, assets, and all `src/id_detector/` files are unchanged.

## F. Quality

`git diff --check`, direct Ruff check, and fixture audit pass. Stale comments still claim rule (b) counts prepared attempts (`src/idea_web/jobs/worker.py:2106`, `src/idea_web/migrations/0004_shared_breaker.up.sql:5`). The restore drill’s relative DB path is not representative of production, and the backup gate does not implement its documented concurrency scenario.

## Required fixes

### Realistic

1. **P0** Refuse any backup destination overlapping the source database or work tree, build snapshots in a fresh staging directory, and atomically publish once while refusing reuse of a sealed destination.
2. **P0** Require every copied-DB terminal run/bundle to have verified listed artefacts and mark every non-terminal run as skipped even when its media lock happens to be momentarily free.
3. **P0** Publish restore as one durable recoverable boundary, fsync every renamed parent, remove artefacts absent from the snapshot, and retain enough rollback state to recover after power loss.
4. **P0** Confine the restore database to the locked work root—or acquire the correct database’s supervisor lock—and rewrite/normalize absolute DB artefact paths for the restored root.
5. **P1** Copy fuse-run files instead of hard-linking them, and make busy-medium lock acquisition genuinely non-waiting and bounded independently of mutable-tree copy duration.
6. **P1** Persist a re-enable cutoff/generation timestamp so pre-re-enable failures cannot immediately reopen the shared breaker.
7. **P1** Open operations databases read-only without creating files, WAL sidecars, or changing journal mode.
8. **P1** Carry the selected DB bundle path through cache-hit rendering and publish exact terminal spend into the page document.
9. **P1** Add regressions for the failures above and a real simultaneous bundle-commit/GC/backup gate; each regression must fail when its corresponding fix is removed.
10. **P2** Preserve the parked job’s page document in a follow-up so it remains visible and cancellable while waiting.
11. **P2** Correct stale prepared-versus-dispatched comments and make the restore fixture use production path semantics.

### Adversarial

12. **P2** Reject actual symlinks and Windows junction/reparse points explicitly; `is_symlink()` alone at `src/idea_web/backup.py:167-172` does not prove the claimed Windows reparse-point rule.

VERDICT: FIX_FIRST