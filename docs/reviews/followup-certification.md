# Follow-up: certification scope (the two defects deferred by the truth-corpus cycle)

Base: main at `0915f85`. Builder pass, nothing committed, no branch created. This closes the two items
listed under "Deferred to the certification follow-up (required before any freeze or certify)" in
`docs/reviews/followup-truth-corpus.md` (round-7 review P0-2 and P1-5, `followup-truth-sol-r7.md`), and
then decides the certification gate.

> **Final state: the certification gate is CLOSED (`truth.CERTIFICATION_ENABLED = False`).** The first
> pass below opened it; the second-model review (sol xhigh) returned FIX_FIRST with five P0s, and the
> orchestrator decided to land the fixes with certification OFF. Read "Fix pass: gate closed" and
> "Required before the certification gate may open" at the end; they supersede the first pass's gate
> decision, its proofs table and its gate outputs. Both defect fixes, the certify-output guards and
> the clearer messages are kept as hardening and stay tested with the gate opened by fixture.

Nothing under `data/corpus/` or `work/` was written, moved, frozen or verified. No `idea truth freeze`,
`verify`, `second-pass`, `resolve` or any other mutation was run against the real corpora; every test
corpus is a temporary copy of a test fixture. No live provider was called, no PowerShell gate was run,
and no server was started.

## Defect 1 -- an L3 run list over a subset of a frozen corpus could be certified

**Reproduced.** `scripts/score_corpus.py` judged each run-list truth file on its own
(`truth_status`, `truth_independent`), and `score_run_list` called the selected subset certifiable.
A frozen corpus with one set reviewed with IDea's predictions on screen produced a "certifiable"
report of the rest, just by leaving the exposed set out of the run list. Reverting the fix makes the
new tests fail (X1, X2 below).

**Rule, in one place:** `benchmark.scorer.certification_scope(truth_paths)` returns a
`CertificationScope(complete, independent, corpus_root, reasons)`:

1. every truth path must belong to ONE corpus (a standalone truth file, or paths from two corpora,
   can never be certifiable);
2. that corpus must be frozen, and its on-disk population must equal its manifest exactly
   (`require_frozen_inventory`, enforced here too -- a lost or added set is still refused outright);
3. the records the run list names must EXACTLY equal the frozen inventory (`complete`);
4. independence is judged over that COMPLETE inventory -- every vetted record's live evidence and
   every manifest entry's recorded exposure -- never over the caller's subset (`independent`).

Everything is read through `open_corpus(mutate=False)`. Reasons are ordinary words, say what to do
next, and name folders and sets rather than absolute paths so a report is the same on any machine.

**Where it is enforced**

- `scripts/score_corpus.py::score_run_list`: `l3.certifiable` needs verified + time-matched +
  independent **and** `scope.certifiable`; `l3.independent` is false when the whole frozen corpus is
  not independent, even if the run list left the exposed set out. The L3 block gains
  `not_certifiable_because` (every reason, empty when certifiable) and `scope`
  (`whole_frozen_corpus`, `corpus_independent`, `reasons`). `--print` and the one-line summary print
  `NOT CERTIFIABLE: <reason>` for a score of frozen truth.
- `benchmark.scorer.score_corpus_detailed` (behind `idea benchmark score`, `idea benchmark corpus
  --set`, certify and calibration validation): a tier is `certified` only when the truth path is the
  whole frozen corpus. One record named out of a frozen corpus is a subset and stays `provisional`.
  The hard-coded ten-set minimum became the constant `CERTIFICATION_MIN_SETS` so the scope rule can
  be tested on its own.

A partial run list is still scored, exit code 0, all numbers present; it is just never certifiable.

**Tests** (`tests/test_certification_followup.py`)

- `test_a_whole_clean_frozen_corpus_is_certifiable_with_the_gate_open` (the control)
- `test_leaving_the_exposed_set_out_of_the_run_list_never_certifies_the_rest[sidecar beside the set]`
  and `[recorded in the manifest]` -- the reported attack; also asserts the corpus is byte-identical
- `test_a_partial_run_list_over_a_clean_frozen_corpus_is_scored_but_not_certifiable`
- `test_a_run_list_spanning_two_frozen_corpora_is_never_certifiable`
- `test_certification_scope_judges_exposure_over_the_complete_frozen_inventory`
- `test_the_generic_scorer_never_certifies_one_record_named_out_of_a_frozen_corpus`

## Defect 2 -- calibration scratch was not guaranteed outside the configured work root

