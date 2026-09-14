# Follow-up: Windows long-path safety in retention (`idea gc`)

Cycle 2b (`6825ade`) follow-up. Verifier-then-fixer, run in an isolated worktree on base `ec32f97`.

> Worktree note: the harness created this worktree at a stale commit (`9bfacf8`, which has no
> `retention.py`). Its tree was clean, so the worktree's own branch was reset to `ec32f97`
> before any work started. Nothing in the owner's checkout was touched.

## Machine facts

- `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled = 0x0`.
- pytest's shared temp counter is now `pytest-1016`+ (it was `pytest-1015` when the orchestrator
  measured it).
- A probe with a 299-character file path, made through `atomic_write_bytes`, showed:
  - `Path.rglob("*")` listed the 293-character directory but silently did not descend into it.
  - `is_file()`, `exists()` and the parent's `is_dir()` all answered `False`.
  - `stat()` and `iterdir()` raised `FileNotFoundError`.
  - `shutil.rmtree(<plain>)` raised `FileNotFoundError`.
  - `shutil.rmtree("\\?\...")` worked.
- The owner's real tree (listed read-only; `gc` was never run on it) has files of up to 294
  characters (`recognise/invocations/<id>/raw/<64-hex>.json`) and invocation-level sidecars
  of about 248 characters.

## STEP 1: reproduction against unmodified `retention.py`

These tests were added to `tests/test_phase2b_retention.py`. Every file is seeded through
`atomic_write_bytes`/`atomic_write_json` and every assertion uses extended paths. By default the
work root is padded so the media directory is 205 characters long: under the 248-character
directory limit, like the owner's (~175). Real artefact names then land past 260: a sealed
`observations.gen0.jsonl` sidecar at ~272, `raw/<64-hex>.json` at ~320, `present/bundles/<64>/index.html` at ~297.

Run on `ec32f97` `retention.py`, default temp root: **5 failed, 1 passed**.

| Misbehaviour asked about | Reproduced? | Evidence (failing test on the old code) |
|---|---|---|
| Manifest inventory silently skips files | **YES** | `test_long_paths_manifest_inventories_every_file`: the manifest omitted `recognise/invocations/<id>/observations.gen0.jsonl`, its `.done.json`, and `raw/<64>.json`, so `total_size` under-reported. Cause: `rglob` does not descend past MAX_PATH, and `is_file()` answers False there. |
| Downstream evidence not sealed before an upstream moves | **YES** (found during verification) | `test_long_paths_sidecar_is_sealed_before_its_upstream_moves`: `observations.gen0.done.json` kept the raw hash `ca3d16…` instead of `{"pruned_upstream": "ca3d16…"}`. Its upstream `windows/` was still moved, so that artefact would later fail verification as "upstream hash differs" rather than "pruned". Same `rglob` cause. |
| Purge of `.trash` fails | **YES: `gc --apply` crashes** | `test_long_paths_expired_media_is_trashed_then_purged`: the move into `.trash/2026-09-11/…` worked (it already used `native_path`), but the purge nine days later raised `FileNotFoundError` from `shutil.rmtree(action.path)` at `retention.py:476`. The expired trash is never reclaimed, and every later `gc --apply` crashes the same way. |
| Dry run plans differently from what `--apply` does | **YES** | `test_long_paths_dry_run_plans_exactly_what_apply_does`: the dry run planned the moves plus a purge, but `--apply` raised at `retention.py:476` while purging a long `.trash/2026-09-01/…` tree. The CLI reports nothing it applied. |
| File wrongly treated as absent (kept) | **YES**, once the media dir itself passes MAX_PATH | `test_media_directory_beyond_max_path_is_still_collected` (work root padded so the media dir is 275 characters): the dry-run plan was `[]` instead of `[("move", <media>/windows)]`. `media_dir.is_dir()` (~180) answers False, so the whole media dir is silently ignored. This is not reached at the owner's current 43-character work root, but it is reached by any work root about 85+ characters longer. |
| Referenced bundle or original treated as missing, so the media is collected | **NO in this pass, WRONG: superseded below.** This test kept `present/bundles` itself under 260, so the verdict rested on code inspection. With the bundles directory past 260, `main` does collect the referenced media; see the second-pass section. | `test_long_paths_published_bundle_blocks_collection` passed on the old code. `_has_publication_reference` already counts *any* `present/bundles` entry via `iterdir()` of the ~190-character bundles dir, the original check already used `path_is_file`, and `present/current` and `index.json` already used `read_text`. Kept as a regression guard. |
| Move to `.trash` fails | **NO in this pass, WRONG: superseded below.** No test put the move operands themselves past 260, so this was code inspection. With the target past 260, `main` silently skips the move while reporting it; see the second-pass section. | Covered by the trash and purge tests: `_move_to_trash` already used `native_path` for `makedirs`/`os.replace`, and `destination.exists()` is only asked about short relative targets. |

