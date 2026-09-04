# Stage 4b — Transforms & schedule

Date: 2026-09-04  
Plan: revision 5.2, Stage 4b row only  
Corpus: `controlled-synth-1` (25 sets, 56 audited boundaries)

## Outcome

Stage 4b is implemented. The pipeline can generate `none`, `resample`, `tempo`, and
`pitch` siblings with the revision-5 algebra and rational sample maps. It exposes all 18
schedule/phase combinations, selects one observation per logical trial before alignment while
retaining rejected hypotheses as provenance-only `rejected_evidence`, and includes a reproducible
local benchmark comparing `off` with `global` transforms.

The recorded defaults (plan rev 5.2) are:

- generation-0 schedule: **12,000 ms window, 9,000 ms hop, phase 0**;
- rescan policy (a config value consumed in Stage 4c): **12,000 ms window, 5,000 ms hop, phase 0**;
- transform grid: `rate_e4 = [9200, 9600, 10400, 10800]` for both `resample` and
  `tempo`, plus `semitones = [-2, -1, 1, 2]` for `pitch`;
- operational policy: `rescan_only` (the benchmark uses `global` to measure the upper-bound
  benefit and cost).

The decision and complete machine-readable evidence are in
`data/corpus/controlled-synth-1/transforms-schedule.json` (sha256
`d1dc7ba15985629a4f7f7ee4fe1a2f077e955b9eb301d187485d15733b882e81`, verified byte-identical on a
second run). `id-detector.example.toml` cites that artifact. No Git commit or push was performed.

## What was built

### File map

- `src/id_detector/windows.py`
  - `WindowSchedule`, `TransformGrid`, all 18 schedule options, and the literal
    `hop <= window - L` coverage classifier;
  - corrected source spans: `round(12000 / r)` for `resample`/`tempo`, 12,000 ms for
    `pitch`;
  - revision-5 filters: `asetrate=16000/r,aresample=16000`, chained `atempo=1/r`, and
    `asetrate=16000/p,aresample=16000,atempo=p`;
  - integer `sample_map` coefficients (`10000/rate_e4` for rate transforms and identity for
    pitch), `support_ms`, and 0/100 ms transform uncertainty;
  - Windows-safe asynchronous FFmpeg rendering with argument arrays, a timeout, process-tree
    handling inherited from the Stage 1 launcher, temporary output followed by atomic rename, and
    exact 192,000-frame normalisation; `write_transformed_wav` returns the **pre-normalisation**
    frame count so the vectors can assert on what FFmpeg actually produced;
  - sibling `logical_trial_id` assignment to the `none` window and policy-aware generation.
- `src/id_detector/providers/base.py`, `id-detector.example.toml`
  - validated `transforms.policy = off|rescan_only|global`, grid values, `[schedule]`, and the new
    `[rescan]` table;
  - plan rev 5.2 defaults. Existing exact pins remain `shazamio==0.8.1` and
    `shazamio-core==1.1.2`; the project already contains the Stage 4a `yt-dlp` dependency and
    invokes it through the Python environment.
- `src/id_detector/cli.py`
  - config-aware `analyse` window generation and `benchmark transforms-schedule`; a malformed
    `--config` file is now a usage error, not a traceback.
- `src/id_detector/local_fixture.py`
  - controlled, content-bound recognition whose rate and pitch residuals must be within 3%;
  - a **decoy catalogue entry** (a rate edit pressed 12.5% fast) that a wrong transform hypothesis
    can match instead of the true recording, so the false-match metric has a non-zero achievable
    range;
  - rate-aware anchors used to fit each transform hypothesis.
- `src/id_detector/contracts.py`
  - the observation natural key gains `transform`; `EpisodeRecord` gains `rejected_evidence`.
- `src/id_detector/fuse/alignment.py`, `src/id_detector/fuse/episodes.py`
  - pitch keeps temporal rate 1.0;
  - majority-candidate/smallest-skew selection is applied before episode assignment;
  - proved bounds, `evidence_support_ms`, competition and tiers read **only** the per-trial
    selected observations; rejected siblings are kept in `rejected_evidence`;
  - tiers count `T_ind` (greedy maximum set of non-overlapping trial supports).
- `src/id_detector/benchmark/scorer.py`
  - `score_corpus_detailed` also returns the per-set `ScoreState`, exposing raw numerators.
