# Build 2b — Retention and sidecar pruning

Implemented cycle 2b only. GC is explicit, defaults to a read-only dry run, moves expired
artefacts into dated trash only with `--apply`, and never runs from analyse, serve, or web jobs.
No dependency, backup, upload-staging, hosted lease-expiry, library, sharing, or publication
database work was added. No live provider call was made.

## Files changed

- `src/id_detector/retention.py`: adds local/hosted retention planning and application, per-media locking, recoverable dated trash moves, seven-day trash purge, reference protection, sidecar rewriting, and active-tree size manifests.
- `src/id_detector/io.py`: accepts a validated per-upstream `pruned_upstream` hash marker while still requiring the derived artefact hash to match.
- `src/id_detector/cli.py`: adds dry-run-by-default `idea gc --policy local|hosted [--apply]` and the analysis `--keep-intermediates` option.
- `src/id_detector/journal.py`: persists the intermediate-retention opt-out on every terminal invocation entry.
- `src/id_detector/contracts.py`: adds `keep_intermediates` and the future terminal statuses `quota_exceeded` and `dead_letter`, with legacy journal compatibility.
- `docs/schemas/invocation_journal_entry.schema.json`: synchronizes the invocation schema.
- `tests/golden/invocation_journal_entry.json`: synchronizes the journal golden.
- `tests/test_phase2b_retention.py`: adds the complete offline phase gate.
- `scripts/gate_local_mode.ps1`: makes final process cleanup tolerate Edge exiting a child during `taskkill`; all browser/audio assertions remain strict.
- `docs/reviews/build-2b.md`: records this build and its validation evidence.

Separate playlist/UI work appeared concurrently in the shared worktree (`README.md`, `idea.cmd`,
`present/{page,theme}.py`, `playlists/`, and `tests/test_playlists.py`). It is not part of cycle 2b
and is not described above. The required repository-wide formatter mechanically formatted two of
those files after that concurrent writer stopped; their functionality was not changed by this
cycle.

## Tests added

`tests/test_phase2b_retention.py` covers all three successful statuses and all eight unsuccessful
terminal statuses; immediate window removal; the 48-hour PCM threshold; hosted seven-day versus
local indefinite original retention; unsuccessful-run 24-hour original and seven-day unreferenced
media thresholds; dry-run immutability and explicit `--apply`; dated trash moves and old/young
purge boundaries; valid `pruned_upstream` verification plus derived-artefact corruption rejection;
the real journaled `--keep-intermediates` path; non-terminal run protection; sealed-bundle
reference protection; sealed bundle/fuse-manifest immutability across pruning; Free-to-Deep
re-derivation with byte-identical frozen windows; altered
re-fetch through the existing `source_changed` exit-5 path; and an on-demand generated
`tone-3600s.wav` whose hosted post-GC active manifest is at most 25 MB.

The one-hour fixture was generated at `tests/fixtures/audio/tone-3600s.wav` (115,200,044 bytes).
It is ignored and uncommitted as required.

## Contract choices

- The newest valid journal entry controls media-level retention. A newest non-terminal or
  timestamp-less entry causes a conservative skip; GC never falls back to an older terminal run.
- `--keep-intermediates` protects windows and decoded PCM. It does not extend original retention
  or override the final seven-day removal of an unreferenced unsuccessful media directory.
- A pruned sidecar value is represented as
  `{"logical/path": {"pruned_upstream": "<original sha256>"}}`. The verifier validates the marker
  shape and digest and still validates the consuming artefact's own hash. Re-derived upstreams
  may subsequently replace the marker through the normal sidecar writer.
- Sidecars in manifest-sealed `fuse/runs/` and `present/bundles/` snapshots are immutable copies
  and are not rewritten. Their recorded hashes remain evidence, while mutable working sidecars
  receive pruning markers; this preserves both historical manifests and the publication invariant.
- Individual PCM/original artefacts retain their relative path under
  `work/.trash/YYYY-MM-DD/<source_key>/<media_key>/`; window and whole-media directories are moved
  as units. No active artefact is unlinked. Only a dated trash directory at least seven calendar
  days old is permanently purged.
- Threshold age uses the terminal journal's `finished_at`, falling back to `started_at`, in UTC.
- The implemented local reference set is: every sealed bundle on disk, a non-empty
  `present/current`, and every matching top-level or alias entry in `work/index.json`. A referenced
  media directory is never collected. Bundle/fuse-run history is not independently collected in
  this cycle because library/share tables do not exist yet.
