# Build — local polish pass

Owner-directed local work, left uncommitted at base commit `e591971`. No branch or commit was
created by this pass. `PAGE_VERSION` remains 25; `profiles/`, the golden Local Free result,
`data/corpus/`, `work/`, and the files reserved to other sessions are unchanged.

No live provider call or network hint fetch was made. All test processes used
`IDEA_TEST_MODE=1` with an empty `AUDD_API_TOKEN`; `IDEA_ENGINE_SHAZAM=off` was set only inside
the two offline measurement processes, never around the suite. The owner cache was copied to
`%LOCALAPPDATA%\Temp\idea-polish-measure-20260922` without original media or decoded audio. The
copy and the revert-proof copy were deleted afterward. Timestamp checks against the recorded
`2026-09-22T11:17:48.6894111Z` start time found zero newer files in both the worktree and owner's
`work/` and `data/corpus/` trees.

## 1. Local restart progress

`LocalWorker._restarted()` now takes the same durable progress snapshot used by hosted retries.
It folds prior `carried_seconds`, completed phase seconds, and the open phase up to the last bar
evaluation into one new `carried_seconds` value. It also carries the bar's integer and fractional
high-water marks, window counts, resolved title and audio path, while leaving per-attempt phase
timings and rate samples fresh. `JobManager._execute()` assigns a new `started_at` when the
replacement attempt begins; it does not preserve the original timestamp. The elapsed floor still
survives because the completed and open duration is folded into `carried_seconds`. Thus a process
restart and a browser reload both recover the real elapsed floor.

`test_a_local_worker_restart_mid_recognition_carries_the_real_bar_forward` restarts at 135/450
windows after 207 seconds, checks an exact 207-second carry, checks that old phase buckets are not
reused, and proves the immediate and subsequent bar values neither return to zero nor move back.
Against the untouched `e591971` source it fails at `carried_seconds == 0.0`.

## 2. Strict untimed-hint corroboration

An eligible untimed hint can now support a work only after identity fusion's existing field-level
match and mix-wide ambiguity veto resolve it to exactly one recognised work. The hint is attached
to the audio plays of that work, but is never a directly positioned backing: it cannot create a
hint-only episode, choose an occurrence, remove `scatter`/`contradicted`, or enter the calibrated
boundary model. The fusion recipe is now `fusion:4`, so saved Free results can receive the change
through the offline re-fusion path.

The four new tests cover the positive case with byte-for-byte timing assertions, two-work
ambiguity (plus a one-work positive control), an unheard hint (plus a heard positive control),
and retained `scatter`/`contradicted` suppression. All four fail when the production source is
reverted to `e591971`.

### Cached-copy measurement

The comparison uses the immutable `fusion:3` source bundles and their `fusion:4` successors on the
same copied fourteen-mix cache. “Attached hints” below uses the previous hint build's metric:
unique eligible named hints present in audio episode evidence. Page rows use the normal published
floor; release-1 numbers use `scripts/score_corpus.py`'s work matcher over the seven truth sets.

| Measurement | Before (`fusion:3`) | After (`fusion:4`) |
|---|---:|---:|
| Eligible named hints attached | 109 | 125 |
| All unique hint evidence IDs attached | 130 | 146 |
| Audio episodes flagged `hint_supported` | 90 | 98 |
| Listed page rows, all 14 mixes | 386 | 387 |
| Release-1 listed rows | 227 | 228 |
| Release-1 work recall | 154/218 · 70.6% | 155/218 · 71.1% |
| Release-1 work precision | 156/207 · 75.4% | 157/208 · 75.5% |
| Release-1 `likely` precision | 62/64 · 96.9% | 62/64 · 96.9% |

On `ed4ca55359f9`, **16/18** formerly attachable untimed hints attach. The other two both name
`MPH — One Sixty`, whose work has two recognised occurrences, so they are refused as positionally
ambiguous instead of being assigned by longest, earliest, or best occurrence. Eight episodes gain
support; listed rows move 40 → 41. The sole newly listed occurrence is
`The Bug — Jah War (feat. Flowdan)`.

