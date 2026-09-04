# Stage 4d — Profile freeze

Date: 2026-09-04
Plan: revision 5.2, Stage 4d row of the Build order table only
Evidence: `data/corpus/controlled-synth-1/ablations.json` (Stage 4c, 8 arms, corpus
`controlled-events-1`) and `data/corpus/controlled-synth-1/shortlist.json` (Stage 3, corpus
`controlled-synth-1`)

## Outcome

Stage 4d is implemented. Profiles are first-class, immutable, versioned artefacts derived
**mechanically** from the two committed reports; every number a profile states about the world is
cited from a named report field, so a profile re-derives byte-for-byte from the evidence. The
`free` and `max_accuracy` profiles were frozen as `free-v1` and `max_accuracy-v1`. Both enable only
Shazam (the only available engine), with the certified free features on and the not-evaluated
engines recorded as eligible-when-available but not enabled. `analyse --profile <name>` selects a
frozen profile and rejects any name that is not a frozen artefact. No paid provider was called and
no credentials or JDK were required.

## What was built

### File map

- `src/id_detector/profiles.py` (new) — the whole Stage 4d module: the mechanical derivation
  (`derive_profile`), the freeze driver (`freeze_profiles`) that writes `profiles/<name>-v<K>.json`
  plus its `.done.json` sidecar, the evidence-cited report reader (`_Citer` / `_dig`), the loader
  (`load_profile`, rejecting unknown or non-frozen names via `UnknownProfile`), and the runtime
  mapping `profile_app_config`.
- `src/id_detector/contracts.py` — `ProfileRecord` and its sub-models (`ProfileEngine`,
  `ProfileFeature`, `ProfileEvidence`, `ProfileSchedule`, `ProfileRescan`, `ProfileBudget`,
  `ProfilePaidEstimate`, `ProfileCostReport`, `ProfileProvenance`); registered in `SCHEMA_MODELS`
  and `NATURAL_KEY_FIELDS` (natural key `("name", "version")`).
- `src/id_detector/cli.py` — `benchmark freeze-profiles --ablations --shortlist --out` and
  `analyse --profile`; a `--profile` value that is not a frozen artefact exits 2.
- `docs/schemas/profile.schema.json` (new, generated) and `tests/golden/profile.json` (new) —
  contract schema and golden; the schemas README natural-key table gains a `profile` row.
- `profiles/free-v1.json`, `profiles/max_accuracy-v1.json` (+ `.done.json` sidecars) — the frozen
  artefacts, and byte-identical packaged copies under `src/id_detector/resources/profiles/` so
  `analyse --profile` resolves from an installed wheel.
- `id-detector.example.toml` — documents `--profile` and the freeze command.
- `tests/test_stage4d_profiles.py` (new) and an added packaged-resource assertion in
  `tests/test_stage1_wheel.py`.

No new dependency was added (`uv lock --check` resolves unchanged); `data/raw/` is untouched.

## How to run it

From the repository root (PowerShell; `\`` line continuations, or one line in bash):

```powershell
uv run id-detector benchmark freeze-profiles `
  --ablations data/corpus/controlled-synth-1/ablations.json `
  --shortlist data/corpus/controlled-synth-1/shortlist.json `
  --out profiles