**Reproduced.** `_score_certification` had no `work_root` parameter and built its scratch corpus with
`tempfile.TemporaryDirectory`; `idea benchmark calibration-validate --work-root <temp folder>` put a
truth corpus inside the work root. The old r7 test even asserted the parameter did not exist.

**Fix**

- New `truth.refuse_scratch_destination(scratch, *, work_root)`: on the path as spelled, BEFORE any
  file or folder exists, it refuses a link on the path, a folder inside `work_root`, a folder that
  already exists, and a folder inside any corpus (an ancestor holding a corpus file, by `lstat`; or a
  parent that holds set folders directly, as a manifest-less corpus such as `release-1` does). The
  parent is listed once and in full: it is normally the system temp folder, which holds far more
  than `CORPUS_LISTING_LIMIT` entries, and this runs once per calibration validation, not per corpus
  open (the bounded-ancestry rule for the gateway is untouched).
- `calibrate/validate.py`: `run_calibration_validation` passes `work_root` to
  `_score_certification`, which validates a fresh, not-yet-created scratch path, creates it with a
  create-only `os.mkdir`, and removes it afterwards. The same check also runs at the top of
  `run_calibration_validation`, before any corpus is opened, so a bad work root is refused in a
  second rather than after the whole corpus has been analysed.

**Tests**

- `test_the_scratch_corpus_is_refused_before_anything_is_created_inside_the_work_root[the temp folder]`
  and `[a folder above the temp folder]` -- the temp folder stays empty
- `test_the_scratch_corpus_is_never_created_inside_a_corpus[a corpus without a manifest | a frozen corpus | a set]`
  -- both corpora byte-identical
- `test_calibration_validation_refuses_a_temp_work_root_before_it_opens_any_corpus`
- `test_the_scratch_corpus_lives_outside_the_work_root_and_is_removed` (happy path)
- `test_refuse_scratch_destination_accepts_a_crowded_ordinary_temp_folder` (2,005 entries)
- `tests/test_truth_corpus_followup_r7.py::test_calibration_scores_a_scratch_corpus_outside_work_and_every_corpus`
  updated: it now asserts the `work_root` parameter exists and passes one.

## Also found and fixed while opening the gate

`run_certify` was unreachable while the gate was closed, so round 10's "every public writer is
guarded" sweep never covered it. Its `--out` report and the predictions file beside it now pass
`refuse_generated_output(..., work_root=work_root)` before anything is read and again before the
predictions write (`score_corpus` already revalidates the report). Test:
`test_certify_refuses_a_report_inside_a_corpus_before_it_opens_any_corpus` (a manifest-less corpus,
which only the explicit guard catches, and a path beneath the work root).

## Draft scoring still works, and is never certifiable

- `test_draft_truth_is_scored_labelled_a_draft_and_never_certifiable[open]` and `[closed]`: exit 0, all
  numbers present, `truth_status: draft`, `certifiable: false`, the reason says the corpus is not
  frozen and what the owner does next, and `--print` still says "DRAFT truth ... working numbers, not
  release numbers".
- Read-only run of this worktree's scorer over the owner's real run list
  (`data/local/release-1-runs-free.json` in the main checkout, `--print` only, so it writes only to a
  temp folder): 7 mixes scored against DRAFT truth, work recall 64.2 %, work precision 75.3 %, likely
  precision 89.7 %. Same shape as `docs/accuracy/release-1-free-draft.md`.
- `tests/fixtures/corpus-mini/expected.json` and `expected-time.json`: only the `l3` block changed
  (the regeneration script asserted every other key is identical): it gains
  `not_certifiable_because` and `scope`, which say the corpus is not frozen. (Final state, gate
  closed: `certification` still carries the disabled message, exactly as on main.)

## Refusal messages, in ordinary words

| Refusal | Now says |
|---|---|
| certify on a corpus with no manifest / a draft inventory | it has not been frozen; check every tracklist by ear, run `idea truth freeze`, certify again; score it as a draft meanwhile |
| certify on an exposed corpus | still scoreable for development; a certified number needs truth made without seeing IDea's answers |
| certify with a used test version / existing report | a test version is evaluated exactly once; run again with a new `--test-version` |
| freeze with draft or unverified rows | nothing was changed; open the set in `idea truth review`, check it, save, freeze again; freezing is final. The list is capped at 25 problems with "... and N more" (a half-checked corpus has hundreds of draft rows) |
| closed gate (`idea truth freeze`, `idea benchmark certify`, `idea benchmark score`, `score_corpus.py`) | the exact disabled message, then `CERTIFICATION_DISABLED_NEXT_STEP` (final wording): nothing is wrong and there is nothing to fix on the owner's side; freezing and certifying stay off until the remaining work listed in this document is built; draft scoring with `scripts/score_corpus.py` still works and is how accuracy is measured meanwhile |
| partial run list / two corpora / standalone truth / unfrozen corpus | see `certification_scope` reasons above |
| scratch inside the work root / inside a corpus | choose a `--work-root` that is not the temp folder, or point `TEMP` at an ordinary folder |