## STEP 2: what changed

### `src/id_detector/retention.py`

Every filesystem question GC asks now goes through long-path-safe primitives. The `Path`
values GC reports and compares keep their ordinary spelling, so `GCAction` output and the CLI
are unchanged.

- Existence, directory and stat checks use `io.native_path`:
  - `_exists`/`_is_dir` replace every `exists()`/`is_dir()` (work root, source/media dirs, `.trash`, trash destinations, `windows/`, move targets, `media_dir` before the manifest, dated trash dirs).
  - `_children` (via `os.listdir`) replaces `iterdir()`.
  - The manifest uses `os.stat(native_path(item))`.
  - Sibling lookup uses `io.path_is_file`.
- `_files(root)` replaces `rglob` in `_rewrite_sidecars` and `_write_manifest`. It is an
  `os.scandir` walk over extended paths, sorted, that skips a directory it may not list (as
  `rglob` does). **It is slightly stricter than `rglob`:** it does not descend into symlinked
  or **junctioned** directories. 3.12's `rglob` follows junctions, which could have inventoried
  files outside the media dir or rewritten sidecars there.
- `glob("original*")` and `glob(f"{base}.*")` became `fnmatch` over `_children`. `fnmatch`
  is case-insensitive on Windows, like pathlib's glob.
- The purge now runs `shutil.rmtree(_extended(path))`, guarded by `_is_dir` and
  `not _is_link` (`islink` **or** `isjunction`, on the unresolved extended path).
- Link resolution `_real`/`_resolved` (used by the reparse-point and within-root checks, and by
  `_original_path`/`_is_removed`/`collect`) is now `os.path.realpath` over the unresolved
  extended form, with the `\\?\` prefix stripped. Plain `Path.resolve()` past MAX_PATH hits
  `ERROR_PATH_NOT_FOUND` and gives up resolving the tail, so a junction deep in a long path
  could have escaped the containment check.

**The one new helper is `_extended` (in `retention.py`, not `io.py`).** `io.native_path` calls
`Path.resolve()` first, which is correct for reading a file but wrong wherever the link itself is
the subject:

- resolving a path, since resolving needs the unresolved spelling as input;
- asking whether a path *is* a link or junction;
- `rmtree`, which must never be handed a junction's *target*.

`io.py` was left untouched because another agent is editing it.

**Safety properties preserved:**

- Nothing is deleted in place: moves still go to `work/.trash/<date>/`.
- A purge still happens only after `_TRASH_GRACE` (8 days).
- A reparse point or junction is still refused at `.trash`, at dated trash dirs, and at the
  source and media levels (resolved path must equal the walked path).
- Every artefact target still passes `_within(target, media_dir)`, and the destination parent
  still passes `_within(…, trash_root)`.
- Anything unreadable still counts as a reference: `listdir` `OSError` other than
  `FileNotFoundError` returns True, as does an unreadable `current`/`index.json`.
- Dot-directories (including `work/.idea/app.db`) are still skipped.
- All mutations still happen under the media lock.

The existing junction tests (`test_reparse_point_*`) ran, not skipped, and pass.

### `tests/test_phase2b_retention.py`

- `_seed_media`, `_journal` and every inline fixture write now go through `_write`
  (`atomic_write_bytes`). The hour fixture uses `os.unlink`/`os.link`/`open` on `native_path`.
- Every existence and absence assertion uses `_exists`/`_is_file`/`_is_dir` (extended path).
  A plain `assert not path.exists()` past MAX_PATH passes whether or not the file is really
  gone, so the old absence checks were vacuous at long paths.
- The dry-run "nothing changed" comparison uses `_tree()` (`os.walk` over the extended path)
  instead of `rglob`, which silently misses long files.
- Added the six real-length tests in the table above.
- `--basetemp`, `addopts` and fixture-path lengths were **not** changed.

## At-risk seeding helpers elsewhere (P2: none fail now, not changed)

These estimates assume today's temp root, about
`C:\Users\natha\AppData\Local\Temp\pytest-of-natha\pytest-10xx\<test name, at most 30 chars>0\`
(~93 characters). Each helper writes with plain `pathlib`/`wave` under a
`<64-hex>/<64-hex>` media tree, so it will start failing, as `_seed_media` did, once its
deepest path passes ~259 characters: a longer home dir, a longer test name, or pytest's counter
reaching `pytest-10000`.

| Helper / site | Deepest plain write (est.) | Headroom |
|---|---|---|
| `tests/test_stage7_server.py::_seed_work_root` (also used by `test_phase1a_bundles.seed` and `test_phase1a_cached_open`) | `<root>/<64>/<64>/fuse/identities.gen0.json` ≈ 249 | ~10 chars |
| `tests/test_stage7_server.py` inline writes (lines ~128–181, `tmp/wr/<64>/<64>/fuse/…`, `decode/pcm.json`) | ≈ 250 | ~9 chars |
| `tests/test_stage10_webapp.py::_seed_fetched_mix` | `<64>/<64>/ingest/original.m4a` ≈ 243 | ~16 chars |
| `tests/test_truth_review.py::_seed_work` | `work/<64>/<64>/ingest/tone.wav` ≈ 244 | ~15 chars |
| `tests/test_phase3a_honesty.py` lines ~506–510 and ~708–710 (`invocations.jsonl` under `a*64/b*64`, `c*64/d*64`) | ≈ 242 | ~17 chars |
| `tests/test_playlists.py` lines ~123–125 (`present/tracklist.json`) | depends on its media root; **second session's file, not touched** | — |

Not at risk: the `tests/test_phase1a_bundles.py` and `tests/test_phase1a_cached_open.py`
(lines ~89–97) writes and walks on `seed()`/`publish()` results (already `\\?\` paths), and the `_write_clip` helpers in `test_phase0b_attempts.py`,
`test_paid_clip.py` and `test_local_index.py` (`tmp_path/"media"`, not hex-nested).

## Gates (default temp root)

```
$ uv run pytest tests/test_phase2b_retention.py -q          # before the fix (ec32f97)
4 failed, 27 passed, 5 warnings in 167.89s (0:02:47)