- `src/id_detector/benchmark/transforms_schedule.py`
  - verifies frozen truth plus every source hash and WAV duration;
  - runs all schedules under `off` and `global`, the Stage 2a scorer, and one-sided 95%
    set-cluster bootstrap comparisons over **raw per-set counts** with zero-denominator sets
    excluded;
  - loads the active and superseded provider configs from `provider_configs/`;
  - records recognition cost independently from accuracy and records hypothesis- and
    episode-level false discoveries against the set's truth works.
- `data/corpus/controlled-synth-1/transforms-schedule.json`
  - reproducible benchmark results, provider-measurement context, selection rule, and recorded
    defaults; passes the fixture audit.
- `README.md`
  - config-policy and benchmark usage.
- `tests/test_stage4b_transforms_schedule.py`
  - every one of the 12 transform factors (raw frame count, sample map, **content marker**, anchor
    bias), actual FFmpeg generation, schedule classification, config policies, the active-`L_min`
    gate, decoy reachability, sibling id uniqueness, committed-result contract, and fixture audit
    coverage.
- `tests/test_stage2b_fuser.py`
  - conflicting transform candidates and skew tie-breaking, proved bounds from selected
    observations only, `rejected_evidence`, and `T_ind` insensitivity to hop density.

## How to run

From the repository root in PowerShell:

```powershell
uv sync --frozen
uv run id-detector doctor
uv run id-detector benchmark transforms-schedule `
  --corpus controlled-synth-1 `
  --out data/corpus/controlled-synth-1/transforms-schedule.json `
  --work-root data/local/work-transforms-schedule
