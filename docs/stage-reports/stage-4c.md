# Stage 4c — Rescans, scanners, events

Date: 2026-09-04
Plan: revision 5.2, Stage 4c row only
Corpora: `controlled-events-1` (new, 145 sets / 356 boundaries / ≥30 cases per event type) and
`controlled-synth-1` (unchanged 25-set case set, refreshed truth)

## Outcome

Stage 4c is implemented. The orchestrator owns a real generation loop, rescan policies are
per-trigger and budget-aware, spectral-novelty change points are computed locally from the
canonical PCM, file-scanner observations fuse through the same fuser with the plan's
second-commercial-engine dependence prior, the event state machine has an explicit truth contract
and is measured per type, Panako stays excluded, and the controlled ablations are committed.

**All four acceptance gates in the Stage 4c task are met on the controlled stratum.** Numbers are
in [Acceptance gates](#acceptance-gates); the ablation record is
`data/corpus/controlled-synth-1/ablations.json` (sha256
`79c4656676e736b426cfb1913500a42e7fc3b6e338ba96bba3b182371ddf7ecb`, byte-identical on a second
run).

## What was built

### File map

- `src/id_detector/rescan.py` (new) — per-trigger policy table, priority order, region-anchored
  window scheduling, and budget-aware request planning (`plan_within_budget`). The `[rescan]`
  config table is the base policy and clamps every derived policy.
- `src/id_detector/novelty.py` (new) — log-mel flux over the canonical 16 kHz PCM with a 3 s
  median baseline and a MAD scale, configurable z-threshold, minimum separation and event cap.
  Returns integer millisecond change points.
- `src/id_detector/orchestrate.py` (new) — `run_generation_loop`: fuse gen 0, convert
  `fuse/rescan_plan.gen<N>.jsonl` into `windows/windows.gen<N+1>.jsonl` and
  `recognise/queries.gen<N+1>.jsonl`, append `observations.gen<N+1>.jsonl`, re-fuse the union of
  every generation into `fuse/episodes.gen<N+1>.json`, and copy the final generation to
  `fuse/episodes.json`. Stops on `no_requests`, `max_generations`, `budget_exhausted` or
  `no_new_windows`.
- `src/id_detector/fuse/scanners.py` (new) — `scanner_logical_trial_id(provider, chunk_index)`,
  the documented anchor-conversion registry, `merge_engine_observations` (a union, never a
  cascade), and `engine_independence` (which commercial engine carries the 0.5 prior).
- `src/id_detector/benchmark/ablations.py` (new) — the Stage 4c arm runner, paired cluster
  bootstraps (including a relative-p90 statistic), per-type event metrics with cluster bounds,
  per-engine status rows, and the gate evaluation written to `ablations.json`.
- `src/id_detector/fuse/episodes.py` — `T_ind` now returns ten-thousandths and applies the
  dependence prior; per-trigger rescan policies replace the single hard-coded policy; `novelty`
  requests and `gap.evidence.n_novelty_events`; requests are deduplicated by region and suppressed
  when an earlier generation already requested the region or already scanned every window the
  policy would place (this is what makes the loop converge); `fuse_generation` publishes any
  generation and its sidecar lists **every** input generation.
- `src/id_detector/fuse/alignment.py` — the plan's precedence with `jump`/`drift` narrowed to
  within-occurrence gaps, so a >30 s reference recurrence is labelled `replay` instead of being
  consumed by `jump`; `replay` is now emitted as a dated `alignment_event` on the new occurrence.
- `src/id_detector/windows.py` — `plan_rescan_windows`, `generate_rescan_windows_async` (real
  WAVs for generation N), `plan_fixture_rescan_windows` (in-memory benchmark path), and
  deduplication against geometries an earlier generation already scanned.
- `src/id_detector/recognise.py` — `recognise_generation(generation=N)` writes
  `queries.gen<N>.jsonl` / `observations.gen<N>.jsonl` / `raw_index.gen<N>.json`, and a
  **cross-generation content cache** guarantees that one fingerprinted content is submitted
  exactly once even when a later generation produces a byte-identical window.
- `src/id_detector/benchmark/scorer.py` — events are read from the new `ground_truth.events`
  contract instead of the Stage 2a `note` convention; `replay` is scored from the dated alignment
  event (falling back to `occurrence_index > 0`); matching admits the detection lag
  (`[at − 2 s, at + 30 s]`) and the strict ±2 s variant is computed alongside; `event_replay` is a
  new metric.
- `src/id_detector/benchmark/controlled.py` — `--cases events` renders 30 replicates each of
  `ev_loop`, `ev_jump`, `ev_drift` and `ev_replay`, each containing exactly one rendered
  discontinuity with exact truth; `--corpus-version` writes the final corpus version directly.
- `src/id_detector/local_fixture.py` — the event-case reference behaviours, per-window time skew
  for the drift cases, and a corrected anchor: the mix anchor is the first *matched* sample, not
  the window start, so a window that begins before a track becomes audible can no longer
  manufacture a zero-slope segment.
- `src/id_detector/providers/{audd,acrcloud}.py` — both adapters now build their trial ids with
  the shared `scanner_logical_trial_id`; ACRCloud groups music and own-bucket hits at one scan
  offset into **one** logical trial with `simultaneous_source` distinguishing them.
- `src/id_detector/contracts.py` — `TruthEvent` + required `GroundTruthRecord.events`;
  `AlignmentEvent.type` gains `replay`; `BenchmarkMetrics.event_replay` (defaulted so reports
  written before replay scoring existed stay readable).
- `src/id_detector/providers/base.py`, `id-detector.example.toml` — `[rescan].max_generations`.
- `src/id_detector/cli.py` — `analyse --max-generations/--novelty`, `benchmark ablations`,
  `benchmark render --cases/--corpus-version`.
- `data/corpus/controlled-events-1/` (new) — 145 frozen truth sets, `render_manifest.json` and
  `corpus-version.json`. Audio stays local under `data/local/controlled/controlled-events-1/`.
- `data/corpus/controlled-synth-1/ablations.json` (new) — the Stage 4c decision record.
- `tests/test_stage4c_{generations,rescans,scanners,events,ablations}.py` (new).

### Regenerated artefacts

Adding the required `ground_truth.events` field changed every committed truth file, so they were
regenerated rather than hand-patched:

- `data/fixtures/controlled/stage-2a/` re-rendered from `.stage2a-sources` at seed 20260904. The
  25 mixes are byte-identical (every `media_key` is unchanged); only `events`/`note` moved.
  `synthetic_predictions.json` and `benchmark_report.json` were regenerated with it.
- `data/corpus/controlled-synth-1/` truth refreshed from that render and re-frozen.
- `data/corpus/dev-1/` truth gained `"events": []`; its **draft** inventory was regenerated with
  `truth manifest-draft` (still `frozen: false`, still unverified — CR-2B-01 remains blocked).
- `tests/golden/{ground_truth,benchmark_report}.json` and `docs/schemas/*.schema.json`.
- `data/corpus/controlled-synth-1/baseline-free.json` re-run. **This surfaced a stale Stage-2b
  artefact:** it still carried plain-`T` tiers. Under rev 5.2's `T_ind`, a 22 s controlled set
  scanned at 12 s / 9 s has three *overlapping* supports and therefore `T_ind = 1`, so those
  episodes are `unclear`, not `possible` (`empirical_tier_precision_e4.possible` 10000 → 0 on
  those sets). That change was introduced by Stage 4b and is only now reflected in the committed
  baseline. It is also the clearest motivation for rescans: shorter rescan windows produce
  genuinely disjoint supports and raise `T_ind` honestly.

## How to run it

From the repository root in PowerShell:

```powershell
uv sync --frozen
uv run id-detector doctor

# Full multi-generation analysis (rescans are on by default; 0 disables them).
uv run id-detector analyse <URL-or-local-media> --config id-detector.toml
uv run id-detector analyse <URL-or-local-media> --max-generations 3 --novelty
uv run id-detector analyse <URL-or-local-media> --max-generations 0

# Render and freeze the Stage 4c event corpus (audio stays local and uncommitted).
uv run id-detector benchmark render --sources .stage2a-sources `
  --out data/corpus/controlled-events-1 `
  --audio-out data/local/controlled/controlled-events-1 `
  --seed 20260904 --cases events --corpus-version controlled-events-1
uv run id-detector truth freeze --truth data/corpus/controlled-events-1 `
  --corpus-version controlled-events-1 --out data/corpus/controlled-events-1/corpus-version.json

# Stage 4c ablations and acceptance gates.
uv run id-detector benchmark ablations --corpus controlled-events-1 `
  --out data/corpus/controlled-synth-1/ablations.json `
  --work-root data/local/work-ablations

uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
uv run python scripts/audit_fixtures.py
```

## Acceptance gates

Measured on `controlled-events-1` (145 sets, 356 boundaries), seed 20260904, 2,000 set-cluster
bootstrap replicates. Every number below is copied from the committed `ablations.json`.

| Gate | Target | Observed | Met? |
|---|---|---|---|
| controlled stratum boundaries | ≥ 100 | **356** | **met** |
| `best_start` p90 improvement, rescans on vs off (paired) | ≥ 20 % relative | **55.55 %** (10,800 ms → 4,800 ms), one-sided 95 % cluster lower bound **55.55 %** | **met** |
| `loop` precision / recall on ≥ 30 cases | ≥ 80 % each | **100 % / 93.75 %** on **32** cases (lower bounds 100 % / 85.29 %) | **met** |
| `jump` (cue-jump) precision / recall on ≥ 30 cases | ≥ 80 % each | **83.78 % / 100 %** on **31** cases (lower bounds 70.73 % / 100 %) | **met** |
| `drift` precision / recall on ≥ 30 cases | ≥ 80 % each | **100 % / 96.77 %** on **31** cases (lower bounds 100 % / 90.47 %) | **met** |
| `replay` precision / recall on ≥ 30 cases | ≥ 80 % each | **100 % / 100 %** on **30** cases (lower bounds 100 % / 100 %) | **met** |
| no double submission across generations | 0 | **0** — three failure injections, one submission per content | **met** |

Caveats stated plainly:

- The p90 cluster lower bound equals the point estimate because the controlled corpus is highly
  regular: under every bootstrap resample the pooled p90 is 10,800 ms (a 12 s generation-0 window
  ending at 12,000 ms against a truth start of 1,200 ms) versus 4,800 ms (a 6 s edge-rescan window
  ending at 6,000 ms). This is a property of the controlled stratum, not a general guarantee.
- Every event error in the whole corpus comes from the three **legacy Stage 2a** event sets, whose
  renders contain several discontinuities but whose truth records only one. Exactly:
  `controlled-019-loop`, `controlled-021-repeated-section` and `controlled-022-drifting-tempo` each
  emit 2 unmatched `jump` events (6 of the 37 emitted jumps, the whole of the `jump` precision
  shortfall) and each miss their own single truth event (2 `loop` misses, 1 `drift` miss — the
  whole of the `loop` and `drift` recall shortfall). All 120 Stage 4c event replicates, which
  render exactly one discontinuity each, are matched with no spurious event.
- `reset` has **0** truth cases: the Stage 4c task names loop, cue-jump, drift and replay, so no
  reset case was rendered. The detector emitted **0** reset events, so the type is *unexercised*,
  not passing. Adding reset replicates is the obvious next corpus increment.
- Every number is from the content-bound `local_fixture` oracle on synthetic audio. It measures
  the fuser and the state machine; it is **not** an estimate of open-world accuracy, catalogue
  coverage or false-match risk.

## Ablations

Arms are the production pipeline with exactly one capability changed. `rescans_on` is the Stage 4c
default (generation-0 12 s/9 s/0, `transforms.policy = rescan_only`, novelty on,
`max_generations = 3`).

| arm | windows | work recall e4 | work precision e4 | segment recall e4 | segment precision e4 | start p90 ms | generations |
|---|---:|---:|---:|---:|---:|---:|---:|
| `rescans_off` | 583 | 9,189 | 10,000 | 9,346 | 8,520 | 10,800 | 145 |
| `rescans_on` | 37,761 | 10,000 | 9,867 | 9,999 | 8,484 | 4,800 | 413 |
| `rescans_on_no_novelty` | 32,009 | 9,189 | 10,000 | 9,346 | 8,520 | 4,800 | 401 |
| `transforms_off` | 3,489 | 9,189 | 10,000 | 9,346 | 8,520 | 4,800 | 413 |
| `transforms_global` | 44,120 | 10,000 | 9,867 | 10,000 | 8,483 | 4,800 | 413 |
| `schedule_12_5_0` | 37,234 | 10,000 | 9,867 | 9,999 | 8,420 | 4,800 | 413 |
| `schedule_8_5_0` | 39,717 | 10,000 | 9,867 | 9,999 | 8,856 | 4,800 | 413 |

Paired one-sided 95 % cluster bounds (challenger − baseline, margin 0):

| comparison | work recall Δ / lower | segment recall Δ / lower | work precision Δ / lower | best_start p90 |
|---|---|---|---|---|
| rescans on − off | +811 / +470 | +653 / +374 | 0 / 0 (13 zero-denominator sets excluded) | +55.55 % / +55.55 % |
| novelty on − off | +811 / +473 | +653 / +377 | 0 / 0 | 0 |
| transforms `rescan_only` − `off` | +811 / +470 | +653 / +371 | 0 / 0 | 0 |
| transforms `global` − `rescan_only` | 0 / 0 | +1 / 0 | 0 / 0 | 0 |
| schedule 12/5/0 − 12/9/0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 |
| schedule 8/5/0 − 12/9/0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 |

The most useful finding for Stage 4d: **novelty triggers are what make the transform grid
reachable at all.** A controlled set whose track is rate- or pitch-shifted produces *no*
generation-0 evidence, so it has no episode, no edge trigger and (being 22 s long) no ≥ 45 s gap.
Without a novelty trigger it is never rescanned and the transform grid never runs on it: the
`rescans_on_no_novelty` and `transforms_off` arms both sit at 9,189 e4 work recall, exactly the
`rescans_off` value. With novelty on, recall reaches 10,000 e4 at the cost of 133 e4 of work
precision. `transforms_global` buys nothing over `rescan_only` here (+1 e4 segment recall) for
17 % more windows, which supports the Stage 4b default.

### Per-engine ablation status

| engine | capability | status | in ablation |
|---|---|---|---|
| `local_fixture` | `clip_recognizer` | evaluated (controlled oracle) | yes |
| `shazam` | `clip_recognizer` | not evaluated — no controlled-stratum coverage (Stage 3: 73 attempts, 1 false identification, work P/R 0/0 on synthetic audio) | no |
| `audd` | `file_scanner` | not evaluated (no credentials); fusion validated on authored fixtures | no |
| `acrcloud` | `file_scanner` | not evaluated (no credentials); fusion validated on authored fixtures | no |
| `panako` | `local_index_query` | **excluded from v1 pending JDK** (`doctor`: `JDK not found — Panako disabled`) | no |

Not evaluable: **hints on/off**. The controlled stratum carries no hint evidence and the held-out
`dev-2` corpus does not exist, so the Stage 4a gate remains the authority and remains blocked on
that corpus. `ablations.json` records this explicitly under `not_evaluable`.

## Rescan policies

| trigger | window ms | hop ms | phase ms | priority | source |
|---|---:|---:|---:|---:|---|
| `gap` | 8,000 | 4,000 | 2,000 | 100 | plan: shorter window + shifted phase |
| `question_cluster` | 8,000 | 4,000 | 2,000 | 95 | as `gap` |
| `contested` | 12,000 | 5,000 | 0 | 90 | `[rescan]` config table (rev 5.2) |
| `hint_cluster` | 8,000 | 4,000 | 2,000 | 80 | as `gap` |
| `edge` | 6,000 | 3,000 | 0 | 70 | plan: shorter window for boundaries |
| `novelty` | 6,000 | 3,000 | 0 | 65 | as `edge` |
| `long_episode` | 12,000 | 5,000 | 0 | 60 | `[rescan]` config table (rev 5.2) |

Every policy carries the configured transform grid when `transforms.policy != off`. Window lengths
are clamped by `[rescan].window_ms`, so shortening the config shortens every derived policy. The
grid is anchored at the *region* start, which is what shifts a rescan off the generation-0 phase.

Why boundary triggers need a shorter window rather than a denser hop: `start_no_later_than_ms =
min support_ms[1]`. At a fixed 12 s window the earliest support ends at 12,000 ms no matter how
dense the hop is, so the proved start bound cannot improve. This is exactly what the measured p90
shows (10,800 ms at 12/9/0 and at 12/5/0; 4,800 ms once a 6 s edge rescan runs).

## Generation loop

- Termination: no requests / `max_generations` (default 3, `[rescan].max_generations`) / budget
  exhausted. A short synthetic run converges in exactly two generations
  (`test_short_synthetic_run_converges_in_two_generations`).
- Request ids are `sha1(media_key ‖ "rescan_request" ‖ natural_key)` with the natural key
  `(generation, trigger, start_ms, end_ms, policy)`; duplicates within one plan are emitted once.
- A region is not re-requested if an earlier generation already requested it, and not requested at
  all if every window the policy would place has already been scanned.
- `fuse/episodes.gen<N>.json`'s completion sidecar records the sha256 of **every** generation's
  `windows.gen<K>.jsonl` and `observations.gen<K>.jsonl`.
- `fuse/episodes.json` is a plain copy of the final generation with `generation` recorded.

## What was verified and how

### Stage 4c test suites

```text
> uv run pytest -q tests/test_stage4c_generations.py tests/test_stage4c_rescans.py `
    tests/test_stage4c_scanners.py tests/test_stage4c_events.py tests/test_stage4c_ablations.py
54 passed, 1 warning
```

They cover: two-generation convergence; every generation's artefacts and sidecars; rescan windows
marked `rescan` with a request id and never repeating a scanned geometry; deterministic request
ids and byte-identical reruns; budget exhaustion; `--max-generations 0`; three failure injections
with one submission per content; the per-trigger policy table, priority order, region anchoring
and budget accounting; novelty on a known step signal and on a steady tone; scanner trial ids,
null transforms, documented anchor conversions, union merge, the 0.5 second-commercial-engine
prior, scanner-only fusion and Panako unavailability; the replay-before-jump precedence, replay
dating, event-truth contract reading, detection-lag matching; and the committed corpus and
ablation contracts.

### Ablation determinism

```text
> uv run id-detector benchmark ablations --corpus controlled-events-1 --out ... --work-root ...
ablated 8 arms on controlled-events-1 (145 sets, 356 boundaries);
controlled_boundaries_at_least_100=true;
best_start_p90_improves_20_percent_relative_with_rescans=true;
event_loop_precision_and_recall_at_least_80_percent=true;
event_jump_precision_and_recall_at_least_80_percent=true;
event_drift_precision_and_recall_at_least_80_percent=true;
event_replay_precision_and_recall_at_least_80_percent=true
```

A second run into a different output path produced the identical sha256
`79c4656676e736b426cfb1913500a42e7fc3b6e338ba96bba3b182371ddf7ecb`.

### Corpus render

```text
> uv run id-detector benchmark render --sources .stage2a-sources --out data/corpus/controlled-events-1 `
    --audio-out data/local/controlled/controlled-events-1 --seed 20260904 --cases events `
    --corpus-version controlled-events-1
rendered 145 sets and 356 boundaries
> uv run id-detector truth freeze --truth data/corpus/controlled-events-1 --corpus-version controlled-events-1 --out ...
froze 145 sets as controlled-events-1
```

### Live smoke

One live run against the real Shazam adapter, on a 60 s excerpt of an already-downloaded local
mix (`data/local/`, uncommitted), with `transforms.policy = off` so the cost stayed small:

```text
> uv run id-detector analyse data/local/stage4c-live-excerpt.wav `
    --work-root data/local/work-stage4c-live --config data/local/stage4c-live.toml `
    --no-hints --max-requests 60 --max-generations 2
7 matches; 0 failures; 37 physical attempts; 3 generations (stop=no_requests); 1 episodes;
tracklist=...\present	racklist.json
```

Observed artefacts:

| generation | windows | episodes | `best_start_ms` | work tier | requests emitted | triggers |
|---|---:|---:|---:|---|---:|---|
| 0 | 7 | 1 | 12,000 | likely | 3 | `edge`, `novelty` |
| 1 | 29 | 1 | **6,000** | likely | 1 | `edge` |
| 2 | 1 | 1 | 6,000 | likely | 0 | — |

`fuse/episodes.gen2.done.json` lists `windows/windows.gen{0,1,2}.jsonl` and
`observations.gen{0,1,2}.jsonl` with their hashes, and `fuse/episodes.json` is byte-identical to
`fuse/episodes.gen2.json`. 37 physical attempts equals 7 + 29 + 1 windows: one submission per
window, none repeated across generations. No `pytest -m live` test was run.

### Required repository checks

```text
> uv run pytest -q
........................................................................ [ 47%]
........................................................................ [ 63%]
........................................................................ [ 79%]
........................................................................ [ 95%]
....................                                                     [100%]
452 passed, 3 deselected, 1 warning in 117.72s (0:01:57)

> uv run ruff check .
All checks passed!

> uv run ruff format --check .
122 files already formatted

> uv lock --check
Resolved 53 packages in 1ms

> uv run python scripts/audit_fixtures.py
audited 317 files
fixture audit passed

> uv run pytest -q tests/test_stage4c_*.py
54 passed, 1 warning in 33.48s

> git diff --check
exit 0

> uv run id-detector doctor
Panako              WARN    JDK not found - Panako disabled
```

The three deselections are the `live`-marked tests, skipped by the default marker expression. The
single warning is pydub importing Python 3.12's deprecated `audioop`; this project is pinned to
3.12.

## Plan-silent decisions

- **Event detection horizon.** The plan's own alignment rules make event detection strictly
  lagging: the state machine can only label a discontinuity after observing a point past it, and
  `drift` needs "> 3 % over at least 3 points". A prediction therefore matches truth when it lands
  in `[at_ms − 2,000, at_ms + 30,000]`. The strict symmetric ±2 s numbers are computed and
  reported alongside (`strict_2s_precision_e4` / `strict_2s_recall_e4` in `ablations.json`); on
  `rescans_on` they are 100/93.75 for `loop`, 83.78/100 for `jump`, 100/100 for `replay` and
  **0/0 for `drift`**, which is the honest statement that drift is never detectable within 2 s.
- **Event truth contract.** `TruthEvent` is `{type, at_ms, episode_index, note}` at the set level,
  because `replay` is a property of a pair of occurrences, not of one episode. `at_ms` is the
  exact mix time of the rendered discontinuity.
- **Per-trigger geometry.** The plan gives both a `[rescan]` table (12/5/0) and a sentence
  requiring "shorter windows (6–8 s) and shifted phases" for edges and gaps. Both are honoured:
  the config table is the base policy for whole-episode triggers and clamps the shorter
  boundary policies. The exact 6,000/8,000 ms values are ours.
- **Novelty parameters.** 128 ms frames at a 100 ms hop, 40 mel bands, `log1p` compression, a 3 s
  centred median baseline with a MAD scale, z ≥ 3.0, ≥ 4 s separation, ≤ 512 events per media,
  and ±10 s padding merged into non-overlapping rescan regions.
- **Convergence rule.** A rescan region is suppressed when every window its policy would place has
  already been scanned. Without it the loop would run to `max_generations` on every media, because
  an improved boundary always nominates a slightly different edge region.
- **Cross-generation content cache.** Window ids carry the generation, so a later generation that
  fingerprints byte-identical audio would otherwise create a new query id and submit it again.
  Recognition now reuses the earlier generation's stored raw response by cache key.
- **ACRCloud chunk index.** The plan's scanner key is `sha1(provider ‖ chunk_index)`. ACRCloud
  reports one entry per matched bucket, so distinct scan offsets are indexed in ascending order
  and music/own-bucket hits at one offset share a trial with `native.simultaneous_source`
  distinguishing them. AudD already used the chunk ordinal.
- **Dependence prior.** `T_ind` is computed in ten-thousandths. Among the commercial engines
  present for a candidate, the one with the most observations counts full trials and every other
  commercial engine's trials count 5,000 e4 (0.5). Behaviour is unchanged when one engine runs.
- **Ablation corpus.** The ablations run on `controlled-events-1` (which contains the 25
  `controlled-synth-1` cases plus the 120 event replicates) and are written to
  `data/corpus/controlled-synth-1/ablations.json` as the task requires; the artefact records its
  own `corpus_version`.
- **`BenchmarkMetrics.event_replay` has a default** so reports written before replay scoring
  existed (`shortlist.json`, the cached dev-1 seed comparison) remain readable. Every report the
  current scorer writes sets it explicitly.

## Deviations from plan

1. **Event precedence.** The plan lists `continuation → loop → reset → jump → drift → replay →
   outlier`. Taken literally, `replay` is unreachable whenever any point follows it: a returning
   reference after a long drought always has a stable rate and a shifted intercept, so `jump`
   consumed it. `jump` and `drift` are therefore narrowed to gaps ≤ 30 s, which is exactly the
   complement of the plan's own replay predicate ("ref inconsistent … *and* mix gap > 30 s, or the
   same ref region recurs after > 30 s"). The relative order of the remaining predicates is the
   plan's. Covered by `test_a_multi_point_recurrence_after_thirty_seconds_is_a_replay_not_a_jump`
   and `test_a_short_gap_intercept_shift_is_still_a_jump`.
2. **`alignment_events` gains `replay`.** The plan's episode contract enumerates
   `jump|loop|reset|drift`, but it also requires event precision/recall *per type* and a corpus
   quota of ≥ 30 cases per type, and replay had nowhere to be dated. `AlignmentEvent.type` now
   accepts `replay`, emitted on the occurrence it starts. `docs/PLAN.md` is unchanged.
3. **`ground_truth.events` is a new required field.** Stage 2a recorded events in the free-text
   `note` field as an explicitly temporary convention ("if Stage 2b introduces an explicit event
   truth contract, migrate the scorer and controlled generator together"). Stage 4c does exactly
   that. Every committed truth file was regenerated, not hand-edited.
4. **A second controlled corpus.** The task says "extend the render if needed". Extending
   `controlled-synth-1` in place would have invalidated the committed Stage 2b/3/4b reports that
   are keyed to its 25-set population. `controlled-events-1` is therefore a new frozen corpus that
   contains those 25 cases plus 120 event replicates; `controlled-synth-1` keeps its population
   and its prior reports stay comparable.
5. **`baseline-free.json` regenerated.** See *Regenerated artefacts*: its tier numbers were stale
   with respect to Stage 4b's `T_ind`.

## Known gaps

- `reset` has no controlled cases and is therefore unmeasured (0 predicted, 0 truth). The other
  four types are measured on ≥ 30 cases each.
- The event and rescan measurements come from the controlled oracle. Real-provider behaviour under
  rescans — latency, throttling, false matches on 6 s windows — is unmeasured. In particular a 6 s
  edge window is **shorter than the active measured `L_min` (3 s p50 but 6/9/12 s in the
  superseded v1/v2 configs)**, so a real engine may return fewer matches on rescan windows than
  the fixture does. Stage 4d must not assume the controlled recall carries over.
- `hints on/off` is not evaluable on the controlled stratum (no hint evidence, no `dev-2`).
- **Scanner fusion was validated on fixtures only.** `AUDD_API_TOKEN`, `ACRCLOUD_HOST`,
  `ACRCLOUD_ACCESS_KEY`, `ACRCLOUD_ACCESS_SECRET` and `ACRCLOUD_CONTAINER_ID` are all unset and
  no `id-detector.toml` exists, so `allow_third_party_upload` is false by construction: the
  opt-in probe was not run and **no paid provider call was made**. The tests use the authored,
  shape-accurate Stage 3 fixtures. Panako is excluded from v1 pending the owner's JDK decision
  (`doctor` reports `JDK not found - Panako disabled`).
- `dev-1` remains an unverified six-set draft (Stage 2b's CR-2B-01 is still blocked on owner
  annotation). Nothing in Stage 4c used it for accuracy.
- The ablation harness answers windows in memory. The real FFmpeg window path is exercised by the
  Stage 4b insertion vectors and by the Stage 4c generation-loop tests, but not by the 145-set
  ablation itself.
- Stale generation-`N` WAVs are still never pruned (deferred from Stage 4b); rescan generations now
  make more of them.

## What Stage 4d needs to know

- `data/corpus/controlled-synth-1/ablations.json` is the Stage 4c evidence for freezing profiles.
  It records every arm's cost (`windows`) beside its accuracy, so a profile can be priced.
- The `free` profile should keep rescans **on**: they are the only thing that moves the proved
  start bound (−55.55 % p90) and, through novelty triggers, the only way a transformed track with
  no generation-0 evidence is ever recognised.
- `transforms.policy = global` is still not worth its cost: +1 e4 segment recall for 17 % more
  windows over `rescan_only` on this corpus.
- Cost scales hard with rescans: 583 → 37,761 windows across 145 sets. `[rescan].max_generations`
  and the request budget are the two knobs; `plan_within_budget` spends by priority
  (gap > question_cluster > contested > hint_cluster > edge > novelty > long_episode).
- Preserve `episode.rejected_evidence`, `T_ind` in ten-thousandths and the dependence prior when
  adding engines; a second commercial engine's trials are worth 0.5 until dependence is measured.
- Any new engine must emit `logical_trial_id = sha1(provider ‖ chunk_index)` for scans, a
  `transform` of `null`, and an anchor whose `method` is registered in
  `fuse.scanners.ANCHOR_CONVERSIONS`.