The persisted status string `CERTIFICATION_DISABLED` is unchanged (it is a schema enum value in
`benchmark_report.schema.json`; changing it would invalidate existing reports). `run_certify` still
raises exactly that message (the r8 test asserts equality); the CLI adds the next step when it prints.

Tests: `test_certify_on_an_unfrozen_corpus_says_what_to_do_next`,
`test_freezing_a_draft_corpus_is_refused_with_next_steps_and_changes_nothing`.

## First-pass gate decision: open -- SUPERSEDED, the gate is closed (see "Fix pass: gate closed")

Both deferred defects are closed with regressions that fail when reverted, and I found no remaining
reason to hold the gate:

- every emitter of a positive claim was already routed through the one gate in rounds 9-11, so
  opening it restores exactly the pre-moratorium behaviour plus the two new rules;
- the two committed frozen corpora verify, `profiles/` re-derive byte-for-byte
  (`test_stage4d_profiles.py`), the golden Local Free output is unchanged, and no schema changed;
- opening the gate freezes nothing. `idea truth freeze` is still a command only the owner runs, it
  refuses any row that is still draft or unverified (all of `release-1` today), and a frozen corpus
  is terminal. The owner's listening is still what makes `release-1` freezable.

Both directions stay tested:

- `tests/conftest.py`: `certification_gate_open` now *pins* the gate open (so the ten modules that
  use it -- `test_stage2a_truth`, `test_stage2a_scorer`, `test_truth_corpus_followup`, `_sol`, `_r2`,
  `_r3`, `_r4`, `_r6`, `_r7`, `_r8` -- and `test_stage4d_profiles` test the logic, not the production
  default; their comments say so), and a new `certification_gate_closed` fixture closes it.
- Closed-gate assertions close the gate explicitly: r8 (3 tests), r9 (all of section 1), the r10
  closed-gate scan with its gate-open control, `test_truth_corpus_followup.py`, `_sol`, and now
  `test_score_corpus.py` (the frozen-manifest and free-recipe tests are parametrised over both
  states), `test_stage5_calibration.py` (certify refusals parametrised over both states: open ->
  `CorpusNotFrozen` / `DuplicateTestVersion`, closed -> `CertificationDisabled`), and
  `test_stage6_enrich.py` (open: `gate.pass` is `False`; closed: `null`, via the new fixture).
- `test_truth_corpus_followup_r9.py::test_the_production_gate_is_closed` became
  `test_the_production_gate_is_open_and_one_switch_closes_it`;
  `test_certification_followup.py::test_the_production_gate_is_open_and_every_refusal_survives_closing_it`
  scores one whole frozen corpus in both states.

## First-pass reverted-rule proofs (superseded by the fix-pass table)

Each rule disabled in place, its tests run, the file restored and hash-checked byte-identical:

| Toggle (rule disabled in place) | Result |
|---|---|
| X1 run-list scope rule ignored by score_corpus.py (subset certifiable again) | FAILS as required (4 failed, 1 warning in 18.33s) |
| X2 scope judges exposure only over the records the caller named | FAILS as required (3 failed, 1 warning in 6.89s) |
| X3 generic scorer no longer needs the whole frozen corpus | FAILS as required (1 failed, 1 warning in 9.08s) |
| X4 L3 block no longer says why it is not certifiable | FAILS as required (4 failed, 1 warning in 8.36s) |
| X5 scratch built without validating it against the work root and every corpus | FAILS as required (5 failed, 1 warning in 2.90s) |
| X6 calibration validation no longer refuses a temp work root up front | FAILS as required (1 failed, 1 warning in 2.09s) |
| X7 scratch validator ignores the work root | FAILS as required (3 failed, 1 warning in 2.29s) |
| X8 scratch parent listing bounded again (fails closed in a crowded temp folder) | FAILS as required (1 failed, 1 warning in 2.94s) |
| X9 certify outputs no longer guarded | FAILS as required (1 failed, 1 warning in 1.74s) |
| X10 certify's unfrozen refusal back to the terse message | FAILS as required (1 failed, 1 warning in 1.78s) |
| X11 freeze refusal back to the bare error list | FAILS as required (1 failed, 1 warning in 2.26s) |
| X12 production gate closed again | FAILS as required (4 failed, 1 warning in 2.04s) |
| X13 closed gate no longer refuses certify (run_certify ignores the gate) | FAILS as required (2 failed, 2 passed, 1 warning in 1.48s) |

