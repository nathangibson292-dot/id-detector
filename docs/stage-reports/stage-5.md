# Stage 5 — Calibration & test

Date: 2026-09-04
Plan: revision 5.2, Stage 5 row of the Build order table only
Contingency honoured: **no owner-verified real-mix calibration or test corpus exists** (dev-1 is
unverified drafts; only the controlled corpus has exact truth, and the plan forbids certifying
real-mix tiers from controlled renders). The full calibration machinery is built and exercised
end-to-end on the controlled corpus, clearly labelled as *machinery validation*; every real-mix
tier stays `provisional` with `n_test_predictions: 0`. No certification is fabricated.

## Outcome

Stage 5 is implemented. A monotone, interpretable calibrator (isotonic pool-adjacent-violators on a
documented integer ordering index per dimension, plus empirical prediction intervals) turns the
heuristic tiers into calibrated `score_kind: "calibrated"` scores, tiers and PIs, keyed per
`(profile, dimension)`. Calibrated scores are wired into `analyse` **only when a frozen calibration
artefact exists for the selected profile**; none is committed for `free`/`max_accuracy`, so the
production pipeline stays heuristic and honest. A single frozen-test certification command scores a
frozen corpus against the plan's pre-registered targets with Clopper–Pearson and cluster gates; on
the only corpora that exist it produces an all-`provisional` block with a zero denominator, because
the certification population is real-mix test sets and controlled sets are excluded by construction.

## What was built

### File map

- `src/id_detector/calibrate/` (new package):
  - `features.py` — deterministic, integer/fixed-point feature extraction (`build_features`,
    `EpisodeFeatureInputs`) covering the plan's features: `T` (`t_ind_e4`, with the 0.5 dependence
    prior), `S` (`span_ms`), alignment residuals + segment count, `score_raw` where available,
    transform consistency, engine agreement discounted by the correlation prior, one vote per
    `logical_trial_id` and per `provenance_group`, contradictions, identity conflicts, version
    agreement. Also the three documented monotone ordering indices (`work`/`version`/`boundary`).
  - `model.py` — isotonic PAV (`_pav`, `isotonic_score_e4`), tier thresholds at the plan precisions
    (possible 0.70 / likely 0.90 / verified 0.99), empirical PIs (0.9 coverage target),
    `fit_calibration` → immutable `CalibrationModelRecord`, `CalibrationApplier` (`apply` /
    `apply_episode`, structural caps for version/boundary), `load_calibration` (repo `calibration/`
    then packaged resources; `None` when absent), `next_version_number`.
  - `reconstruct.py` — rebuild `EpisodeFeatureInputs` from persisted `episodes.json` + `identities`
    + `observations`, so a feature is computed identically at fit time and at analyse time.
  - `labeling.py` — turn scored predictions into labelled `CalibrationExample`s using the scorer's
    exact association + correctness definitions.
  - `certify.py` — `run_certify` (single frozen-test evaluation), `registered_targets`,
    `build_prediction_document`, and the refusal exceptions `CorpusNotFrozen` /
    `DuplicateTestVersion`.
  - `validate.py` — `run_calibration_validation`: whole-set split, fit, apply, per-tier precision
    with CP + cluster lower bounds, PI coverage/width/Winkler, and the controlled-labelled report.
- `src/id_detector/contracts.py` — new records `CalibrationFeatures`, `CalibrationModelRecord`
  (+ `CalibrationBin`, `CalibrationTierThreshold`, `CalibrationDimensionModel`,
  `CalibrationIntervalModel`, `CalibrationCertEntry`, `CalibrationProvenance`),
  `CalibrationValidationRecord` (+ sub-models); registered in `SCHEMA_MODELS` and
  `NATURAL_KEY_FIELDS`.
- `src/id_detector/fuse/episodes.py` — `build_episodes`/`fuse_generation`/`fuse_generation_zero`
  accept an optional duck-typed `calibrator`; when present each episode gets calibrated
  scores/tiers/PIs, `score_kind: "calibrated"`, recomputed badge and `version_status`, and the
  certification block is stamped from the model (all provisional). Default path is unchanged
  (`heuristic`, `start_pi`/`end_pi` null, certification `not-run`).
- `src/id_detector/orchestrate.py`, `src/id_detector/cli.py` — thread `calibrator` through the
  generation loop and `_analyse`; `analyse --profile <name>` loads a calibration model if one exists
  (else heuristic). New commands `benchmark certify` and `benchmark calibration-validate`.
- `src/id_detector/benchmark/corpus.py` — `_run_controlled` accepts a calibrator and returns its
  observations; `_scoring_config` accepts pre-registered `certification_targets`.