- `retention-manifest.json` inventories every active file after an applied GC, records each byte
  size, and records their total; it excludes itself and the separate `.trash` tree.
- GC takes the existing non-blocking `.media.lock` and skips active media. This is the seam for
  the broader artefact/backup coordination added in 4b-iii; no backup behavior was built here.

## Validation

All pipeline executions used committed injected fakes. The serve smoke was launched through a
small Python environment wrapper so Windows preserved `AUDD_API_TOKEN=` and
`IDEA_ENGINE_SHAZAM=off`; the local-mode script itself injects FakeShazam for preparation, then
sets the empty token and Shazam hard-off before serving. The first wrapped local-gate attempt
intentionally demonstrated that inheriting hard-off into preparation exits 6; no network call was
made. Two subsequent Edge runs passed every assertion but exposed an existing post-pass cleanup
race, which was fixed and verified by the final exact command below.

```text
> uv run pytest -q
........................................................................ [ 94%]
..........................................................               [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
1066 passed, 93 deselected, 1 warning in 525.20s (0:08:45)

> uv run ruff check .
All checks passed!

> uv run ruff format --check .
264 files already formatted

> uv run python scripts/audit_fixtures.py
audited 437 files
fixture audit passed

> uv run pytest tests/test_phase2b_retention.py -q
....................                                                     [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
20 passed, 1 warning in 27.02s

> uv run pytest tests/test_phase1a_bundles.py tests/test_phase1a_cached_open.py tests/test_phase1a_compat.py tests/test_phase1b_breaker_scorer.py tests/test_golden_local_free.py tests/test_stage4d_profiles.py tests/test_score_corpus.py tests/test_stage10_webapp.py -q
........................................................................ [ 32%]
........................................................................ [ 64%]
........................................................................ [ 96%]
.........                                                                [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
225 passed, 1 warning in 140.10s (0:02:20)

> powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'

> powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=<temporary bundle>/tracklist.json
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-5a5316b515c8474b8ca2fffadc7c3d2a
```

No dependency was added, so `uv sync` was not needed. There was no live-only check to omit.
Nothing required by cycle 2b could not be completed.
`docs/PLAN-v2.md`, frozen `profiles/`, and `data/corpus/` are unchanged. Every retention test used
a temporary work root. `git status --short -- work data` was empty at the end: neither the build
nor its tests touched the owner's `work/` or `data/` trees.

## Working-tree snapshot

The required final command outputs follow.

```text
> git status --short
 M README.md
 M docs/schemas/invocation_journal_entry.schema.json
 M idea.cmd
 M scripts/gate_local_mode.ps1
 M src/id_detector/cli.py
 M src/id_detector/contracts.py
 M src/id_detector/io.py
 M src/id_detector/journal.py
 M src/id_detector/present/page.py
 M src/id_detector/present/theme.py
 M tests/golden/invocation_journal_entry.json
?? docs/reviews/build-2b.md
?? src/id_detector/playlists/
?? src/id_detector/retention.py
?? tests/test_phase2b_retention.py
?? tests/test_playlists.py

> git diff --stat
 README.md                                         | 20 +++++++++++++-
 docs/schemas/invocation_journal_entry.schema.json |  2 +-
 idea.cmd                                          |  2 +-
 scripts/gate_local_mode.ps1                       |  5 +++-
 src/id_detector/cli.py                            | 33 ++++++++++++++++++++++-
 src/id_detector/contracts.py                      |  4 +++
 src/id_detector/io.py                             |  9 ++++---
 src/id_detector/journal.py                        |  2 ++
 src/id_detector/present/page.py                   | 16 +++++++----
 src/id_detector/present/theme.py                  |  6 ++---
 tests/golden/invocation_journal_entry.json        |  2 +-
 11 files changed, 84 insertions(+), 17 deletions(-)
```

`git diff --stat` does not include untracked files; the status output above is the authoritative
list and distinguishes the concurrent playlist/UI work noted earlier.

## Review + fix pass (sol xhigh second review folded in)

Adversarial review of the uncommitted cycle-2b work, then the fixes, by Claude Opus 5. Findings
were formed independently first and then reconciled with the first reviewer's (Codex gpt-5.6-sol,
xhigh, read-only, inspection-only). Every check ran in a temporary work root; `idea gc` was never
pointed at `work/`, in any mode. No live provider call was made: the pipeline tests use the
committed injected fakes, and the two PowerShell gates run their own offline preparation.