The two tests no toggle targets are controls (the whole clean corpus certifies; the scratch happy path).

## First-pass gate outputs (gate open; superseded by the fix-pass outputs)

`uv run pytest --collect-only -q`:

```text
1975/2072 tests collected (97 deselected) in 8.37s
```

Full default suite in five shards. A small runner asserts the shards name every `tests/test_*.py`
exactly once (plus `tests/idea_web`):

```text
S1 tests/idea_web:                                      377 passed, 1 warning in 310.08s (0:05:10)
S2 test_[a-o]*, test_pa*, test_ph*:                     656 passed, 1 warning in 537.88s (0:08:57)
S3 test_pl*, test_pr*, test_sc*, test_se*, stage1-3:    356 passed, 25 deselected, 1 warning in 150.43s (0:02:30)
S4 test_stage4* .. test_stage9*:                        281 passed, 1 skipped, 68 deselected, 1 warning in 26.25s
S5 test_t* (truth follow-ups, gateway guard, review):   303 passed, 1 skipped, 4 deselected, 1 warning in 119.76s (0:01:59)
total: 1,973 passed + 2 skipped = 1,975 = the collected total; 97 deselected; 0 failed
```

No known flake fired. S2 finished inside the limit but close to it (8 min 57 s); split it at
`test_pa*` next time.

Slow tests the earlier rounds ran (`-m slow` over `test_stage2a_controlled.py`,
`test_truth_corpus_followup_r6.py`, `test_truth_corpus_followup_r8.py`):

```text
22 passed, 26 deselected, 1 warning in 96.84s (0:01:36)
```

```text
uv run ruff check .             All checks passed!
uv run ruff format --check .    401 files already formatted
uv run python scripts/audit_fixtures.py
                                audited 531 files (530 before this report was added)
                                fixture audit passed
uv run python scripts/check_page_js.py
                                page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

Read-only corpus check, through the gateway (`open_corpus(mutate=False)`, `truth_is_frozen_verified`,
`certification_scope`):

```text
controlled-events-1: sets=145 manifest_file=True frozen=True verified=True whole_frozen_corpus=True corpus_independent=True
controlled-synth-1: sets=25 manifest_file=True frozen=True verified=True whole_frozen_corpus=True corpus_independent=True
dev-1: sets=6 manifest_file=True frozen=False verified=False whole_frozen_corpus=False corpus_independent=None
release-1: sets=7 manifest_file=False frozen=False verified=False whole_frozen_corpus=False corpus_independent=None
```

`git status --short -- data work`:

```text
(empty)
```

`git status --short -- profiles data tests/golden docs/schemas` is empty too: the frozen profiles, both
committed frozen corpora, the golden Local Free output and every schema are unchanged.

`git status --short`:

```text
 M scripts/score_corpus.py
 M src/id_detector/benchmark/scorer.py
 M src/id_detector/calibrate/certify.py
 M src/id_detector/calibrate/validate.py
 M src/id_detector/cli.py
 M src/id_detector/contracts.py
 M src/id_detector/truth.py
 M tests/conftest.py
 M tests/fixtures/corpus-mini/expected-time.json
 M tests/fixtures/corpus-mini/expected.json
 M tests/test_score_corpus.py
 M tests/test_stage2a_scorer.py
 M tests/test_stage2a_truth.py
 M tests/test_stage4d_profiles.py
 M tests/test_stage5_calibration.py
 M tests/test_stage6_enrich.py
 M tests/test_truth_corpus_followup.py
 M tests/test_truth_corpus_followup_r2.py
 M tests/test_truth_corpus_followup_r3.py
 M tests/test_truth_corpus_followup_r4.py
 M tests/test_truth_corpus_followup_r6.py
 M tests/test_truth_corpus_followup_r7.py
 M tests/test_truth_corpus_followup_r8.py
 M tests/test_truth_corpus_followup_r9.py
 M tests/test_truth_corpus_followup_sol.py