- `docs/schemas/calibration_{features,model,validation}.schema.json` (generated),
  `tests/golden/calibration_{features,model,validation}.json` (new goldens).
- `tests/test_stage5_calibration.py` (new, 11 tests). `id-detector.example.toml` documents the
  Stage 5 commands.
- `data/corpus/controlled-synth-1/calibration-validation.json` (committed machinery-validation
  report, labelled `population: "controlled -- not real-mix certification"`).

Local-only (gitignored `data/local/`, never committed): the fitted controlled model
`data/local/calibration/controlled-synth-1/controlled-machinery-v1.json`, and certify reports +
registries under `data/local/certification/`.

## How to run it

```bash
# 1) Machinery validation on the controlled corpus (fit + apply + measure; NOT certification):
uv run id-detector benchmark calibration-validate --corpus controlled-synth-1

# 2) Single frozen-test certification (real-mix tiers stay provisional; controlled is excluded):
uv run id-detector benchmark certify --corpus controlled-synth-1 --profile free --test-version v1
#    - refuses an unfrozen corpus (e.g. dev-1) with exit 2
#    - refuses to reuse (profile, test_version) with exit 2

# 3) Analyse a mix with a profile (heuristic today; calibrated iff calibration/<profile>-v*.json):
uv run id-detector analyse <URL-or-local-media> --profile free
```

## What was verified and how

### Machinery validation on the controlled corpus (25 sets → 13 calibration / 12 test)

```text
> uv run id-detector benchmark calibration-validate --corpus controlled-synth-1
validated calibration machinery on controlled-synth-1 (13 calibration sets, 12 test sets);
certified_triples=0 (controlled -- not real-mix certification);
model=...\data\local\calibration\controlled-synth-1\controlled-machinery-v1.json;
report=...\data\corpus\controlled-synth-1\calibration-validation.json
```

`data/corpus/controlled-synth-1/calibration-validation.json` (machinery behaviour, controlled
oracle — deterministic truth-derived recognition, so precision is perfect by construction):

- per-tier (every dimension × tier): `n=13`, `correct=13`, `precision_e4=10000`,
  `cp_lower_e4=7941`, `cluster_lower_e4=10000`;
- prediction intervals: start/end/boundary `coverage_e4=10000`; median widths 7000 / 8800 / 7000 ms;
  Winkler 6700 / 8800 / 7750;
- `certification`: all 15 entries `provisional`, `n_test_predictions=0`.

This proves feature extraction → isotonic tier calibration → empirical PIs → scoring run end-to-end.
It measures the fuser on synthetic audio, **not** open-world accuracy, and certifies nothing.

### Certification command (guardrail + refusal paths)

```text
> uv run id-detector benchmark certify --corpus controlled-synth-1 --profile free --test-version machinery-1
certified corpus=controlled-synth-1 profile=free test_version=machinery-1;
certified_triples=0; n_test_predictions=0; report=...\certification-machinery-1.json
# report.certification: 15 entries, all provisional, n=0, n_sets=0

> uv run id-detector benchmark certify --corpus controlled-synth-1 --profile free --test-version machinery-1
(free, machinery-1) already certified; use a new --test-version           # exit 2

> uv run id-detector benchmark certify --corpus dev-1 --profile free --test-version d1
corpus dev-1 is not frozen; freeze it before certification                 # exit 2
```

### Required repository checks

```text
> uv run pytest -q
487 passed, 3 deselected, 1 warning in 122.07s (0:02:02)

> uv run ruff check .
All checks passed!

> uv run ruff format --check .
137 files already formatted

> uv lock --check
Resolved 53 packages in 2ms

> uv run python scripts/audit_fixtures.py
audited 330 files
fixture audit passed
```

The 3 deselections are the `live` network tests; the 1 warning is pydub importing Python 3.12's
deprecated `audioop`. No `-m live` test was run; no paid provider and no network were used.

## Plan-silent decisions

- **Calibrator form.** Isotonic PAV on a documented integer *ordering index* per dimension (the plan
  allows "isotonic or logistic … no black boxes"). Isotonic keeps the model a fully-integer step
  function — no floats in any artefact, monotone by construction, and interpretable as a lookup
  table. The index formulae are stored verbatim in the model.
- **Tier ↔ precision mapping.** possible→0.70, likely→0.90, verified→0.99 (e4 7000/9000/9900),
  matching the plan's "≥ 299/29/9 error-free" one-sided CP requirement.