$ uv run pytest tests/test_phase2b_retention.py -q -k "long_paths or beyond_max_path"   # new tests, old retention.py
5 failed, 1 passed, 31 deselected, 1 warning in 8.29s

$ uv run pytest tests/test_phase2b_retention.py -q          # after
37 passed, 1 warning in 29.55s

$ uv run pytest -q
........................................................................ [ 94%]
................s....................................................    [100%]
1292 passed, 1 skipped, 93 deselected, 1 warning in 572.55s (0:09:32)
(the one skip is outside test_phase2b_retention.py: that file ran 37/37 with no skips, so both
junction tests really executed)

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
300 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 459 files
fixture audit passed

$ git status --short -- work data
(empty)
```

Final foreground re-run of the retention file, default temp root (`pytest-1024`), `-rs`:

```
$ uv run pytest tests/test_phase2b_retention.py -q -rs
37 passed, 1 warning in 29.68s
```

All four tests that failed on `main` now pass under the default root: `test_success_statuses_…[complete|degraded|partial]` and `test_reparse_point_cannot_move_files_outside_work`. No `--basetemp` or `addopts` was used.

**PowerShell gates (`smoke_serve.ps1`, `gate_local_mode.ps1`) were not run by this agent.**
- This session's worktree-isolation guard refuses to launch `powershell`.
- The orchestrator asked me not to run them: it is using ports 8791/8792 for another fix and
  will run both gates against this code itself.
- The `.sh` twin was not used as a substitute.
- `retention.py` is not on the serve path.

Cleanup:
- `git status --short -- work data` is empty.
- `src/id_detector/io.py` was **not** edited.
- Every process this agent started has exited: the two pytest runs and the one-shot probe. No server was started.
- The probe's temporary `lp-probe-*` directories were removed.

## Second-model review (sol xhigh) + fix pass

The review was Codex gpt-5.6-sol at xhigh, read-only, and returned FIX_FIRST (full text in the
orchestrator's scratchpad as `diff-review-followup-longpath-sol.out.md`). Its central point:
every mutation GC makes (rename, `makedirs`, sidecar or manifest write, `rmtree`) could
follow a link out of the work root or the dated trash. Several of those paths already exist in
the committed 2b code on `main`.

### How reproduction was proven

Each finding got a regression test in `tests/test_phase2b_retention.py`, and the new tests were
run, in the foreground under the default temp root, against three versions of `retention.py`:

- **v0**: `main` at `ec32f97`;
- **v1**: my first long-path pass;
- **v2**: this pass.

`jobs.py` and `io.py` are unchanged in all three.

Platform probes (Windows 11, `LongPathsEnabled = 0`):
- Unprivileged file symlinks work here.
- Junctions work.
- `icacls /deny (RD)` makes `listdir` raise `PermissionError`.
- `shutil.rmtree("\\?\…")` removes an inner junction without following it.
- A dangling junction is absent to `os.path.exists` but present to `lexists`, and `native_path`
  resolves it to its target.
- The owner's real `work/` tree has **no** reparse points (1246 files and their directories,
  read-only `lstat`; `gc` was never run on it).

| Finding | Reproduced? | Test | v0 main | v1 first pass | v2 |
|---|---|---|---|---|---|
| P0-1a: an in-media junction is accepted and its **target** is moved | **YES** | `test_linked_windows_directory_is_refused_not_its_target_moved` | FAIL: `kept-windows` moved (`{} == {...}`) | FAIL: same | pass |
| P0-1a: an in-media file symlink's target is moved | **YES** | `test_linked_pcm_file_is_refused_not_its_target_moved` | FAIL: `elsewhere.pcm` gone | FAIL: same | pass |
| P0-1b: a dangling link at the trash destination sends the owner's file outside | **YES** | `test_dangling_link_at_trash_destination_is_never_followed` | FAIL: `windows/` landed in `outside/landing` | FAIL: same | pass |
| P0-1c: `makedirs` runs before containment, through a dated-trash junction | **YES** | `test_linked_trash_ancestor_creates_nothing_outside` | FAIL: directories created outside, then `OSError` crashed `gc --apply` (line 291) | FAIL: same (line 380) | pass |
| P0-2: a `*.done.json` file symlink's target is overwritten | **YES** | `test_linked_sidecar_is_never_written_through` | FAIL: victim bytes changed | FAIL: same | pass |
| P0-2: a `retention-manifest.json` file symlink's target is overwritten | **YES** | `test_linked_manifest_is_never_written_through` | FAIL: victim now holds the manifest | FAIL: same | pass |
| P0-3: `.trash` swapped for a junction after planning; the external dated tree is purged | **YES** | `test_trash_swapped_for_a_link_after_planning_is_not_purged` | FAIL: `outside/2026-09-01/precious` deleted | FAIL: same | pass |
| P1-4: an unlistable subtree is skipped, so moves go ahead and the manifest is incomplete | **YES** | `test_unlistable_subtree_fails_closed_for_its_media` | FAIL: PCM move planned and applied | FAIL: same | pass |
| P1-5: `ProcessLock` gives two spellings of one long media path different keys | **NO, demonstrated** | `test_media_lock_is_one_lock_for_two_spellings_past_max_path` | pass | pass | pass |
| P1-6a: the `present/bundles` directory itself past 260 | **YES on `main`** (my first-pass "NO" was wrong) | `test_long_paths_bundles_directory_past_max_path_blocks_collection` | FAIL: referenced media moved to trash | pass | pass |
| P1-6b: rename operands past 260 | **YES on `main`** (my first-pass "NO" was wrong) | `test_long_paths_trash_move_operands_past_max_path` | FAIL: PCM not moved although reported "move" | pass | pass |
| P1-6b: original past 260 follows its policy | **YES on `main`** | `test_long_paths_original_past_max_path_follows_its_policy` | FAIL: expired hosted original not moved although reported | pass | pass |

Totals for the 20 long-path and link tests: v0 16 failed, 4 passed; v1 8 failed, 12 passed;
v2 all pass. The 4 that pass on v0 are the two media-lock tests, the first-pass bundle test and
the P1-5 test.

**P1-5 detail (not reproduced, equivalence shown).** Both spellings are over 260 characters:
the real path, and an alias through a junction (294/296 and 344/346 in the probe). Both
produce the same `ProcessLock.path`. The test also shows that a second **process** using the
alias spelling gets `JobStoreLocked` while the real spelling is held, and acquires once it is
released. Why: CPython 3.12's `ntpath.realpath` returns a `\\?\` path when the final path
exceeds MAX_PATH, and the constructor already strips that prefix. `jobs.py` was **not**
changed.

**P1-6 detail (what `main` does past MAX_PATH).**
- `_has_publication_reference` lists `present/bundles` with plain `iterdir()`. Past 260 that
  raises `FileNotFoundError`, which the code reads as "no bundle here", so a published media
  directory is collected.
- The apply loop guards every rename with plain `target.exists()`. Past 260 that answers
  False, so the rename is silently skipped while the action list and CLI still report `move`.
  Because of that, the collision-overwrite half of the operands test is never reached on
  `main`. On v1 and v2 it passes: an earlier trashed copy survives and the new one lands at
  `audio.pcm.1`.

Both were already fixed by v1, but v1's record called them non-reproductions, based on code
inspection. That is corrected in the table above.

### What changed in this pass (`src/id_detector/retention.py` only)

**Mutations never follow links.**
- `_stat_is_link` treats any symlink or `FILE_ATTRIBUTE_REPARSE_POINT` as a link. That covers
  junctions and every other reparse point, so GC fails closed and the artefact is simply kept.
- `_unlinked(path, root)` requires *path* to be lexically inside *root*, with no `.`/`..`
  parts, and no existing step from *root* down to *path* (both included) to be a link. An
  inspection error counts as refused.

**P0-1: moves.**
- `_targets` refuses any candidate that is, or sits under, a link, and reports it as a `skip`.
- `_original_path` keeps the lexical spelling instead of resolving, so a linked original is
  refused rather than its target collected.
- `_trash_destination` detects collisions with `lexists`, so a dangling link is an occupied
  name.
- `_move_to_trash` works in this order:
  1. prove the source is unlinked;
  2. `_checked_destination` proves the destination parent is unlinked, *before* `makedirs`;
  3. `makedirs(_extended(parent))`;
  4. re-prove parent containment, `_within`, absence of the destination, and the source;
  5. `os.replace(_extended(source), _extended(destination))`, with unresolved operands.
- A refusal is a `_Refused` (a subclass of `OSError`), recorded as a `skip` for that target.
- The dry run calls the same `_checked_destination`, so it plans exactly the same skips.

**P0-2: sidecar and manifest writes.**
- `_files` walks with `entry.stat(follow_symlinks=False)` and neither lists nor descends into
  links.
- `_rewrite_sidecars` works only from that link-free inventory and re-proves each sidecar
  (`_is_real_file` and `_unlinked`) immediately before `atomic_write_json`. A refusal skips
  the media before anything moves.
- `_write_manifest` refuses a manifest path that is a link, sits under one, or is not a plain
  file. The refusal is recorded as a `skip` on `retention-manifest.json`; the recoverable
  moves have already happened.

**P0-3: purges.**
- `_purgeable` is re-proven immediately before each `rmtree`:
  - the parent is exactly `work/.trash`;
  - both `.trash` and the dated directory are real, non-reparse directories according to
    `lstat`;
  - the whole chain is unlinked;
  - both resolve to themselves.
- Otherwise the planned purge becomes `skip: trash changed since planning; not purged`.
- `_trash_root` and `_purge_actions` now also use `lstat`, so a dangling `.trash` link is
  refused.

**P1-4: fail closed.**
- `_files` no longer swallows `PermissionError`: any listing error raises.
- In both dry run and apply, the media's complete listing is taken before anything else. If
  it fails, the media gets `skip: media tree cannot be fully listed` and nothing moves.
- A listing failure while writing the manifest (a race after the moves) writes no manifest and
  records a skip.

**Preserved exactly** (the reviewer confirmed these, and the existing tests still pass):
- The `_TRASH_GRACE` 8-day proof.
- Nothing is deleted in place.
- Source-level dot-directories (`.idea`, `.trash`) are skipped.
- A reference `OSError` fails closed.
- Every `relative_to` / `is_relative_to` / equality comparison uses `_resolved` with the `\\?\`
  prefix stripped.
- Everything happens under the media lock.
- `_write_manifest`'s test seam (`media_dir` positional, the rest as keywords) is unchanged.

**Residual risk (P2, documented rather than fixed).**
- **Check-then-act races.** Each check is repeated immediately before its mutation, but a
  hostile process racing between that check and the syscall (for example swapping a directory
  for a junction in that window) is not excluded. Doing so would need handle-relative Win32
  operations. The threat this closes is links that already exist when GC runs, or appear
  between planning and execution.
- **Cloud placeholders.** A OneDrive or cloud-file placeholder is a non-surrogate reparse point,
  so GC would now keep such artefacts. The owner's tree has none today.

### Tests added in this pass

- The 8 link and fail-closed regressions and the P1-5 contention test, as named in the table.
- The 3 P1-6 tests, each asserting that the exact listed directory or rename operand is over
  260 characters.
- New helpers: `_junction` (uses `_winapi.CreateJunction` with the extended link path, because
  `mklink` fails past 248 characters), `_file_link`, `_lexists`, and `_unlistable` (an `icacls`
  deny, always restored).
- No `--basetemp`, `addopts` or fixture-path shortening.

### Gates (default temp root, foreground)

```
$ uv run pytest tests/test_phase2b_retention.py -q -rs      # final bytes, default root pytest-1043
49 passed, 1 warning in 56.63s                               # no skips: every junction/symlink/icacls test ran