The earlier claim that the unchanged unmatched-prediction list proved “no new wrong row appears”
was not valid: the work matcher ignores time and permits several predictions to map to one truth
work. The new occurrence-level check compares the changed rows with timed truth. `Jah War` is
published at 4,761,000–4,773,000 ms inside its 4,693,000–4,807,000 ms truth occurrence. The
opening `One Sixty` remains listed at 45,000–138,000 ms inside its 0–124,000 ms truth occurrence;
the second recognised occurrence at 2,862,000–2,874,000 ms remains hidden and gains no untimed
hint support. The honest measured gain is therefore one listed track.

The first attempt to apply `fusion:4` also exposed a genuine upgrade-chain problem: a `fusion:3`
offline run deliberately contains outputs, not copied completion sidecars. Re-fusion now follows
the sealed `source_bundle` chain to the original proved inputs, with confinement, hash, cycle and
missing-source refusals. A regression test performs two consecutive fusion-version upgrades
offline; it fails against `e591971` because the second upgrade is treated as a cache hit.

## 3. Refusal cost wording

Recipe evidence is read from both the selected bundle and validated legacy metadata. One proved
name is Free or Deep; missing or conflicting names are `unknown`. A compatibility-less page-only
`legacy-*` bundle does not hide BENWAL's validated Free invocation journal. Deep detection remains
money-conservative: any proved Deep record still prevents offline re-fusion.

The exact per-mix text, shared by startup logging, the CLI completion callback, the home page and a
later `/upkeep/status` request, is:

```text
Free:
BENWAL: The result was left as it is because its stored records disagree with each other: the final generation reference disagrees with its generation sidecar. Re-running this one is free: it costs nothing, but it will take time.

Deep (the same suffix is shown for both proved Deep mixes):
Speed Garage & Bass Mix - Holly Olivia (March 26): The result was left as it is because it is a paid (Deep) result, and the windows its second opinion checked were chosen by the older fusion rules, so it cannot simply be rebuilt. Warning: re-running this one will spend real AudD credit.

Unknown recipe:
Unknown recipe mix: The result was left as it is because the legacy invocation journal is malformed or truncated; without trustworthy legacy metadata it is not safe to assume the stored result was Free. Warning: its recipe could not be determined, so ID'er must treat it as paid; re-running this one may spend real AudD credit.
```

The final status line is:

```text
To analyse a skipped mix again, re-run it with --refresh; each line above says whether that re-run costs money.
```

The owner-data copy classified BENWAL as Free and the Holly Olivia and Garage Mix results as Deep.
The test also checks a second startup after BENWAL's safe page-only refresh, an unknown/malformed
legacy journal, the startup/CLI output, the initially rendered page, and the later status response.
Its full owner-visible test fails against `e591971` because skipped records have no recipe field.

## 4. `idea` snapshot commands

`idea backup`, `idea restore`, and `idea verify-artefacts` are thin Typer adapters over the existing
`idea_web.backup.main` entry point. They add no snapshot logic. Consequently all existing path
exclusion, sealing, supervisor-lock, running-tool refusal, transactional restore and non-zero
verification-failure behavior remains in one implementation.

The help summaries are:

```text
idea backup: Create a sealed database/result snapshot without copying audio or touching any corpus.
idea restore: Restore the database and saved-result artefacts, not audio or any truth corpus.
idea verify-artefacts: Verify hashes, manifests and sidecars without changing work, data or the snapshot.
```

The end-to-end CLI test checks all three help pages, makes a real sealed fixture snapshot, verifies
it, restores it to another work root, semantically verifies that restore, damages a member, and
checks `verify-artefacts` exits non-zero with `"ok": false`. Against `e591971` it fails immediately
with `No such command 'backup'`.

## 5. Revert proof and focused runs

The exact new-test selection was run with `PYTHONPATH` pointing at a clean `git archive` of
`e591971` (the current tests stayed in place). Output:

```text
FFFFFFFF                                                                 [100%]
8 failed, 1 warning in 18.30s
```

Those eight are: one progress restart test, four untimed-policy tests, one CLI snapshot test, one
second-generation re-fusion test, and one owner-visible refusal test. Every new test therefore
fails when its corresponding fix is reverted.

Focused current-source outputs included:

```text
.....                                                                    [100%]
5 passed, 72 deselected, 1 warning in 3.46s

2 passed, 1 warning in 39.54s

2 passed, 1 warning in 57.07s
```