# Select a frozen profile for a real analysis (Shazam-only pipeline in v1):
uv run id-detector analyse <URL-or-local-media> --profile free
uv run id-detector analyse <URL-or-local-media> --profile max_accuracy
```

`freeze-profiles` chooses the next version number per name (v1 into an empty directory). The
command is deterministic: re-running it into a clean directory reproduces the committed artefacts
byte-for-byte.

## The two frozen profiles in plain language

Both profiles carry **Shazam** as their only enabled engine, because Stage 3 shows Shazam is the
only available production recognizer (it ran live over the whole corpus; 73 attempts) and it is the
only free clip recognizer. The controlled ablations were run on the `local_fixture` oracle, which
is explicitly excluded from every production profile — it measures the fuser, not open-world
accuracy. Shazam's own controlled-stratum accuracy is not measured (the synthetic corpus is in no
catalogue), so the profile is honest that the **feature toggles, not the engine, are what the
ablations certify**.

The feature decisions (identical in both profiles, each traceable to `ablations.json`):

- **Rescans — ON.** They are the only thing that moves the proved start bound: `best_start` p90
  improves 55.55% relative, paired one-sided 95% cluster **lower bound +5555 e4**
  (`comparisons.rescans_on_minus_off.best_start_p90.lower_bound_e4`), and work/segment recall rise
  with lower bounds **+470 / +374 e4**, precision non-inferior (both `pass=true`).
- **Novelty triggers — ON.** They are the only trigger that reaches a rate- or pitch-shifted track
  with no generation-0 evidence. Work/segment recall lower bounds **+473 / +377 e4**
  (`comparisons.novelty_on_minus_off`), precision non-inferior.
- **Transforms — ON at `rescan_only`.** Applying the grid on rescans beats off with recall lower
  bounds **+470 / +371 e4** at non-inferior precision
  (`comparisons.transforms_rescan_only_minus_off`). It is **not** escalated to `global`: global buys
  no recall over `rescan_only` (**0 e4** lower bound) and fails segment-precision non-inferiority
  (`comparisons.transforms_global_minus_rescan_only.segment_precision.pass = false`).
- **Schedule — left at the plan default 12 s / 9 s / phase 0** for generation 0 (rescan policy
  12 s / 5 s / phase 0). The 12/5 gen-0 challenger fails segment-precision non-inferiority
  (lower bound **-76 e4**). The 8/5 gen-0 challenger *does* improve segment precision
  (lower bound **+294 e4**) but uses a 5000 ms hop; adopting a hop below 9000 ms would inflate
  `T_ind` against tier thresholds calibrated at 9 s hops (rev 5.2), so it is **deferred to a
  re-calibrated v2** rather than adopted now.
- **Hints — ON but uncertified.** The plan keeps hints always-on and non-blocking, and they never
  raise the version tier, so they cannot lower precision by construction. Their accuracy benefit is
  *not* certified: the controlled stratum carries no hint evidence and the held-out `dev-2` corpus
  does not exist, so the Stage 4a gate remains the authority and stays blocked
  (`not_evaluable` in `ablations.json`). Shown as provisional (`certified: false`).

What distinguishes the two profiles:

- **`free`** (`engine_policy: free_only`): only free engines may ever be enabled. The paid scanners
  AudD and ACRCloud are recorded but marked `eligible_when_available: false` — the free profile is
  free-only by policy (plan principle 8). Panako (free, self-hosted) is
  `eligible_when_available: true`, gated on a JDK. Budget: `max_usd_e2 = 0`,
  `allow_third_party_upload = false`.
- **`max_accuracy`** (`engine_policy: all_available`): every available independent engine runs over
  the whole set with no suppression. AudD and ACRCloud are `eligible_when_available: true` (gated on
  credentials and the two upload-consent gates); Panako is eligible-when-JDK. Their expected cost is
  reported separately in `cost_report.paid_when_enabled` (AudD 29¢, ACRCloud 29¢ for the 25-set
  `controlled-synth-1` corpus, from `shortlist.engines[*].expected_trial_cost_usd_e2`).

**In v1 the two profiles reduce to the same operational pipeline** — Shazam plus the three certified
free features — because no credentials and no JDK are present, so no additional engine is available.
They differ only in which engines they would additionally enable when resources exist, and in cost
accounting. Both state this explicitly in their `notes`. Cost of the audio path itself is reported
from the ablations: `windows_rescans_on = 37761` vs `windows_rescans_off = 583` across 145 sets.

## What was verified and how

### Freeze command and byte-for-byte reproduction

```text
> uv run id-detector benchmark freeze-profiles --ablations ...ablations.json --shortlist ...shortlist.json --out profiles
froze free-v1.json: engines=shazam transforms=rescan_only rescans=true novelty=true hints=true(certified=false); -> profiles\free-v1.json
froze max_accuracy-v1.json: engines=shazam transforms=rescan_only rescans=true novelty=true hints=true(certified=false); -> profiles\max_accuracy-v1.json
```

Re-freezing into a clean directory produced files identical to the committed `profiles/*.json`
(`test_committed_profiles_rederive_byte_for_byte`). Every `{report, field, value}` citation in both
committed profiles was re-read from the live reports and matched
(`test_every_cited_number_is_traceable_to_a_report_field`). `frozen_from` records the real report
hashes: ablations `79c4656676…7ddf7ecb` (the sha256 the Stage 4c report states) and shortlist
`56fc133423…308ab`.

### `analyse --profile`

```text
> uv run id-detector analyse <mix-url> --profile turbo
unknown profile 'turbo'; frozen profiles are: free, max_accuracy      (exit 2)
```

A frozen name loads and maps onto the runtime `AppConfig` (transforms `rescan_only`, gen-0
12 s / 9 s / 0, rescan 12 s / 5 s / 0, `max_generations = 3`, novelty on, hints on); a non-frozen
file with a higher version is rejected. Covered by the Stage 4d suite.

### Required repository checks

```text
> uv run pytest -q
463 passed, 3 deselected, 1 warning in 125.14s (0:02:05)

> uv run ruff check .
All checks passed!

> uv run ruff format --check .
126 files already formatted

> uv lock --check
Resolved 53 packages in 2ms

> uv run python scripts/audit_fixtures.py
audited 320 files
fixture audit passed
```

The three deselections are the `live`-marked tests (network); the single warning is pydub importing
Python 3.12's deprecated `audioop`. The new `tests/test_stage4d_profiles.py` (9 tests) covers
byte-for-byte re-derivation, citation traceability, engine enablement/eligibility, feature
decisions, the rejection of global transforms and dense gen-0 hops, loader accept/reject, the
`AppConfig` mapping, and both `analyse --profile` paths. No `pytest -m live` test was run; no paid
provider call was made.

## Plan-silent decisions

- **`ProfileRecord` is a first-class contract.** The plan says profiles are "immutable versioned
  artefacts … packaged in `resources/` like provider configs" with a `frozen_from` provenance block,
  so they are modelled as a Stage-0-style `Record` with a schema, a golden and a natural key
  `("name", "version")`, and mirrored at repo-root `profiles/` and packaged
  `resources/profiles/`. `id = sha1(ablations_report_ref ‖ "profile" ‖ natural_key)`, binding the
  identity to the evidence.
- **Evidence vs configuration.** Every claim about the world (feature lower bounds, engine status,
  paid cost, window counts) is a cited report field. Concrete geometry (12/9 gen-0, 12/5 rescan,
  `max_generations = 3`) and budget ceilings (`shazam_requests_per_minute = 18` from the plan
  throttling row, `max_requests_per_media = 2000` from the analyse default) are plan configuration
  and are cited to the plan in `notes`, not to a report.
- **`free` marks paid engines `eligible_when_available: false`.** The task's wrapper asked that
  AudD/ACRCloud/Panako be *recorded* as eligible-when-available but not enabled. That is honored in
  `max_accuracy`. In `free` the paid scanners are recorded and not enabled, but marked ineligible
  because the free profile is free-only by policy (plan principle 8); Panako, being free and
  self-hosted, stays eligible-when-JDK in both. This is a small accuracy improvement over a blanket
  "eligible everywhere"; the reasoning is written into each engine's `reason`.
- **Hints enabled-but-uncertified** rather than off: the freeze rule's "positive lower bound"
  governs `certified`, while the plan's "always-on, non-blocking" mandate governs `enabled`. The two
  are recorded as separate booleans (plan principle 6, "provisional, visibly").
- **The 8/5 gen-0 schedule gain is recorded, not adopted.** A schedule challenger is adopted for
  generation 0 only if it improves a metric at non-inferior precision **and** keeps the hop
  ≥ 9000 ms (rev 5.2). Both challengers use a 5000 ms hop, so neither is adopted; the +294 e4 8/5
  segment-precision gain is cited and deferred to a re-calibrated v2.

## Deviations from plan

None. `docs/PLAN.md` is unchanged. The Stage 4d row ("`free`, `max_accuracy` from 4c ablations;
Documented with numbers") is delivered as specified.

## Known gaps

- **Both profiles reduce to Shazam-only in v1.** No paid credentials and no JDK exist, so
  `max_accuracy` currently enables the same engine set as `free`. The difference is latent
  (eligibility + cost), not operational, and both profiles say so.
- **Feature certification is from the controlled `local_fixture` oracle**, on synthetic audio. It
  measures the fuser and the state machine, not open-world accuracy, catalogue coverage or
  false-match risk. A 6 s edge rescan window is shorter than the active measured `L_min`, so a real
  engine may return fewer matches on rescan windows than the fixture does — the controlled recall
  does not carry over to real Shazam.
- **Hints remain uncertified** (no `dev-2` corpus; Stage 4a gate blocked). The profile enables them
  by plan mandate but cannot claim a benefit.
- **No calibration.** Tiers/badges stay provisional until Stage 5; the profiles fix the pipeline but
  do not certify any `(dimension, tier)`.

## What the owner must do for paid engines to be considered in a v2 profile

Paid engines are enabled only in `max_accuracy`, and only for audio the owner owns or may upload.

1. Obtain provider credentials and set them in the environment only (never in TOML, source, history
   or logs), following the Stage 3 procedure: `AUDD_API_TOKEN`, and `ACRCLOUD_HOST`,
   `ACRCLOUD_ACCESS_KEY`, `ACRCLOUD_ACCESS_SECRET`, `ACRCLOUD_CONTAINER_ID`.
2. Open both upload-consent gates: `allow_third_party_upload = true` in `id-detector.toml`, and the
   per-run `--i-own-this-audio-or-have-permission` flag.
3. Run the entitlement smoke and re-run the Stage 3 shortlist with credentials so the paid engines
   move from `not_evaluated` to `evaluated` with real per-engine numbers and cost, then re-run the
   Stage 4c ablations so the paid engines appear `in_ablation` with paired bounds.
4. For Panako: install a JDK ≥ 11 and build the reference pool (plan Stage 8); `doctor` must stop
   reporting `JDK not found — Panako disabled`, after which reference-pool recognition is no longer
   excluded.
5. Re-run `benchmark freeze-profiles`. With the reports now showing available paid/self-hosted
   engines, the derivation will enable them in `max_accuracy` (unsuppressed, cost reported
   separately) and write `max_accuracy-v2`, leaving v1 immutable.

## What the next stage (5 — calibration & test) needs to know

- `analyse --profile <name>` is the supported entry point; it fixes engines, transform/schedule/
  rescan geometry and the hint/novelty toggles from a frozen artefact. Calibration must run per
  profile and key its certification block off the profile `name`/`version`.
- The frozen profiles set `score_kind: heuristic` semantics only — nothing here certifies a tier.
- Preserve the immutability contract: re-freezing writes a new version; never edit a frozen file.
  The byte-for-byte re-derivation test guards this.
- Cost scales hard with rescans (583 → 37,761 windows across 145 sets); `max_generations` and the
  request budget are the knobs. The profiles carry those numbers in `cost_report`.