$ uv run pytest -q
1304 passed, 1 skipped, 93 deselected, 1 warning in 586.80s (0:09:46)
exit=0                                                       # the one skip is outside test_phase2b_retention.py

# New tests (-k long_paths|beyond_max_path|linked|dangling|swapped|unlistable|media_lock|
# bundles_directory|original_past|move_operands), against each reverted retention.py:
v0 main ec32f97 : 16 failed, 4 passed
v1 first pass   :  8 failed, 12 passed
v2 this pass    : 20 passed

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
301 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 460 files
fixture audit passed

$ git status --short -- work data src/id_detector/io.py src/id_detector/jobs.py
(empty)
```

- **PowerShell gates:** not run by this agent, as the orchestrator asked. It had already run
  `smoke_serve.ps1` and `gate_local_mode.ps1` against the first pass from this worktree (both
  passed) and will re-run them against this code.
- `src/id_detector/io.py` and `src/id_detector/jobs.py` are unmodified.
- `git status --short -- work data` is empty.

## Round-2 review (sol xhigh) + fix pass

The round-2 review (Codex gpt-5.6-sol, xhigh, read-only; `diff-review-followup-longpath-sol-r2.out.md`)
confirmed:
- all six round-1 findings are fixed, and each test fails when its fix is reverted;
- no long-path-unsafe call remains in retention;
- every retention invariant is intact;
- the scope is clean.

It returned FIX_FIRST on one P1 and two P2s, all about the dry run matching `--apply`. Nothing
else was touched: only `retention.py`, its test file and this record changed. `io.py` and
`jobs.py` are unmodified.

### Findings, reproduction and fixes

Each test below was first run against the round-1 `retention.py` (saved as
`retention_v2_round1.py`), then against the fix. Both runs were in the foreground under the
default temp root.

| Finding | Test | Round-1 code | This pass |
|---|---|---|---|
| **P1:** an invalid `.trash` (junction, dangling link, regular file) makes the dry run list `move` for every due target, while `--apply` skips the media | `test_invalid_trash_dry_run_plans_exactly_what_apply_does[linked\|dangling\|file]`: exact dry-run == apply action tuples, the only action is `("skip", .trash)`, and the tree is byte-identical after both | **3 FAILED**: dry run returned the `.trash` skip plus the `move` actions | pass |
| **P2:** the dry run never preflights the manifest; apply adds a manifest `skip` | `test_linked_manifest_is_never_written_through`, now also asserting `result.actions == planned.actions` | **FAILED**: "the preview must announce the manifest refusal" | pass |
| **P2:** the lock regression never passes a literal `\\?\` spelling | `test_media_lock_is_one_lock_for_two_spellings_past_max_path`, now with the plain, junction-alias and literal `\\?\` spellings (all over 260 characters on Windows) | pass (pins existing `jobs.py` behaviour; nothing to revert) | pass |

**P1 fix.**
- `collect` now checks `trash_root is None` at the top of the per-media loop, before the dry-run
  and apply branches split. With an unusable trash neither branch plans or reports any media
  action, so both return exactly the `.trash` skip.
- The dry run's old `if trash_root is not None` guard around destination validation is gone,
  since it can no longer be reached with `None`.

**P2 (manifest) fix.**
- The link and plain-file check moved out of `_write_manifest` into a new helper,
  `_manifest_refusal(media_dir, work_root)`.
- Apply still calls it immediately before `atomic_write_json`, via `_write_manifest`.
- The dry run now calls the same helper after planning a media's moves, unless it planned the
  whole media directory to move, which is when apply writes no manifest. A refusal becomes the
  same `skip` on `retention-manifest.json`, in the same position in the action list.
- The one apply-only refusal left is a listing failure *during* manifest writing. That can
  only be a race after the moves, which a preview cannot predict. A listing failure that
  already exists when GC starts is announced by both branches through the media-level
  `_UNLISTABLE` skip.

**P2 (lock) test.**
- Every one of the three spellings gives the same `ProcessLock.path`.
- For each spelling held in this process, a separate process using each of the three spellings
  gets `JobStoreLocked`.
- After release, every spelling acquires.
- `jobs.py` is unchanged.

### Residual risks

These two check-then-act windows are accepted by the round-2 review as `[adversarial]` residual
risks and are deliberately **not** closed. Closing them would need handle-relative Win32
operations. Each needs a hostile local process that deliberately races a window of a few
bytecodes, and for media mutations also ignores the media lock.

1. **Move and write window.** Between the last `_unlinked` / `_lexists` / `_is_real_file` check
   and the syscall that acts on it, a checked destination ancestor (or a sidecar or manifest
   path) could be swapped for a junction or link. The syscalls in question are `os.replace` in
   `_move_to_trash`, and `atomic_write_json` in `_rewrite_sidecars` and `_write_manifest`.
2. **Purge window.** After `_purgeable`'s final comparison and before
   `shutil.rmtree(_extended(...))`, `.trash` could be replaced by a junction.

Also documented, not a safety risk: GC refuses *any* reparse point on a mutation path. A future
OneDrive or cloud-file placeholder in `work/` would therefore be kept, never collected. The
owner's tree has none today.

### Gates (default temp root, foreground)

```
# New and changed tests against the round-1 retention.py (before the fix):
$ uv run pytest tests/test_phase2b_retention.py -q -rA -k "invalid_trash or linked_manifest or media_lock_is_one_lock"
PASSED test_media_lock_is_one_lock_for_two_spellings_past_max_path
FAILED test_invalid_trash_dry_run_plans_exactly_what_apply_does[linked]
FAILED test_invalid_trash_dry_run_plans_exactly_what_apply_does[dangling]
FAILED test_invalid_trash_dry_run_plans_exactly_what_apply_does[file]
FAILED test_linked_manifest_is_never_written_through
4 failed, 1 passed, 47 deselected, 1 warning in 12.64s

