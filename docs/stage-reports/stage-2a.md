# Stage 2a — Scorer and controlled slice

## What was built

### File map

- `src/id_detector/benchmark/scorer.py`: strict identity-graph-backed prediction input,
  evidence-derived proved bounds, recording-specific/conflict-vetoed exact version equivalence,
  versioned hashed scoring/preregistration snapshots, support-time/30-second occurrence
  association, separate identity,
  occurrence, set-valued per-ms segment, start, and end scoring, unknown-region policy, proved
  bound violations, point/range errors, PI coverage/width/Winkler scores, per-tier precision and
  one-sided Clopper–Pearson bounds, seeded set-cluster bootstrap bounds, certification populations,
  and the paired one-sided 1 pp non-inferiority helper.
- `src/id_detector/benchmark/controlled.py`: deterministic local-source and synthetic-source
  helpers, all requested FFmpeg transformations, transactional JSON/audio publication into
  separate directories, extended Windows paths, exact sample-count render durations, EBU R128
  400 ms/100 ms momentary loudness extraction, the revision-5 hysteresis/median/minimum-run/silence
  audible rule, per-set truth, and a hash manifest.
- `src/id_detector/truth.py`: hint/manual-tracklist draft seeding, complete-annotation import for
  corrections/additions/roles/overlaps/regions/versions, separately stored first and independent
  second passes, distinct third-annotator resolution, local source-link storage, and verified
  corpus freezing with annotation hashes.
- `src/id_detector/cli.py`: `benchmark score`, split-output `benchmark render`, and
  `truth seed|verify|second-pass|resolve|freeze`.
- `src/id_detector/contracts.py`, `docs/schemas/{ground_truth,benchmark_report}.schema.json`, and
  their goldens: draft truth states plus the requested side-specific bound, interval, empirical
  lower-bound, and cluster-certification fields. All artifact numbers remain integers.
- `scripts/make_controlled_predictions.py`: creates explicitly truth-derived predictions only for
  exercising scorer plumbing before Stage 2b supplies a fuser.
- `tests/test_stage2a_scorer.py`: a fully hand-computed pure vector covering a long truth episode
  with short support, occurrence 2, simultaneous layers, proved-bound violations, all unknown
  region types, exact/work equivalence, every report metric, interval propriety, CP bounds,
  bootstrap behavior, hashed preregistration, identity conflicts, work-only exclusion, and schema
  validation.
- `tests/test_stage2a_controlled.py`: audible-rule vectors, measured transform probes for every
  factor, long-path and transactional-publication regressions, no-audio fixture policy, and two
  independent byte-identical renders.
- `tests/test_stage2a_truth.py`: draft/import, full timeline editing, independent passes, distinct
  resolver, work-only freeze, and impossible-timeline vectors.
- `data/fixtures/controlled/stage-2a/`: 25 pseudonymous controlled truth sets, JSON-only render
  manifest, identity-backed truth-derived prediction input, and benchmark plumbing report.
  Rendered audio is local under `data/local/controlled/<corpus_version>/` and ignored.
- `.gitignore`: local source links and controlled audio; `README.md`: Stage 2a command entry points.

## Review fixes

| Finding | Change | Regression test |
|---|---|---|
| P1 — proved bounds trusted/forged | `ScoredEpisode` now derives the required values from every evidence span and rejects either mismatch; the plumbing generator emits those derived values and fixtures/report were regenerated. | `test_proved_bounds_must_be_derived_from_evidence_support`; committed-fixture bound audit |
| P1 — exact identity bypass/veto | Every prediction set now carries its resolved identity graph and each episode its `candidate_id`; association/segment scoring uses canonical work components. Exact scoring accepts recording namespaces only, rejects contradictory shared IDs, and contested/conflicting components cannot claim a version tier. | `test_exact_equivalence_requires_consistent_recording_specific_ids`; `test_contested_recording_identity_cannot_claim_a_version_tier`; `test_scorer_uses_resolved_work_identity_instead_of_episode_text` |
| P1 — unregistered certification/false config key | Prediction input requires a versioned config/preregistration snapshot, verifies its SHA-256, and uses targets by profile × dimension × tier. Missing targets remain provisional; report entries record target and registration version. | `test_prediction_document_rejects_a_forged_config_hash`; `test_certification_uses_profile_dimension_tier_preregistration` |
| P1 — work-only truth treated as version failure | Freeze permits completed work annotations with `version_verified=false`; exact-version metrics and version certification exclude that truth and its associated prediction. | `test_work_only_truth_can_freeze_without_exact_version_evidence`; `test_work_only_truth_is_excluded_from_exact_version_metrics` |
| P1 — biased/non-independent truth workflow | Full annotation imports can replace seed identity and add/edit episodes, version IDs, roles, overlaps, and regions. Passes are separate hashed files; test freeze requires a distinct independently authored second pass and a distinct third resolver for disagreement. Mixed timed/untimed seeds now require all missing cues explicitly. | `test_seed_verify_second_pass_freeze_state_machine`; `test_first_pass_can_replace_seed_with_full_timeline_annotation`; `test_mixed_timed_and_untimed_seed_requires_explicit_cues` |
| P1 — impossible ground-truth timelines | Record validation enforces ordered/in-duration boundary ranges and regions, coherent episode boundaries, role containment, symmetric valid overlaps, non-overlapping regions, and per-work occurrence uniqueness. | `test_ground_truth_rejects_impossible_timelines`; `test_ground_truth_requires_symmetric_overlaps_and_unique_occurrences` |
| P2 — duration-weighted out-of-pool policy | Unknown scoring is one binary target per annotated region: any intersecting ID matches `out_of_pool`; emissions in unresolved/silence regions are explicit false positives. | hand-computed scorer vector (`unknown_region` = 1 TP / 2 FP / 0 FN) |
| P2 — transform labels without signal checks | Renamed `coupled` to contract term `resample`; known-marker/tone probes verify source progression, first/last mapped markers, middle marker rate, pitch, and exact output samples for all 12 factors. | `test_rendered_rate_transform_maps_known_markers_and_pitch`; `test_rendered_pitch_transform_maps_known_markers_and_pitch` |
| P2 — unsafe/non-transactional controlled rendering | FFmpeg, temp, replace, scan, and cleanup paths use extended-path helpers. JSON and audio render in validated sibling staging directories and replace prior directories only after completion. | `test_controlled_ffmpeg_writes_an_extended_length_path`; `test_failed_render_keeps_previously_published_corpus`; stale-file checks in `controlled_renders` |
| P1 — audio silently ignored under fixtures | The renderer separates JSON `--out` from local-only `--audio-out`; the Stage 2a fixture was regenerated with 28 JSON files and no audio. Fresh seeded output is byte/hash compared with the committed manifest/truth. | `test_committed_controlled_fixture_is_json_only_and_matches_fresh_render` |