One early eleven-file affected run became pathologically slow and was stopped near ten minutes at
65% with one already-visible help-text failure. The isolated failing file reproduced it in
`1 failed in 6.59s`; after correcting the assertion to account for terminal wrapping, its focused
run passed and the same files were covered again in the smaller complete-suite shards below. No
conclusion rests on the stopped run.

## 6. Complete suite, foreground shards

Collection:

```text
$ uv run pytest --collect-only -q -p no:cacheprovider
2070/2167 tests collected (97 deselected) in 8.29s
```

Every one of the 111 collected `test_*.py` files appears exactly once in the following runs. The
backup file was isolated; the other 20 web files were assigned modulo five, and the 90 top-level
files modulo eight. The totals are 2,068 passed + 2 expected skips = 2,070 runnable tests, with 97
per-file selection deselections, exactly matching collection.

Shard membership (the backup shard contains `test_backup.py` alone):

```text
web-1: test_accounts.py, test_followup_round2.py, test_followup_round8.py, test_ops.py
web-2: test_auth.py, test_followup_round4.py, test_followup_round9.py, test_parity.py
web-3: test_coalescing.py, test_followup_round5.py, test_headers_forms.py, test_refusion_money.py
web-4: test_followup_queue_money.py, test_followup_round6.py, test_legacy_contract.py, test_refusion_upkeep.py
web-5: test_followup_review_fixes.py, test_followup_round7.py, test_local_queue.py, test_worker.py
root-1: test_accuracy_fixes.py, test_engine_corroboration.py, test_paid_clip.py, test_phase1a_bundles.py, test_playlists.py, test_service_api.py, test_stage10_webapp.py, test_stage3_entitlements.py, test_stage4c_ablations.py, test_stage7_page.py, test_truth_corpus_followup_r2.py, test_truth_gateway_guard.py
root-2: test_acrcloud_clip.py, test_fixture_audit.py, test_phase0a_crash_cache.py, test_phase1a_cached_open.py, test_progress_wallclock.py, test_stage1_jobs.py, test_stage2a_controlled.py, test_stage3_providers.py, test_stage4c_events.py, test_stage7_server.py, test_truth_corpus_followup_r3.py, test_truth_review.py
root-3: test_audd_clip.py, test_followup_money_resume.py, test_phase0a_money.py, test_phase1a_compat.py, test_projection.py, test_stage1_live.py, test_stage2a_scorer.py, test_stage3_shortlist.py, test_stage4c_generations.py, test_stage8_candidates.py, test_truth_corpus_followup_r4.py
root-4: test_certification_followup.py, test_golden_local_free.py, test_phase0a_security.py, test_phase1b_breaker_scorer.py, test_refusion.py, test_stage1_privacy.py, test_stage2a_truth.py, test_stage4a_connectors.py, test_stage4c_rescans.py, test_stage8_panako.py, test_truth_corpus_followup_r6.py
root-5: test_collapse.py, test_hint_corroboration.py, test_phase0a_status.py, test_phase1b_fusion.py, test_scan.py, test_stage1_process.py, test_stage2b_alignment.py, test_stage4a_parser.py, test_stage4c_scanners.py, test_stage9_config.py, test_truth_corpus_followup_r7.py
root-6: test_contracts.py, test_identity_fuzzy.py, test_phase0b_attempts.py, test_phase1b_targeting.py, test_scan_targeting.py, test_stage1_shazam.py, test_stage2b_corpus.py, test_stage4a_pipeline.py, test_stage4d_profiles.py, test_stage9_exports.py, test_truth_corpus_followup_r8.py
root-7: test_crowd_id_merge.py, test_io_backstop_followup.py, test_phase0b_audd.py, test_phase2b_retention.py, test_score_corpus.py, test_stage1_wheel.py, test_stage2b_fuser.py, test_stage4a_relations_fusion.py, test_stage5_calibration.py, test_truth_corpus_followup.py, test_truth_corpus_followup_r9.py
root-8: test_doctor.py, test_local_index.py, test_phase0b_config.py, test_phase3a_honesty.py, test_semantics.py, test_stage1_windows.py, test_stage2b_pipeline.py, test_stage4b_transforms_schedule.py, test_stage6_enrich.py, test_truth_corpus_followup_r10.py, test_truth_corpus_followup_sol.py
```