$ uv run pytest tests/test_phase2b_retention.py -q -rs      # after the fix, default root pytest-1054
52 passed, 1 warning in 36.02s                               # no skips

$ uv run pytest -q
1307 passed, 1 skipped, 93 deselected, 1 warning in 545.70s (0:09:05)
exit=0                                                       # the skip is outside the retention file

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
301 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 460 files
fixture audit passed

$ git status --short -- work data src/id_detector/io.py src/id_detector/jobs.py
(empty)
```

- **PowerShell gates:** not run by this agent. The orchestrator ran `smoke_serve.ps1` and
  `gate_local_mode.ps1` against the round-1 code (both passed) and will re-run them against this
  pass.
- **Cleanup:** no process started by this agent is left running.

## Orchestrator gate runs on the final code

The worktree guard in the fixer's session refused PowerShell, so the orchestrator ran both PowerShell gates from the fixer's worktree against the final code, after checking the loopback ports were free:

```text
ports free at 22:41:10
running from: C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a215a8533ef9de243   HEAD ec32f97   changed: 3 paths
=== smoke_serve.ps1 (long-path fix, round-3 final code) ===
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
smoke exit=0
=== gate_local_mode.ps1 (long-path fix, round-3 final code) ===
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-f937fc5c1ab74078ab414da5110e949e
local-mode exit=0
=== processes left running from this worktree ===
0
=== owner data in the worktree untouched ===
(empty = untouched)
```

Second-model reviews (Codex gpt-5.6-sol at xhigh, model verified from each log header), filed in round order:

- `docs/reviews/followup-longpath-sol-r1.md`
- `docs/reviews/followup-longpath-sol-r2.md`
- `docs/reviews/followup-longpath-sol-r3.md`

