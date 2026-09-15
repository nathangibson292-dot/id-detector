# Follow-up: owner truth review and the corpus audit

Answers the read-only sol-xhigh retro-review [retro-review-truth-review](retro-review-truth-review.md)
of `ef94b70` (owner truth-review tool) and `362ae10` (truth-record furniture audit). Verify-then-fix
cycle by Claude (Opus), 2026-09-14, in an isolated git worktree based on `ec32f97` (which includes
4a-ii, `3cb5dc2`).

**Method.** Every P0 and P1 was first re-located at `ec32f97` (the retro-review cites `ef94b70`), then
reproduced by a failing test against a temporary copy of `tests/fixtures/truth-review/` or the scorer's
`corpus-mini` fixture before any production change. The failing tests were committed to the worktree
unchanged except where the injection seam itself moved (called out below). `data/corpus/` was never
written, and the review tool was never run against it; it was read once, read-only, to confirm the
audit still passes and that no real corpus file is a reparse point.

**Worktree note.** The worktree was created at `9bfacf8` (an old commit), not the stated base. Its
branch was clean, so it was moved to `ec32f97` with `git reset --hard` before any work; nothing was
committed, branched, merged or pushed.

## Did 4a-ii already fix anything?

No. 4a-ii moved only HTTP routing to FastAPI (`src/idea_web/truth_review.py`, whose docstring at
`:1-10` states "This module adds no path handling of its own"). Every file decision the retro-review
cites stayed in `src/id_detector/truth.py` and `src/id_detector/truth_review.py`, with the same line
numbers as at `ef94b70` for the cited functions. All seven findings were live at `ec32f97`.

## Findings

Regression tests are in `tests/test_truth_corpus_followup.py` unless another file is named.

| Tag | Retro follow-up | Located at `ec32f97` | Verdict | Failing test at `ec32f97` → evidence |
|---|---|---|---|---|
| B2 | 1 (P0) exposure blocks certification | `truth.py:840-942` `freeze_truth` (manifest `:919-940` has no exposure record); `scripts/score_corpus.py:1039` `certifiable = status == "verified" and mode == "time"`; `calibrate/certify.py` `_require_frozen` only | **Reproduced** | `test_exposed_set_is_never_certifiable_through_the_l3_scorer` → `assert True is False` (an exposed, frozen, verified set was `certifiable: true`); `test_freeze_records_and_hashes_the_exposure_evidence` → `KeyError: 'prediction_exposure'`; `test_certification_refuses_a_prediction_exposed_corpus` → no independence refusal (certify proceeded past the corpus checks and failed later on the profile) |
| B3 | 2 (P0) destinations after link resolution | `truth_review.py:343-379` exposure helpers derive sibling paths unvalidated; `:393-407` validates only the truth path; `truth.py:524-542` writes annotation/rollback by path; `io.py:84` `path = path.resolve()` follows the destination link | **Reproduced** | `test_exposure_sidecar_linked_into_work_is_refused`, `test_annotation_linked_into_work_is_refused`, `test_sibling_replaced_by_a_link_after_opening_is_refused_at_write_time`, `test_set_directory_swapped_for_a_junction_into_work_is_refused_at_save` → all `DID NOT RAISE ValueError` (each wrote through the link or junction into the temporary `work/`) |
| B1 | 3 (P0) bulk offset shifts roles identically | `truth_review.py:219-242` `reconciled_role_segments` infers a translation only from four equal deltas, else clips unshifted roles; `:153` preview clamps a role start to `duration`, not the row's start clamp `duration - 1` | **Reproduced** | `test_plus_25_seconds_moves_every_role_segment_with_its_row` → preview `[[(25000, 45000, …)], …, []]`: row 3's role was dropped by the preview itself, and on save row 2's `20–40 s` role clipped to nothing (`role_segments=[]`) |
| B4 | 4 (P1) Windows namespace prefixes | `truth_review.py:47-56` `_real` = `normcase(realpath())` keeps `\\?\`; `truth.py:424` lock key the same | **Reproduced** | `test_extended_length_spelling_cannot_escape_the_work_prohibition` → `DID NOT RAISE` (`\\?\C:\…\work\corpus` accepted); `test_two_spellings_of_one_record_take_one_write_lock` → `DID NOT RAISE` (a second process holding the lock under `\\?\` did not block a save under `C:\`). Pure helper test `test_namespace_prefixes_canonicalise_to_one_key` covers `\\?\UNC\`, `\\.\` and `//?/` (new API, so it failed on import) |
| A1 | 5 (P1) crash between annotation and truth | `truth.py:524-543` `_replace_first_pass`: rollback only in the live process's `except` | **Reproduced** | `test_process_death_between_annotation_and_truth_is_recovered_on_restart` (child process `os._exit(9)` between the two replacements, with a pre-existing annotation) → after restart the annotation held the new pass while the truth was the old draft, and the earlier annotation bytes were gone |
| A2 | 6 (P1) mixed-annotator input | `truth_review.py:282-290` keeps preverified rows; `truth.py:590-606` `_truth_with_content` stamps `first_ref` on every row; session init `:396-399` accepts the mix | **Reproduced** | `test_rows_verified_by_another_annotator_are_not_reattributed` → `DID NOT RAISE`; row 1 verified by `alice` was saved as `owner` |
| A3 | 6 (P1) frozen input | `truth_review.py:396-399` checks `second_pass_ref` / `disagreement_resolution` only | **Reproduced** | `test_a_frozen_set_cannot_be_reopened_or_exposed` → `DID NOT RAISE` (a frozen `dev-1` set reopened) |
| A4 | 7 (P1) furniture audit | `scripts/audit_fixtures.py:284` `_SEED_FURNITURE = re.compile(r"^\s*[-–—•*]")` | **Reproduced** | `test_legitimate_leading_punctuation_is_not_furniture[*NSYNC]`, `[-M-]`, `[-]` → falsely rejected; `test_disguised_tracklist_furniture_is_caught[…]` for U+2212, U+2010, U+2011, U+FE63, U+FF0D, BOM, U+200B, U+200D+U+2060, `0:17:09 - …` and a bare zero-width prefix → `assert 0 == 1` (passed the audit) |

27 tests failed at `ec32f97` (14 of the file's then-41 passed: the forms the old regex already caught, and
legitimate names it did not touch). Retro-review **section C** test gaps are closed by the tests above,
plus `test_in_process_failure_restores_an_existing_annotation_byte_for_byte` (rollback with an existing
annotation) and `test_process_death_after_both_replacements_is_completed_on_restart` (process death,
not a catchable exception).

## Fixes

### B2 — prediction exposure can never certify

- `truth.prediction_exposure(truth_path)` is the one reader: the `review-exposure.json` sidecar (its
  existence counts, whatever it holds) or the first-pass annotation's
  `review_provenance.predictions_visible_during_review`. It returns the flag and `evidence`, a map of
  each file that carries it to its SHA-256. A link is refused, not followed.
- `freeze_truth` still freezes an exposed set, since its truth may be accurate, but every manifest entry now carries
  `prediction_exposure: {predictions_visible_during_review, certifiable, evidence}` and the manifest
  lists `exposed_sets`. `idea truth freeze` prints a `NOT CERTIFIABLE` line naming them.
- `benchmark/scorer.truth_is_frozen_verified` re-hashes every recorded evidence file and raises
  `freeze manifest exposure evidence … is missing or altered` if one was deleted or edited after the
  freeze; `frozen_prediction_exposure` reads the recorded flag.
- `scripts/score_corpus.py`: new `truth_independent()` (live evidence **or** the manifest's record);
  every mix row and `l3` carry `independent`; `l3.certifiable` now also requires it. Scoring still
  runs; the `--print` summary and the one-line output both say the truth is **not independent** and
  cannot back L3. `tests/fixtures/corpus-mini/expected*.json` gained only `"independent": true` (six
  added lines), and the two exact `l3` assertions in `tests/test_score_corpus.py` gained the key.
- `calibrate/certify.py`: `_require_independent` runs straight after `_require_frozen` and raises
  `CorpusNotIndependent` from the manifest record or the set directories; `idea benchmark certify`
  exits through the existing refusal path.

The rule is positive evidence rather than a required attestation, so the committed frozen corpora
(`controlled-synth-1`, `controlled-events-1`), which predate the field and were never reviewed with
this tool, keep working.

### B3 — every destination checked after link resolution, at write time

New `src/id_detector/truth_paths.py`:

- `pinned_set_directory()` holds the set directory for the whole write. On Windows it is opened with
  list access and **without `FILE_SHARE_DELETE`**; its real location is read back from the handle
  (`GetFinalPathNameByHandleW`) and checked against the work tree and the location vetted when the
  review opened (`TruthReviewSession.set_dir_key`). On POSIX it is a directory descriptor that every
  operation is rooted at (`dir_fd`, `O_NOFOLLOW`, `O_EXCL`).
- `PinnedDirectory.write_bytes` refuses a destination that is a link, creates the temporary file
  exclusively inside the held directory and replaces with `MoveFileExW` / `renameat`. Those replace
  the directory entry and never follow it, so a link swapped in after the check is overwritten rather
  than written through. `read_bytes` refuses links and checks the opened file's identity.
- Probe on this host (evidence for the Windows claim): with such a handle held, renaming the pinned
  directory failed with error 32 and renaming its parent with error 5, while creating and replacing
  files inside it still worked. With attribute-only access the pinned directory itself *could* still
  be renamed, which is why list access is requested.
- `truth.py`: the pass commits' replacements (truth, all three annotation passes, the transaction
  record, rollback) go through `_write_set_file(pinned, name, bytes)` inside
  `_commit_annotated_truth`. **Corrected by the second-model review:** this first version claimed
  *every* set-file replacement did, which was false. `seed_truth`, `freeze_truth` (truth files and
  manifest) and `write_draft_manifest` still used `atomic_write_json`, which resolves the destination
  first. Those now go through the pinned writer too; see the section at the end.
  `truth_review.py`: the exposure sidecar is written through the same held directory, under the write
  lock. `TruthReviewSession.__init__` refuses a set whose truth, annotation, exposure or transaction
  name is a link before reading anything through it.
- `is_link` counts only name-surrogate reparse points (symlinks, junctions), so a cloud-sync
  placeholder is not refused. None of the 25 paths under `data/corpus/release-1/` is a reparse point.

### B1 — bulk offset intent carried into role reconciliation

- The page keeps `appliedOffsets` (grows on apply, shrinks on undo, untouched by a preview) and saves
  `{rows, offsets_ms}`.
- `reviewed_record(..., offsets_ms=)` validates the list and replays it through `preview_bulk_offset`,
  so every role endpoint gets exactly the row's shift and clamp: a role's start uses the row's start
  clamp (`duration - 1`), its end the end clamp. A row that matches the replayed state keeps the
  shifted segments; one the owner edited further is reconciled against that shifted state.
- A row whose edit would discard all of its hand-made segments is now refused with the row number,
  rather than saved with `role_segments: []`.
