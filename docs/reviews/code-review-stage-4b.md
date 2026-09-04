# Stage 4b code review — transforms & schedule

*Reviewer: Claude (Opus) — Codex usage limit reached 2026-09-04. Same review prompt as prior stages; reviewer ran read-only.*

Reviewed: `git status --short` (14 modified, 4 untracked), full `git diff`, all untracked files, `docs/PLAN.md` §§ Window schedule / Transform hypotheses / Alignment / window+observation+episode contracts, `docs/stage-reports/stage-4b.md`, `stage-1.md`, `stage-2b.md`.

The engineering here is largely solid: the `12000/r` spans, rational integer sample maps, one-sided bounds, natural keys, `logical_trial_id` grouping, 18-way schedule enumeration and the literal `hop ≤ window − L` classifier all match the plan; the committed decision record is byte-reproducible (verified below) and contains no handles, URLs, platform IDs or raw lines. The problems are concentrated in (a) one new fusion regression and (b) a benchmark/test layer that reports several numbers it cannot actually measure.

### [P0] Rejected and minority-candidate observations now prove another candidate's boundary

**What.** `_assign_observations` was changed so that every final match in a logical trial is re-labelled with the selected variant's candidate, then dropped into that candidate's bucket. The bucket (`evidence`) is used verbatim for `proved_bounds()` and `evidence_support_ms`. So an observation that (i) the fuser rejected as a hypothesis and (ii) identified a different track contributes its `support_ms` to the winner's proved bound. Demonstrated: minority sibling `resample-10800`, support `(9000, 20111)`; winner's own support `(9000, 21000)` → `start_no_later_than_ms: 20111` proved from the rejected sibling. The plan makes proved bounds conditional on identity and keeps one point per logical trial, retaining the rest only as `hypothesis_rejected` evidence.

**Where.** `src/id_detector/fuse/episodes.py:133-152`, `:313`, `:317`.

**Fix.** Compute `proved_bounds` and `evidence_support_ms` from the per-trial selected observations ("votes"), not from `evidence`. Keep rejected/minority records for provenance only. `tests/test_stage2b_fuser.py:236` should assert the bound equals the selected variant's support end with a minority sibling whose support differs.

### [P1] The insertion vectors cannot fail: output length is forced, and the map check is a tautology

**What.** `write_transformed_wav` calls `_force_wav_sample_count`, which zero-pads or truncates to exactly `output_samples`, so `abs(output_samples − 192_000) <= 1` can never fail (raw FFmpeg output deviates by up to −91 samples for `tempo-10800`; a half-length input still yields exactly 192,000 frames with a silent tail). First/last mapped-sample checks are arithmetic over the same `transform_spec` and inspect no audio. No anchor-bias assertion exists. The slope test runs on synthetic tokens against a recogniser that derives anchors from the same `set_id` it is checked against. The stage report presents all four as verified.

**Where.** `src/id_detector/windows.py:254-276`, `:326`; `tests/test_stage4b_transforms_schedule.py:214`, `:222-223`, `:269-302`; stage report "Transform insertion vectors".

**Fix.** Assert on the pre-normalisation frame count with an explicit tolerance and record the observed worst case. Add a content assertion per factor: render a mix with a known marker at a known original sample, apply the production undo, assert the marker lands at `sample_map⁻¹` within budget.

### [P1] The committed precision deltas are an artefact of scoring 0/0 as 0

**What.** `_metric_pairs` feeds `paired_non_inferiority` per-set ratios; `_ratio_e4` returns 0 for a 0 denominator, so 12 sets with zero predictions get precision 0, producing `work_precision delta_e4: 4800` when both arms are 10000. The report attributes the gap to duration weighting, which is wrong.

**Where.** `src/id_detector/benchmark/transforms_schedule.py:228-251`; `src/id_detector/benchmark/scorer.py:622-625`; committed `transforms-schedule.json`; stage report.

**Fix.** Pass raw `(correct, predicted)` / `(correct, truth)` counts per set; exclude 0-denominator sets; regenerate the artefact; correct the report.

### [P1] The reported false-match rate is zero by construction

**What.** `_run_policy` counts a false match as a hit whose `source_ids` lack `controlled-truth:`, but every fixture hit carries that prefix — the predicate is unsatisfiable (15,301 hypotheses, 1,333 matches, 0 false, no code path to non-zero). The report calls it "the measured multiple-testing result".

**Where.** `src/id_detector/benchmark/transforms_schedule.py:180-183`; `src/id_detector/local_fixture.py:276`.

**Fix.** Give the fixture recogniser a decoy identity that can match a different set's stem within tolerance, or drop the number and state the harness cannot estimate a false-match rate.

### [P1] Duplicate observation natural keys once a window has siblings

**What.** Byte-identical sibling WAVs share one cache key; the query fans out to every window; the observation key omits the transform, so `none` and the four `pitch` siblings collide on all components and receive the same id (20 s silence + `global` → 22 windows, 1 distinct `wav_sha256`). Duplicates bias the majority vote in `select_logical_trial_points`.

**Where.** `src/id_detector/recognise.py:429`, `:449`, `:225`, `:239`; `src/id_detector/contracts.py:997`; `src/id_detector/fuse/alignment.py:143-158`.

**Fix.** Add `transform` (or window id) to the observation natural key; add invariant tests for id uniqueness and that the majority vote is over distinct windows.

### [P1] Default hop 9,000 → 5,000 inflates the work tier, and the report claims no deviation

**What.** Halving the hop nearly doubles `T` on identical evidence (`tests/test_stage2b_pipeline.py:153` badge POSSIBLE → LIKELY with no new evidence); the plan's default is still 12/9 and the report says "Deviations: None". The `coverage_complete_l6` gate (from superseded v2 `L_min`) is what eliminates 12/9 contrary to Stage 1's guidance.

**Where.** `src/id_detector/providers/base.py` `DEFAULT_HOP_MS`; `id-detector.example.toml`; `tests/test_stage2b_pipeline.py:153`; `docs/PLAN.md` window schedule; stage report.

**Fix (decided in plan rev 5.2).** Gen-0 default stays 12/9; 12/5/0 becomes the rescan policy; only the active `L_min` gates; tiers count `T_ind` (non-overlapping supports).

### [P2] The benchmark cannot "choose the grid" — the corpus factors are the grid
### [P2] `analyse --config` leaks a traceback and its default path is CWD-relative
### [P2] `historical_l_min_ms` is hard-coded and v2 is used as an active gate
### [P2] Stage 4c friction: no per-observation `hypothesis_rejected` marker; benchmark needs `data/local/`; stale gen-0 WAVs never pruned; contract test `pytest.skip`s when the artefact is missing

## Verified
`ruff check` clean; `ruff format --check` 110 files; `uv lock --check` ok; fixture audit 167 files passed; `pytest -q` 367 passed / 3 deselected; doctor all PASS (Panako WARN: JDK not found). Benchmark re-run byte-identical to the committed artefact (sha256 `08b816e0…`). Privacy clean. Windows file/process handling: no defects found.

REVIEW VERDICT: FIX_FIRST