?? docs/reviews/followup-certification.md
?? tests/test_certification_followup.py
```

`git diff --stat` (tracked files; the two new files are above):

```text
 scripts/score_corpus.py                       |  86 ++++++++++++++--
 src/id_detector/benchmark/scorer.py           | 140 +++++++++++++++++++++++++-
 src/id_detector/calibrate/certify.py          |  46 ++++++---
 src/id_detector/calibrate/validate.py         |  35 ++++++-
 src/id_detector/cli.py                        |   8 +-
 src/id_detector/contracts.py                  |   6 +-
 src/id_detector/truth.py                      |  84 ++++++++++++++--
 tests/conftest.py                             |  20 +++-
 tests/fixtures/corpus-mini/expected-time.json |  12 ++-
 tests/fixtures/corpus-mini/expected.json      |  13 ++-
 tests/test_score_corpus.py                    |  61 ++++++++---
 tests/test_stage2a_scorer.py                  |   6 +-
 tests/test_stage2a_truth.py                   |   6 +-
 tests/test_stage4d_profiles.py                |   5 +-
 tests/test_stage5_calibration.py              |  35 +++++--
 tests/test_stage6_enrich.py                   |  21 +++-
 tests/test_truth_corpus_followup.py           |   6 +-
 tests/test_truth_corpus_followup_r2.py        |   6 +-
 tests/test_truth_corpus_followup_r3.py        |   6 +-
 tests/test_truth_corpus_followup_r4.py        |   6 +-
 tests/test_truth_corpus_followup_r6.py        |   6 +-
 tests/test_truth_corpus_followup_r7.py        |  11 +-
 tests/test_truth_corpus_followup_r8.py        |   6 +-
 tests/test_truth_corpus_followup_r9.py        |  16 ++-
 tests/test_truth_corpus_followup_sol.py       |   6 +-
 25 files changed, 540 insertions(+), 113 deletions(-)