uv run pytest -q tests/test_stage4b_transforms_schedule.py
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
uv run python scripts/audit_fixtures.py
```

To apply the operational defaults, copy the transform, schedule and rescan tables from
`id-detector.example.toml` into `id-detector.toml`, then run:

```powershell
uv run id-detector analyse <URL> --config id-detector.toml
```

`off` sends only the `none` sibling. `global` sends the full grid on normal scheduled windows.
`rescan_only`, the default, sends `none` on the global pass and makes the grid available to the
Stage 4c rescan plan.

## Verification

### Transform insertion vectors

The Stage 2a synthesiser rendered a known stem independently at each of four native rate factors
for both `resample` and `tempo`, and at each of four semitone factors for `pitch`. Each vector then
sliced the transformed mix and applied the production undo transform. A second, independent vector
set writes silence carrying a single 30 ms 1 kHz marker at a known original sample, applies the
same production undo, and locates the marker. The tests assert:

- the **raw** FFmpeg output length (before the fixed-length normalisation) is inside an explicit
  per-family budget: 16 samples (1 ms) for `resample`, 1,600 samples (100 ms — the declared WSOLA
  `uncertainty_ms`) for `tempo` and `pitch`;
- the published file is 12,000 ms, 192,000 samples at 16 kHz, within the allowed one sample;
- mapped first/last output samples agree with the rational `sample_map` within one source sample;
- **content**: the known marker lands at the inverse `sample_map` position within the same
  per-family budget;
- **anchor bias**: for every factor, an observation built by the production adapter from a recorded
  Shazam response anchors on that sibling's own `support_ms[0]`, applies the active
  `provider_configs/shazam-v3.json` `adapter_bias_ms`, and reports
  `uncertainty_ms >= adapter_bias_uncertainty_ms` with `reliable = true`;
- the local recogniser's fitted anchor slope agrees with the hypothesis within 2%;
- `support_ms` is exactly the plan formula;
- uncertainties are 0 ms for `resample` and 100 ms for `tempo` and `pitch`.

Observed worst cases (recorded here so the budgets are auditable):

| family | worst raw-length deviation | worst marker error |
|---|---:|---:|
| `resample` | 5 samples (0.3 ms), `resample-10400` | 3 samples (0.2 ms), `resample-9200` |
| `tempo` | 588 samples (36.8 ms), `tempo-10800` | 568 samples (35.5 ms), `tempo-10800` |
| `pitch` | 620 samples (38.8 ms), `pitch--2` | 593 samples (37.1 ms), `pitch--2` |

The WSOLA families show a systematic negative offset — a fixed `atempo` latency — well inside the
plan's declared 100 ms uncertainty, and `resample` behaves as the exact rational map predicts.

```text
> uv run pytest -q tests/test_stage4b_transforms_schedule.py
.........................................................                [100%]
57 passed, 1 warning in 16.41s
```

The warning is pydub importing Python 3.12's deprecated `audioop`; it does not affect the vectors.

### Provider latency and coverage

The active `provider_configs/shazam-v3.json` measured p50/p90/p95 `L_min` as 3 seconds. The
superseded v1 and v2 reports measured 6/9/12 seconds. Per plan rev 5.2 **only the active value
gates** coverage-completeness; the superseded configurations are loaded from `provider_configs/`
(never hard-coded) and reported as an extra column. With `hop <= window - L`:

| window/hop/phase (s) | complete L=3 (active, gates) | complete L=6 (v1/v2, reported only) | requests off | requests global |
|---|---:|---:|---:|---:|
| 6/5/0 | no | no | 124 | 1,512 |
| 6/5/2.5 | no | no | 100 | 1,200 |
| 6/9/0 | no | no | 75 | 875 |
| 6/9/4.5 | no | no | 74 | 862 |
| 6/15/0 | no | no | 71 | 823 |
| 6/15/7.5 | no | no | 51 | 563 |
| 8/5/0 | yes | no | 100 | 1,200 |
| 8/5/2.5 | yes | no | 100 | 1,200 |
| 8/9/0 | no | no | 74 | 862 |
| 8/9/4.5 | no | no | 73 | 813 |
| 8/15/0 | no | no | 52 | 576 |
| 8/15/7.5 | no | no | 51 | 563 |
| 12/5/0 | yes | yes | 77 | 901 |
| 12/5/2.5 | yes | yes | 77 | 901 |
| **12/9/0** | **yes** | no | **73** | **813** |
| 12/9/4.5 | yes | no | 51 | 563 |
| 12/15/0 | no | no | 51 | 563 |
| 12/15/7.5 | no | no | 47 | 511 |

Six schedules are coverage-complete at the active 3-second value, including the plan's
generation-0 12/9 default. The previous revision of this report eliminated 12/9 with the
superseded 6-second value; that gate is gone.

### Controlled benchmark and recorded decision

Observed benchmark command:

```text
> uv run id-detector benchmark transforms-schedule --corpus controlled-synth-1 --out data/corpus/controlled-synth-1/transforms-schedule.json --work-root data/local/work-transforms-schedule
benchmarked 18 schedules with off/global policies; rescan-policy=12000/5000/0; report=data\corpus\controlled-synth-1\transforms-schedule.json
```

For the generation-0 12/9/0 schedule:

| metric (e4; 10000 = 1.0) | off | global | paired delta | one-sided 95% lower bound | n sets (excluded) |
|---|---:|---:|---:|---:|---:|
| work precision | 10,000 | 9,333 | 0 | 0 | 13 (12) |
| work recall | 5,714 | 10,000 | +4,286 | +2,727 | 25 (0) |
| segment precision | 8,196 | 8,174 | 0 | 0 | 13 (12) |
| segment recall | 5,397 | 9,678 | +4,281 | +2,608 | 25 (0) |

The paired statistic is now computed from **raw per-set counts** (`correct`/`predicted` and
`correct`/`truth`; `tp`/`tp+fp` and `tp`/`tp+fn`), and a set whose denominator is zero under either
arm is excluded rather than scored as 0. Twelve of the 25 sets emit no prediction at all under
`off` — those are exactly the transformed sets — so the precision comparison legitimately rests on
13 sets and its true delta is 0, not the `+4,800` the previous revision reported. That figure was
an artefact of averaging `0/0` as 0; the earlier "duration weighting" explanation was wrong.

The one-sided bootstrap clusters on sets (`seed 20260904`, 2,000 resamples, zero margin).

Cost rises from 73 requests / 73 logical trials under `off` to 813 requests for the same 73 trials
under `global` (11.14 hypotheses/trial; short tail windows omit variants whose required source
span is unavailable).

### False matches

The local fixture recogniser now holds a **decoy** catalogue entry beside the controlled truth: an
unlicensed rate edit of the same performance pressed 12.5% fast (`residual_rate_e4 = 11250`). A
window matches the decoy when, after the undo, both its temporal and its frequency residual land
within the same ±3% tolerance of the decoy ratio — exactly the rule used for the true recording,
just centred elsewhere. The ratio was chosen so that **no untransformed query can reach it**: the
largest residual any `none` query can present is 1.08. The decoy is therefore reachable only
through a wrong rate hypothesis, and a match is counted as false when its label names no work in
that set's truth (a truth-based predicate, not a bookkeeping prefix).

Measured on this corpus:

| policy | hypotheses | matches | false matches | hypothesis false-match rate | episode FDR |
|---|---:|---:|---:|---:|---:|
| 12/9/0 `off` | 73 | 43 | 0 | 0 | 0 |
| 12/9/0 `global` | 813 | 76 | 3 | 0.0394 | 0.0667 |
| all 18 schedules `off` | 1,321 | 773 | 0 | 0 | 0 |
| all 18 schedules `global` | 15,301 | 1,401 | 68 | 0.0485 | 0.0667 |

Two sets (`controlled-016-resample-10400` and `controlled-017-resample-10800`) are reachable by the
grid; each is matched by exactly one wrong hypothesis. This is a **measurement of this fixture
catalogue**, not an estimate of open-world false-match risk: the magnitude is a property of the
decoy we registered. What it does establish is that the metric has a non-zero achievable range and
that the `off` arm's zero is a fact about the policy rather than about an unsatisfiable predicate.

### Recorded defaults

Generation 0 keeps the plan's 12/9/0 schedule: it is coverage-complete at the active measured
`L_min`, and it is the hop the provisional tier thresholds were calibrated against. The rescan
policy is the best coverage-complete schedule **at the production window length**, so a rescan
changes only hop and phase and its supports stay comparable with generation-0 supports; that is
12/5/0. The artifact also records `best_schedule_any_window_length` = 8/5/0, which has slightly
higher controlled segment recall (9,787 vs 9,721) at 1,200 rather than 901 requests, so the
shorter-window option is visible rather than silently discarded.

The transform grid is **not** selected by this benchmark: the controlled corpus is rendered at
exactly the grid's factors, so the comparison can measure the grid's cost and benefit but cannot
choose it. The grid is fixed by the plan, and this is now stated in the artifact's
`grid.provenance` field and in `selected_defaults.decision`.

The full grid converts work recall from 0.5714 to 1.0, but its 11.1x request multiplier, and the
false matches it creates, make `global` an unjustified production default. The chosen
`rescan_only` policy preserves the measured transform benefit for targeted gaps while keeping the
initial pass at one hypothesis per trial.

### Per-trial selection and proved bounds

Unit vectors provide conflicting candidate variants in one logical trial. Only a variant agreeing
with the majority candidate is eligible; among eligible variants the minimum
`abs(frequencyskew) + abs(timeskew)` wins. Proved bounds, `evidence_support_ms`, the competing-
candidate test and `T_ind` are computed from the winners only. All other matches — including
minority candidates naming a different track — are recorded in the new `episode.rejected_evidence`
list and flagged `hypothesis_rejected`; they cannot move a bound or seed a separate episode.

## Review fixes

Applied from `docs/reviews/code-review-stage-4b.md`.

| # | Finding | Change | Test |
|---|---|---|---|
| P0 | Rejected and minority-candidate observations proved another candidate's boundary | `fuse/episodes.py` computes `raw_supports`, `supports`, `all_supports` and the tier count from `votes` (the per-trial selected observations); `evidence` now lists the votes and hints, and the rejected siblings move to the new `EpisodeRecord.rejected_evidence` (contract, schema, goldens, export entry `n_rejected_hypotheses`) | `test_episode_proofs_use_only_the_selected_observation_of_each_logical_trial`, `test_rejected_minority_candidate_never_proves_the_winner_bound` (minority sibling support `(9000, 20111)`; the bound stays at the winner's `21000`) |
| P1 | Insertion vectors could not fail: output length forced, map check a tautology, no anchor-bias assertion | `write_transformed_wav` returns the pre-normalisation frame count; vectors assert it against explicit per-family budgets, assert a known marker lands at the inverse `sample_map` position after the production undo, and assert the adapter's anchor bias per factor. Observed worst cases recorded above | `test_known_insertions_apply_every_undo_factor_with_exact_duration_and_maps`, `test_known_marker_lands_at_the_inverse_sample_map_after_the_production_undo`, `test_transform_sibling_anchors_apply_the_measured_adapter_bias` (12 factors each) |
| P1 | Committed precision deltas were an artefact of scoring `0/0` as 0 | `score_corpus_detailed` exposes per-set raw counts; `_metric_pairs` passes `(numerator, denominator)` and drops zero-denominator sets; artifact regenerated and byte-reproducible; report corrected (the delta is 0 over 13 sets, not `+4,800`) | `test_policy_window_counts_and_committed_benchmark_contract`, plus the committed artifact's `n_sets_excluded_zero_denominator` fields |
| P1 | Reported false-match rate was zero by construction | Decoy identity added to the fixture recogniser (reachable only by a wrong hypothesis); the false-match predicate is now truth-based (label not among the set's truth works) | `test_false_match_metric_can_be_non_zero_and_off_policy_stays_clean` |
| P1 | Duplicate observation natural keys once a window has siblings | Observation natural key gains `transform` (null for scanners) in `contracts.py` and every producer; goldens and dependent assertion ids regenerated; the plan's contract line updated | `test_sibling_observations_have_unique_ids_and_one_vote_per_distinct_window` (20 s silence + `global`: one distinct `wav_sha256`, one observation per window, all ids distinct), `test_majority_vote_counts_each_window_once` |
| P1 | Default hop 9,000 → 5,000 inflated the work tier; report claimed no deviation | `DEFAULT_HOP_MS` back to 9,000; 12/5/0 recorded as the new `[rescan]` config table; only the active `L_min` gates selection (superseded configs loaded from `provider_configs/`, reported as columns); tiers count `T_ind` by greedy interval scheduling | `test_config_defaults_and_policy_switches`, `test_example_config_matches_the_recorded_stage_4b_defaults`, `test_only_the_active_l_min_gates_schedule_selection`, `test_dense_overlapping_hops_do_not_inflate_the_work_tier`, and the reverted `tests/test_stage2b_pipeline.py` badge expectation |
| P2 | Benchmark cannot "choose the grid" | Artifact gains `grid.provenance`; `selected_defaults.decision` and this report say the grid is fixed by the plan and only measured here | `test_policy_window_counts_and_committed_benchmark_contract` (grid block) |
| P2 | `analyse --config` leaked a traceback | `_load_app_config` reports a bad file as exit code 2 with a redacted message; used by `analyse` and `benchmark shortlist` | `test_config_defaults_and_policy_switches` covers the validation errors the helper reports |
| P2 | `historical_l_min_ms` hard-coded, v2 used as an active gate | Superseded configs come from `load_provider_configs(project_root)`; `coverage_reported_for_l_ms` is derived, and only the active value carries `gates_selection` | `test_only_the_active_l_min_gates_schedule_selection` |
| P2 | No per-observation `hypothesis_rejected` marker | `episode.rejected_evidence` carries the ids explicitly; exports report `n_rejected_hypotheses` | `test_rejected_minority_candidate_never_proves_the_winner_bound` |
| P2 | Contract test `pytest.skip`s when the artefact is missing | The committed decision record is asserted to exist | `test_policy_window_counts_and_committed_benchmark_contract` |

### Why the earlier POSSIBLE → LIKELY badge change was wrong

The previous revision changed `tests/test_stage2b_pipeline.py:153` from `POSSIBLE` to `LIKELY`
because the default hop had been halved. That was a **density effect, not new evidence**: the same
75-second fixture, the same single audible episode and the same recogniser produced more
overlapping 12-second windows, so the old `T` (a plain count of logical trials) crossed the
`T >= 4` threshold. No additional independent evidence existed. The expectation is reverted to
`POSSIBLE`, and rev 5.2's `T_ind` now makes the tier structurally immune to hop density: trials
whose `support_ms` intervals overlap contribute one, not many.

### Disputed findings

None. Every finding in the review was reproduced and accepted.

### Deferred

- **P2 (Stage 4c friction) — the benchmark requires `data/local/`.** Running
  `benchmark transforms-schedule` needs the uncommitted controlled renders under
  `data/local/controlled/`, by design of the committed-corpus policy: the audio may not be
  committed. Making the benchmark runnable from a clean clone needs a separate render-then-verify
  entry point and belongs with the Stage 4c corpus work.
- **P2 (Stage 4c friction) — stale generation-0 WAVs are never pruned.** `generate_windows_async`
  rewrites `windows.gen0.jsonl` but leaves WAVs from a previous schedule or policy in
  `windows/gen0/`. They are never referenced (records are matched by hash) but they waste disk.
  Pruning interacts with the generation loop's caching, which Stage 4c owns.
- **P2 — `--config` default is CWD-relative.** `Path("id-detector.toml")` resolves against the
  working directory. This is deliberate (the file is per-checkout and Git-ignored) and every other
  path option behaves the same way; changing it to a search order is a CLI-wide decision, not a
  Stage 4b fix.

## Plan-silent implementation decisions

- The 18-way benchmark uses content-bound planned-window tokens in memory after verifying the
  controlled truth is frozen and every backing audio SHA-256 and duration. This avoids writing and
  decoding more than 15,000 redundant benchmark WAV files. The production FFmpeg path is tested
  independently for every factor using actual Stage 2a-rendered audio and, for the content
  assertion, a purpose-built marker signal.
- FFmpeg receives exactly the specified filtergraphs. Its output is then padded or trimmed at the
  end if needed to make the file exactly 192,000 frames; the vectors assert on the raw length
  before that step.
- A pitch correction keeps temporal slope 1.0 because its compensating `atempo=p` retains tempo.
- A non-`none` tail sibling is omitted when its corrected original span would extend past the
  decoded media. The logical trial remains present through its `none` sibling, and request counts
  expose the omission.
- The rescan schedule is selected among schedules at the production window length, so that only
  hop and phase change; the unconstrained best is reported alongside.

## Deviations from plan

The plan's `observation` natural key and `episode` field list were updated in `docs/PLAN.md` to
carry the two contract changes rev 5.2 requires (`transform` in the observation key,
`rejected_evidence` on the episode). No other plan text changed.

The optional live spot-check was not run: no network request was needed for acceptance, and the
controlled benchmark plus all insertion vectors are network-free.

## Known gaps

- The decoy makes the false-match metric measurable but does not make it *representative*. The
  local recogniser still cannot estimate real Shazam's open-world false-match distribution. A
  future budgeted live experiment needs unrelated negative windows as well as transformed
  positives.
- The `rescan_only` configuration and rescan transform payload are ready, but executing the full
  multi-generation rescan loop belongs to Stage 4c. `build_episodes` still emits its own
  8,000/4,000/2,000 `RescanPolicy`; wiring the `[rescan]` config into the emitted plan is Stage
  4c's job.
- The benchmark corpus has 56 boundaries, below Stage 4c's eventual requirement of at least 100.
- No Stage 4b live spot-check result is claimed.

## What Stage 4c needs to know

- Consume the grid already placed in each rescan request when policy is `rescan_only`; keep all
  siblings under the `none` window's `logical_trial_id`.
- Read the rescan window/hop/phase from `AppConfig.rescan_*` (`[rescan]`, default 12,000/5,000/0)
  and feed it into the emitted `RescanPolicy`.
- Use asynchronous `generate_windows_async` in pipeline code. The synchronous wrapper exists only
  for non-async compatibility.
- Preserve `episode.rejected_evidence` through later generations and file-scanner fusion, and keep
  proved bounds off it.
- `T_ind` is computed over `support_ms` intervals, so a denser rescan raises a tier only when it
  produces genuinely disjoint support.
- Treat `data/corpus/controlled-synth-1/transforms-schedule.json` as the Stage 4b decision record:
  generation 0 at 12/9/0, rescans at 12/5/0, the full grid for rescans, and `rescan_only` globally.
- Do not interpret the controlled false-match numbers as an open-world guarantee; add negatives
  when evaluating scanner/transform ablations and report physical requests separately.

## Required repository checks

Observed full suite:

```text
> uv run pytest -q
........................................................................ [ 18%]
........................................................................ [ 36%]
........................................................................ [ 54%]
........................................................................ [ 72%]
........................................................................ [ 90%]
......................................                                   [100%]
398 passed, 3 deselected, 1 warning in 76.34s (0:01:16)
```

The three deselections are tests marked `live`, skipped by the default pytest marker expression.

Observed static and lock checks:

```text
> uv run ruff check .
All checks passed!

> uv run ruff format --check .
111 files already formatted

> uv lock --check
Resolved 53 packages in 4ms
```

Final fixture audit, including this report:

```text
> uv run python scripts/audit_fixtures.py
audited 168 files
fixture audit passed
```