```text
tests/idea_web/test_backup.py
50 passed, 1 warning in 37.63s

idea_web remainder shard 1
60 passed, 1 warning in 70.37s (0:01:10)
idea_web remainder shard 2
76 passed, 1 warning in 57.07s
idea_web remainder shard 3
71 passed, 1 warning in 108.38s (0:01:48)
idea_web remainder shard 4
80 passed, 1 warning in 68.89s (0:01:08)
idea_web remainder shard 5
58 passed, 1 warning in 57.07s

top-level shard 1
186 passed, 2 deselected, 1 warning in 109.40s (0:01:49)
top-level shard 2
154 passed, 18 deselected, 1 warning in 48.38s
top-level shard 3
162 passed, 11 deselected, 1 warning in 175.52s (0:02:55)
top-level shard 4
232 passed, 1 skipped, 4 deselected, 1 warning in 370.00s (0:06:09)
top-level shard 5
305 passed, 1 warning in 69.53s (0:01:09)
top-level shard 6
185 passed, 1 deselected, 1 warning in 78.97s (0:01:18)
top-level shard 7
280 passed, 1 warning in 233.69s (0:03:53)
top-level shard 8
169 passed, 1 skipped, 61 deselected, 1 warning in 92.83s (0:01:32)
```

## 7. Static and fixture gates

`ruff format .` was run after the first check named two changed files; it reported:

```text
2 files reformatted, 413 files left unchanged
```

Final requested outputs:

```text
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
415 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 541 files
fixture audit passed

$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

## 8. Final repository checks

The command outputs below were captured after this report was written.

```text
$ git diff --stat
 src/id_detector/cli.py                 | 84 +++++++++++++++++++++++++++++++
 src/id_detector/fuse/episodes.py       | 19 ++++---
 src/id_detector/recipes.py             |  3 +-
 src/id_detector/refusion.py            | 83 ++++++++++++++++++++++++++----
 src/idea_web/jobs/local.py             | 16 +++++-
 src/idea_web/progress.py               |  6 +--
 src/idea_web/server.py                 | 47 ++++++++++-------
 tests/idea_web/test_backup.py          | 55 +++++++++++++++++++-
 tests/idea_web/test_local_queue.py     | 42 +++++++++++++++-
 tests/idea_web/test_refusion_money.py  |  6 +--
 tests/idea_web/test_refusion_upkeep.py |  8 +--
 tests/test_hint_corroboration.py       | 79 ++++++++++++++++++++++++++++-
 tests/test_phase0a_money.py            | 10 ++--
 tests/test_phase0a_status.py           |  4 +-
 tests/test_phase1a_compat.py           |  6 +--
 tests/test_phase1b_targeting.py        |  8 +--
 tests/test_refusion.py                 | 92 ++++++++++++++++++++++++++++------
 17 files changed, 489 insertions(+), 79 deletions(-)
```

```text
$ git status --short
 M src/id_detector/cli.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/recipes.py
 M src/id_detector/refusion.py
 M src/idea_web/jobs/local.py
 M src/idea_web/progress.py
 M src/idea_web/server.py
 M tests/idea_web/test_backup.py
 M tests/idea_web/test_local_queue.py
 M tests/idea_web/test_refusion_money.py
 M tests/idea_web/test_refusion_upkeep.py
 M tests/test_hint_corroboration.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_targeting.py
 M tests/test_refusion.py