- **PI point estimate.** `best_start`/`best_end` is the calibrated interval midpoint (exactly what
  the scorer requires for calibrated episodes); the interval is `[proved+q05, proved+q95]` from the
  empirical `(true boundary − proved bound)` distribution on the calibration split.
- **Structural caps stay in force after calibration.** Version tier is capped at `unclear` without
  corroborated recording-specific ids / when contested; boundary tier at `unclear` without a global
  alignment. Calibration refines the score within the plan's guardrails; it never overrides them.
- **No real-mix calibration model is committed.** Committing a model fit on controlled renders under
  `calibration/free-v1.json` would silently change real-mix scores from synthetic evidence, so the
  controlled model is written to gitignored `data/local/` under the name `controlled-machinery`; the
  production profiles have no model and `analyse` stays heuristic.
- **Certification population.** The scorer already excludes controlled and self-index strata from the
  test population; `certify` reuses it, so a frozen *controlled* corpus certifies nothing — the
  honest result when no real-mix test corpus exists.

## Deviations from plan

`docs/PLAN.md` is unchanged and the Stage 5 row ("Calibrated scores/tiers per profile; PIs; single
frozen-test evaluation; certification per pre-registration") is delivered; the report carries
per-`(dimension, tier)` status and CIs, all provisional as the contingency requires.

One correction to the original claim of "None" (raised by the Stage 5 code review, now fixed): the
first cut of `certify` scored the real-mix branch (`benchmark/corpus.py:_run_real`) with a
profile-agnostic, always-heuristic `analyse` subprocess — it passed no `--profile`, so the scored
real-set predictions would not have been the selected profile's production output (engines,
schedule/transform geometry, or committed calibration model). This was latent (no real-mix corpus is
frozen, and the controlled branch is unaffected) but would have been wrong for the eventual real-mix
certification. `_run_real` now threads the selected `--profile` into the subprocess, so real-set
predictions are the profile's actual production output. The controlled branch already applied the
in-process calibrator and is unchanged. See the "Review fixes" section below.

## Known gaps

- **Nothing is certified, by design.** No owner-verified real-mix calibration/test corpus exists, so
  every `(profile, dimension, tier)` stays `provisional` with `n_test_predictions: 0`.
- **The committed model is controlled-only** and lives in `data/local/`; it is machinery
  validation, not a shippable calibrator. Its precision is perfect only because the controlled
  oracle is deterministic truth-derived recognition.
- **`analyse` is heuristic in production** until a real-mix model is fit and committed to
  `calibration/`.

## What certification needs from the owner (plain language)

Certification requires an owner-verified **real-mix** corpus; the controlled corpus can never
certify real-mix tiers (plan). The plan's quotas (Corpus construction, Stage 2b) are:

- `calibration` ≥ **10 sets / 120 episodes** (fits the calibrator and the prediction intervals);
- `test` ≥ **12 sets / 150 episodes**, with a **blinded second pass** and **third-annotator
  disagreement resolution** (the frozen population certification scores against);
- both drawn from strata 1–2 (catalogue-covered / reference-pool real mixes), with **≥ 10
  independent sets** at or above a tier for that tier to be eligible;
- held references where recording-level version truth is claimed.

Exact commands, once the sets are annotated (truth tooling → freeze → certify):

```bash
# 1) Annotate each real set (first pass, blinded second pass, third-annotator resolution):
uv run id-detector truth verify       --truth data/corpus/<real>/<set>/ground_truth.json --annotator-ref <a1> --audio <mix>
uv run id-detector truth second-pass  --truth data/corpus/<real>/<set>/ground_truth.json --annotator-ref <a2> --audio <mix>
uv run id-detector truth resolve      --truth data/corpus/<real>/<set>/ground_truth.json --resolver-ref <a3> --annotation <resolved.json>

# 2) Freeze the corpus (writes the hash-checked corpus-version.json):
uv run id-detector truth freeze --truth data/corpus/<real> --corpus-version <real-v1> --out data/corpus/<real>/corpus-version.json

# 3) Fit + commit a real-mix calibration model for the profile (calibration split), then
#    certify the frozen test split exactly once per test version:
#    (fit uses the same code path as `benchmark calibration-validate`, pointed at the real corpus;
#     commit the resulting model to calibration/<profile>-v1.json so `analyse` uses it.)
uv run id-detector benchmark certify --corpus <real-v1> --profile free         --test-version <real-v1>
uv run id-detector benchmark certify --corpus <real-v1> --profile max_accuracy --test-version <real-v1>
```

A `(profile, dimension, tier)` flips to `certified` only when, on the real-mix **test** split, the
Clopper–Pearson one-sided 95% lower bound ≥ target **and** the by-set cluster bootstrap lower bound ≥
target **and** ≥ 10 independent sets — otherwise it stays `provisional`, visibly.