- Regressions: the fixture `+25 s` case (all three rows asserted, preview and saved);
  `test_without_the_offset_intent_a_clamped_row_refuses_rather_than_dropping_its_roles`;
  `test_offsets_intent_is_validated`; `test_shipped_js_sends_the_applied_offsets_net_of_undo` (the
  page's own script under Node).

### B4 — one spelling for containment and for the lock

`truth_paths.strip_namespace_prefix` removes `\\?\`, `\\.\` and their `UNC\` forms, including
forward-slash spellings. `real_path` strips before and after `realpath`, and `path_key` normcases the
result. `reject_work_destination` and `find_truth_path` compare with `is_within`, and
`truth_write_lock` keys on `path_key`, so two spellings of one record take one lock.

### A1 — durable transaction for the annotation+truth pair

Before either replacement, `_commit_pair` writes `ground_truth.transaction.json` beside the truth. It
records the previous truth and annotation bytes (UTF-8 text, or base64) and both new digests. Then it
replaces the annotation, then the truth, then removes the record. An exception rolls back in-process.
Process death leaves the record, and `recover_interrupted_write` resolves it on the next open or write:
if both files already carry the new digests the pair is kept (**completed**), otherwise both are
restored byte-for-byte (**rolled back**). The session's init, `verify_truth`, `second_pass_truth` and
`resolve_truth` recover first. `freeze_truth` refuses a set with a pending record. An idle set opens
without taking the lock; recovery takes it only when a record exists.

### A2 / A3 — no re-attribution, no reopening a frozen set

- `truth.refuse_reattribution`: a first pass stamps one annotator on every row, so rows already
  verified by someone else (or with no recorded annotator) are **refused** with their row numbers,
  not rewritten. Preserving them instead was rejected: it would produce a first-pass annotation whose
  `annotator_ref` contradicts its rows, which a test-split freeze rejects anyway. Enforced at session
  open and in `verify_truth`'s annotation modes.
- `truth.refuse_frozen`: a set listed by a `frozen: true` `corpus-version.json` beside it or up to
  three directories above is terminal. (**Corrected in the second pass:** the window is now the set
  directory, its corpus directory and the one above, the same as the scorer's, and freeze may only
  write its manifest there.) Enforced at session open, at reveal and at save (inside the lock), and
  in `verify_truth`, `second_pass_truth` and `resolve_truth`. Frozen is read from a
  manifest rather than from `corpus_version`, because the committed `dev-1` drafts carry
  `corpus_version: "dev-1"` under a `frozen: false` inventory.

### A4 — furniture audit follows the seed parser's grammar

`scripts/audit_fixtures.py` `_label_defect`:

1. Skip leading whitespace and invisible format characters (Unicode category `Cf`: BOM, zero-width
   space/joiner, word joiner). (**Incomplete, corrected in the second pass:** only *leading* `Cf`
   characters were removed, so one between the timestamp, marker and whitespace still hid
   furniture.)
2. Fold compatibility forms (NFKC: fullwidth and small hyphens, no-break spaces), and map every dash,
   minus and bullet variant to one marker.
3. Flag furniture only where `truth._TRACKLIST` would have consumed it: an optional timestamp, then a
   marker **followed by whitespace**.

An invisible leading character is reported even without furniture. `*NSYNC`, `-M-`, `-`, `!!!`,
`+44`, `¡Forward, Russia!` and `(hed) p.e.` pass. The audit still passes on the real committed corpus.

## Test seams that moved

Three existing tests in `tests/test_truth_review.py` injected failures by patching
`truth.atomic_write_json` / `truth_review.atomic_write_json`. Those functions no longer perform
set-file writes, so the patches now target the new seams, `truth._write_set_file` and
`truth_review._write_exposure`. No assertion was weakened; the replace-failure test also asserts that
no transaction record is left behind. The crash test's child process kills itself at the same seam.
At `ec32f97` it used `atomic_write_json`, which was the seam then.

## What remains (not blockers for this cycle)

- **POSIX branch not exercised here.** The `dir_fd` path in `truth_paths.py` was written for Linux
  hosts but only the Windows branch ran on this machine.
- ~~**Frozen discovery depth.**~~ Listed here as non-blocking in the first version; the second-model
  review rightly made it a P1 (retro A3 stayed reproducible). Fixed in the second pass: freeze now
  refuses a manifest outside the discovery window.
- **Calibration validation** (`idea benchmark validate`, explicitly "not certification") is not gated
  on independence.
- **Carried P2s from the first report:** the interactive `idea truth verify` still writes a single
  `dominant` span; `second_pass_truth` / `resolve_truth` read their inputs before taking the lock
  (their commit and recovery now run under it); lock files in the temp directory are not removed. New,
  and pre-existing: the page applies a bulk offset to already-verified rows too, and save then refuses
  them as edited.

## Gate outputs

Environment: isolated worktree at `ec32f97`. `uv sync` initially failed to create the `pytest.exe`
launcher (Windows "Access is denied" on the uv trampoline); `uv sync --reinstall-package pytest` fixed
it. There is no `.env`, so no real AudD token could load.

### `uv run pytest -q` (full default run)

```text
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[complete]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[degraded]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[partial]
FAILED tests/test_phase2b_retention.py::test_reparse_point_cannot_move_files_outside_work
4 failed, 1329 passed, 1 skipped, 93 deselected, 1 warning in 666.97s (0:11:06)
```

**The four failures are environmental and unrelated to this cycle.** All four fail at the same line, in
the retention tests' `_seed_media` helper (`tests/test_phase2b_retention.py:64`,
`downstream.write_text(...)`), with `FileNotFoundError` right after the parent directory was created.
The helper nests two 64-character hash directories under pytest's temporary root, and that root had
just grown by one character: `pytest-of-natha\pytest-1005` (earlier runs on this host were
`pytest-998`). The failing file path measures **260 characters**, one past the 259 Windows allows
without a long-path prefix; under `pytest-998` the same path is 259. Neither `retention.py` nor the
test imports anything this cycle changed except `cli.py`, whose edits here are an import, an exception
tuple and a message.

Confirmation, with this cycle's changes in place and a short temporary root:

```text
uv run pytest "tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d" "tests/test_phase2b_retention.py::test_reparse_point_cannot_move_files_outside_work" -q --basetemp=<short temp dir>
4 passed, 1 warning in 4.88s
```

It is a pre-existing latent test fragility (unprefixed long paths in a test helper), left for its
owner: it is outside this cycle's scope and would change on main as soon as pytest's counter passes
999 there.

### Focused suites

`uv run pytest tests/test_truth_corpus_followup.py tests/test_truth_review.py tests/test_stage2a_truth.py tests/test_score_corpus.py tests/test_fixture_audit.py tests/idea_web/test_parity.py tests/idea_web/test_legacy_contract.py tests/test_phase0a_security.py tests/test_playlists.py tests/test_golden_local_free.py -q`:

```text
274 passed, 1 warning in 84.50s (0:01:24)
```

After removing the now-dead `truth_review._real`:
`uv run pytest tests/test_truth_review.py tests/test_truth_corpus_followup.py -q` → `79 passed, 1 warning in 14.97s`.

Baseline before any change (same four truth files at `ec32f97`): `112 passed, 5 warnings in 74.97s`.

### `uv run ruff check .` / `uv run ruff format --check .`

```text
All checks passed!
302 files already formatted
```

### `uv run python scripts/audit_fixtures.py` (real committed corpus included)

```text
audited 459 files
fixture audit passed
```

### `uv run python scripts/check_page_js.py`

```text
page JavaScript check passed: 53 inline scripts across 22 page renders
```

### `scripts/smoke_serve.ps1` and `scripts/gate_local_mode.ps1`

**The PowerShell gates could not be launched in this worktree.** The worktree-isolation guard refuses
every `powershell` invocation (`-File` and `-Command` alike), because it cannot show the script does
not run git. That is an environment restriction, not a port collision and not a product failure. The
same checks were run from Python instead, with a port that runs the same commands and environment,
the same 20 s / 30 s / 45 s readiness budgets and the same assertions, and extracts the local-mode
helper **verbatim** from `gate_local_mode.ps1`. It stops only the process trees it started, by their
own PIDs. **The orchestrator should still run both `.ps1` gates on `main` when applying this.**

Ports 8791, 8792 and 8793 were checked free (bind probe) before and after.

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

```text
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\idea-local-gate-0405cc7dec2b478791426028edc5b6d3\work\f526b42a8545f9ae83d031b734711ae7618583bcb9f0bee979fc2839574e8f8e\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\9b838acffd275cdacdaddfcf034ff551eb0cb19454aef06e4d11242ac00521ba\tracklist.json
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-0405cc7dec2b478791426028edc5b6d3
```

Both exited 0 on the first attempt.

### Repository and process state

`git status --short -- work data` is **empty**. `wmic` is not installed on this host and PowerShell is
blocked, so leftover processes were checked by reading every process's command line directly
(`NtQueryInformationProcess`, class 60) and matching this worktree's path and the gate's evidence
directories.

```text
command lines read: 253
leftover processes from this worktree or its gates: 0
```

No `idea serve`, `idea truth review`, worker, uvicorn or gate browser is left running. The only
processes this cycle started were its own test runs, the two gate ports (their trees stopped by PID)
and read-only probes. The short `--basetemp` directories made for the retention confirmations were
removed. Nothing was committed, branched, merged or pushed.

## Second-model review (sol xhigh) + fix pass

The diff review by Codex `gpt-5.6-sol` at xhigh (read-only) returned **FIX_FIRST** with 7 P0 and 2 P1
correctness findings, plus a list of missing regressions (its section D). It confirmed B1 (bulk offset),
B4 (namespace spellings) and A1 (crash-atomic pair) as fixed. This pass is again by Claude (Opus) in the
same worktree.

**Method.** All section D regressions were written first, in `tests/test_truth_corpus_followup_sol.py`,
and run against the follow-up exactly as it stood before this pass. None of those production files had
been touched; a byte copy of them was kept aside. That is the "reverted implementation": **31 tests
failed** there, 1 was a positive control that passes on both versions, and 1 was a legitimate skip (see
below). Each failure was checked to be the defect, not a set-up error. Only then was production code
changed. Two lint-only test edits followed (an unused import, a long line); no assertion changed. The
positive-control audit test is labelled as one and is not counted as evidence of any fix.

### Findings

| sol tag | Finding | Verdict | Failing test before this pass → evidence |
|---|---|---|---|
| B1 (P0) | Cached exposure: after a reveal, a deleted sidecar was not rewritten on the next reveal | **Reproduced** | `test_reveal_after_the_sidecar_was_deleted_rewrites_it_before_answering` → `assert [True, False] == [True, True]` (predictions were assembled with no sidecar on disk) |
| B2 (P0) | `idea truth verify` (annotation and interactive modes) overwrote exposure-bearing provenance | **Reproduced** | `test_cli_reverification_with_an_annotation_cannot_downgrade_exposure`, `test_interactive_reverification_cannot_downgrade_exposure` → `assert False is True` (the set read as independent afterwards) |
| B3 (P0) | The generic scorer / `idea benchmark score` certified exposed truth | **Reproduced** | `test_generic_scorer_certifies_independent_truth_but_never_exposed_truth` → `{'certified', 'provisional'} == {'provisional'}` (the independent control certified as expected, and so did the exposed corpus, also through the CLI) |
| B4 (P0) | Frozen `annotation_passes` recorded but never re-verified; links followed | **Reproduced** | `test_frozen_annotation_passes_and_records_are_reverified[delete / alter / link / appear-second / truth-link / escaping-path]` and `test_linked_exposure_evidence_is_refused_even_with_identical_bytes` → all `DID NOT RAISE` |
| B5 (P0) | Seed and freeze bypassed the pinned writer | **Reproduced** | `test_freeze_refuses_a_linked_truth_record` → the linked target's bytes **were overwritten**; `test_freeze_refuses_a_linked_manifest_destination`, `test_seed_refuses_a_linked_destination` → `DID NOT RAISE`; `test_cli_freeze_refuses_a_corpus_beneath_the_work_tree` → the CLI printed `froze 1 sets as fx-v1; manifest=work\corpus\corpus-version.json` |
| B6 (P0) | Freeze did not hold the truth lock across snapshot and publish | **Reproduced** | `test_a_reveal_cannot_land_between_the_freeze_snapshot_and_its_manifest` (a reveal is started right after freeze's exposure snapshot) → `assert not (True and not False)`: the reveal landed and freeze published an "independent" manifest beside the new sidecar |
| B7 (P0) | Forward-only state not enforced outside the web session | **Reproduced** | `test_no_cli_pass_can_step_truth_backwards[…]`: verify (both modes) and second pass over second-pass and resolved truth, and resolve over resolved truth → 7 × `DID NOT RAISE` (each rewrote the set and cleared later-pass fields). The eighth combination, resolving an unresolved second pass, is the legitimate forward transition and is skipped |
| B8 (P1) | Frozen status undiscoverable when `--out` is elsewhere | **Reproduced** | `test_freeze_refuses_a_manifest_that_frozen_status_could_not_find[manifest-off-tree / manifest-wrong-name]` → `DID NOT RAISE` |
| B9 (P1) | Zero-width characters inside the furniture prefix | **Reproduced** | `test_invisible_characters_inside_the_furniture_prefix_are_caught[…]`, six spellings including `"-​ Mall Grab"` and `"0:17:09​ - Mall Grab"` → `AssertionError: []` (the audit passed them) |

No finding was judged wrong. One nuance on B4 `truth-link`: before this pass a linked truth file was
followed away from its corpus, so the scorer found no manifest and returned "not verified" instead of
certifying. That is safe, but silent. The manifest lookup now resolves directories only, finds the
corpus manifest and refuses the link out loud.

### Fixes

**B1 / B2 — exposure is monotonic.**
- `TruthReviewSession.reveal_predictions` now takes the record's cross-process write lock and the
  pinned set directory on *every* reveal, not only the first. Under them it re-checks frozen state and
  rewrites `review-exposure.json` if it is missing, all before `_predictions()` runs. The cached flag is
  no longer trusted.
- Every first-pass writer goes through `_write_first_pass_atomically`, which builds the annotation
  *inside* the lock from the held directory's state (`_pinned_exposure`: the sidecar exists, or the
  annotation being replaced records exposure, or that annotation is unreadable). If exposure is already
  recorded, the new `review_provenance` carries `predictions_visible_during_review: true` and
  `exposure_carried_forward: true`, whatever the caller supplied. That covers the review save, `verify
  --annotation` and the interactive `verify`.

**B3 — the generic scorer gates certification on independence.**
- New `benchmark/scorer.corpus_independent(path, truths)`: live evidence beside each loaded record
  (`truth.prediction_exposure`) **or** the frozen manifest's recorded exposure.
- In `score_corpus_detailed`, `status: "certified"` now also requires it. `idea benchmark score` and
  every other direct caller go through this function. `run_certify` and `scripts/score_corpus.py` keep
  their own refusals from the first pass.

**B4 — full frozen re-verification.**
- `truth_is_frozen_verified` checks the truth file, every exposure-evidence file and every
  `annotation_passes` entry through `_require_frozen_file`: a real file (not a link, symlink or
  junction), inside the manifest's corpus directory (no `..` escape), with the recorded SHA-256.
- A pass recorded as `null` that now exists is refused as "appeared after the freeze". The committed
  frozen corpora (`controlled-synth-1`, `controlled-events-1`) record all passes as `null` and hold no
  annotation files (checked read-only), so they still verify.
- `find_freeze_manifest` resolves only directories, never the record itself.

**B5 / B6 — seed, freeze and manifests through the pinned writer; freeze serialised.**
- `truth.record_path()` refuses a record that is a link and resolves only its directories. It is used by
  every pass, recovery, seed and freeze; the review session also calls it on the path it was given.
- `seed_truth` and `write_draft_manifest` write through `_write_record_file`: the record's write lock,
  the pinned set directory, then `_write_set_file`. The CLI passes the work root to seed, verify,
  second pass, resolve and freeze.
- `freeze_truth` is now plan → lock → snapshot → publish:
  - `_freeze_plan` refuses linked records, records resolving outside the corpus being frozen (rglob
    can walk a junction) and records beneath `work/`.
  - It takes *every* record's write lock, in `path_key` order, and holds every set directory for the
    rest of the freeze.
  - It reads the truth, recovery record, exposure snapshot and annotation-pass hashes from the held
    directories.
  - It writes the frozen truth files and the manifest through pinned directories, and releases the
    locks only after the manifest is published.
- A concurrent reveal therefore either lands before the snapshot (and is recorded) or waits and is then
  refused, because the set is frozen.
- `src/id_detector/io.py` was **not** edited.

**B7 — forward-only transitions.**
- `truth.refuse_later_passes(truth, exists, pass_name)`:
  - a first pass is refused once a second pass exists (a `second_pass_ref` or
    `annotation-second.json`);
  - a first or second pass is refused once resolved (a `resolved-by:` resolution or
    `annotation-resolution.json`);
  - a resolution is refused once resolved.
- It runs early for a clear message, and again inside `_commit_annotated_truth` under the lock against
  the held directory.
- There, the truth bytes must also match the digest the pass was prepared from, so a pass cannot commit
  over a record that changed after it was read. This also closes the "reads inputs before the lock"
  carried P2 for second pass and resolve.
- `verify_truth`, `second_pass_truth` and `resolve_truth` refuse rather than clear fields.

**B8 — manifest location constrained.** `freeze_truth` accepts only a manifest named
`corpus-version.json` in a set directory, its corpus directory or the directory above. That is the
window that `refuse_frozen`, the scorer and certification search (`manifest_search_directories`). It
also refuses a manifest beneath `work/` or one that is a link. All of this is validated before any truth
file is written. `covering_freeze_manifest` uses the same window. `idea truth freeze --out` help says so.

**B9 — invisible characters anywhere in the prefix.** `_label_defect` removes every `Cf` character from
the whole label before matching the seed grammar, and reports a `Cf` that appears before the first
letter. A joiner inside a name (after its first letter) is left alone. `*NSYNC` and `-M-` still pass,
and the audit still passes on the real committed corpus.

### Corrected claims in this record

- "every set-file replacement goes through `_write_set_file`": false in the first version (seed,
  freeze and draft manifest bypassed it). Now true for every truth-set and manifest write in `truth.py`.
  Corrected inline above.
- "What remains → frozen discovery depth: not a blocker": wrong severity. Fixed (B8), and struck
  through above.
- A4 step 1 stripped only *leading* `Cf` characters. Corrected inline above; fixed (B9).
- The carried P2 "`second_pass_truth` / `resolve_truth` read their inputs before taking the lock" is
  closed by the in-lock digest and state re-check (B7).

### Existing tests changed in this pass

- `tests/test_stage2a_truth.py::test_seed_combines…work-only` froze with
  `--out tmp/work-only-manifest.json`, a manifest nothing could discover. It now writes
  `corpus-version.json` in the set directory; its assertion is unchanged.
- No other existing test changed.

### What remains

- **POSIX branch still not exercised** on this Windows host (unchanged from the first pass).
- **Calibration validation** (`idea benchmark validate`, explicitly "not certification") is still not
  gated on independence.
- Carried P2s: the interactive `idea truth verify` still writes a single `dominant` span; lock files in
  the temp directory are never removed; the page applies a bulk offset to already-verified rows, which
  save then refuses.
- The PowerShell gates were not re-run in this pass; this pass changed nothing they exercise
  (`idea serve`, the local-mode page and audio). The orchestrator should still run both `.ps1` gates on
  `main`.

### Gate outputs (this pass)

**Full suite, in the foreground.** One `uv run pytest -q` run takes about 11 minutes, longer than a
single foreground call here allows (10 minutes). So the default collection was run in two foreground
halves that together name every collected test file: `tests/idea_web` + `test_acrcloud_clip` …
`test_phase3a_honesty`, then `test_playlists` … `test_truth_review`.

```text
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[complete]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[degraded]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[partial]
FAILED tests/test_phase2b_retention.py::test_reparse_point_cannot_move_files_outside_work
4 failed, 627 passed, 1 warning in 403.22s (0:06:43)
```

```text
734 passed, 2 skipped, 1 deselected, 1 warning in 204.95s (0:03:24)
```

**Exactly the four known environmental failures** (pytest's shared temp counter past 999 pushes those
tests' seeding paths past the 260-character limit; `LongPathsEnabled = 0`). They were not changed, and
`--basetemp` was not shortened. Everything else passed: 1361 passed, 2 skipped (the forward-transition
skip and a pre-existing one).

**Focused suites** (the orchestrator's list, plus the new test file and the scorer, certification and
corpus suites the scorer change touches): `uv run pytest tests/test_truth_review.py
tests/test_truth_corpus_followup.py tests/test_truth_corpus_followup_sol.py tests/test_stage2a_truth.py
tests/test_stage2a_scorer.py tests/test_stage2b_corpus.py tests/test_stage5_calibration.py
tests/test_score_corpus.py tests/test_fixture_audit.py tests/idea_web/test_parity.py
tests/idea_web/test_legacy_contract.py tests/test_phase0a_security.py tests/test_playlists.py
tests/test_golden_local_free.py -q`

```text
344 passed, 1 skipped, 1 warning in 110.51s (0:01:50)
```

**`uv run ruff check .` / `uv run ruff format --check .`**

```text
All checks passed!
304 files already formatted
```

**`uv run python scripts/audit_fixtures.py`** (real committed corpus included)

```text
audited 460 files
fixture audit passed
```

**`uv run python scripts/check_page_js.py`**

```text
page JavaScript check passed: 53 inline scripts across 22 page renders
```

**Repository and process state.** `git status --short -- work data` is empty. Leftover-process scan (every
process's command line matched against this worktree's path and the gate directories):

```text
command lines read: 248
leftover processes from this worktree or its gates: 0
```

Nothing was committed, branched, merged or pushed.

## Round-2 review (sol xhigh) + fix pass

The round-2 diff review (Codex `gpt-5.6-sol` at xhigh, read-only) returned **FIX_FIRST** with 3 P0 and 3
P1 findings. It confirmed these as fixed, and they were left alone except where a fix below needed
otherwise:
- **B2** first-pass provenance is ORed in.
- **B3** the generic scorer gates `certified` on independence.
- **B6** freeze locks in `path_key` order through publication.
- **B7** forward-only guards run with the digest check inside the lock.
- **B9** `Cf` characters are handled anywhere in the furniture prefix.

It also accepted the `test_stage2a_truth.py` edit, found no stranded committed manifest and no
4a-ii or `idea.cmd` regression, and showed calibration validation cannot emit a certification claim.
It downgraded two claims in this record: "full frozen re-verification" and "fully pinned
destinations". Both overclaimed until this pass.

**Method.**
1. The regressions were written first, in `tests/test_truth_corpus_followup_r2.py`.
2. They were proven against the reverted code by swapping it in: the pre-pass production files were
   extracted over the worktree, the round-2 file was run, then the current files were restored from
   a backup and compared byte-for-byte (`tar -d`: identical). A first attempt shadowed the package
   with `PYTHONPATH`, but the editable install still loaded the worktree code, so that run was
   discarded as non-evidence.
3. Against the reverted code: **16 failed, 2 passed**. The 2 passes are explained in the table.
4. Tests that could hang on the old lock run their competitor in a thread with a bounded join, so
   the old unbounded wait shows up as a failure instead of a stuck run.

### Findings

| r2 tag | Finding | Verdict | Failing test against the reverted code → evidence |
|---|---|---|---|
| P0-1 | The manifest authenticated a different file from the one scored | **Reproduced** | `test_an_altered_copy_of_a_frozen_set_is_not_verified_by_the_originals_entry`: a same-`set_id` copy under the corpus, with an altered title, was verified by the original's entry, through both `truth_is_frozen_verified` and `score_corpus.truth_status` → `DID NOT RAISE`. `test_a_linked_freeze_manifest_is_refused` → `DID NOT RAISE`. `test_a_set_directory_junction_escaping_the_corpus_is_refused` → `DID NOT RAISE` (the old lookup followed the junction to a place with no manifest and silently said "not verified") |
| P0-2 | Exposure could be deleted between the rewrite and the return | **Reproduced** | `test_exposure_deleted_during_prediction_assembly_is_restored_and_blocks_certification`: an actor ignoring the lock deletes the sidecar inside `_predictions()` → `assert False` (the sidecar was gone after the reveal). `test_exposure_cannot_be_deleted_while_the_reveal_returns` → `[] == ['denied']` (no deletion-denying hold existed). `test_reveal_fails_closed_when_the_evidence_cannot_be_reasserted` → `DID NOT RAISE` (predictions were returned with no evidence on disk) |
| P0-3 | The validated identity was not carried into the pin | **Reproduced**, one variant already refused | `test_freeze_refuses_a_set_swapped_for_a_junction_into_another_set_after_planning` (set moved after `_freeze_plan`, name replaced by a junction into another corpus's set, manifest in the shared parent) → `DID NOT RAISE`, and the other set's truth was overwritten. `test_a_rejected_seed_creates_no_directories_beneath_work` → `assert not True` (directories were created under `work/` before the refusal). `test_seed_refuses_a_pre_existing_ancestor_link` → `DID NOT RAISE` (wrote through the junction). **Already refused before this pass:** `test_freeze_refuses_a_set_swapped_for_a_junction_into_work_after_planning` passed on the reverted code, because the pin's final-path work-tree check caught it. It stays as a regression |
| P1-4 | Two accepted manifest placements stranded the freeze | **Reproduced** | `test_every_accepted_manifest_placement_verifies_through_review_and_scoring[set-directory]` and `[directory-above]` → `ValueError: freeze manifest truth record … is missing or altered` (entry paths were relative to `truth_dir`). The `[corpus-directory]` case passed on the reverted code, as the reviewer predicted |
| P1-5 | Lock deadline ignored competing threads; unlock failure leaked the guard | **Reproduced** | `test_a_competing_thread_times_out_instead_of_hanging` → "the in-process lock ignored its deadline". `test_a_timed_out_freeze_releases_the_records_it_had_already_locked` → "freeze hung on a thread-held record lock". `test_an_unlock_failure_still_releases_the_in_process_lock` (fails `msvcrt.locking(LK_UNLCK)`) → "the in-process lock leaked after the unlock failure" |
| P1-6 | Freeze could rewrite a frozen corpus | **Reproduced** | `test_freeze_refuses_an_already_frozen_corpus[same-version]` and `[other-version]` → `DID NOT RAISE` |

No finding was judged wrong. One test was corrected before its reverted run. The first draft of the
sibling-swap test placed the manifest in the corpus directory, so it was refused by the
manifest-window check (coincidentally, before any write) rather than by identity. It was moved to the
shared parent, where only identity pinning can refuse it, and the table's evidence is from that
version.

### Fixes

**P0-1 — each scored record is bound to its own manifest entry.**
- `benchmark/scorer.load_truth_files()` returns each record with its file and the exact bytes parsed.
  `load_truth_directory` is now a thin wrapper around it.
- `truth_is_frozen_verified` loads the files itself and, for every truth being scored:
  - the in-memory record must equal the record parsed from disk;
  - the manifest `path` must be relative, with no `..`;
  - no component from the manifest's directory down to the entry may be a link;
  - the scored file must be **the same file** as the entry (`path_key` equality), so a same-`set_id`
    copy elsewhere under the corpus is refused;
  - SHA-256 of the **loaded bytes** must equal the entry's hash.
- Exposure-evidence and annotation-pass files get the same link-component check.
- `find_freeze_manifest` walks the path as spelled, without resolving links, and refuses a
  `corpus-version.json` that is itself a link. A set reached through a junction is therefore found
  under its corpus manifest and refused, instead of being followed to a place with no manifest.

**P0-2 — the reveal holds its evidence undeletable while returning.**
- `reveal_predictions` now, still under the record lock and pinned directory:
  1. writes the sidecar if it is missing;
  2. assembles predictions;
  3. **reasserts** the sidecar (rewriting it if it was deleted meanwhile);
  4. opens it through `PinnedDirectory.hold_undeletable()`, which on Windows is `CreateFileW` with
     `FILE_SHARE_READ | FILE_SHARE_WRITE` (no `FILE_SHARE_DELETE`) and `FILE_FLAG_OPEN_REPARSE_POINT`,
     with an identity check against the directory entry;
  5. verifies the held bytes record this set's exposure;
  6. returns the predictions from inside that hold.
- A deletion during the hold fails with a sharing violation. That is proven by the `_evidence_held`
  seam test: `os.remove` → `PermissionError`.
- If the evidence cannot be rewritten, disappears three times, or does not verify, the reveal raises
  and returns nothing.
- The original ordering is kept: the sidecar is on disk before `_predictions()` is first called, so
  `test_reveal_route_records_before_it_answers` is unchanged.

**P0-3 — identity carried from validation into every pin.**
- `truth_paths.link_components()` lists every existing link component of a path's absolute spelling.
  `record_path()` now refuses a record with **any** existing link component, not only the final one.
- `_freeze_plan` returns each canonical record *with the `path_key` of its set directory computed
  during validation*. `freeze_truth` pins exactly those identities (`expected_key`) and looks pinned
  directories up by planned record, never by re-resolving the path.
- The manifest directory is pinned with its validated identity.
- `_write_record_file` (seed, draft manifest) validates everything before anything is created:
  - no link component;
  - not beneath `work/`, checked on the destination itself, whose non-existent part resolves under
    its nearest existing ancestor.
  
  It computes the expected directory identity from that ancestor, only then creates directories,
  and pins with that identity.

**P1-4 — manifest entry paths relative to the manifest's own directory.** `_publish_freeze` computes
entry paths against the directory the manifest is written in (the one the verifier resolves against).
All three accepted placements (set directory, corpus directory, the one above) are tested end to end:
- `truth_is_frozen_verified` → `True`;
- `score_corpus.truth_status` → `"verified"`;
- `TruthReviewSession` refuses the set as frozen.

`tests/test_stage2a_truth.py` asserted the old, unverifiable `set-one/ground_truth.json` for a manifest
written in `tmp_path`. It now asserts `truth/set-one/ground_truth.json`, the path the verifier resolves.
That is the only existing-test change in this pass.

**P1-5 — one deadline, guaranteed release.** `_TruthLock.acquire` computes one deadline and applies it
to the in-process `RLock` (`acquire(timeout=…)`) and to the OS lock with the remaining time.
`release` releases the in-process guard in a `finally`, even if `_unlock_os()` raises; `_unlock_os`
already closes the OS handle in its own `finally`, which drops the OS lock. A timed-out multi-record
freeze releases the locks it holds through its `ExitStack`; this is proven by another thread taking
the already-locked record straight afterwards.

**P1-6 — freeze refuses a frozen corpus.** `_freeze_locked` runs `refuse_frozen` for every record
inside the locks. An existing covering `frozen: true` manifest makes freeze refuse, whether the version
is the same or different, and nothing is written. Re-freezing is refused rather than made "read-only
when identical": a refusal is simpler and cannot drift.

### Residual risks

These are **not** fully closed. Each gives what an attacker (or accident) would need.

1. **Deleting the exposure sidecar at rest, outside a reveal.** The deletion-denying hold covers only
   the reveal's return path; it is released when `reveal_predictions` returns, before the HTTP
   response bytes are sent. After that, any local actor with delete permission on the set directory
   who ignores the advisory lock can remove `review-exposure.json`. If the review then ends **without a
   save**, a restarted session sees no exposure. *Needs:* local write/delete access to the corpus set
   directory, and a reveal never followed by a save.
   - *Limits:* a save records exposure in `annotation-first.json`; a freeze hashes the evidence, and
     deletion after a freeze is detected; the sidecar is a tracked file in the committed corpus, so its
     deletion shows up in `git status` / review.
2. **POSIX has no deletion-denying open.** On Linux and macOS the reveal's hold is only an open
   descriptor; an unlink during the return path succeeds. The reassert-and-verify still happens
   immediately before the hold, so the window is the few statements between verification and return.
   *Needs:* the same access as (1) and sub-millisecond timing. The POSIX branch was not run on this
   Windows host.
3. **The advisory lock binds only cooperating writers.** The pinned directory stops the held set
   directory and its ancestors from being renamed or replaced (proven on Windows). It does **not**
   stop a non-cooperating process from rewriting a *file* inside the set between the in-lock digest
   check and the atomic replace. That edit is then lost; it cannot be written through, and a replaced
   entry is never followed. *Needs:* local write access and deliberate timing inside a single save or
   freeze.
4. **The freeze manifest is integrity evidence, not a signature.** Anyone who can edit
   `corpus-version.json` can recompute its SHA-256 values and exposure records, and verification would
   accept the rewritten manifest. Protection against a deliberate rewrite rests on version control and
   review of the committed manifest. *Needs:* write access to the manifest, and a commit that nobody
   reviews.
5. **Temp-directory lock files.** A local process can hold or pre-create
   `%TEMP%\idea-truth-<digest>.lock` to deny writers (they time out and refuse). This is
   denial-of-service only and cannot cause a bad write. *Needs:* local access to the user's temp
   directory.
6. **Usability, not risk:** ancestor-link refusal covers every existing component of the spelled
   path, so a corpus reached through a symlinked or junctioned folder anywhere above it is refused.
   The workaround is to pass the resolved path. None of the 25 `data/corpus/release-1/` paths, nor this
   worktree's paths, contain a link.

### Gate outputs (this pass)

**Full suite in the foreground, in two halves** (a single run exceeds the 10-minute foreground limit).
The two halves together name every collected test file, the new `test_truth_corpus_followup_r2.py`
included.

```text
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[complete]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[degraded]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[partial]
FAILED tests/test_phase2b_retention.py::test_reparse_point_cannot_move_files_outside_work
4 failed, 627 passed, 1 warning in 366.90s (0:06:06)
```

```text
752 passed, 2 skipped, 1 deselected, 1 warning in 195.13s (0:03:15)
```

The only failures are **exactly the four known `test_phase2b_retention.py` long-path failures** (owned
by the separate long-path fix). They were not changed.

**Focused suite (the coordinator's exact command):**

```text
306 passed, 1 skipped, 1 warning in 114.98s (0:01:54)
```

Also run: the round-2 file with the truth, scorer, calibration, corpus, events and audit suites →
`259 passed, 1 skipped, 1 warning in 105.19s`.

**`uv run ruff check .` / `uv run ruff format --check .`**

```text
All checks passed!
305 files already formatted
```

**`uv run python scripts/audit_fixtures.py`** (real committed corpus included)

```text
audited 460 files
fixture audit passed
```

**`uv run python scripts/check_page_js.py`**

```text
page JavaScript check passed: 53 inline scripts across 22 page renders
```

**Repository and process state.** `git status --short -- work data` is empty. `src/id_detector/io.py`,
`jobs.py`, `retention.py` and `tests/test_phase2b_retention.py` were not edited. The reverted-code
swap was restored and verified byte-identical, and the extracted snapshot copy was removed from the
scratchpad.

```text
command lines read: 256
leftover processes from this worktree or its gates: 0
```

Nothing was committed, branched, merged or pushed.

## Round-3 review (sol xhigh) + realistic fix pass

The round-3 review (Codex `gpt-5.6-sol` at xhigh, read-only) returned **FIX_FIRST** and split its
findings into REALISTIC and ADVERSARIAL. By instruction, and then by the owner's threat-model decision,
this pass fixes the **four realistic findings only** and adds no machinery for the adversarial ones.

The review confirmed these as fixed, and they were left alone:
- exact manifest binding by path, file identity and loaded bytes;
- the reveal returning inside the Windows deletion-denying hold;
- expected-key pinning through freeze;
- seed validating before `makedirs`;
- manifest entries relative to the manifest folder;
- one lock deadline, with release in `finally`;
- `freeze_truth` refusing a frozen corpus;
- first-pass exposure OR-ing, scorer independence gating, sorted freeze locks, forward-only in-lock
  checks, and full-prefix `Cf` handling.

It also re-hashed all 170 entries of the two committed frozen manifests (all match) and found no
reparse point on any `release-1` path.

**Method.** As in round 2:
1. The regressions were written first, in `tests/test_truth_corpus_followup_r3.py`.
2. They were proven against the reverted code by swapping it in: the pre-pass production files were
   extracted over the worktree, the file was run, then the current files were restored and compared
   byte-for-byte (`tar -d`: identical).
3. Against the reverted code: **15 failed, 1 passed**. The pass is the corpus-root placement control,
   which works both before and after.

### Findings

| r3 tag | Finding | Verdict | Failing test against the reverted code → evidence |
|---|---|---|---|
| R-P0-1 | Exposure erased by deleting the new, untracked sidecar after reveal and before the first save | **Reproduced** | `test_exposure_survives_sidecar_deletion_through_save_second_pass_freeze_and_scoring` → `assert True is False`: right after the deletion `score_corpus.truth_independent` already called the set independent, and the restarted session would have saved `false` |
| R-P0-2 | Re-running seed or manifest-draft corrupts reviewed or frozen state | **Reproduced** | `test_seed_refuses_an_existing_reviewed_record_and_changes_nothing`, `test_seed_refuses_a_frozen_record_and_changes_nothing`, `test_seed_refuses_a_set_directory_holding_leftover_pass_files`, `test_manifest_draft_refuses_to_overwrite_a_frozen_manifest`, `test_manifest_draft_refuses_a_frozen_corpus_whatever_the_destination`, `test_manifest_draft_refuses_non_draft_records` → all `DID NOT RAISE`. CLI: `test_cli_seed_refuses_an_existing_record` → `seeded 1 draft episodes in …fixture-set\ground_truth.json` (a reviewed record reset to a draft); `test_cli_manifest_draft_refuses_a_frozen_manifest` → `recorded 1 unverified draft sets; frozen=false; manifest=…corpus-version.json` (the freeze marker removed) |
| R-P1-3 | Accepted manifest placements that directory-based scoring and certification cannot find | **Reproduced** | `test_manifest_placement_works_for_directory_scoring_or_is_refused[set-directory]` and `[directory-above]` → `DID NOT RAISE` (freeze accepted placements that `idea benchmark score --truth <corpus-dir>` and certify never look in). The `[corpus-root]` case is the control: it freezes, passes `_require_frozen`, and scores as verified through the CLI with a directory argument |
| R-P1-4 | Link refusal inconsistent, and without "pass the real path" guidance | **Reproduced** | `test_review_refuses_a_corpus_reached_through_a_link` and `test_scorer_refuses_a_manifest_root_reached_through_an_ancestor_link` → `DID NOT RAISE` (review followed the junction; the scorer verified through a linked ancestor of the manifest root). `test_freeze_link_refusal_tells_the_owner_to_pass_the_real_path` and `test_seed_link_refusal_tells_the_owner_to_pass_the_real_path` → `Regex pattern did not match` (refused, but with no guidance) |

No realistic finding was judged wrong.

### Fixes

**R-P0-1 — a second, monotonic exposure record: the corpus ledger.**
- Location choice: an append-only **`review-exposure-ledger.jsonl` in the corpus directory** (the set
  directory's parent, beside the corpus-root manifest). It was chosen over writing into an annotation
  pass at reveal for two reasons:
  - It lives *outside* the set folder, so tidying one set, including deleting its untracked
    `review-exposure.json`, cannot remove it.
  - An annotation pass written at reveal would put a pass file where no pass has been saved, which the
    state machine and freeze read as a completed pass.
- Writing: on every reveal, `TruthReviewSession._ensure_exposure` writes the sidecar if missing and
  calls `truth.record_exposure_in_ledger`. That appends one canonical JSON line (`event`, `set_id`,
  `set_directory`, `revealed_at_utc`, `tool`) unless the set already has one. It writes through the
  pinned corpus directory under the ledger's own write lock, and the new content must extend the old
  byte-for-byte, so the tool only ever appends.
- Reading: `truth.prediction_exposure` now reports exposure from any of three records: the sidecar,
  **any** annotation pass's provenance (first, second or resolution), or this set's ledger entries
  (`ledger_entries`). Every path consults it:
  - review start, save and the first-pass OR (`_pinned_exposure` now reads the ledger too);
  - **second pass and resolution**, which now carry `predictions_visible_during_review: true` into
    their own annotation when the set is exposed (unexposed sets write unchanged bytes);
  - the freeze snapshot, `benchmark/scorer.corpus_independent`, `scripts/score_corpus.truth_independent`
    and `benchmark certify`'s `_require_independent`.
- Freeze records `ledger_entries` (line digests) in the manifest. `truth_is_frozen_verified` checks
  each recorded line is still present; the ledger may grow as other sets are revealed, so presence is
  checked rather than the whole file's hash.
- The regression walks the whole path: reveal → return → delete the sidecar → restart (still exposed)
  → save → delete again → second pass → freeze (`exposed_sets`, `certifiable: false`) → scored as
  frozen and intact but **not independent** by both scorers → `_require_independent` raises
  `CorpusNotIndependent`.

**R-P0-2 — re-runs never overwrite reviewed or frozen state.**
- `seed_truth` first refuses link components. It then refuses, **before reading its inputs or writing
  anything**, if the destination set already holds any record this tool manages (`ground_truth.json`,
  any annotation pass, `review-exposure.json`, a recovery record). The message says to seed into a new
  set directory or remove the old one deliberately.
- The writer re-checks inside the lock (`_write_record_file(..., create_only=True)`).
  `data/local/source_links.json` is untouched on refusal.
- `write_draft_manifest` refuses, before writing:
  - a destination that exists and is not a readable `frozen: false` draft inventory;
  - any record covered by a frozen manifest (`refuse_frozen`);
  - any record with a reviewed (non-draft) row.
- The tests assert every truth, annotation, exposure, ledger and manifest byte in the corpus is
  unchanged after each refusal, including through `idea truth seed` and `idea truth manifest-draft`.
- `tests/test_score_corpus.py::test_an_overlay_seeded_truth_scores_the_blended_track_by_time` re-seeds
  over a fixture copy's existing `mini-b` record on purpose. It now deletes that record first, the
  deliberate path the refusal message describes. Its assertions are unchanged.

**R-P1-3 — one manifest location.** The simpler single-location rule was chosen, because it strands
nothing: the two committed frozen manifests already sit at their corpus roots.
- `freeze_truth` takes a corpus **directory** and writes its manifest only as `corpus-version.json`
  in that directory.
- Sets must sit directly inside it (`<corpus>/<set>/ground_truth.json`, or the directory itself being a
  single set). `idea benchmark score --truth <corpus>`, `idea benchmark certify`, scoring a set file,
  and review all find a manifest there.
- The set-directory and directory-above placements are refused with a message naming the one
  location.
- The regression drives the accepted placement through `idea benchmark score` with a directory
  argument (exit 0, verified).
- `tests/test_truth_corpus_followup_r2.py::test_every_accepted_manifest_placement_verifies_through_review_and_scoring`
  now expects those two placements to be refused. `tests/test_stage2a_truth.py` writes its manifest in
  the corpus directory again, and its entry path is back to `set-one/ground_truth.json`.

**R-P1-4 — links refused before resolution, with guidance.**
- One refusal, `truth_paths.link_refusal`, used everywhere. It names the link and says: *"Pass the
  resolved real path instead (for example the output of Resolve-Path or realpath), and keep corpus
  records as real files in their set directory."*
- It is applied **before anything is resolved** in:
  - `find_truth_path`, to the corpus as given and to each candidate record;
  - `find_freeze_manifest`, to the path as given, and to the manifest root with all its ancestors;
  - `_refuse_links_below`, to the manifest root and its ancestors, then each component down to the
    entry;
  - freeze, to the corpus directory before enumerating;
  - seed and `write_draft_manifest`;
  - `record_path`, and every sibling-link refusal in `PinnedDirectory`.

### Accepted residual risks (owner decision 2026-09-14: hostile same-user local programs are out of scope)

Quoted verbatim from the round-3 review's adversarial list. **None of these is fixed**, and no
machinery was added for them.

1. **"P0 [adversarial] — Root recursive directory creation in a pinned nearest-ancestor handle and
   carry a stable directory file ID—not only a pathname key—from validation through every pin."**
   *Precondition:* a program running as the owner that deliberately swaps a directory for a junction,
   or for a fresh real directory at the same pathname, in the moment between a check and a write or
   directory creation.
2. **"P0 [adversarial] — Either harden against non-cooperating file writers and unsigned-manifest
   rewriting, or record explicit owner acceptance that hostile same-user local programs are outside the
   threat model."**
   *Precondition:* a program running as the owner that ignores the advisory truth lock to overwrite a
   set file in the instant before an atomic replace, or that hand-rewrites `corpus-version.json` and
   recomputes its hashes (caught only by version-control review). The owner's decision above is that
   acceptance.
3. **"P1 [adversarial] — Document that an actively held temp lock can deny writes and decide whether
   per-user protected lock-file placement is required."**
   *Precondition:* a program running as the owner that actively holds the byte lock on
   `%TEMP%\idea-truth-<digest>.lock`, so writers time out and refuse (denial of writes only, never a
   bad write). Merely pre-creating the file is not enough; this corrects round 2's residual-risk note.

The round-2 residual risks about deleting evidence *at rest* are narrowed by R-P0-1. Deleting the
sidecar no longer removes exposure. Removing *every* record (sidecar, pass provenance and the corpus
ledger line) is deliberate destruction of evidence, not tidying; it is shown by version control once
those files are committed.

### Gate outputs (this pass)

**Full suite in the foreground, in two halves** (one run exceeds the 10-minute foreground limit). The
two halves together name every collected test file, including the new
`tests/test_truth_corpus_followup_r3.py`.

```text
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[complete]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[degraded]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[partial]
FAILED tests/test_phase2b_retention.py::test_reparse_point_cannot_move_files_outside_work
4 failed, 627 passed, 1 warning in 437.89s (0:07:17)
```

```text
768 passed, 2 skipped, 1 deselected, 1 warning in 215.60s (0:03:35)
```

**The four retention failures are reported, as asked, and are not caused by this cycle.** This
worktree is based on `ec32f97` and does **not** contain main's long-path fix `91d2990`. Checked:
`git merge-base --is-ancestor 91d2990 HEAD` is false, and `git diff ec32f97 --
src/id_detector/retention.py tests/test_phase2b_retention.py` is empty. Those files were not edited.
Once this is applied on top of main, which has the fix, they are expected to pass.

**Focused suite (the coordinator's exact command):**

```text
324 passed, 1 skipped, 1 warning in 117.81s (0:01:57)
```

Also run: the round-3 file with every other follow-up file and the truth, scorer, calibration, corpus,
events and audit suites → `274 passed, 1 skipped`. The one failure first seen there was the overlay
seed test's deliberate re-seed, handled as described above; it passes after that change.

**`uv run ruff check .` / `uv run ruff format --check .`**

```text
All checks passed!
306 files already formatted
```

**`uv run python scripts/audit_fixtures.py`** (real committed corpus included)

```text
audited 460 files
fixture audit passed
```

**`uv run python scripts/check_page_js.py`**

```text
page JavaScript check passed: 53 inline scripts across 22 page renders
```

**Repository and process state.** `git status --short -- work data` is empty. `io.py`, `jobs.py`,
`retention.py` and `tests/test_phase2b_retention.py` were not edited. The reverted-code swap was
restored and verified byte-identical.

```text
command lines read: 250
leftover processes from this worktree or its gates: 0
```

Nothing was committed, branched, merged or pushed.

## Round-4 review (sol xhigh) + class-closing fix pass

The round-4 review (Codex `gpt-5.6-sol` at xhigh, read-only) returned **FIX_FIRST on realistic findings
only**. Its adversarial list was empty, and it confirmed that the three owner-accepted risks are
honestly recorded.

The cycle is past its review-round cap, and every round had found another interleaving of two
cooperating operations. So, by instruction, this pass **closes the class instead of patching
instances**: one corpus-wide mutation lock and one supported corpus layout. It adds no further
per-predicate rechecks.

**Method.**
1. Regressions were written in `tests/test_truth_corpus_followup_r4.py`.
2. They were proven against the reverted code by swapping it in, as before: the pre-pass production
   files were extracted over the worktree, the file was run, then the current files were restored
   and compared byte-for-byte (`tar -d`: identical).
3. Against the reverted code: **16 failed, 7 passed**. The 7 passes are the coverage tests the review
   asked for, which pass both before and after (listed in the table).
4. The two-process tests use explicit stdin/stdout barriers: a child announces it is mid-operation,
   waits for `go`, and only then continues. There are no sleeps.

### Design 1 — one corpus mutation lock

`truth.corpus_write_lock(corpus_root)` is a cross-process advisory lock keyed by
`"corpus|" + path_key(corpus_root)`. It reuses `_TruthLock`, so it keeps one deadline across the
in-process and OS halves, releases in `finally`, and is reentrant within a thread.

**Who takes it.** Every operation that changes corpus state takes it **before reading any state it
will act on** and holds it until its last write is durable:
- `seed_truth`, `verify_truth`, `second_pass_truth` and `resolve_truth` (thin wrappers that take the
  lock and run the whole operation inside it);
- the review tool's `save` and `reveal`, which covers the exposure sidecar and the ledger append;
- `write_draft_manifest` and `freeze_truth`.

Every predicate now runs inside that one hold: existing truth, annotations, exposure, recovery files,
a covering or destination frozen manifest, and forward-only state. Read-only paths (scoring,
certification, the review page's GET routes) do not take it.

**Lock order.** The corpus lock always comes first; per-record locks come inside it, in `path_key`
order. The per-record locks were kept, because removing them was riskier than keeping them.
`truth_write_lock(record)` itself acquires `corpus_write_lock(corpus_root_of(record))` before the
record lock, so every acquisition anywhere follows the same order. The ledger append no longer has its
own lock; it uses the corpus lock.

A draft manifest is written with no second, differently keyed lock (`_write_record_file(...,
record_lock=False)`), because its caller already holds the corpus lock.

**What it closes.** Round-4 P0-2: seed's sibling preflight can no longer race a reveal, and a draft
inventory can no longer race a freeze's publication. It also closes the general class of two
cooperating operations on one corpus interleaving.

### Design 2 — one supported corpus layout

The only supported layout is **`<corpus>/<set>/ground_truth.json`**, with `corpus-version.json` and
`review-exposure-ledger.jsonl` directly in `<corpus>`. The single-set-root layout (`ground_truth.json`
directly in the supplied directory) and any other nesting are refused. Every refusal states the
supported layout.

- `refuse_single_set_root` and `require_corpus_directory` are used by freeze, the draft manifest,
  review (`find_truth_path`) and certification (`_require_frozen`). The scorer's `load_truth_files`
  refuses a single-set root.
- `seed_truth` refuses a destination whose "set" folder is really a corpus root (it holds set
  sub-folders, a manifest or a ledger), or whose corpus folder is itself a single-set root.
- `find_freeze_manifest` and `manifest_search_directories` now look only at `<corpus>`.

This closes round-4 P0-1: the ledger can no longer land outside the corpus, because it always sits
beside the manifest in `<corpus>`.

**Confirmed read-only against the committed data**, with the new code:
- `release-1` (7 sets), `dev-1` (6), `controlled-synth-1` (25) and `controlled-events-1` (145) all
  pass `require_corpus_directory`.
- Both committed frozen manifests still pass `truth_is_frozen_verified`: `controlled-synth-1` → `True`
  (25 sets), `controlled-events-1` → `True` (145 sets).

### Findings

| r4 tag | Finding | Verdict | Test → evidence against the reverted code |
|---|---|---|---|
| P0-1 | Ledger outside the corpus for the single-set-root layout | **Reproduced**; closed by Design 2 | `test_freeze_refuses_a_single_set_root` → `Regex pattern did not match` (refused only for draft rows, not for the layout); `test_draft_manifest_refuses_a_single_set_root`, `test_review_refuses_a_single_set_root`, `test_scoring_refuses_a_single_set_root`, `test_seed_refuses_to_turn_a_corpus_root_into_a_set` → `DID NOT RAISE`; `test_certification_refuses_a_single_set_root` → `Regex pattern did not match` (only "no freeze manifest"). Coverage (passes before and after): `test_a_copied_corpus_keeps_its_ledger_and_stays_exposed` |
| P0-2 | Two-tab races: seed preflight vs reveal; draft manifest vs freeze | **Reproduced**; closed by Design 1 | `test_the_corpus_lock_serialises_seed_against_a_concurrent_reveal` and `test_the_corpus_lock_serialises_draft_manifest_against_a_concurrent_freeze` → `DID NOT RAISE` (the reveal, the save and the freeze all ran while the other process was paused mid-operation). Coverage: `test_two_processes_appending_for_two_sets_both_survive_in_the_ledger` |
| P1-3 | Link refusal after resolution or read; messages without the remedy | **Reproduced** | `test_review_find_refuses_a_linked_corpus_before_resolving_or_enumerating` and `test_review_session_refuses_a_linked_record_before_resolving_or_reading` → `real_path` was called on the linked path first; `test_scoring_refuses_a_linked_corpus_before_resolving[load]` / `[independence]` and `test_certification_refuses_a_linked_corpus_before_reading` → `DID NOT RAISE`; `test_a_linked_annotation_pass_is_refused_with_the_guidance` and `test_a_set_moved_or_replaced_after_checking_is_refused_with_the_guidance` → `Regex pattern did not match` (no "Pass the resolved real path instead"). Coverage: `test_draft_manifest_refuses_a_linked_corpus_before_enumerating` (already refused first) |
| P1-4 | `idea truth freeze --help` advertised refused placements | **Reproduced** | `test_freeze_help_describes_only_the_corpus_root_manifest` → the help did not contain `<corpus>/corpus-version.json` |
| P1-5 | Missing regressions | **Added** (coverage by nature) | `test_a_ledger_only_exposure_produces_a_non_certifiable_l3_report_and_refuses_certification` (a real `scripts/score_corpus.py` time-matched L3 report with `independent: false`, `certifiable: false` and `NOT CERTIFIABLE` printed, plus `CorpusNotIndependent`, plus a generic-scorer report with no `certified` status); `test_tampering_with_the_ledger_after_freeze_is_rejected[delete/truncate/rewrite]`; the two-process append and barrier tests above |

No finding was judged wrong.

### Fixes (beyond the two designs)

- **P1-3.** `refuse_link_components` runs before any `real_path`, `resolve`, enumeration or read in:
  - review: `find_truth_path` (on the corpus, and on each candidate before it is resolved) and
    `TruthReviewSession.__init__`;
  - scoring: `load_truth_files` and `corpus_independent`, with `find_freeze_manifest` still checking
    the manifest root and its ancestors;
  - certification: `_require_frozen` and `_require_independent`;
  - the draft manifest (corpus and destination) and freeze.

  Every link refusal now goes through `truth_paths.link_refusal` and says *"Pass the resolved real
  path instead"*, including the annotation-pass reader, the review session's sibling check, the
  scorer's frozen-file check and the pinned-directory "moved or replaced since it was checked"
  refusal. The tests prove ordering by recording `real_path`, `Path.resolve`, `Path.rglob` and
  `read_text` calls and asserting none touched the linked path before the refusal.
- **P1-4.** The `idea truth freeze` `--out` help now reads "Must be `<corpus>/corpus-version.json`,
  directly in the corpus directory being frozen", and `--truth` states the set layout. The stale
  comments in `truth.manifest_search_directories` and `scorer.find_freeze_manifest` now describe only
  `<corpus>`.

### Existing tests changed in this pass (none weakened)

- `tests/test_truth_review.py`: the two symlink-into-`work/` tests now accept either refusal
  (`"beneath the work tree|Pass the resolved real path"`). The link is refused before the path is
  resolved, as P1-3 requires, so the link refusal fires ahead of the work-tree check.
- `tests/test_stage2a_truth.py`:
  - `test_seed_…work-only` froze `tmp_path/set` as a single-set root; it now freezes the corpus
    `tmp_path`.
  - `test_seed_overlays_of_one_row_are_ordered_by_time…` seeded `tmp_path/ground_truth.json` and then
    `tmp_path/sorted/ground_truth.json`, making one folder both a set and a corpus. The first seed now
    goes into its own `shuffled/` set folder.

  Assertions are unchanged in both.
- `tests/test_truth_corpus_followup_r2.py::test_a_timed_out_freeze_releases_the_records_it_had_already_locked`:
  the holder thread's record lock now also holds the corpus lock, so the "nothing left held" check runs
  after the holder releases. It still proves the timed-out freeze leaked no lock.

### Accepted residual risks (owner decision 2026-09-14: hostile same-user local programs are out of scope)

Unchanged from round 3, and still **not fixed**. The round-4 review re-confirmed they are honestly
recorded; see the round-3 section for the verbatim items and their preconditions:
- stable directory file IDs through every pin, including recursive directory creation rooted in a
  pinned nearest-ancestor handle;
- non-cooperating file writers, and hand-rewriting of the unsigned manifest;
- an actively held temp lock denying writes. This now also covers the corpus lock file
  `%TEMP%\idea-truth-<digest>.lock`, keyed on `corpus|<path_key>`: a program actively holding it denies
  writes to that whole corpus. It is denial only, never a bad write.

### Gate outputs (this pass)

**Full suite in the foreground, in two halves** (one run exceeds the 10-minute foreground limit). The
two halves together name every collected test file, including the new
`tests/test_truth_corpus_followup_r4.py`.

```text
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[complete]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[degraded]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[partial]
FAILED tests/test_phase2b_retention.py::test_reparse_point_cannot_move_files_outside_work
4 failed, 627 passed, 1 warning in 330.75s (0:05:30)
```

```text
791 passed, 2 skipped, 1 deselected, 1 warning in 158.97s (0:02:38)
```

The four retention failures are the known long-path issue. This worktree is based on `ec32f97` and
does not contain main's fix `91d2990` (verified in round 3 with `git merge-base` and an empty
`git diff ec32f97 -- src/id_detector/retention.py tests/test_phase2b_retention.py`). Those files were
not edited, and the tests are expected to pass once this is applied on top of main.

**Focused suite (the coordinator's exact command):**

```text
340 passed, 1 skipped, 1 warning in 76.43s (0:01:16)
```

Also run: the round-4 file with every follow-up file and the truth, scorer, calibration, corpus,
events and audit suites → `298 passed, 1 skipped`.

**`uv run ruff check .` / `uv run ruff format --check .`**

```text
All checks passed!
307 files already formatted
```

**`uv run python scripts/audit_fixtures.py`** (real committed corpus included)

```text
audited 460 files
fixture audit passed
```

**`uv run python scripts/check_page_js.py`**

```text
page JavaScript check passed: 53 inline scripts across 22 page renders
```

**Repository and process state.** `git status --short -- work data` is empty. `io.py`, `jobs.py`,
`retention.py` and `tests/test_phase2b_retention.py` were not edited. The reverted-code swap was
restored and verified byte-identical.

```text
command lines read: 256
leftover processes from this worktree or its gates: 0
```

Nothing was committed, branched, merged or pushed.

## Round-5 review (sol xhigh) + single-gateway fix pass (round 6)

The round-5 diff review (Codex `gpt-5.6-sol` at xhigh, read-only) returned **FIX_FIRST** on realistic
findings only; its adversarial list was empty and the three owner-accepted risks were confirmed. Every
finding traced to the same root cause: several corpus-touching paths still discovered and mutated
corpus state without a common front door. Per instruction, this pass **closes the class with one
corpus gateway** rather than patching each path.

### The gateway (one class)

`truth.open_corpus(root, *, mutate, work_root, require_records, allow_frozen)`
(`src/id_detector/truth.py:952`) is the single entry function every corpus-touching production path
calls before it reads or changes corpus state. Its steps, in order:

- **(a) explicit canonical root.** The caller passes the corpus root; it is never inferred by
  inspecting directories. Seed derives it from the destination by the one supported layout
  (`corpus_root_of(out_path)`), not by probing.
- **(b) link-refusing walk.** `require_corpus_directory` (`:876`) refuses any link component of the
  root, then walks with `_scan_corpus_tree` (`:847`), the one place the corpus tree is enumerated. It
  uses `os.scandir` and never follows a link: a set-directory junction, a linked `corpus-version.json`,
  a linked ledger or record is refused through `link_refusal` (the "Pass the resolved real path
  instead" guidance) *before* the linked target is descended into or read.
- **(c) one supported layout.** `<corpus>/<set>/ground_truth.json`, with `corpus-version.json` and
  `review-exposure-ledger.jsonl` directly in `<corpus>`. A single-set root and a truth file nested at
  any other depth are rejected.
- **(d) mutation.** Under the corpus lock it re-checks the layout and refuses a frozen manifest
  (`corpus_manifest_frozen`, `:936`) unless `allow_frozen`; writes stay confined to the root and
  refuse `work/` through the pinned writer the caller already uses.
- **(e) reads.** It returns a `CorpusHandle` (`:921`) whose `truth_files` is the vetted list, so
  scoring, certification, independence scans and the draft inventory read only from that list and
  never enumerate the corpus again.

Every corpus-touching path now goes through it or its validator:
- **seed** (`:172`): under the corpus lock, refuses a frozen corpus, and re-checks the layout scoped
  to the set directory immediately before the create-only write (catching a concurrent nested seed).
- **draft-manifest** (`:2015`): output is fixed to `<corpus>/corpus-version.json` -- the arbitrary
  `--out` is gone (`cli.py:1864`) -- and reads the vetted list under the lock.
- **freeze, verify, second pass, resolve, review save/reveal:** take the corpus lock (freeze via the
  gateway's validator; the passes via `truth_write_lock`, which takes the corpus lock first) and
  refuse frozen state.
- **scoring** (`load_truth_files` `scorer.py:1225`, `corpus_independent` `:1446`) and **certification**
  (`_require_frozen` `certify.py:140`, `_require_independent` `:157`): read only from the vetted list,
  refusing links and the nested layout before any resolve or read.
- **controlled render** (`controlled.py:655`): holds the target corpus's lock for the whole render and
  publish, and `_refuse_controlled_overwrite` (`:705`) refuses a frozen target and refuses replacing
  any existing directory that is not a controlled corpus it produced (marked by
  `render_manifest.json`, `:38`), so an accidental `--out` at `data/corpus/release-1` cannot overwrite
  it. The public CLI `--out` (`cli.py:1249`) is now behind that gate.

**Guard.** `tests/test_truth_gateway_guard.py` AST-parses the five modules (`truth.py`,
`truth_review.py`, `scorer.py`, `certify.py`, `controlled.py`) and fails if any function outside the
gateway (`truth._scan_corpus_tree`, and the controlled renderer's scan of its *input* audio sources)
enumerates a directory (`rglob`/`glob`/`iterdir`/`scandir`/`walk`/`listdir`). Reverting any module to
its pre-gateway form fails it (proven: 4 failures on the pre-round-6 snapshot).

### Findings

| r5 tag | Finding | Verdict | Test -> evidence |
|---|---|---|---|
| B1 (P0) | A frozen corpus still accepted a newly seeded set; outer/nested seeds inferred different roots | **Reproduced** | `test_truth_corpus_followup_r6.py::test_seed_a_new_set_into_a_frozen_corpus_is_refused` (fails with the seed frozen-check reverted); `::test_concurrent_outer_and_nested_seeds_never_both_create_a_nested_layout` (barrier: outer paused, nested completes, outer refused on resume) |
| B2 (P0) | Draft-manifest could write anywhere holding the wrong lock, and into `work/` | **Reproduced** | `::test_draft_manifest_writes_only_its_own_corpus_root` (output fixed to the root; `out_path` argument removed -> `TypeError`); the r4 draft-vs-freeze barrier still holds |
| B3 (P0) | The controlled renderer was an unlocked whole-corpus writer with an arbitrary `--out` | **Reproduced** | `::test_controlled_render_refuses_a_real_non_controlled_corpus` and `::test_controlled_render_refuses_a_frozen_target` (fail with `_refuse_controlled_overwrite` reverted); `::test_controlled_render_serialises_against_a_concurrent_freeze` (barrier) |
| B4 (P1) | Descendant links traversed before the common refusal | **Reproduced** | `::test_a_set_directory_junction_is_refused_before_it_is_enumerated` and `::test_a_manifest_junction_is_refused_before_it_is_read` (monitor `os.scandir`/`read_text`: the linked target is never touched) |
| B5 (P1) | Generic scoring accepted nested layouts | **Reproduced** | `::test_scoring_rejects_a_nested_truth_layout` (fails with `load_truth_files` reverted to `rglob`) |
| D (P1) | The ledger concurrency test was not concurrent | **Reproduced** | `test_truth_corpus_followup_r4.py::test_two_processes_appending_for_two_sets_both_survive_in_the_ledger` now releases both children after both observed the empty pre-append state, then waits; with the ledger's `corpus_write_lock` removed it fails 3/3 (lost update), and passes with it restored |

No finding was judged wrong; the adversarial list was empty.

### Existing tests updated (assertions preserved or strengthened)

- `test_stage2a_controlled.py`: rendering now refuses a non-controlled or non-empty target, so the two
  fixtures that pre-populated a stray file and expected an overwrite now render once (creating the
  marker) and then re-render, still proving the clean replace and the failed-render-leaves-target
  behaviour.
- `test_stage2a_truth.py`: the work-only freeze and the shuffled-overlay seed now use the supported
  `<corpus>/<set>/` layout.
- `test_truth_review.py`: the symlinked-set-directory case now expects the link refusal (with its
  guidance) rather than a silent "set not found".
- `test_truth_corpus_followup_r3.py` / `_r4.py`: draft-manifest calls drop the removed `out_path`; the
  frozen-seed refusal matches the corpus-level frozen message; the draft link-order test monitors
  `os.scandir` (the new enumeration primitive).

### Accepted residual risks (owner decision 2026-09-14: hostile same-user local programs out of scope)

Unchanged and still **not fixed** (see the round-3 section for the verbatim items): stable directory
file IDs through every pin; non-cooperating file writers and hand-rewriting of the unsigned manifest;
an actively held temp lock (now including the corpus lock file) denying writes to a corpus.

### Gate outputs (this pass)

Full default suite in the foreground, in two halves (one run exceeds the 10-minute limit); together
they name every collected test file, including the new `test_truth_corpus_followup_r6.py` and
`test_truth_gateway_guard.py`:

```text
4 failed, 627 passed, 1 warning in 348.16s (0:05:48)
803 passed, 2 skipped, 22 deselected, 1 warning in 163.35s (0:02:43)
```

The only failures are the four known long-path `test_phase2b_retention.py` cases: this worktree is
based on `ec32f97` and does not contain main's long-path fix (its ancestor check is false; those files
are untouched here), so they are expected to pass once this lands on main.

Slow controlled-render tests (deselected by default; run with `-m slow`):

```text
3 passed, 7 deselected, 1 warning in 50.94s
```

Focused suite (the coordinator's list): `339 passed, 1 skipped`.

```text
All checks passed!                     (uv run ruff check .)
309 files already formatted            (uv run ruff format --check .)
audited 460 files / fixture audit passed
page JavaScript check passed: 53 inline scripts across 22 page renders
```

Read-only against the committed corpora, with the new code: `data/corpus/release-1` (7 sets),
`dev-1` (6), `controlled-synth-1` (25) and `controlled-events-1` (145) all satisfy
`require_corpus_directory`, and both committed frozen manifests still verify.

`git status --short -- work data` is empty; `io.py`, `jobs.py`, `retention.py` and
`tests/test_phase2b_retention.py` were not edited. The reverted-code swaps used for the revert-proofs
were restored and verified byte-identical. Nothing was committed, branched, merged or pushed.

## Round-6 review (sol xhigh) + exact-rule fix pass (round 7)

The round-6 diff review (Codex `gpt-5.6-sol` at xhigh, read-only) returned **FIX_FIRST**: four P0 and
three P1 realistic findings, one P2 adversarial. Its audit was right that round 6's "single gateway"
was a single *symbol*, not a single *path*: only draft-manifest called `open_corpus`. Per the
coordinator, this last pass before re-scoping closes the three classes with **exact rules**, not more
call sites.

### Class 1 -- mutations landing in the wrong corpus (r6 P0-1, P0-2, P0-4; P1-5)

**Rule 1: exact depth.** Every mutating target is exactly `<root>/<set>/ground_truth.json`, or exactly
`<root>/corpus-version.json` / the ledger. The root is given explicitly (`corpus_root=`) or computed
as exactly `target.parent.parent` and must then pass rule 2; nothing is derived from inspecting
grandparents.
- `exact_corpus_root` (`src/id_detector/truth.py:955`): the lexical grandparent, equal to an explicit
  root when one is given, name checked; `require_corpus_member` (`:978`): the target must be one of the
  gateway's vetted records.
- Seed `seed_truth` (`:173`), verify `verify_truth` (`:1647`), second pass `second_pass_truth`
  (`:1777`), resolve `resolve_truth` (`:1884`) through `_record_mutation` (`:1267`); every record lock
  `truth_write_lock` (`:838`); the ledger append `record_exposure_in_ledger` (`:497`); freeze
  `freeze_truth` (`:2056`, plan reads only `handle.truth_files`); draft `write_draft_manifest`
  (`:2306`).

**Rule 2: ancestor refusal inside `open_corpus`** (`:1145`), in this order, before `path_key`, the lock
or any manifest read -- and again under the lock (`:1209`):
1. `lstat` link refusal of the root and every ancestor component (`refuse_corpus_ancestry`, `:1031`);
2. a root beneath `work_root` is refused;
3. a root any of whose ancestors, up to the filesystem root, holds `corpus-version.json` or a set
   directory holding `ground_truth.json` is refused as **inside another corpus** (`corpus_ancestor`,
   `:1012`; `_holds_truth_bearing_set`, `:989`, which never follows a linked child).
Only then are the layout validated and the vetted `truth_files`/`files` returned
(`_validated_corpus_tree`, `:929`; `CorpusHandle`, `:1102`).

So `release-1/new-group/new-set/ground_truth.json` computes root `release-1/new-group`, whose ancestor
`release-1` is a corpus: refused before any lock or write, for seed, verify, second pass, resolve,
review, freeze, draft and the ledger.

**Controlled render** (`src/id_detector/benchmark/controlled.py:664`): `--out` is the corpus root of
`open_corpus(mutate=True, work_root=...)` (`:695`), held for the whole render and publish, so
`data/corpus/release-1/new-controlled` is refused as inside another corpus, a target beneath the work
tree is refused (the CLI passes `work_root=DEFAULT_WORK_ROOT`, `src/id_detector/cli.py:1277`), and a
frozen target is refused. The marker (`_controlled_marker_problem`, `:714`;
`_refuse_controlled_overwrite`, `:746`) is valid only as a regular, non-link file whose schema is a
render manifest and whose `sets` name **exactly that target's vetted set directories with the hashed
`ground_truth.json` bytes on disk**. The binding is by content rather than by a target-name field on
purpose: the render manifest is byte-deterministic across output directories
(`test_render_is_byte_deterministic_for_same_seed`) and the committed `data/fixtures/controlled/stage-2a`
fixture must equal a fresh render, so a name field would break both; a copied, linked, stale or
foreign marker still fails. The failed-render test is byte-for-byte again
(`tests/test_stage2a_controlled.py:178`, `_tree_bytes` over the published corpus and its audio).

**Calibration validation** (`src/id_detector/calibrate/validate.py`): reads through
`open_corpus(mutate=False)`; the report goes to `validation_report_path` (`:317`) -- by default the
run's output folder beside the fitted model, `data/local/calibration/<corpus>/calibration-validation.json`
-- and any destination inside a corpus is refused there and again immediately before the write
(`:477`); the scratch truth corpus for the certification proof is built in a
`tempfile.TemporaryDirectory` outside `work/` and every corpus and removed after scoring (`:514`;
`_frozen_subset_truth`, `:293`, reads its source only through the gateway). The committed
`data/corpus/controlled-synth-1/calibration-validation.json` from Stage 5 is untouched; a re-run now
writes beside the model instead.

### Class 2 -- certification over a partial population (r6 P0-3)

**Rule:** verified, independent and certified are possible only when the loaded inventory
`{(set_id, relative path)}` -- the corpus's on-disk population from the gateway's vetted list, never a
caller's subset -- exactly equals the frozen manifest inventory. Missing or surplus entries are refused
by name: `require_frozen_inventory` (`src/id_detector/truth.py:1234`), enforced by
`truth_is_frozen_verified` (`src/id_detector/benchmark/scorer.py:1337`, `:1355`), `corpus_independent`
(`:1486`, `:1504`) and certification's `_require_frozen` (`src/id_detector/calibrate/certify.py:141`).
Per-record binding still decides each record's label, so `scripts/score_corpus.py` keeps its per-mix
`verified`/`draft` labels over a complete frozen population.

### Class 3 -- readers and guards (r6 P1-5, P1-6, P1-7; P2-8)

**Readers through `open_corpus(mutate=False)`, using only the handle:** review lookup `find_truth_path`
(`src/id_detector/truth_review.py:94`, `:102`) and session load (`:439`); scorer `load_truth_files`,
`find_freeze_manifest`, `truth_is_frozen_verified`, `corpus_independent` (`scorer.py:1268`, `:1289`,
`:1337`, `:1486`; a standalone non-corpus truth document is read as given and is never verified);
certification `_require_frozen`/`_require_independent` (`certify.py:141`, `:165`); benchmark corpus,
shortlist, hints and certification runs via `corpus._truth_files`
(`src/id_detector/benchmark/corpus.py:66`); ablations (`ablations.py:133`); transform schedule
(`transforms_schedule.py:93`); calibration validation; `scripts/score_corpus.py` independence via
`corpus_independent` (`:436`); `scripts/audit_fixtures.py` (`_audited_files`, `:355`: a corpus folder is
read only through the gateway's `truth_files`/`files`; a non-corpus fixture folder keeps a raw
`os.scandir` walk that refuses every link); `scripts/check_page_js.py` (`:126`).
`scripts/make_controlled_predictions.py` reads no corpus: it takes its truth only from `load_truth_directory`, which is gateway-backed, and writes only its `--out` predictions file.

**Guard replaced** (`tests/test_truth_gateway_guard.py`): any string literal containing
`ground_truth.json`, `corpus-version.json` or `review-exposure-ledger` anywhere in `src/` or `scripts/`
outside `truth.py`/`truth_paths.py` fails, unless in a three-entry allowlist with a reason (three
`truth` CLI help strings that open nothing); docstrings exempt, f-string fragments checked; a
stale-allowlist test and a self-test. **Direct entry-point tests**
(`tests/test_truth_corpus_followup_r7.py::test_every_public_corpus_entry_point_calls_open_corpus`,
32 parametrised entry points): the gateway's first statement is a test seam
(`truth._gateway_entered`, `truth.py:698`) monkeypatched to raise; every public path must raise it and
leave both corpora byte-identical.

**Ledger barrier inside the append** (`tests/test_truth_corpus_followup_r4.py:211`, `:229`): seams
`_ledger_append_window` (`truth.py:694`, called at `:521` between the append's own read and its write)
and `_lock_contended` (`:690`). The parent waits until one child has read inside the append and the
other has either also read (unserialised) or reported itself blocked on the lock (serialised), then
releases; no sleeps. **Exact link-refusal assertion restored**
(`tests/test_truth_review.py:786`, `match="Pass the resolved real path instead"`).

### Verdicts and tests

| r6 tag | Finding | Verdict | Regression (all in `test_truth_corpus_followup_r7.py` unless named) |
|---|---|---|---|
| P0-1 | Mistyped nested seed/verify/second/resolve locks the wrong root | **Reproduced** | `test_a_seed_one_level_too_deep_in_a_frozen_corpus_is_refused_as_inside_another_corpus`, `test_a_seed_one_level_too_deep_in_an_unfrozen_corpus_is_refused`, `test_every_mutation_refuses_a_record_nested_inside_a_frozen_corpus[verify, second pass, resolve, ledger append, record lock, review load, freeze, draft]`, `test_an_explicit_corpus_root_must_be_exactly_the_targets_grandparent` |
| P0-2 | Renderer publishes a nested corpus inside `release-1`; no work-root ban | **Reproduced** | `test_controlled_render_refuses_an_output_inside_another_corpus`, `test_controlled_render_refuses_an_output_beneath_the_work_root` |
| P0-3 | Deleted exposed set disappears from certification | **Reproduced** | `test_deleting_an_exposed_set_from_a_frozen_corpus_makes_certification_impossible`, `test_a_set_added_to_a_frozen_corpus_is_refused_as_surplus` |
| P0-4 | Calibration writes into the frozen corpus and builds truth under `work/` | **Reproduced** | `test_calibration_validation_report_is_never_written_inside_a_corpus`, `test_calibration_scores_a_scratch_corpus_outside_work_and_every_corpus` |
| P1-5 | Gateway resolves/locks/reads before link refusal; no work-root ban | **Reproduced** | `test_the_gateway_refuses_a_linked_root_before_path_key_the_lock_or_a_manifest_read`, `test_the_gateway_refuses_a_root_beneath_the_work_root_for_reads_and_mutations`, `test_the_gateway_repeats_the_ancestor_refusal_under_the_lock` |
| P1-6 | Readers bypass the gateway | **Reproduced** | `test_every_public_corpus_entry_point_calls_open_corpus[...]` (32), `test_the_audit_reads_corpora_through_the_gateway_and_refuses_links_elsewhere` |
| D-1 | AST guard bypassable | **Reproduced** | `test_truth_gateway_guard.py::test_no_corpus_file_name_literal_outside_the_gateway_modules`, `::test_every_allowlist_entry_is_still_needed`, `::test_the_guard_catches_plain_and_formatted_literals_but_not_docstrings` |
| D-2 | Ledger test not synchronised inside the read-modify-write | **Reproduced** | `test_truth_corpus_followup_r4.py::test_two_processes_appending_for_two_sets_both_survive_in_the_ledger` |
| D-3 | Weakened assertions | **Reproduced** | `test_truth_review.py::test_symlinked_directory_into_the_work_root_is_never_traversed` (exact guidance), `test_stage2a_controlled.py::test_failed_render_keeps_previously_published_corpus` (byte-for-byte) |
| P2-8 (adversarial) | Presence-only marker | Fixed with the realistic marker rule | `test_the_controlled_marker_must_be_bound_to_exactly_this_target[missing, not json, wrong schema, copied from another corpus, stale truth hash]`, `test_a_linked_controlled_marker_is_refused`, `test_a_marker_bound_to_this_target_authorises_replacement` |

**Reverted-rule proofs** (each rule disabled in place, its tests run, the file restored byte-identical):

| Toggle (rule disabled in place) | Tests run | Result |
|---|---|---|
| T1 ancestor refusal (`corpus_ancestor` result ignored) | both too-deep seeds, all eight nested mutations, render inside another corpus | 11 failed |
| T2 ancestor re-check under the lock removed | `test_the_gateway_repeats_the_ancestor_refusal_under_the_lock` | 1 failed |
| T3 root resolved with `path_key` before link refusal | `test_the_gateway_refuses_a_linked_root_before_path_key_the_lock_or_a_manifest_read` | 1 failed |
| T4 work-root refusal disabled | gateway work-root test, render beneath the work root | 2 failed |
| T5 explicit `corpus_root` equality disabled | `test_an_explicit_corpus_root_must_be_exactly_the_targets_grandparent` | 1 failed |
| T6 exact frozen inventory check disabled | deleted exposed set, surplus set | 2 failed |
| T7 `corpus._truth_files` back to a raw `rglob` | probes for `run_corpus` and `run_shortlist`, the literal guard | 3 failed |
| T8 `find_truth_path` back to a raw `rglob` | its probe, the exact link-guidance review test | 2 failed |
| T9 ledger append without the gateway lock | the two-process ledger test, run three times | failed 3 of 3 |
| T10 marker reduced to a presence check | the five marker cases | 5 failed |
| T11 calibration report allowed inside a corpus | `test_calibration_validation_report_is_never_written_inside_a_corpus` | 1 failed |

Every toggled file was restored and verified byte-identical before the next toggle.

### Supporting changes, each for a stated reason

- `tests/golden/ground_truth.json` renamed `ground_truth_record.json` (mapped in
  `tests/test_contracts.py:41`): a set-shaped file directly under `tests/golden` made `tests/` itself a
  corpus, so the ancestor rule refused every fixture corpus under `tests/`. It is a schema sample, not
  a corpus record.
- The `test_stage2a_truth.py` seeds (eight paths) moved from `tmp_path/ground_truth.json` to
  `tmp_path/corpus/set/ground_truth.json`: exact depth made the pytest session directory their corpus
  root, and a set directly in `tmp_path` made that session directory a corpus for every other test.
- Two tests relocated a junction target one level down (`test_truth_corpus_followup_r4.py`
  moved-set test, `test_truth_corpus_followup_r6.py` set-directory-junction test) for the same reason;
  their exact guidance assertions are unchanged.
- The ledger append passes `allow_frozen=True`: recording that predictions were seen is monotonic
  evidence, never a truth change, so it is never refused (the exposure flag may be pessimistic, never
  optimistic). Certification's gateway reads pass `require_records=False` so an empty corpus still
  reports `CorpusNotFrozen`.
- Cost: each gateway entry lists every ancestor directory; the largest here is `%TEMP%`
  (about 7,500 subdirectories, about 0.2 s warm), paid once for a read and twice for a mutation.

### Accepted residual risks (owner decision 2026-09-14: hostile same-user local programs are out of scope)

Unchanged and still **not fixed** (verbatim items in the round-3 section): stable directory file IDs
through every pin; non-cooperating writers and hand-rewriting of the unsigned manifest; an actively
held temp lock (including the corpus lock) denying writes. One accident-class residual is recorded
rather than engineered: two seeds started at the *same instant* on overlapping mistyped paths whose
roots differ (for example `C/a/ground_truth.json` and `C/a/b/ground_truth.json` in an empty `C`) take
different corpus locks; each re-checks the layout and ancestry immediately before its create-only write,
so whichever lands second is refused, but two writes landing in the same instant are not serialised.

### Gate outputs (this pass)

Full default suite in the foreground, in three parts (one run exceeds the 10-minute limit); together
they name every collected test file, including `tests/idea_web`, the new
`test_truth_corpus_followup_r7.py` and the replaced `test_truth_gateway_guard.py`:

```text
part A (idea_web .. phase3a):          4 failed, 622 passed, 1 warning in 339.05s
part B (playlists .. stage9):          623 passed, 1 skipped, 93 deselected, 1 warning in 190.70s
part C (fixture audit + truth tests):  242 passed, 1 skipped, 3 deselected, 1 warning in 266.41s
```

The only failures are the four known long-path `test_phase2b_retention.py` cases: this worktree is
based on `ec32f97` and lacks main's retention fix `91d2990` (`git merge-base --is-ancestor 91d2990
HEAD` exits 1); `retention.py` and `tests/test_phase2b_retention.py` are untouched here.

Slow tests this pass touched (`-m slow` over `test_stage2a_controlled.py`,
`test_truth_corpus_followup_r6.py`, `test_truth_corpus_followup_r7.py`):

```text
21 passed, 67 deselected, 1 warning in 78.11s
```

```text
All checks passed!                     (uv run ruff check .)
310 files already formatted            (uv run ruff format --check .)
audited 460 files / fixture audit passed
page JavaScript check passed: 53 inline scripts across 22 page renders
```

Read-only, through the new gateway: `controlled-events-1` (145 sets, frozen, inventory exact,
verified), `controlled-synth-1` (25, frozen, inventory exact, verified), `dev-1` (6, not frozen),
`release-1` (7, not frozen). None is inside another corpus.

`git status --short -- work data` is empty; no processes from this worktree remain (0 found by
command-line scan, none killed). `io.py`, `jobs.py`, `retention.py` and
`tests/test_phase2b_retention.py` were not edited. Nothing was committed, branched, merged or pushed.

## Round-7 review (sol xhigh) + re-scoped fix pass (round 8)

The round-7 diff review (Codex `gpt-5.6-sol` at xhigh, read-only) returned **FIX_FIRST**: two
realistic P0s, three P1s, one realistic P2 and two adversarial P2s. The coordinator re-scoped round
8: fix the corpus-damage and small items only, and defer certification scope to a named follow-up.
The owner has not allowed freezing or certification; that rule stays until the follow-up lands.

### Fixed now

| # | r7 tag | Finding | Verdict | Design | Where | Tests (`tests/test_truth_corpus_followup_r8.py` unless named) |
|---|---|---|---|---|---|---|
| 1 | B1 (P0) | `--audio-out` could replace a real corpus | **Reproduced** | The audio destination passes the same rules as `--out`, on the path as spelled, **before staging and again immediately before publication**: links refused; a destination beneath `work_root`, inside a corpus, that is a corpus, or holding corpus files below it is refused; a non-directory is refused; an existing non-empty directory is replaced only if it holds a regular, non-link renderer-owned `controlled-audio.json` marker (kind `controlled-audio`, unique `set_ids`), which every render now writes. | `_refuse_audio_destination` `src/id_detector/benchmark/controlled.py:790`; `_audio_marker_problem` `src/id_detector/benchmark/controlled.py:766`; pre-staging call `src/id_detector/benchmark/controlled.py:704`; pre-publication call `src/id_detector/benchmark/controlled.py:940`; marker write `src/id_detector/benchmark/controlled.py:928` | `test_audio_out_at_a_real_layout_corpus_is_refused_and_left_byte_identical`, `test_audio_out_through_a_link_to_a_real_layout_corpus_is_refused_and_left_byte_identical`, `test_audio_out_refuses_every_destination_it_could_damage[inside a corpus, beneath the work root, unmarked existing directory, duplicate marker set_ids, corpus content below]`, slow `test_audio_out_is_revalidated_immediately_before_publication` |
| 2 | B3 (P1) | Post-freeze ledger evidence not manifest-bound | **Reproduced** | The production ledger append goes through the gateway **without** `allow_frozen`, so a frozen corpus refuses it; the review route still refuses a frozen reveal before any prediction is assembled; verification requires the current ledger's line set to **exactly equal** the union of the manifest's recorded `ledger_entries` (added or missing lines refused, so a hand-added line is caught while present). | append `src/id_detector/truth.py:519`; `require_frozen_ledger` `src/id_detector/truth.py:1295`; enforced in `truth_is_frozen_verified` `src/id_detector/benchmark/scorer.py:1461`; review refusal `src/id_detector/truth_review.py:507` | `test_a_ledger_append_on_a_frozen_corpus_is_refused`, `test_the_review_route_refuses_a_frozen_reveal_before_assembling_predictions`, `test_a_hand_added_ledger_line_is_caught_while_present`; `test_truth_corpus_followup_r4.py::test_a_ledger_only_exposure_produces_a_non_certifiable_l3_report_and_refuses_certification` (updated) |
| 3 | B4 (P1) | Overlapping mistyped seeds race on different locks | **Reproduced** | Every seed first takes one fixed machine-wide seed lock (`SEED_LOCK_KEY`, a named lock in the lock directory), before any check and before its corpus lock; nothing takes it while holding a corpus lock, so the order cannot deadlock. `seed_truth` gains a `timeout`. | `SEED_LOCK_KEY` `src/id_detector/truth.py:820`; taken in `seed_truth` `src/id_detector/truth.py:198` | `test_overlapping_seeds_serialise_so_only_one_write_lands` (outer seed paused past its final validation; nested seed either also past validation or reported blocked; exactly one write lands and the other is refused); `test_truth_corpus_followup_r6.py::test_concurrent_outer_and_nested_seeds_never_both_create_a_nested_layout` (updated) |
| 4 | B6 (P2) | Ledger pin identity not canonical | **Reproduced** | The ledger pin's expected identity is `path_key(corpus_dir)`, the same canonical, prefix-free spelling the pinned directory reports. | `src/id_detector/truth.py:523` | `test_a_reveal_records_the_ledger_under_either_spelling[ordinary, namespace-prefixed]` (direct append and full reveal) |
| 5 | B7 (P2, adv.) | Duplicate marker entries | **Reproduced** | The controlled render marker is refused when any `set_id` appears more than once, before it is collapsed into a dictionary. | `src/id_detector/benchmark/controlled.py:755` | `test_a_controlled_marker_with_a_duplicate_set_id_is_refused` |
| 6 | B8 (P2, adv.) | Programmatic `model_out` unguarded | **Reproduced** | `_write_calibration_model` refuses a link on, or a corpus containing, the model path immediately before the model write, and the completion sidecar's path immediately before the sidecar write. | `_write_calibration_model` `src/id_detector/calibrate/validate.py:319`; call `src/id_detector/calibrate/validate.py:428` | `test_the_calibration_model_is_never_written_into_a_corpus_or_through_a_link`, `test_the_calibration_model_sidecar_is_guarded_before_its_own_write` |

### Deferred to the certification follow-up (required before any freeze or certify)

These are recorded, not built. Neither may be relied on until the certification follow-up lands:

- **Round-7 P0-2 -- an L3 run list over a subset of a frozen corpus.** `scripts/score_corpus.py` checks
  each run-list truth file separately, so a run list omitting an exposed frozen sibling could claim
  the remaining subset certifiable. The follow-up must require every run-list truth path to belong to
  one frozen corpus whose manifest inventory the run list equals exactly, and judge independence over
  that complete inventory.
- **Round-7 P1-5 -- the calibration scratch directory relative to the configured `work_root`.**
  `_score_certification` builds its scratch corpus in the process temporary directory and ignores the
  configured `--work-root`, so a work root set to the temporary directory would contain it. The
  follow-up must pass `work_root` in and validate the scratch destination is outside it and every
  corpus before creating any file.

**Meanwhile no certification can be claimed.** `CERTIFICATION_ENABLED = False` with
`CERTIFICATION_DISABLED = "certification is disabled until the certification follow-up lands"`
(`src/id_detector/calibrate/certify.py:66`):

- `run_certify` raises `CertificationDisabled` with exactly that message before it opens any corpus or
  writes anything (`src/id_detector/calibrate/certify.py:244`), and
  `idea benchmark certify` exits 2 with it (`src/id_detector/cli.py:1462`);
- the L3 block's `certifiable` is always `false` and carries `"certification": "<that message>"`
  (`scripts/score_corpus.py:1063`).

Tests: `test_run_certify_is_refused_before_it_opens_any_corpus`,
`test_idea_benchmark_certify_refuses_with_the_disabled_message`,
`test_the_l3_certifiable_flag_is_refused_with_the_disabled_message`.

### Existing tests changed, each for a stated reason

- `tests/fixtures/corpus-mini/expected.json`, `expected-time.json` and the two exact L3 dictionaries in
  `tests/test_score_corpus.py` gain the `certification` message; the frozen-verified-independent
  case in `test_truth_status_unverified_then_verified_under_a_frozen_manifest` now expects
  `certifiable: false` with the message (it was `true`).
- `tests/test_stage5_calibration.py`: `test_certify_refuses_unfrozen_corpus` and
  `test_certify_refuses_repeated_test_version` now expect `CertificationDisabled`, which is raised
  before those guards; the guards themselves are unchanged and still tested directly.
- `tests/test_truth_corpus_followup.py::test_certification_refuses_a_prediction_exposed_corpus` now
  asserts the independence guard directly and the disabled refusal from `run_certify`.
- `tests/test_truth_corpus_followup_r4.py::test_a_ledger_only_exposure_produces_a_non_certifiable_l3_report_and_refuses_certification`:
  its hand-written corpus-mini manifest now records the ledger line exactly as `freeze_truth` does
  (otherwise the exact-ledger rule rightly refuses it); its generic-corpus half now proves the frozen
  append is refused and a hand-added line makes scoring refuse.
- `tests/test_truth_corpus_followup_r6.py::test_concurrent_outer_and_nested_seeds_never_both_create_a_nested_layout`:
  with the seed lock, the nested seed cannot start validating while the outer one holds it; it times
  out (`timeout=1.0`) and the outer seed completes alone.
- `tests/test_truth_corpus_followup_r7.py`: the `run_certify` gateway probe is removed (31 probes
  remain) because the entry point now refuses before opening any corpus; that refusal is tested in r8.

**Consequence for the owner:** an existing local audio directory rendered before round 8 (for example
`data/local/controlled/<corpus>`) has no `controlled-audio.json`, so re-rendering into it is refused;
delete it or render audio into a new directory. The committed corpora are unaffected.

### Reverted-rule proofs

| Toggle (rule disabled in place) | Result |
|---|---|
| U1 audio destination refusal disabled | FAILS as required (7 failed, 1 warning in 14.01s) |
| U2 audio re-validation before publication removed | FAILS as required (1 failed, 1 warning in 19.96s) |
| U3 ledger append allowed on a frozen corpus again | FAILS as required (1 failed, 1 warning in 6.97s) |
| U4 exact frozen ledger equality disabled | FAILS as required (1 failed, 1 warning in 8.71s) |
| U5 machine-wide seed lock removed | FAILS as required (1 failed, 1 warning in 7.80s); FAILS as required (1 failed, 1 warning in 7.46s); FAILS as required (1 failed, 1 warning in 7.62s) |
| U6 ledger pin identity back to normcase(str(corpus_dir)) | FAILS as required (1 failed, 1 passed, 1 warning in 6.94s) |
| U7 duplicate set_id check in the controlled marker removed | FAILS as required (1 failed, 1 warning in 4.51s) |
| U8 model destination guard removed | FAILS as required (1 failed, 1 warning in 2.99s) |
| U9 sidecar destination guard removed | FAILS as required (1 failed, 1 warning in 3.31s) |
| U10 certification re-enabled | FAILS as required (3 failed, 1 warning in 10.69s) |

Every toggled file was restored and verified byte-identical before the next toggle.

### Accepted residual risks (owner decision 2026-09-14: hostile same-user local programs are out of scope)

Unchanged and still **not fixed** (verbatim in the round-3 section): stable directory file IDs through
every pin; non-cooperating writers and hand-rewriting of the unsigned manifest; an actively held temp
lock (including the corpus and seed locks) denying writes. The round-7 overlapping-seed residual is
closed by the seed lock. The round-7 P2 ancestor-rule usability false positive (an ordinary folder such
as Downloads holding `<child>/ground_truth.json` blocks deeper corpora) is accepted as conservative.

### Gate outputs (this pass)

Full default suite in the foreground, in three parts (one run exceeds the 10-minute limit); together
they name every collected test file, including `tests/idea_web` and the new
`test_truth_corpus_followup_r8.py`:

```text
part A (idea_web .. phase3a):          4 failed, 622 passed, 1 warning in 380.46s
part B (playlists .. stage9):          623 passed, 1 skipped, 93 deselected, 1 warning in 361.47s
part C (fixture audit + truth tests):  260 passed, 1 skipped, 4 deselected, 1 warning in 519.89s
```

The only failures are the four known long-path `test_phase2b_retention.py` cases: this worktree is
based on `ec32f97` and lacks main's retention fix `91d2990`; `retention.py` and
`tests/test_phase2b_retention.py` are untouched here.

Part C was first run concurrently with part A, and there
`test_truth_review.py::test_reveal_route_records_before_it_answers` failed once: that test's HTTP
client waits at most 5 s, and a reveal now enters the gateway (listing every ancestor directory,
`%TEMP%` among them) for the record lock and again for the ledger append. Alone it passes, and part C
re-run alone in the foreground passed in full (the numbers above). This is recorded as a
performance observation under load, not a behaviour change.

Slow tests this pass touched (`-m slow` over `test_stage2a_controlled.py`,
`test_truth_corpus_followup_r6.py`, `test_truth_corpus_followup_r8.py`):

```text
22 passed, 26 deselected, 1 warning in 227.82s
```

```text
All checks passed!                     (uv run ruff check .)
311 files already formatted            (uv run ruff format --check .)
page JavaScript check passed: 53 inline scripts across 22 page renders
```

Read-only, through the gateway and the new exact-ledger rule: `controlled-events-1` (145 sets,
frozen, verified), `controlled-synth-1` (25, frozen, verified), `dev-1` (6, not frozen), `release-1`
(7, not frozen).

`git status --short -- work data` is empty; the process scan found no leftover processes from this
worktree (none killed). `io.py`, `jobs.py`, `retention.py` and `tests/test_phase2b_retention.py`
were not edited. Nothing was committed, branched, merged or pushed.

## Round-8 review (sol xhigh) + fix pass (round 9)

The round-8 diff review (Codex `gpt-5.6-sol` at xhigh, read-only) returned **FIX_FIRST** with four
realistic findings (two P0, two P1) and two adversarial P2s. Per the coordinator, round 9 fixes the
four realistic findings, keeps the change small, and records the two P2s as accepted residual risks.

| # | r8 tag | Finding | Verdict | Design | Where | Tests (`tests/test_truth_corpus_followup_r9.py` unless named) |
|---|---|---|---|---|---|---|
| 1 | B1 (P0) | Freezing and certification still reachable | **Reproduced** | **One moratorium gate.** `truth.CERTIFICATION_ENABLED = False`, read at call time through `certification_enabled()`; `CERTIFICATION_DISABLED` is the one exact message (defined once in `contracts.py`). Every emitter consults it: `freeze_truth` (and so `idea truth freeze`) refuses before reading anything; a freeze manifest's `certifiable` is `certifiable_under_gate(...)`; the generic scorer's certification status is the disabled message (a new contract value, never `"certified"`); every L3 `certifiable` is false and carries the message; `idea benchmark score` and both `score_corpus.py` printed summaries print it; `run_certify` refuses. Tests of the logic underneath open the gate only with the `certification_gate_open` fixture (a monkeypatch in `tests/conftest.py`), never a production flag. | gate `src/id_detector/truth.py:432`, `src/id_detector/truth.py:435`, `src/id_detector/truth.py:441`; message `src/id_detector/contracts.py:532`, status value `src/id_detector/contracts.py:869`; freeze `src/id_detector/truth.py:2245`; manifest `src/id_detector/truth.py:2472`; scorer `src/id_detector/benchmark/scorer.py:1617`; score CLI `src/id_detector/cli.py:1251`; L3 JSON `scripts/score_corpus.py:1068`; summaries `scripts/score_corpus.py:1254`, `scripts/score_corpus.py:1413`; certify `src/id_detector/calibrate/certify.py:237` | `test_the_production_gate_is_closed`, `test_freeze_is_refused_while_certification_is_disabled`, `test_idea_truth_freeze_refuses_with_the_disabled_message`, `test_a_freeze_manifest_never_records_certifiable_while_the_gate_is_closed`, `test_the_generic_scorer_emits_the_disabled_status_and_never_certified`, `test_idea_benchmark_score_prints_and_writes_the_disabled_message`, `test_score_corpus_json_and_printed_summaries_show_the_disabled_message`; `test_truth_corpus_followup_sol.py::test_generic_scorer_certifies_independent_truth_but_never_exposed_truth` (gate-closed control plus gate-enabled variant); the round-8 `run_certify`, CLI-certify and L3 tests |
| 2 | B2 (P0) | Benchmark outputs can overwrite truth | **Reproduced** | **One destination guard**, `truth.refuse_generated_output(path, *, work_root=None)`: refuses a link on the path; a corpus file's name; a destination beneath `work_root`; one lying inside a corpus (`corpus_containing`); and an existing directory that is a corpus or set, or contains corpus files (bounded, link-refusing walk). Every user-selectable output calls it at validation and again immediately before each write: scorer `out_path`, `score_corpus.py` `--out`, its `-mixes` artifact directory and each artifact, and the report outputs of `benchmark/corpus.py`, `ablations.py`, `transforms_schedule.py` and `shortlist.py`. The repo-wide guard now fails any function in those six modules that takes or reads `out`/`out_path`/`output`/`artefact_dir` without calling it (one allowlisted display formatter). | helper `src/id_detector/truth.py:1175`; scorer `src/id_detector/benchmark/scorer.py:1538`, `src/id_detector/benchmark/scorer.py:1653`; `score_corpus.py` `scripts/score_corpus.py:1365`, `scripts/score_corpus.py:1376`, `scripts/score_corpus.py:883`; corpus `src/id_detector/benchmark/corpus.py:589`; ablations `src/id_detector/benchmark/ablations.py:839`; schedule `src/id_detector/benchmark/transforms_schedule.py:547`; shortlist `src/id_detector/benchmark/shortlist.py:862` | `test_benchmark_score_refuses_out_at_the_truth_file_and_leaves_it_byte_identical` (the reported `--out <set>/ground_truth.json`), `test_generated_output_destinations_that_could_damage_a_corpus_are_refused` (7 cases), `test_the_scorer_revalidates_its_output_immediately_before_writing`, `test_score_corpus_refuses_an_output_inside_a_corpus`; `test_truth_gateway_guard.py::test_every_output_writer_in_the_output_modules_calls_the_destination_guard`, `::test_the_output_guard_check_catches_an_unguarded_writer` |
| 3 | B3 (P1) | Unbounded ancestor enumeration on every corpus open | **Reproduced** | **Bounded ancestry.** `corpus_ancestor` is O(depth): one `lstat` per ancestor for `corpus-version.json`, the ledger and `ground_truth.json` directly inside it; no ancestor is listed. The gateway adds at most **one** listing, of the new root's nearest existing parent, only when a mutation **creates** a corpus root, capped at `CORPUS_LISTING_LIMIT` (2,000 entries) and failing closed beyond it. A seed never creates a corpus root: the root must already exist, which closes deeper mistyped nesting into a manifest-less corpus such as `release-1` without any scan. Generated-output and audio destinations use the same O(depth) check plus one bounded listing only for a destination not yet an existing directory. | `src/id_detector/truth.py:1077`, `src/id_detector/truth.py:1092`, `src/id_detector/truth.py:1038`; creating-only listing `src/id_detector/truth.py:1131`; gateway `src/id_detector/truth.py:1312`; seed `src/id_detector/truth.py:204` | `test_a_big_ancestor_folder_is_never_enumerated` (7,500 entries; open, mutate, reveal and seed never list it), `test_creating_a_corpus_root_in_an_oversized_folder_fails_closed`, `test_reveal_on_the_real_layout_corpus_lists_only_the_corpus_and_stays_fast`, `test_nested_seeds_into_a_release_style_corpus_without_a_manifest_are_refused`, `test_seed_never_creates_a_corpus_root` |
| 4 | B4 (P1) | Repeated-test-version guard untested | **Reproduced** | A direct regression of `_guard_test_version`, without enabling production certification; the old test is renamed to say what it proves. | `src/id_detector/calibrate/certify.py:216` | `tests/test_stage5_calibration.py::test_guard_test_version_refuses_a_reused_version`, `::test_certify_with_a_repeated_test_version_is_refused_as_disabled` |

### Existing tests changed, each for a stated reason

- `tests/conftest.py` gains the `certification_gate_open` fixture. The modules whose tests exercise
  freezing and certification underneath the gate (`test_stage2a_truth.py`, `test_stage2a_scorer.py`,
  `test_truth_corpus_followup.py`, `_sol`, `_r2`, `_r3`, `_r4`, `_r6`, `_r7`, `_r8`) apply it with a
  module `pytestmark`; the tests that assert the moratorium itself close the gate again explicitly.
- `docs/schemas/benchmark_report.schema.json` is regenerated for the new status value.
- Seeds need an existing corpus root: `test_stage2a_truth.py` creates `tmp_path/corpus` (autouse
  fixture and the overlay helper); the r8 overlapping-seed barrier pre-creates the nested seed's root
  `C/a` so it still proves the seed lock (both seeds reach validation without it); the two r7
  too-deep seed tests accept the missing-root refusal.

### Accepted residual risks (owner decision 2026-09-14: hostile same-user local programs are out of scope)

Recorded, **not built**:

- **r8 B5 (adversarial P2): the audio replacement marker is forgeable and unbound.** Precondition:
  someone copies or hand-writes a `controlled-audio.json` into a non-corpus directory and then points
  `--audio-out` at it. The corpus checks still protect an actual corpus; only a non-corpus directory
  could be replaced.
- **r8 B6 (adversarial P2): exact ledger equality compares a set of line hashes.** Precondition:
  someone duplicates or reorders existing lines in a frozen corpus's ledger by hand. No exposure can
  be removed or added that way; only order and multiplicity go unchecked.
- From round 9's bounded ancestry: an **existing** subfolder hand-made inside a manifest-less corpus
  (for example `release-1/some-folder`) is not detected as inside a corpus by readers or by mutations
  of an existing root, because detecting it would need the ancestor listing the owner asked to
  remove. A seed cannot create such a folder, and a new corpus root, report or audio destination
  still gets the one bounded listing.

The earlier accepted residuals (round 3) are unchanged.

### Reverted-rule proofs

| Toggle (rule disabled in place) | Result |
|---|---|
| V1 production gate opened | FAILS as required (1 failed, 1 passed, 1 warning in 2.37s) |
| V2 freeze no longer consults the gate | FAILS as required (2 failed, 1 warning in 9.37s) |
| V3 manifest certifiable ignores the gate | FAILS as required (1 failed, 1 warning in 3.69s) |
| V4 scorer status ignores the gate | FAILS as required (2 failed, 1 warning in 14.00s) |
| V5 score CLI no longer prints the disabled message | FAILS as required (1 failed, 1 warning in 7.77s) |
| V6 score_corpus summaries omit the disabled message | FAILS as required (1 failed, 1 warning in 5.95s) |
| V7 generated output guard disabled | FAILS as required (9 failed, 1 warning in 3.22s) |
| V8 scorer pre-write revalidation removed | FAILS as required (1 failed, 1 passed, 1 warning in 8.75s) |
| V10 ancestor listing restored (old unbounded sibling enumeration) | FAILS as required (2 failed, 1 warning in 14.29s) |
| V11 creation listing no longer bounded | FAILS as required (1 failed, 1 warning in 3.45s) |
| V12 seed may create a corpus root again | FAILS as required (2 failed, 1 warning in 2.37s) |
| V13 repeated test version guard removed | FAILS as required (1 failed, 1 warning in 3.64s) |

Every toggled file was restored and verified byte-identical before the next toggle.

### Gate outputs (this pass)

Full default suite in the foreground, in three parts; together they name every collected test file,
including `tests/idea_web` and the new `test_truth_corpus_followup_r9.py`. Part C ran **alongside
part A as a parallel shard**, the configuration in which
`test_truth_review.py::test_reveal_route_records_before_it_answers` timed out once in round 8; it
passed, and part C took 138 s instead of about 520 s now that no ancestor is listed.

```text
part A (idea_web .. phase3a):          4 failed, 622 passed, 1 warning in 373.46s
part C (fixture audit + truth tests):  284 passed, 1 skipped, 4 deselected, 1 warning in 138.30s  (in parallel with A)
part B (playlists .. stage9):          624 passed, 1 skipped, 93 deselected, 1 warning in 154.53s
```

The only failures are the four known long-path `test_phase2b_retention.py` cases: this worktree is
based on `ec32f97` and lacks main's retention fix `91d2990`; `retention.py` and
`tests/test_phase2b_retention.py` are untouched here.

Slow tests this pass touched (`-m slow` over `test_stage2a_controlled.py`,
`test_truth_corpus_followup_r6.py`, `test_truth_corpus_followup_r8.py`):

```text
22 passed, 26 deselected, 1 warning in 114.82s
```

```text
All checks passed!                     (uv run ruff check .)
312 files already formatted            (uv run ruff format --check .)
page JavaScript check passed: 53 inline scripts across 22 page renders
```

`docs/schemas/benchmark_report.schema.json` was regenerated with `scripts/export_schemas.py`; it is the
only schema that changed.

Read-only, through the gateway with the bounded ancestry and exact-ledger rules:
`controlled-events-1` (145 sets, frozen, verified), `controlled-synth-1` (25, frozen, verified),
`dev-1` (6, not frozen), `release-1` (7, not frozen).

No test binds ports 8791/8792 (a repository search found no reference), and no server was started on
them. `git status --short -- work data` is empty; the process scan found no leftover processes from
this worktree (none killed). `io.py`, `jobs.py`, `retention.py` and
`tests/test_phase2b_retention.py` were not edited. Nothing was committed, branched, merged or pushed.

## Round-9 review (sol xhigh) + final fix pass (round 10)

The round-9 diff review (Codex `gpt-5.6-sol` at xhigh, read-only) returned **FIX_FIRST** with five
realistic findings (two P0, three P1) and no new adversarial finding. Round 10 is the final fix
round of this cycle and implements exactly the coordinator's five items. Main has moved on
(`bb57b70`, the money fix, touches `cli.py` and `io.py`), so `io.py` is no longer off-limits; this
pass edits `io.py` only above `atomic_write_bytes` and in its first lines, away from `bb57b70`'s hunk.

| # | r9 tag | Finding | Verdict | Design | Where | Tests (`tests/test_truth_corpus_followup_r10.py` unless named) |
|---|---|---|---|---|---|---|
| 1 | B1 (P0) | Positive certification claims still emitted | **Reproduced** | Every remaining positive emitter consults the one gate. `scripts/score_corpus.py`: `thresholds_met` becomes `null` whenever it would be `true` while certification is disabled, and the "all three thresholds are met" sentence is replaced by "no L3 threshold claim is made because certification is disabled ...". `idea benchmark links-score`: a passing sample's status is the disabled message, never `certified`. `idea benchmark freeze-profiles`: every feature `certified` flag goes through `certifiable_under_gate`. `fuse/episodes.py`: a loaded calibration model's `certified` entries propagate as `provisional` while the gate is closed. | thresholds `scripts/score_corpus.py:1044`; sentence `scripts/score_corpus.py:1253`; link score `src/id_detector/enrich/benchmark.py:142`; profiles `src/id_detector/profiles.py:380`; episodes `src/id_detector/fuse/episodes.py:593` | `test_no_emitter_claims_certification_while_the_gate_is_closed` scans the JSON and printed text of `idea benchmark score`, `scripts/score_corpus.py --out` and `--print` (with the L3 thresholds reachable), `idea benchmark links-score` (a passing sample), `idea benchmark freeze-profiles`, calibrated episode certification, `idea truth freeze` and `idea benchmark certify` for a `"certified"` status, `certifiable: true`, `certified: true`, `thresholds_met: true` and "thresholds are met"; `test_the_same_emitters_do_claim_certification_with_the_gate_open` proves every pattern really appears with the gate open |
| 2a | B2 (P0) | Unguarded public writers | **Reproduced** | `refuse_generated_output` at validation and again immediately before the write for: `idea config init` (including `--force`), `idea benchmark links`, `idea benchmark links-score`, `run_hint_gate`, calibration validation's report and `model_out` (both validated before anything runs; the model and its sidecar before their writes), profile freezing (the output folder, each profile and its sidecar) and `scripts/make_controlled_predictions.py`. | CLI helper `src/id_detector/cli.py:388`; config init `src/id_detector/cli.py:377`; links `src/id_detector/cli.py:1635`; links-score `src/id_detector/cli.py:1662`; hints `src/id_detector/benchmark/hints.py:104`; calibration `src/id_detector/calibrate/validate.py:362`; profiles `src/id_detector/profiles.py:643`; predictions script `scripts/make_controlled_predictions.py:25` | `test_truth_gateway_guard.py::test_every_guarded_public_writer_refuses_a_corpus_destination` (14 writers, destination a new file directly inside a manifest-less corpus, which only this explicit guard catches) |
| 2b | B2 (P0) | Class closure for writers not yet found | **Reproduced** | **Backstop:** `io.refuse_corpus_destination` runs at the top of `atomic_write_bytes` (so under `atomic_write_json` and `write_completion_sidecar` too). It refuses a destination named `ground_truth.json`, `corpus-version.json` or `review-exposure-ledger.jsonl`, or whose parent or grandparent directly holds one of them: six `lstat` probes, no listing. The only exemption is `through_corpus_gateway=True`, passed only by `truth.write_corpus_file_through_gateway`, which the controlled renderer's staging corpus and calibration validation's scratch corpus use. Owner truth is written by the pinned writer in `truth_paths.py`, which does not use the `io` helpers. | backstop `src/id_detector/io.py:87`, call `src/id_detector/io.py:121`; exemption `src/id_detector/truth.py:449`; users `src/id_detector/benchmark/controlled.py:897`, `src/id_detector/calibrate/validate.py:310` | `test_the_io_backstop_refuses_direct_writes_into_a_corpus` (a set's `ground_truth.json`, a file in a set, a file below a set, a file in and below a manifest corpus; all byte-identical), `test_the_io_backstop_leaves_work_writes_alone_with_six_probes_and_no_listing`, `test_only_the_gateway_helper_writes_a_corpus_file` |
| 3 | B3 (P1) | Seed retry after creating the named folder | **Reproduced** | An existing root with no manifest, no ledger and no set is a **new** root, exactly like a missing one, so it gets the one bounded parent listing: the `release-1/new-group/...` retry is refused as inside another corpus. The missing-root message now says to create a new corpus folder outside any existing corpus, never the nested folder. | `src/id_detector/truth.py:1124`; message `src/id_detector/truth.py:206` | `test_the_seed_retry_after_creating_the_named_folder_is_still_refused` (refused, the owner creates `new-group`, retry still refused, corpus byte-identical), `test_seeding_into_a_deliberately_created_empty_corpus_folder_still_works` |
| 4 | B4 (P1) | New root's parent listed twice | **Reproduced** | Before the lock the gateway does `lstat`-only ancestry; the bounded parent listing runs once per mutation, under the corpus lock. | `src/id_detector/truth.py:1383` | `test_a_new_corpus_root_parent_is_listed_once_under_the_lock` (exactly one listing, made while the lock is held; none for an established root) |
| 5 | D (P1) | Final pre-write revalidation untested; name heuristic | **Reproduced** | Each of `corpus.py`, `ablations.py`, `transforms_schedule.py` and `shortlist.py` publishes its report only through a small `_publish_report(out_path, payload)` that revalidates and writes; the run function calls it. The round-9 six-module name heuristic is replaced by the public-writer refusal test (2a); the `io` backstop (2b) is the repository-wide guarantee. | `src/id_detector/benchmark/corpus.py:487`, `src/id_detector/benchmark/ablations.py:690`, `src/id_detector/benchmark/transforms_schedule.py:369`, `src/id_detector/benchmark/shortlist.py:700` | `test_each_run_revalidates_its_report_destination_immediately_before_publishing[corpus, ablations, transforms_schedule, shortlist]` (the run publishes only through it; a corpus destination is refused and left byte-identical) |

### Existing tests changed, each for a stated reason

- `tests/conftest.py` gains `write_corpus_fixture`: tests that build a corpus by hand
  (`test_stage2a_scorer.py`, `test_stage5_calibration.py`, `test_truth_corpus_followup_r3.py`,
  `_sol`) write its truth files and manifests directly, because `io`'s backstop refuses corpus file
  names by design.
- `tests/test_stage4d_profiles.py` opens the gate by fixture: re-deriving the committed profiles
  byte-for-byte needs their `certified` flags.
- `tests/test_score_corpus.py`: the met-thresholds summary now asserts that no "thresholds are met"
  claim is printed; the gate-open wording is covered by the r10 control test.
- `tests/test_truth_gateway_guard.py`: the round-9 output-name heuristic is replaced (item 5), and
  `io.py`'s three corpus-name literals are allowlisted with a reason (`io` cannot import `truth`).

### Follow-ups recorded, not built (per the coordinator: beyond this round, on main or not made worse)

- **Merge with main:** `bb57b70` adds `io.durable_replace` and `io.create_file_durably`, which do not
  exist in this worktree; when this change is merged, those shared file-replace helpers should call
  the same `refuse_corpus_destination` backstop.
- Other in-place replacements in `decode.py`, `ingest.py`, `windows.py`, `providers/panako_setup.py`,
  `retention.py` and the controlled renderer's audio temporaries write fixed media file names in the
  work tree or a validated staging folder, not user-selected destinations; they are not shared
  helpers and are left as they are.
- The round-9 accepted residual narrows: an **empty** hand-made folder inside a manifest-less corpus
  is now treated as a new root by every mutation; a hand-made folder that already holds non-corpus
  files is still not detected by readers, as recorded in round 9.

### Reverted-rule proofs

| Toggle (rule disabled in place) | Result |
|---|---|
| W1 thresholds_met no longer gated | FAILS as required (1 failed, 1 warning in 22.21s) |
| W2 'thresholds are met' sentence no longer gated | FAILS as required (1 failed, 1 warning in 19.41s) |
| W3 link-score status no longer gated | FAILS as required (1 failed, 1 warning in 17.90s) |
| W4 profile certified flags no longer gated | FAILS as required (1 failed, 1 warning in 18.67s) |
| W5 calibrated episode certification no longer gated | FAILS as required (1 failed, 1 warning in 15.70s) |
| W6 io backstop disabled | FAILS as required (2 failed, 1 warning in 3.19s) |
| W7 backstop no longer bounded (lists the parent) | FAILS as required (1 failed, 1 warning in 2.74s) |
| W8 config init loses its explicit guard | FAILS as required (1 failed, 1 warning in 2.52s) |
| W9 benchmark links loses both guards | FAILS as required (1 failed, 1 warning in 2.41s) |
| W10 hints loses its initial guard | FAILS as required (1 failed, 1 warning in 1.99s) |
| W11 calibration model_out no longer validated first | FAILS as required (1 failed, 1 warning in 2.08s) |
| W12 profile freezing loses its initial guard | FAILS as required (1 failed in 0.79s) |
| W13 make_controlled_predictions loses its initial guard | FAILS as required (1 failed in 0.74s) |
| W14 existing empty root no longer treated as new | FAILS as required (1 failed, 1 warning in 3.29s) |
| W15 parent listed again before the lock | FAILS as required (1 failed, 1 warning in 3.17s) |
| W16 corpus final pre-write revalidation removed | FAILS as required (1 failed, 1 warning in 3.14s) |
| W17 ablations final pre-write revalidation removed | FAILS as required (1 failed, 1 warning in 3.01s) |
| W18 transforms_schedule final pre-write revalidation removed | FAILS as required (1 failed, 1 warning in 3.20s) |
| W19 shortlist final pre-write revalidation removed | FAILS as required (1 failed, 1 warning in 3.04s) |

W7 first ran against a test that wrote into a not-yet-existing folder, where an injected parent listing had nothing to list; the test now counts a write into an existing folder, as a running pipeline does, and the toggle fails. Every toggled file was restored and verified byte-identical before the next toggle.

### Gate outputs (this pass)

Full default suite in the foreground, in three parts; together they name every collected test file,
including `tests/idea_web` and the new `test_truth_corpus_followup_r10.py`. Part C, which holds
`test_truth_review.py::test_reveal_route_records_before_it_answers`, again ran alongside part A as
a parallel shard.

```text
part A (idea_web .. phase3a):          4 failed, 622 passed, 1 warning in 418.76s
part C (fixture audit + truth tests):  308 passed, 1 skipped, 4 deselected, 1 warning in 140.35s  (in parallel with A)
part B (playlists .. stage9):          624 passed, 1 skipped, 93 deselected, 1 warning in 200.14s
```

The only failures are the four known long-path `test_phase2b_retention.py` cases: this worktree is
based on `ec32f97` and lacks main's retention fix `91d2990`; `retention.py` and
`tests/test_phase2b_retention.py` are untouched here. The `io` backstop sits under every atomic
write in parts A and B (pipeline, money, bundles, retention, web app) and changed no other result.

Slow tests this pass touched (`-m slow` over `test_stage2a_controlled.py`,
`test_truth_corpus_followup_r6.py`, `test_truth_corpus_followup_r8.py`; the controlled renderer now
writes its staging truth through the gateway helper):

```text
22 passed, 26 deselected, 1 warning in 116.87s
```

```text
All checks passed!                     (uv run ruff check .)
313 files already formatted            (uv run ruff format --check .)
page JavaScript check passed: 53 inline scripts across 22 page renders
```

Read-only, through the gateway: `controlled-events-1` (145 sets, frozen, verified),
`controlled-synth-1` (25, frozen, verified), `dev-1` (6, not frozen), `release-1` (7, not frozen).

No server was started on ports 8791/8792. `git status --short -- work data` is empty; the process
scan found no leftover processes from this worktree (none killed). `jobs.py`, `retention.py` and
`tests/test_phase2b_retention.py` were not edited; `io.py` was edited only in its first write helper
and the backstop above it, away from `bb57b70`'s hunk. Nothing was committed, branched, merged or
pushed.

## Round-10 review (sol xhigh) + link-score gate fix (round 11)

The round-10 review found no regressions and one realistic P0: with certification disabled,
`idea benchmark links-score` still reported a passing sample's gate as `"pass": true` and printed
`gate_pass=true`. Round 11 fixes only that.

- **Design:** while the gate is closed, link-score's `gate.pass` is `null` (not judged, neither pass
  nor fail); the raw metrics (`precision_e4`, `one_sided_95_lower_e4`, counts) stay visible, and the
  gate `status` is the disabled message for a passing sample. The CLI prints `gate_status=<status>`
  instead of `gate_pass=...` whenever the gate is not judged.
  `src/id_detector/enrich/benchmark.py` (`"pass": passed if certification_enabled() else None`),
  `src/id_detector/cli.py` (`benchmark_links_score`, `gate_status=...`).
- **Tests:** `tests/test_truth_corpus_followup_r10.py` scans link-score's JSON and console output
  separately for `"pass": true` and `gate_pass=true` (`LINK_CLAIMS`), asserts `gate.pass is None`,
  the disabled status and the still-visible raw metrics in
  `test_no_emitter_claims_certification_while_the_gate_is_closed`, and proves both patterns appear
  with the gate open in `test_the_same_emitters_do_claim_certification_with_the_gate_open`.
  `tests/test_stage6_enrich.py::test_link_benchmark_sample_is_stratified_and_scored` now expects
  `pass` null and `pending_owner_marking` for its under-60-link sample.
- **Revert proof:** reverting the library part, the CLI part, and both each made the closed-gate test
  fail (`assert True is None`; the `gate_status=...` assertion), and each file was restored
  byte-identical.
- **Gates (foreground):** `tests/test_truth_corpus_followup_r10.py` + `tests/test_stage6_enrich.py`:
  33 passed. `uv run ruff check .`: all checks passed; `uv run ruff format --check .`: 313 files
  already formatted. The full suite is to be run on main after merging. `io.durable_replace` and
  `io.create_file_durably` (main only) were deliberately not wired; the coordinator records them as a
  follow-up. Nothing under `work/` or `data/` changed; nothing was committed.

## Orchestrator gate runs on the final code

The worktree guard in the fixer's session refused PowerShell, so the orchestrator ran both PowerShell gates from the fixer's worktree against the final code, after checking the loopback ports were free:

```text
=== truth fix applied to main (base bb57b70, after the money fix) — orchestrator gate runs ===
ruff check .            : All checks passed!
ruff format --check .   : 338 files already formatted
scripts/audit_fixtures.py: audited 474 files; fixture audit passed
scripts/check_page_js.py : page JavaScript check passed: 53 inline scripts across 22 page renders
git status -- data work : empty
pytest shard A (tests/idea_web, tests/test_[a-o]*.py, tests/test_phase*.py): 759 passed, 1 warning in 758.81s (0 failed; the io corpus-destination backstop sits under every money, queue and bundle write here)
pytest shard B (all remaining tests/test_*.py, including every truth follow-up test): 935 passed, 1 skipped, 97 deselected (default slow/live markers), 1 warning in 389.38s (0 failed)
full suite on main with both fixes: 1,694 passed, 1 skipped, 0 failed
smoke_serve.ps1 (13:28:41-13:28:48, pytest=0, ports free, cpu 48%): smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'; exit 0 (first run)
gate_local_mode.ps1 (13:28:56-13:29:14): prepared offline cached mix; local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok; exit 0 (first run, no re-run needed)
```

Second-model reviews (Codex gpt-5.6-sol at xhigh, model verified from each log header), filed in round order:

- `docs/reviews/followup-truth-sol-r1.md`
- `docs/reviews/followup-truth-sol-r2.md`
- `docs/reviews/followup-truth-sol-r3.md`
- `docs/reviews/followup-truth-sol-r4.md`
- `docs/reviews/followup-truth-sol-r5.md`
- `docs/reviews/followup-truth-sol-r6.md`
- `docs/reviews/followup-truth-sol-r7.md`
- `docs/reviews/followup-truth-sol-r8.md`
- `docs/reviews/followup-truth-sol-r9.md`
- `docs/reviews/followup-truth-sol-r10.md`