?? docs/reviews/build-local-polish.md
```

```text
$ git status --short -- data work
(no output)
```

Protected-tree timestamp proof:

```text
current_work_newer=0
current_corpus_newer=0
main_work_newer=0
main_corpus_newer=0
current_work_files=0
```

## Fix pass 1 (Codex)

### Occurrence-safe corroboration and chronological rows

The P1 policy now has an occurrence guard as well as the existing work-identity guard. After an
untimed hint resolves to one recognised work, fusion attaches it only when that work has exactly
one recognised occurrence in the mix. If there are several occurrences, none is selected by
longest duration, earliest position, evidence score, or any other heuristic. The hint remains
non-positional: it cannot create or retime an episode and it stays out of boundary calibration.

The reversed range was a pre-existing single-row presentation fault, not a consequence of hint
attachment. A role-aware display start could fall after a separately selected best end for a short
fragment. Flat export rows now fall back to their evidence hull when those bounds cross and the
final projection asserts `end_ms >= start_ms` for every published row. Collapsed groups retain
their existing role-aware start and robust group end; this keeps the golden Local Free result
byte-for-byte unchanged. The focused presentation selection passed:

```text
66 passed in 6 files
```

### Regression and revert proof

The untimed regression constructs two recognised occurrences of one work and proves the untimed
hint appears in neither. With only the occurrence guard temporarily reverted, its isolated output
was:

```text
FAILED tests/test_hint_corroboration.py::test_an_untimed_hint_backs_no_occurrence_when_its_work_plays_twice
1 failed
```

The export regression constructs crossed one-sided bounds and proves the published row is
chronological. With only the chronological fallback and assertion temporarily reverted, the test
failed at the projection invariant:

```text
FAILED tests/test_stage9_exports.py::test_a_single_window_row_uses_its_support_hull_when_proved_bounds_cross
1 failed
```

The progress regression now persists the restarted job as a queue row, reloads it through a
replacement `LocalWorker.run_once()`, and advances that attempt's clock by 17 seconds. With only
the restart carry temporarily reverted it failed because `carried_seconds` was 0 rather than 207.
After restoring all three production changes, the three counterfactual tests passed together. The
original eight-test clean-source proof remains as recorded above; the two fix-pass regressions and
the strengthened durable-queue regression therefore also each fail when their fix is reverted.

`JobManager._execute()` does overwrite `started_at` for the replacement attempt. Continuity does
not depend on retaining the original timestamp: elapsed time through the last evaluated bar is
folded into `carried_seconds`, and the replacement attempt accrues from its new clock.

### Corrected cached measurement

The reduced cache-copy attempt failed on a Windows path-length error in the nested recognition
cache. It was deleted and confirmed absent. As explicitly permitted by the fix brief, the
measurement then read the checksum-validated original `present/`, fusion, recognition and hint
records directly without writing to them; all derived objects stayed in memory and scoring files
lived in a temporary directory outside the repository. Timestamp checks below confirm the source
trees were untouched.

| Measurement | Before (`fusion:3`) | After (`fusion:4`) |
|---|---:|---:|
| Eligible named hints attached | 109 | 125 |
| All unique hint evidence IDs attached | 130 | 146 |
| Audio episodes flagged `hint_supported` | 90 | 98 |
| Listed page rows, all 14 mixes | 386 | 387 |
| Release-1 listed rows | 227 | 228 |
| Release-1 work recall | 154/218 · 70.6% | 155/218 · 71.1% |
| Release-1 work precision | 156/207 · 75.4% | 157/208 · 75.5% |
| Release-1 `likely` precision | 62/64 · 96.9% | 62/64 · 96.9% |

Sixteen of the 18 untimed hints now attach. The two refused hints both name `MPH — One Sixty`,
which has two recognised occurrences, so both are positionally ambiguous. Eight episodes gain
support. On `ed4ca55359f9`, rows move 40 → 41: `MPH — One Sixty` keeps only its opening row and
does not gain the second row, while `The Bug — Jah War (feat. Flowdan)` still gains one.

The old work-matcher result could not prove that no wrong row appeared: it ignores time and lets
multiple predictions map to one truth work. The occurrence-level comparison against timed truth
does establish the changed result. `Jah War` is published at 4,761,000–4,773,000 ms inside its
4,693,000–4,807,000 ms truth occurrence. `One Sixty` remains listed only at 45,000–138,000 ms,
overlapping its sole 0–124,000 ms truth occurrence; its later recognised fragment at
2,862,000–2,874,000 ms remains hidden and has no untimed-hint evidence. The honest gain is one
listed track.

### Complete foreground suite, rerun after the fix

Collection found two additional runnable regressions:

```text
$ uv run pytest --collect-only -q -p no:cacheprovider
2072/2169 tests collected (97 deselected) in 3.81s
```

The same complete file partition recorded above was run in foreground. Root shard 4 first found
that applying the flat-row fallback to collapsed grouping moved the frozen golden start from 57 s
to 45 s (`1 failed, 231 passed, 1 skipped, 4 deselected in 459.32s`). That over-broad change was
removed; the golden and export regression passed together, and the whole shard was rerun. Final
shard outputs were:

```text
tests/idea_web/test_backup.py
50 passed, 1 warning in 38.61s