```

No other session's file was edited (`src/id_detector/playlists/**`, `tests/test_playlists.py`,
`README.md`, `idea.cmd`, `src/id_detector/present/theme.py`). `io.py`'s corpus-destination backstop and
the gateway's link, layout and ancestor refusals are untouched.

## First pass: what I could not do, or left for someone else

- The PowerShell gates (`smoke_serve.ps1`, `gate_local_mode.ps1`) were not run, as instructed.
- The slow full-pipeline modules (`test_stage2b_pipeline`, `test_stage4b_transforms_schedule`,
  `test_stage4c_generations`) were not run; nothing they exercise changed except that the generic
  scorer now asks `certification_scope` once for verified, independent truth.
- `docs/STATUS.md` (the "Follow-ups landed (2026-09-15)" paragraph) still says freezing and
  certification are switched off and tells the owner not to freeze or certify `release-1`. I left the
  status page to the orchestrator; it needs one sentence when this lands: the gate is open, freezing
  is still the owner's action once his listening is done.
- No second-model review of this diff has been run yet.
- Accepted residuals from the earlier rounds are unchanged (hostile same-user programs out of scope).
  One new, accident-class note: `refuse_scratch_destination` refuses calibration validation if the
  temp folder itself directly holds a set folder with a `ground_truth.json`; the message says to point
  `TEMP` at an ordinary folder.

## Fix pass: gate closed

The second-model review of the first pass (Codex sol xhigh, read-only, static) returned **FIX_FIRST**:
five realistic P0s, one P1, one P2 and one adversarial P2. It confirmed the new scope rule itself
(canonical path identities, exact inventory, mixed corpora, standalone truth, exposure over every
frozen record, the frozen-ledger check) and the work-root refusals, and found that certification is
still not safe to enable. The orchestrator's decision, for the owner's local-first priority: **close
the gate again and land the fixes with certification off.** Draft scoring already measures accuracy,
and main has the gate closed today, which is the safe state.

| Review item | What was done in this pass |
|---|---|
| Gate | `truth.CERTIFICATION_ENABLED = False` again. `certification_gate_open` opens it for tests of the logic underneath, exactly as on main; the ten modules plus `test_stage4d_profiles`, `test_stage6_enrich`, `test_truth_corpus_followup_r9` and `_r10` are byte-identical to main again. `certification_gate_closed` stays as a fixture that pins the closed state. |
| P0 4 -- scratch inside a manifest-less corpus through an ordinary subfolder | **Reproduced and fixed.** New `truth.scratch_corpus_ancestor`: the gateway's two ancestor checks (`corpus_ancestor`: a corpus file directly in an ancestor, `lstat` only; `_holds_truth_bearing_set`: set folders directly in an ancestor, as `release-1` has) applied at EVERY ancestor up to the filesystem root, not only the nearest existing parent. `refuse_scratch_destination` uses it before anything is created. Each ancestor is listed once and in full, once per calibration validation; the gateway itself stays O(depth) `lstat`. |
| P1 6 -- certify artefacts and registry | **Fixed.** All three outputs (report, predictions, registry) are validated with `work_root` before anything is read, and `certify._publish_certification` revalidates each one with `work_root` immediately before its own write. The report is now written there, not by the scorer, whose revalidation does not know the work root. `idea benchmark certify` prints `evaluated corpus=...` when zero triples certify and `certified corpus=...` only when some did. |
| P2 7 -- `docs/STATUS.md` | **Fixed.** It says certification remains switched off, what landed, and what is required before the gate opens. |
| P0 1, 2, 3, 5 | **Recorded, not built** -- see the next section. None is reachable with the gate closed. |
| Adversarial P2 | Recorded under accepted residual risks below. |

Wording changed for a closed gate: a draft score's reason no longer tells the owner to run
`idea truth freeze` as the next step (it would refuse). It now says to keep checking the tracklists by
ear, and that freezing comes once every one is checked and certification is switched on.

**Tests added or changed in this pass** (`tests/test_certification_followup.py` unless named)

- `test_the_scratch_corpus_is_never_created_inside_a_corpus[an ordinary folder inside a corpus without a manifest]`
  and `[several levels inside a corpus without a manifest]` -- the review's bypass; refused before
  anything is created, both corpora byte-identical, no scratch folder left behind.
- `test_certify_refuses_a_registry_beneath_the_work_root_before_it_opens_any_corpus`
- `test_each_certify_artefact_is_revalidated_with_the_work_root_just_before_its_write` -- the exact
  order guard -> write for predictions, report and registry, each guard carrying the work root, and
  the scorer no longer writing the report.
- `test_idea_benchmark_certify_says_certified_only_when_something_was[0-evaluated]`, `[3-certified]`
- `test_the_production_gate_stays_closed_and_a_whole_clean_corpus_is_not_certifiable` (production
  default closed; the same corpus certifies with the gate opened by monkeypatch)
- `test_a_closed_gate_tells_the_owner_nothing_is_wrong_and_draft_scoring_still_works`
- `tests/test_score_corpus.py`: the corpus-mini exact L3 block expects the disabled message again;
  `test_truth_status_unverified_then_verified_under_a_frozen_manifest` stays parametrised over both
  gate states; the free-recipe summary test is back to main's form (the review objected to a
  positive threshold sentence expected from a document with no scope).
- `tests/test_stage5_calibration.py`: the two certify-refusal tests stay parametrised over both gate
  states.

**Confirmation asked for (item 5):** with the gate closed, the repo-wide emitter scan from `28d8fe1`,
`tests/test_truth_corpus_followup_r10.py::test_no_emitter_claims_certification_while_the_gate_is_closed`,
passes **unchanged** (`git diff` of `_r9.py` and `_r10.py` is empty), together with its gate-open
control and `_r9.py::test_the_production_gate_is_closed`: `3 passed, 1 warning in 17.45s`. No emitter
says `certified`, `certifiable: true`, `thresholds_met: true` or "thresholds are met".

### Reverted-rule proofs (final code)

Each rule disabled in place, its tests run, the file restored and hash-checked byte-identical:

| Toggle (rule disabled in place) | Result |
|---|---|
| X1 run-list scope rule ignored by score_corpus.py (subset certifiable again) | FAILS as required (4 failed, 1 warning in 14.24s) |
| X2 scope judges exposure only over the records the caller named | FAILS as required (3 failed, 1 warning in 11.60s) |
| X3 generic scorer no longer needs the whole frozen corpus | FAILS as required (1 failed, 1 warning in 3.26s) |
| X4 L3 block no longer says why it is not certifiable | FAILS as required (4 failed, 1 warning in 7.53s) |
| X5 scratch built without validating it against the work root and every corpus | FAILS as required (7 failed, 1 warning in 3.84s) |
| X6 calibration validation no longer refuses a temp work root up front | FAILS as required (1 failed, 1 warning in 2.57s) |
| X7 scratch validator ignores the work root | FAILS as required (3 failed, 1 warning in 5.85s) |
| X8 scratch ancestor listing bounded again (fails closed in a crowded temp folder) | FAILS as required (1 failed, 1 warning in 3.02s) |
| X9 certify outputs no longer guarded before anything is read | FAILS as required (2 failed, 1 warning in 1.56s) |
| X10 certify's unfrozen refusal back to the terse message | FAILS as required (1 failed, 1 warning in 1.71s) |
| X11 freeze refusal back to the bare error list | FAILS as required (1 failed, 1 warning in 2.12s) |
| X12 production gate opened | FAILS as required (4 failed, 1 warning in 2.07s) |
| X13 closed gate no longer refuses certify (run_certify ignores the gate) | FAILS as required (2 failed, 2 passed, 1 warning in 1.53s) |
| X14 scratch ancestry checks only the nearest existing parent (review P0 4) | FAILS as required (2 failed, 3 passed, 1 warning in 3.11s) |
| X15 registry no longer revalidated immediately before its write | FAILS as required (1 failed, 1 warning in 1.92s) |
| X16 report written by the scorer again (revalidated without the work root) | FAILS as required (1 failed, 1 warning in 1.89s) |
| X17 certify CLI always prints 'certified corpus=' | FAILS as required (1 failed, 1 passed, 1 warning in 1.84s) |
| X18 closed-gate line loses its plain next step | FAILS as required (1 failed, 1 warning in 2.03s) |

### Gate outputs (final code, gate closed; every run in the foreground and waited for)

`uv run pytest --collect-only -q`:

```text
1980/2077 tests collected (97 deselected) in 3.00s
```

Full default suite in six shards, the a-o/paid/phase shard split at `test_pa*`. The runner asserts
the shards name every `tests/test_*.py` exactly once (plus `tests/idea_web`):

```text
S1  tests/idea_web:                                      377 passed, 1 warning in 290.15s (0:04:50)
S2a test_[a-o]*:                                         238 passed, 1 warning in 102.07s (0:01:42)
S2b test_pa*, test_ph*:                                  425 passed, 1 warning in 346.29s (0:05:46)
S3  test_pl*, test_pr*, test_sc*, test_se*, stage1-3:    355 passed, 25 deselected, 1 warning in 158.66s (0:02:38)
S4  test_stage4* .. test_stage9*:                        280 passed, 1 skipped, 68 deselected, 1 warning in 35.63s
S5  test_t* (truth follow-ups, gateway guard, review):   303 passed, 1 skipped, 4 deselected, 1 warning in 113.19s (0:01:53)
total: 1,978 passed + 2 skipped = 1,980 = the collected total; 97 deselected; 0 failed
```

No known flake fired. The session was interrupted by a usage limit between S3 and S4; no file changed
between the shards (`git status` and ruff were re-checked before S4), so all six ran on the same code.

Slow tests (`-m slow` over `test_stage2a_controlled.py`, `test_truth_corpus_followup_r6.py`,
`test_truth_corpus_followup_r8.py`):

```text
22 passed, 26 deselected, 1 warning in 91.85s (0:01:31)
```

```text
uv run ruff check .             All checks passed!
uv run ruff format --check .    401 files already formatted
uv run python scripts/audit_fixtures.py
                                audited 531 files
                                fixture audit passed
```

Read-only corpus check, through the gateway:

```text
controlled-events-1: sets=145 manifest_file=True frozen=True verified=True whole_frozen_corpus=True corpus_independent=True
controlled-synth-1: sets=25 manifest_file=True frozen=True verified=True whole_frozen_corpus=True corpus_independent=True
dev-1: sets=6 manifest_file=True frozen=False verified=False whole_frozen_corpus=False corpus_independent=None
release-1: sets=7 manifest_file=False frozen=False verified=False whole_frozen_corpus=False corpus_independent=None
```

`git status --short -- data work` is empty, and so is
`git status --short -- profiles data tests/golden docs/schemas`.

`git status --short -uall`:

```text
 M docs/STATUS.md
 M scripts/score_corpus.py
 M src/id_detector/benchmark/scorer.py
 M src/id_detector/calibrate/certify.py
 M src/id_detector/calibrate/validate.py
 M src/id_detector/cli.py
 M src/id_detector/contracts.py
 M src/id_detector/truth.py
 M tests/conftest.py
 M tests/fixtures/corpus-mini/expected-time.json
 M tests/fixtures/corpus-mini/expected.json
 M tests/test_score_corpus.py
 M tests/test_stage5_calibration.py
 M tests/test_truth_corpus_followup_r7.py
?? docs/reviews/followup-certification.md
?? tests/test_certification_followup.py
```

`git diff --stat` (tracked files; the two new files are above):

```text
 docs/STATUS.md                                |  18 +++-
 scripts/score_corpus.py                       |  86 ++++++++++++++--
 src/id_detector/benchmark/scorer.py           | 140 +++++++++++++++++++++++++-
 src/id_detector/calibrate/certify.py          |  89 ++++++++++++----
 src/id_detector/calibrate/validate.py         |  35 ++++++-
 src/id_detector/cli.py                        |  12 ++-
 src/id_detector/contracts.py                  |   6 +-
 src/id_detector/truth.py                      |  95 ++++++++++++++++-
 tests/conftest.py                             |  14 +++
 tests/fixtures/corpus-mini/expected-time.json |  11 ++
 tests/fixtures/corpus-mini/expected.json      |  12 +++
 tests/test_score_corpus.py                    |  44 ++++++--
 tests/test_stage5_calibration.py              |  35 +++++--
 tests/test_truth_corpus_followup_r7.py        |   5 +-
 14 files changed, 535 insertions(+), 67 deletions(-)
```

No other session's file was edited. The golden Local Free output and the fusion code were not touched
(another builder is regenerating the golden in a different worktree). Nothing was committed.

## Required before the certification gate may open

Recorded, **not built**. `CERTIFICATION_ENABLED` must stay `False` until every item here is built,
tested in both gate states, and reviewed.

- **P0 1 -- "thresholds met" must mean the same thing as "certifiable".** In
  `scripts/score_corpus.py`, `l3.thresholds_met` and the printed sentence "all three thresholds are
  met" are worked out from the numbers alone, before the scope is judged. With the gate open, a
  perfect score of a partial or draft run list would say the thresholds are met while also saying it
  is not certifiable. Both must come from the same complete, verified, timed, independent scope as
  `certifiable`; otherwise they are `null` and the wording is development-only. The partial-run-list
  tests must assert that no positive sentence is printed.
- **P0 2 -- freezing must judge independence over the whole corpus first.** `idea truth freeze`
  records each set's `certifiable` flag from that set's own exposure. If any frozen set was reviewed
  with IDea's predictions on screen, every `certifiable` flag in the manifest must be false. Needs a
  mixed-exposure freeze test.
- **P0 3 -- nothing else may carry certification meaning without a corpus behind it.**
  `idea benchmark links-score` (`"pass": true`, status `certified` from any marked JSON),
  `idea benchmark freeze-profiles` (feature `certified: true` from caller-chosen report files) and
  calibrated episode tiers (`fuse/episodes.py` passes on a loaded model's `certified` entries) make
  certification-sounding claims that are not bound to one verified, complete, independent corpus.
  Either bind their inputs to such a corpus, with provenance that is checked, or rename the claims so
  they do not say "certified". Needs open-gate scope tests for all three.
- **P0 5 -- a test version must be reserved before any work is done.** `(profile, test_version)` is
  only written to the registry after the evaluation. Two tabs can both pass the check and evaluate
  twice, and a crash after provider work leaves the version reusable. It must be durably reserved
  and locked before any evaluation or provider work, the lock held through publication, and a
  concurrent or after-dispatch retry under the same version refused. Needs a two-process test and a
  crash-then-retry test.

## Accepted residual risks

Unchanged from the truth-corpus rounds (owner decision 2026-09-14: hostile same-user local programs
are out of scope), plus one from this review:

- **Adversarial P2 -- the temp-parent swap.** Between `refuse_scratch_destination` validating the
  scratch path and `os.mkdir` / `shutil.rmtree` acting on it, a hostile program running as the owner
  could swap a folder on that path for a junction. The validated parent is not pinned or
  identity-checked through creation and cleanup. Accidents cannot cause this; it needs a deliberately
  racing program. Recorded, not built.
- Accident-class note, conservative by design: the scratch check now lists every ancestor of the temp
  folder once. An ordinary ancestor that directly holds a folder with a `ground_truth.json` in it
  (for example a user profile folder holding a stray copied set) is treated as a corpus, and
  calibration validation is refused with a message that says to point `TEMP` at an ordinary folder.

## Fix pass: what I could not do

- The PowerShell gates were not run, as instructed. The slow full-pipeline modules
  (`test_stage2b_pipeline`, `test_stage4b_transforms_schedule`, `test_stage4c_generations`) were not run.
- No second-model review of this fix pass has been run yet.