### Findings

**R1 — P0 (mine, confirmed by execution; = sol #1). Retention read one journal entry, so one run
could delete what another still owned.** `retention.py:64-79` (old) took only the newest entry and
`:279-283` applied that entry's rule to media-level artefacts that *every* run of that media
shares. Verdict: **correct, and worse than described** — a probe against the builder's code with
the journal `complete (30 d) → failed (2 d)` under `--policy local` moved the original, the PCM
*and* the windows, contradicting §4.6 ("original after 7 d (hosted; local keeps it)"). The same
rule also shortened a fresh success's 48 h PCM window to zero. Fix: `_retention_plan` folds **all**
terminal entries into one conservative plan — each artefact's due time is the *latest* any run
allows, one local success pins the original forever, and one success of any kind takes the whole
media directory off the table. Ambiguity (unreadable journal, newest entry non-terminal, a
terminal entry with no usable timestamp) still skips the media entirely. Sol is also right that
this rule had no plan authority: the "Contract choices" line above — "The newest valid journal
entry controls media-level retention" — is superseded by this pass and no longer describes the
code.

**R2 — P0 (mine, confirmed by execution; = sol #2). `--keep-intermediates` was not durable.** The
same newest-entry rule let a later default (`false`) run drop windows and PCM a `true` run had
pinned (`retention.py:266-278` old). Verdict: **correct**. Sol's second half is also right: these
artefacts are media-shared, so a run-scoped pin is not implementable — the defensible rule is that
any terminal run asking to keep them pins them. `RetentionPlan.keep_intermediates` is now
`any(... is True)` over every terminal entry.

**R3 — P0 (sol #3). The contained-trash guarantee was not enforced.** `is_dir()` follows symlinks
and Windows junctions, so nothing checked that a walked path really lived under the work root
(`retention.py:83-93` old), that `.trash` was a real directory of that root (`:162-176`), or that
a dated trash directory was not a reparse point before `shutil.rmtree` (`:228-240`, `:311-314`).
Verdict: **correct, and demonstrated**: with the new guards disabled, the two new junction tests
fail — GC moves an external media directory's original/PCM/windows into `work/.trash`, and purges
an unrelated external tree through a junctioned `.trash`. Fix: `_media_directories` accepts a path
only when every level resolves to exactly the path walked to reach it; `_trash_root` refuses a
`.trash` that is not a real directory of this work root (and `collect` then does nothing but say
so); `_purge_actions` applies the same test to each dated directory and `rmtree` is guarded by
`is_symlink()`; `_move_to_trash` re-checks the resolved destination parent before `os.replace`;
`_targets` refuses any artefact that resolves outside its own media directory.

**R4 — P0/P1 (sol #4). Terminal state and references were read before the media lock.** Verdict:
**correct on both halves.** (a) The plan was computed at `:259-293` (old) and the lock taken at
`:295`, so a publication that completed inside that window was collected on a stale plan; the
pipeline holds that same `.media.lock` from ingest through publish and journal append
(`cli.py:651-653`, `1325-1342`, `1440-1441`), so reading under it closes the window. `collect`
now acquires the lock *first* in `--apply` and computes the plan, the reference set and the
targets inside it (dry run still takes no lock at all, so it changes nothing, not even a lock
file). (b) `_has_publication_reference` failed open: `read_bundle_manifest(...) is None` treated a
damaged, unreadable or merely locked bundle as absent. It now fails closed — any entry in
`present/bundles`, any unreadable listing, an unreadable `present/current`, and a corrupt or
non-object `work/index.json` all count as a reference. A *missing* index or bundle directory is
still correctly read as "no reference".

**R5 — P1 (sol #5). `retention-manifest.json` raced active writers.** The lock was released at
`:310` (old) and the inventory written at `:315-316`. Verdict: **correct**. `_write_manifest` is
now called inside the locked block, per media, before release.

**R6 — P1 (sol #6). Missing regressions.** Verdict: **correct**; every case named is now covered
(see "Tests added"), plus the two cases sol could not execute.

**R7 — P1 (sol #7). `docs/schemas/README.md` contradicted the new marker.** Verdict: **correct**.
The "Completion sidecars" section now documents `{"pruned_upstream": "<sha256>"}`, what the
verifier does with it, and that re-writing the sidecar replaces it. Sol is also right that
`test_pruned_upstream_is_verified_and_replaced_after_rederivation` never asserted the replacement
its name promises — that behaviour is now asserted (with the divergence check of R10) in
`test_pruned_marker_rejects_a_divergent_upstream`.

**R8 — P1/scope (sol #8). `scripts/gate_local_mode.ps1`'s blanket `catch { }`.** Verdict:
**correct** — with `$ErrorActionPreference = "Stop"` at the top of the script, any `taskkill`
stderr becomes a terminating error, so the empty catch hid a genuine failure to kill the browser
or server as readily as the Edge teardown race. Not reverted, as instructed; narrowed instead: the
catch refreshes the process and re-throws unless it really has exited. The gate passes with it.

**R9 — P1 (mine). The trash purge could delete artefacts younger than seven days.** Old
`:232,239`: `cutoff = today - 7 days`, purge when `trash_date <= cutoff`. A directory stamped
exactly seven days ago still holds whatever was moved at 23:59 that day — about 6.5 days old when
a midday GC purged it. §4.6 says purge after 7 days, and the recovery window is the only thing
standing between a mistake and lost audio. `_purge_actions` now expires a dated directory only
once the day *after* it is itself seven days behind (`_TRASH_GRACE`), which makes the youngest
possible entry provably ≥ 7 days old. The builder's boundary test was rewritten accordingly
(2026-09-03 purged; 2026-09-04 and 2026-09-05 kept at `NOW`).

**R10 — P1 (mine). The `pruned_upstream` marker was never checked again, even once the upstream
came back.** `io.py:194-201` accepted the marker on shape alone, so a re-derived upstream with
*different* bytes verified clean and its stale downstream artefacts were reused — weaker than the
pre-2b behaviour, where a changed upstream invalidated the sidecar and forced a rebuild, and at
odds with §3.4's whole point (the chain re-derives, and divergence must be caught). The verifier
now requires a present upstream to hash to the recorded pruned value; absent, the marker is
accepted as before and the artefact's own hash is still checked.

**R11 — P0-adjacent (mine, found while writing R4's test). A bundle directory past Windows'
MAX_PATH was invisible to the reference check.** `Path.is_dir()` answers `False` rather than
raising for a >260-character path, so a real published bundle in a deep work root would have read
as "no bundle here" and licensed collecting the whole media directory. The check now counts any
entry returned by `iterdir()` without asking `is_dir()`.

**Cleared after attacking them (no change needed).** (2) GC is opt-in: `collect` is imported in
exactly one place, the explicit `idea gc` command (`cli.py:90`, `1466-1485`); nothing in
`_analyse`, `serve`, the web app or `idea.cmd` calls it, and `--apply` is required to change
anything. (4) The per-status rules match §4.6 exactly, including the seven failure statuses and
local-keeps-the-original. (5) Re-derivation works: windows ← PCM ← original ← re-fetch, the Deep
upgrade after pruning reproduces byte-identical frozen windows, and altered re-fetched bytes end
the run `source_changed` (exit 5) through the existing 1a-ii path. (6) No money, reservation,
attempt-state or status-machine change: `contracts.py` only widens the journal enum with the two
statuses §4.6 itself names (`quota_exceeded`, `dead_letter`, emitted by nothing yet) and adds
`keep_intermediates` with a legacy default, and `usd_*`/`costs`/attempt handling are untouched.

### P2 notes (left, with the reason)

- A `--keep-intermediates` pin never expires: the plan gives the flag no lifetime, so inventing an
  expiry here would be inventing contract, and the conservative direction is to keep the bytes.
- `_rewrite_sidecars` skips a sidecar whose artefact sibling cannot be identified unambiguously
  (`retention.py:242-247`). The failure direction is safe — the stale hash makes verification fail
  and the consumer rebuilds — and narrowing it needs the artefact-naming convention that later
  cycles formalise.
- `retention-manifest.json` lists `.media.lock` on POSIX (0 bytes, absent on Windows, where the
  lock is a named mutex). Cosmetic only.
- Bundle and fuse-run history is still not collected independently, as the builder says: the
  library/share tables do not exist yet, and §4.6 forbids deleting a run anything references.

### What changed (cycle 2b paths only)

- `src/id_detector/retention.py` — `RetentionPlan` + `_retention_plan` (R1, R2); `_real`,
  `_within`, containment in `_media_directories`, `_trash_root`, `_move_to_trash`, `_purge_actions`
  and `_targets` (R3); lock-first planning in `collect` and the manifest write inside the lock
  (R4a, R5); fail-closed `_has_publication_reference`/`_index_references` (R4b, R11);
  `_TRASH_GRACE` (R9).
- `src/id_detector/io.py` — the pruned marker is re-checked against a re-derived upstream (R10).
- `docs/schemas/README.md` — documents the marker and its verification (R7).
- `scripts/gate_local_mode.ps1` — the cleanup catch narrowed to the already-exited case (R8).
- `tests/test_phase2b_retention.py` — eleven regressions added, one boundary test rewritten.

### Tests added (`tests/test_phase2b_retention.py`, 31 tests total)

`test_failed_run_keeps_what_an_earlier_success_owns` (R1: local original and 48 h PCM survive a
later failure; hosted still expires at 7 d) · `test_media_dir_survives_a_failure_after_a_success`
(R1) · `test_keep_intermediates_pin_survives_a_later_default_run` (R2) ·
`test_reparse_point_cannot_move_files_outside_work` and `test_reparse_point_trash_is_never_purged`
(R3; real junctions via `mklink /J`, both verified to fail with the guards removed) ·
`test_state_is_read_under_the_media_lock` (R4a; a lock subclass publishes at acquire time —
verified to fail with the old plan-then-lock order) · `test_unreadable_reference_blocks_collection`
(R4b, R11; damaged bundle and corrupt index) · `test_manifest_is_written_under_the_media_lock`
(R5; the spy proves the lock is held) · `test_trash_purge_never_removes_anything_younger_than_7_days`
(R9, rewritten) · `test_pruned_marker_rejects_a_divergent_upstream` (R7, R10) ·
`test_every_pruned_artefact_is_recoverable` (R6: the dated trash holds intact copies of the
original, the PCM and the windows).

### The other session's files — untouched

`src/id_detector/playlists/**`, `src/id_detector/present/page.py`,
`src/id_detector/present/theme.py`, `README.md`, `idea.cmd` and `tests/test_playlists.py` belong to
the concurrent playlist/UI session. They were not read for review, not modified, and none of them
broke a gate in this pass.

### Gates (all re-run here; nothing pasted from the build)

```text
> uv run pytest -q
1077 passed, 93 deselected, 1 warning in 437.80s (0:07:17)

> uv run pytest tests/test_phase2b_retention.py -q
...............................                                          [100%]
31 passed, 1 warning in 33.59s

> uv run pytest tests/test_phase1a_bundles.py tests/test_phase1a_cached_open.py tests/test_phase1a_compat.py tests/test_phase1b_breaker_scorer.py tests/test_golden_local_free.py tests/test_stage4d_profiles.py tests/test_score_corpus.py tests/test_stage10_webapp.py -q
225 passed, 1 warning in 151.87s (0:02:31)

> uv run ruff check .
All checks passed!

> uv run ruff format --check .
264 files already formatted

> uv run python scripts/audit_fixtures.py
audited 437 files
fixture audit passed

> powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'

> powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=<temporary bundle>/tracklist.json
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-46905262ec9b4f53b4b039fb019f4eb6

> git status --short -- work data
(empty)

> PYTHONIOENCODING=utf-8 uv run python scripts/score_corpus.py --run-list data/local/release-1-runs-free.json --out "$TEMP/2b-check.json"
scored 7 mix(es) [draft truth, matched by work]: likely 8971/10000, listed n/a, recall 6422/10000
```

The release-1 pooled numbers are unchanged (likely 8971, recall 6422), as expected: nothing in this
pass touches scoring, fusion or the providers.

Two further checks beyond the listed gates. The junction and lock-ordering regressions were each
re-run against the pre-fix behaviour (guards removed, and the plan computed before the lock) and
each failed there, so they pin the defect rather than the implementation. `idea gc` was also driven
end to end on a synthetic temporary work root: the dry run created nothing at all (no `.trash`, no
lock file), and `--apply` moved `windows/`, `decode/audio.pcm` and `ingest/original.wav` into
`work/.trash/2026-09-11/<source>/<media>/` with their bytes intact and wrote
`retention-manifest.json`. The owner's `work/` tree was never a GC target, in either mode.

Not committed, not branched, not pushed, as instructed.