## What the next stage needs to know

- `analyse --profile <name>` picks up `calibration/<name>-v*.json` automatically; commit a real-mix
  model there to switch a profile from heuristic to calibrated. Immutability mirrors profiles: fit
  a new version, never edit a frozen file.
- The certification block in `fuse/episodes.json` and in the benchmark report is the single source
  of truth for tier status; both read `provisional` until a real-mix test certifies a triple.
- `benchmark certify` is the only sanctioned way to certify; it is idempotent per
  `(profile, test_version)` and refuses unfrozen corpora.

## Review fixes

Applying the Stage 5 code review (`docs/reviews/code-review-stage-5.md`). No finding was disputed.

- **[P1] `certify` never applied the `--profile`/calibrator to real-mix sets.** `_run_real`
  (`src/id_detector/benchmark/corpus.py`) now takes a `profile` and threads `--profile <name>` into
  the `analyse` subprocess via the new pure `_real_analyse_command` helper, so real-set predictions
  are the profile's production output (frozen engine/schedule/transform geometry *and* any committed
  calibration model, which `analyse --profile` loads). `run_certify`
  (`src/id_detector/calibrate/certify.py`) passes `profile_record.name`; the Stage 2b baseline run
  passes its `profile`. The "Deviations from plan" claim above is corrected.
  *Test:* `tests/test_stage5_calibration.py::test_real_set_analyse_command_carries_the_profile`
  asserts the real-set command carries `--profile free` (and is absent when no profile is given).

- **[P1] `recording_supported` / `n_competing_candidates` differed at fit vs analyse time.** The
  authoritative computations are extracted into shared helpers in `src/id_detector/fuse/identity.py`
  (`candidate_recording_supported`, `recording_node_sources_from_observations`) and
  `src/id_detector/fuse/episodes.py` (`competing_candidate_count`, returning the true count rather
  than a clamped 0/1). `identity.py` builds its `recording_supported` frozenset through the helper,
  and `calibrate/reconstruct.py` now calls the same helpers, so both features are provably identical.
  *Test:* `tests/test_stage5_calibration.py::test_recording_supported_and_competition_match_fit_and_analyse`
  builds a single-engine single-ISRC candidate and asserts `reconstruct` and `episodes` agree
  (`recording_supported is False`, equal `n_competing_candidates`) — it fails on the old permissive
  reconstruct rule (verified). `test_competing_candidate_count_returns_the_actual_count` pins the
  un-clamped count.

- **[P2] Certification population was a blocklist.** `src/id_detector/benchmark/scorer.py` replaces
  the "controlled/self-index excluded" test with an allowlist, `CERTIFICATION_STRATA` +
  `is_certification_stratum(...)`, so any unknown/mislabelled stratum is kept out of the certified
  population.
  *Tests:* `tests/test_stage2a_scorer.py::test_is_certification_stratum_is_a_real_mix_allowlist` and
  `::test_certification_population_excludes_unknown_stratum` (10 certifiable test sets under an
  unknown stratum → every certification entry has `n == 0`).

- **[P2] `_split_sets` ignored `seed`; `certify` `n_test_predictions` double-counted.** The dead
  `seed` parameter is dropped from `_split_sets` (`src/id_detector/calibrate/validate.py`; the split
  is a deterministic stratified alternation and the misleading "seed" note is corrected — `split_seed`
  remains only what it truly is, the by-set cluster-bootstrap seed). `certify`'s reported
  `n_test_predictions` is now an actual per-episode count over the certification population (new
  `_population_prediction_count`), not the sum of the 15 overlapping cumulative tier populations.
  *Tests:* `tests/test_stage5_calibration.py::test_split_sets_is_deterministic_stratified_and_seedless`
  and `::test_population_prediction_count_is_per_episode_not_tier_sum`.

- **Nit.** The "audited N files" number in this report is refreshed to the current audit output
  (330).

The committed machinery-validation report
`data/corpus/controlled-synth-1/calibration-validation.json` is byte-identical after these fixes
except for one honest note-wording change (the split is seed-independent): the recording-support fix
changes no value on the controlled corpus (its oracle attaches ≥ 2 ids, so the candidate was already
recording-supported under both rules), and `n_competing_candidates` was already the true count in the
reconstruction path. The certification gate is unchanged: `benchmark certify --corpus
controlled-synth-1 --profile free --test-version postfix-probe-1` still returns all-provisional,
`n_test_predictions=0`, `certified_triples=0` (15 entries, `n=0`, `n_sets=0`).