## Disputed findings

None.

## Deferred

None.

## How to run it

From the repository root in PowerShell:

```powershell
uv sync --dev

uv run id-detector benchmark render --sources <local-audio-dir> --out <truth-json-dir> `
  --audio-out <local-audio-dir>/controlled-r5-seed-7 --seed 7
uv run id-detector benchmark score --truth <truth-dir> --episodes <predictions.json> --out <report.json>

uv run id-detector truth seed --out <set-dir>/ground_truth.json --set-id <set-ref> `
  --duration-ms <duration> --media-key <sha256> --tracklist <tracklist.txt> --split dev-1
uv run id-detector truth verify --truth <set-dir>/ground_truth.json `
  --annotator-ref <first-pass-ref> --annotation <complete-first-pass.json>
uv run id-detector truth second-pass --truth <set-dir>/ground_truth.json `
  --annotator-ref <distinct-second-pass-ref> --annotation <independent-second-pass.json>
uv run id-detector truth resolve --truth <set-dir>/ground_truth.json `
  --resolver-ref <distinct-third-ref> --annotation <resolved-truth.json>
uv run id-detector truth freeze --truth <truth-dir> --corpus-version <version> `
  --out <truth-dir>/corpus-version.json

uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
uv run python scripts/audit_fixtures.py
```

The reproducible synthetic plumbing run used for the checked-in slice was:

```powershell
uv run python -c "from pathlib import Path; from id_detector.benchmark.controlled import synthesize_test_sources; synthesize_test_sources(Path('.stage2a-sources'), seed=20260904, count=4)"
uv run id-detector benchmark render --sources .stage2a-sources `
  --out data/fixtures/controlled/stage-2a `
  --audio-out data/local/controlled/controlled-r5-seed-20260904 --seed 20260904
uv run python scripts/make_controlled_predictions.py data/fixtures/controlled/stage-2a `
  data/fixtures/controlled/stage-2a/synthetic_predictions.json --seed 20260904
uv run id-detector benchmark score --truth data/fixtures/controlled/stage-2a `
  --episodes data/fixtures/controlled/stage-2a/synthetic_predictions.json `
  --out data/fixtures/controlled/stage-2a/benchmark_report.json