idea_web remainder shard 1
60 passed in 78.03s
idea_web remainder shard 2
76 passed in 84.24s
idea_web remainder shard 3
71 passed in 128.55s
idea_web remainder shard 4
80 passed in 83.90s
idea_web remainder shard 5
58 passed in 80.28s

top-level shard 1
186 passed, 2 deselected in 132.67s
top-level shard 2
154 passed, 18 deselected in 76.01s
top-level shard 3
162 passed, 11 deselected in 319.26s
top-level shard 4, corrected rerun
232 passed, 1 skipped, 4 deselected in 441.73s
top-level shard 5
306 passed in 65.87s
top-level shard 6
186 passed, 1 deselected in 87.67s
top-level shard 7
280 passed in 189.87s
top-level shard 8
169 passed, 1 skipped, 61 deselected in 118.10s
```

Final total: 2,070 passed plus two expected skips = 2,072 runnable tests, with the 97 expected
per-file selection deselections. Every test process used `IDEA_TEST_MODE=1` and an empty
`AUDD_API_TOKEN`; no live provider was called and no server was started on 8791 or 8792.

### Static, fixture and repository gates

`uv run ruff format .` reformatted two changed files (`2 files reformatted, 414 files left
unchanged`). Final requested outputs:

```text
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
416 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 542 files
fixture audit passed

$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

The start cutoff was `2026-09-22T11:17:48.6894111Z`. The owner `work/` tree had 38,640 files and
the owner `data/corpus/` tree had 203; both had zero files newer than the cutoff. This worktree had
zero `work/` files and 203 corpus files, also with zero newer than the cutoff. `profiles/`, the
golden Local Free file and every reserved file listed in the brief have no diff.

```text
$ git diff --stat
 src/id_detector/cli.py                 |  84 +++++++++++++++++++++++++
 src/id_detector/fuse/episodes.py       |  21 +++++--
 src/id_detector/present/exports.py     |  38 +++++++++---
 src/id_detector/recipes.py             |   3 +-
 src/id_detector/refusion.py            |  83 ++++++++++++++++++++++---
 src/idea_web/jobs/local.py             |  16 ++++-
 src/idea_web/progress.py               |   6 +-
 src/idea_web/server.py                 |  47 +++++++++-----
 tests/idea_web/test_backup.py          |  55 ++++++++++++++++-
 tests/idea_web/test_local_queue.py     |  67 +++++++++++++++++++-
 tests/idea_web/test_refusion_money.py  |   6 +-
 tests/idea_web/test_refusion_upkeep.py |   8 +--
 tests/test_hint_corroboration.py       | 109 ++++++++++++++++++++++++++++++++-
 tests/test_phase0a_money.py            |  10 +--
 tests/test_phase0a_status.py           |   4 +-
 tests/test_phase1a_compat.py           |   6 +-
 tests/test_phase1b_targeting.py        |   8 +--
 tests/test_refusion.py                 |  92 +++++++++++++++++++++++----
 tests/test_stage9_exports.py           |  20 ++++++
 19 files changed, 594 insertions(+), 89 deletions(-)
```

```text
$ git status --short
 M src/id_detector/cli.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/present/exports.py
 M src/id_detector/recipes.py
 M src/id_detector/refusion.py
 M src/idea_web/jobs/local.py
 M src/idea_web/progress.py
 M src/idea_web/server.py
 M tests/idea_web/test_backup.py
 M tests/idea_web/test_local_queue.py
 M tests/idea_web/test_refusion_money.py
 M tests/idea_web/test_refusion_upkeep.py
 M tests/test_hint_corroboration.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_targeting.py
 M tests/test_refusion.py
 M tests/test_stage9_exports.py
?? docs/reviews/build-local-polish.md
```

```text
$ git status --short -- data work
(no output)
```