```

## What was verified

No live or network test was run. All audio was locally synthesized.

The regenerated split-output render and scorer plumbing run:

```text
rendered 25 sets and 56 boundaries; manifest=C:\Users\natha\Documents\Music\id-detector\data\fixtures\controlled\stage-2a\render_manifest.json
wrote truth-derived plumbing predictions for 25 sets to data\fixtures\controlled\stage-2a\synthetic_predictions.json
scored 25 sets; work precision=10000/10000; report=data\fixtures\controlled\stage-2a\benchmark_report.json
```

Direct fixture-policy/contract inspection:

```text
audio_under_fixtures=0
resample_cases=4
proved_bound_mismatches=0
identity_graphs=25
config_hash=dc6ebea34c5a7069a6e371e6fdda6721450762ecc65c04ca6cb946d4f8399078
```

`uv run pytest -q`:

```text
........................................................................ [ 39%]
........................................................................ [ 79%]
.....................................                                    [100%]
181 passed, 1 deselected, 1 warning in 47.03s
```

The sole warning is pydub's Python 3.13 `audioop` deprecation; this project uses Python 3.12.

`uv run ruff check .`, `uv run ruff format --check .`, `uv lock --check`, and the audit:

```text
All checks passed!
61 files already formatted
audited 108 files
fixture audit passed
Resolved 53 packages in 2ms
```

## Plan-silent decisions

- The scorer input is one JSON document with `corpus_version`, `profile`, a hashed versioned
  `config_snapshot`, per-set identity graphs and candidate-labelled episode projections, plus
  integer cost. Keeping the graph beside each set avoids a second CLI path while still scoring
  resolved components rather than display labels.
- Exact equivalence requires a non-contested candidate, canonical-work equivalence, and at least
  one matching recording-specific ID with no contradictory ID in any shared recording namespace.
  Qualifiers, `mb_work`, and release IDs never establish exact equivalence by themselves.
- Truth `role_segments` define the evaluated per-ms spans. Empty role lists fall back to the
  guaranteed truth interior or prediction best-point hull. Simultaneous labels are mathematical
  sets, so overlap is counted once per distinct work at each millisecond.
- `silence_or_speech` and `unresolved` are excluded from per-ms denominators; predictions whose
  support is wholly in any annotated unknown region are excluded from identity precision.
  `out_of_pool` is a binary “some ID emitted” target; emissions in other annotated unknown-region
  types are its false positives and are reported under `unknown_region`.
- Truth boundary ranges are scored with distance-to-range. A PI covers when it intersects the
  truth range. Winkler uses each PI's `coverage_target`; widths and scores combine start and end in
  the legacy aggregate fields and are also reported separately.
- Per-tier populations are cumulative at-or-above-tier. Targets must exist in the immutable
  snapshot for the exact profile × dimension × tier; absent targets stay provisional. Bootstrap
  uses 2,000 snapshot-seeded set-cluster resamples and the one-sided fifth percentile.
- Controlled JSON and rendered audio have separate publish roots. The default audio root is
  `data/local/controlled/<corpus_version>`; `--audio-out` makes this explicit for tests or other
  local layouts.
- Controlled event truth uses the existing `note` convention `event:<type>@<ms>`, because revision
  5 defines event metrics but no event field in `ground_truth`. Association tolerance is 2 seconds.

## Known gaps

- The included scorer run is truth-derived and proves only schema and command plumbing. It is not
  an identification result, and controlled sets are excluded from real-mix certification.
- Stage 2a delivers 56 boundaries, above its 20-boundary acceptance gate. The later controlled
  quota of at least 100 boundaries and at least 30 cases per event type remains Stage 4c work.
- No representative real-mix corpus exists yet. Consequently every test certification entry is
  provisional, with zero eligible real-mix test sets.
- The generator requires at least three readable local audio files. The synthetic-source helper is
  separate so the main render command never silently substitutes generated audio for user input.
- Momentary loudness is delegated to the installed FFmpeg EBU R128 implementation. Determinism is
  established for the pinned local FFmpeg 8.x environment, not promised across FFmpeg versions.

## Deviations from plan

- Draft truth cannot satisfy revision 5's frozen ground-truth field types because the task
  explicitly requires `verified_against: null` and an explicit draft marker. The ground-truth
  schema therefore now permits null `verified_against`/`annotator_ref` and requires `draft`; model
  validation prohibits a draft from claiming verification, and freeze prohibits drafts/nulls.
- The Stage 0 benchmark schema did not expose separate bound-violation counts, side-specific PI
  metrics, empirical CP bounds, or cluster certification bounds required by this stage. These were
  added without changing `docs/PLAN.md`; existing metric fields remain intact.
- Benchmark certification entries now also expose nullable `target_e4` and
  `registration_version`, so a provisional result distinguishes a failed registered gate from the
  absence of preregistration.
- First/second/resolution annotation snapshots are separate hashed JSON files beside
  `ground_truth.json` and their hashes are recorded in the freeze manifest. The plan requires the
  independent workflow but does not prescribe these supporting filenames.

## What Stage 2b needs to know

- Emit each episode's `candidate_id` with the per-set identities artifact in the scorer projection.
  Preserve every `evidence_support_ms`; both proved bounds must be recomputed from it, and
  association deliberately does not use IoU.
- Use the checked-in hand vector as the regression oracle when adding the fuser. In particular, a
  correctly identified long episode with sparse support must remain an identity TP.
- Real-mix truth should use complete independent annotation files. Frozen `test` sets require a
  distinct independently authored second pass and, on any disagreement, a distinct third
  resolver. Work-only truth is valid; exact-version scoring requires recording-specific identity.
- Profile configuration must produce the versioned scorer snapshot and preregister every intended
  certification target before predictions; missing entries remain provisional by construction.
- Event annotations currently use `note`; if Stage 2b introduces an explicit event truth contract,
  migrate the scorer and controlled generator together and regenerate the schema/golden.
- Preserve integer fixed-point output (`usd_e2`, all `_e4` fields) and deterministic set ordering.
