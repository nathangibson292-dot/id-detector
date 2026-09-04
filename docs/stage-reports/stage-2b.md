# Stage 2b - Corpus dev-1 and baseline fuser

## What was built

### File map

- src/id_detector/fuse/identity.py: deterministic identity graph v0. It creates provider-ID and
  normalized-text nodes, keeps text equality at same_work, delegates recording unions and their
  conflict veto/late contesting to the Stage 0 merge helper, and emits natural-keyed works and
  candidates. Recording support requires two independent sources or a privileged held reference.
- src/id_detector/fuse/alignment.py: one selected point per logical trial/source, majority
  candidate and minimum-skew variant selection, exact rational Theil-Sen refits after three
  points, residual/rate/hypothesis gates, and the required continuation, loop, reset, jump, drift,
  replay, outlier precedence. Contract events are persisted; replay becomes a new occurrence and
  dropped outliers/rejected hypotheses remain flags/evidence.
- src/id_detector/fuse/episodes.py: generation-zero episodes, literal one-sided proved bounds,
  nullable censored sides and PIs, support unions, occurrence indices, performed/component claims,
  time-varying incoming/outgoing/dominant/layer/component roles, exact provisional tier rules,
  overlap-safe Stage 0 duration partitioning, 45-second gaps, all-provisional certification, and
  gap/contested/edge rescan requests. It writes identities.gen0.json, episodes.gen0.json,
  episodes.json, rescan_plan.gen0.jsonl, and completion sidecars.
- src/id_detector/present.py: deterministic flattened JSON and Markdown tracklists. Incoming has
  primary-role precedence and uses best_start_ms; gap rows are labelled ID and every track has a
  badge plus per-dimension tiers.
- src/id_detector/local_fixture.py: local_fixture clip recognizer for controlled audio. It builds
  a recorded-response table keyed by exact window-content SHA-256, emits realistic reference
  offsets, deduplicates equal-content requests, and persists raw fake responses only in ignored
  work storage.
- src/id_detector/benchmark/corpus.py: corpus discovery, local-media preference, source-link
  fallback, full analyse execution, episode/identity scorer projection, aggregate costs, and
  seeded paired regression comparison against a named baseline. Stage 2b accepts the free profile.
- src/id_detector/cli.py: analyse now executes ingest through export while retaining --raw;
  benchmark run and truth manifest-draft commands were added.
- src/id_detector/truth.py: platform and selection-basis seed fields plus an explicit
  non-freezing draft manifest. The existing freeze command still rejects every draft.
- src/id_detector/benchmark/scorer.py, src/id_detector/contracts.py, the benchmark report schema,
  report golden, and Stage 2a report: unverified_seed_comparison is explicit. Uncalibrated best
  points must equal their proved bounds, including valid crossed one-sided bounds.
- data/corpus/dev-1/: six research sets and 120 seeded episodes. Every episode has draft=true,
  null verification, and an explicit pre-recognition "not truth" selection basis. Its
  corpus-version.json says frozen=false and unverified_seed_drafts_not_truth. The one-real-set
  report is unverified-seed-comparison-free.json and carries unverified_seed_comparison=true.
- data/corpus/controlled-synth-1/: 25 exact generated truth sets, frozen hash manifest, and the
  committed baseline-free.json produced through ingest, decode, windows, local_fixture, fuse,
  export, and the Stage 2a scorer.
- data/corpus/README.md, README.md, and .gitignore: corpus layout/commands plus ignored
  data/local/ and data/corpus/**/media/. Source URLs, downloaded media, MixesDB responses, and
  provider responses remain local.
- scripts/audit_fixtures.py: data/corpus is now part of the mandatory repository-wide privacy
  audit.
- tests/test_stage2b_alignment.py: intended-event recall plus precision assertions that no other
  event fires for continuation, loop, reset, jump, drift, replay, and outlier; gate vectors.
- tests/test_stage2b_fuser.py: text/work separation, independent-source recording corroboration,
  conflict veto, late contested components, deterministic IDs, and byte ordering.
- tests/test_stage2b_pipeline.py: full local pipeline partition vector, two-run byte-identical
  episodes.json, all-provisional certification, crossed one-sided proofs, incoming flattening,
  tier output, and ID gaps.
- tests/test_stage2b_corpus.py: dev-1 draft inventory, controlled/unverified report separation,
  and deterministic named-baseline regression.

## How to run it

From the repository root in PowerShell:

~~~powershell
uv sync --dev
uv run id-detector doctor
uv run id-detector analyse <URL-or-local-media>
uv run id-detector analyse <URL-or-local-media> --raw

uv run id-detector benchmark run --corpus controlled-synth-1 --profile free --out data/corpus/controlled-synth-1/baseline-free.json
uv run id-detector benchmark run --corpus controlled-synth-1 --profile free --out <report.json> --baseline controlled-synth-1
uv run id-detector benchmark run --corpus dev-1 --profile free --out <report.json>

uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
uv run python scripts/audit_fixtures.py
~~~

benchmark run prefers data/local/media/<set_id>.* (or the ignored corpus media directory), then
resolves the pseudonymous source ref through data/local/source_links.json.

## What was verified

### Controlled exact-truth baseline

Command:

~~~powershell
uv run id-detector benchmark run --corpus controlled-synth-1 --profile free --out data/corpus/controlled-synth-1/baseline-free.json --work-root data/local/work-controlled-stage2b
~~~

Key output:

~~~text
scored 25 sets; unverified_seed_comparison=false; report=data\corpus\controlled-synth-1\baseline-free.json
controlled sets=25 unverified=False work_P/R=10000/10000 version_P/R=10000/10000 segment_P/R=8625/10000
~~~

This used local synthetic renders and the content-hash local_fixture provider, not Shazam.

### dev-1 construction and enforcement

The six source files were acquired with the project yt-dlp dependency into ignored
data/local/media/. Their committed truth duration is from ffprobe and every media_key is the
SHA-256 of the corresponding local source bytes:

~~~text
dev1-set-001 duration=7286767 episodes=23 drafts=True hash=True
dev1-set-002 duration=4304436 episodes=24 drafts=True hash=True
dev1-set-003 duration=5068141 episodes=10 drafts=True hash=True
dev1-set-004 duration=2525121 episodes=22 drafts=True hash=True
dev1-set-005 duration=4414054 episodes=26 drafts=True hash=True
dev1-set-006 duration=3280422 episodes=15 drafts=True hash=True
all_hashes_match=True
no committed corpus URLs
~~~

Raw MixesDB API lookups for five discoverable pages and all six search attempts are cached under
ignored data/local/mixesdb/. Seeds also used the existing local raw comment corpus and manual
tracklists. No claim of human verification was made. An attempted freeze correctly exited 1,
beginning with:

~~~text
cannot freeze:
dev1-set-001 episode 0 is still draft
dev1-set-001 episode 0 has no completed first-pass verification
~~~

### Live real-set smoke

A network benchmark run used the normal Shazam adapter, generation-zero schedule, and default
request budget on dev1-set-004. No pytest live test was run; this was a direct CLI live smoke.

~~~text
scored 1 sets; unverified_seed_comparison=true; report=data\corpus\dev-1\unverified-seed-comparison-free.json
dev_seed sets=1 unverified=True requests=281 physical=283 work_P/R=270/455
~~~

Those precision/recall values are against unverified seeds and are not accuracy measurements.
A cache replay through the public analyse command verified the final export without new calls:

~~~text
187 matches; 0 failures; 0 physical attempts; 55 episodes; tracklist=<local-work-dir>\present\tracklist.json
~~~

Both tracklist.json and tracklist.md exist. The real artifact duration partition summed to its
2525123 ms decoded duration.

### Tests, formatting, lock, and privacy

Required final uv run pytest -q output:

~~~text
........................................................................ [ 36%]
........................................................................ [ 72%]
......................................................                   [100%]
198 passed, 1 deselected, 1 warning in 47.13s
~~~

The warning is pydub's Python 3.13 audioop deprecation; this project is pinned to Python 3.12.

Required final uv run ruff check . output and supplementary checks:

~~~text
All checks passed!
73 files already formatted
Resolved 53 packages in 2ms
audited 145 files
fixture audit passed
git diff --check: exit 0
~~~

The named-baseline smoke resolved baseline-free.json, emitted four gates, and all four passed.

## Plan-silent decisions

- A draft inventory uses the same corpus-version.json location as a future freeze manifest but
  carries frozen=false, an unverified status, and a warning. truth freeze replaces it only after
  all episodes satisfy the existing verification checks.
- local_fixture is treated as the plan's privileged aligned held reference. It returns two
  recording-specific IDs, so controlled exact-version identity exercises the privileged branch.
- Provider independence is the provider name in generation zero. Repeated Shazam observations do
  not falsely count as multiple independent sources.
- Exports choose incoming before dominant, then outgoing, layer, component, uncertain. This makes
  a later-starting episode cue at best_start_ms even when it subsequently becomes dominant.
- Benchmark predictions remain local; only pseudonymous aggregate reports are committed.
- The regression comparison is embedded in BenchmarkReportRecord.regression. A baseline name
  resolves to data/corpus/<name>/baseline-<profile>.json; a direct report path is also accepted.

## Known gaps

- dev-1 is unverified. Its seed comparison is explicitly not truth and must not be used to claim
  accuracy, tune thresholds, or certify tiers.
- To verify dev-1, the owner must listen to each local mix and complete the Stage 2a first-pass
  workflow, then freeze it. These are the exact PowerShell commands:

~~~powershell
1..6 | ForEach-Object {
  $id = 'dev1-set-{0:D3}' -f $_
  $audio = Get-ChildItem "data/local/media/$id.*" | Select-Object -First 1
  uv run id-detector truth verify --truth "data/corpus/dev-1/$id/ground_truth.json" --annotator-ref owner-pass-1 --audio $audio.FullName
}
uv run id-detector truth freeze --truth data/corpus/dev-1 --corpus-version dev-1 --out data/corpus/dev-1/corpus-version.json
~~~

  The interactive verification must correct identities, add/remove episodes, set audible boundary
  ranges, roles/overlaps, and record work-only versus exact-version evidence. The manifest must
  then be regenerated by the freeze command; do not hand-edit draft flags.
- Generation zero only writes rescan requests. Executing generation loops belongs to Stage 4c.
- The controlled slice has 56 exact boundaries across 25 sets. The later quota of at least 100
  boundaries and at least 30 cases per event remains Stage 4c.
- Scores and every certification tier remain provisional; calibrated PIs and certified tiers are
  Stage 5.
- The baseline has only Shazam on real audio. Its intentionally conservative recording merge can
  expose multiple recording candidates for same-work provider labels until another independent
  source arrives.
- The YouTube source acquisition printed the expected warning that no JavaScript runtime is
  installed, but yt-dlp obtained the requested audio format successfully.

## Deviations from plan

- The Build order row says dev-1 is frozen and its baseline is committed. The task's explicit
  scope note supersedes that: human truth cannot be fabricated, so dev-1 is a non-frozen draft and
  its real report is marked unverified. The committed accuracy baseline is the exact controlled
  stratum.
- Revision 5 says uncalibrated best_start_ms/best_end_ms equal their one-sided proved bounds.
  With one or overlapping positive windows those independent bounds can cross. The Stage 2a
  scorer previously rejected that valid state as an ordered interval, contradicting the plan and
  causing short controlled episodes to disappear. The scorer now requires each uncalibrated best
  point to equal its own proof but does not impose ordering between them. docs/PLAN.md is unchanged.

## What Stage 3 needs to know

- New recognizers should emit the existing ObservationRecord fields, stable provider recording
  IDs, truthful logical_trial_id values, reliable anchors only when measured, and provider names
  that preserve the independence boundary.
- Do not treat same normalized text as a recording merge. Cross-provider recording-ID agreement
  or an allowed privileged source is required; conflicts force version uncertainty.
- Use benchmark run --corpus controlled-synth-1 for deterministic adapter-to-fuser regression.
  dev-1 can exercise plumbing but remains unusable for accuracy comparison until the owner runs
  the commands above.
- rescan_plan.gen0.jsonl is requests only. Stage 3 must not silently execute or mutate generations.
- Preserve integer/fixed-point artifacts, deterministic ordering, completion sidecars, local raw
  response storage, and the expanded corpus privacy audit.

## Review fixes

This section is the authoritative post-review addendum. It supersedes the earlier smoke counts
where they differ. Finding IDs below follow the order in
`docs/reviews/code-review-stage-2b.md`, including the two appended findings.

### Finding to change to test

- CR-2B-01, named `dev-1` deliverable: blocked, not fixed or disputed; see **Blocked finding**.
  No seed was relabelled as verified truth. `test_dev1_is_six_set_unverified_draft_inventory`
  continues to prove that the manifest is non-frozen and every seed episode is a draft.
- CR-2B-02, final-match proofs: episode occurrence assignment now retains every final match for
  `evidence`, the support union, and raw per-match proved bounds; selected logical-trial points are
  used only for voting/alignment. Rejection flags are occurrence-local. Covered by
  `test_episode_proofs_and_evidence_use_all_final_hypotheses`.
- CR-2B-03, same-ID corroboration: recording nodes track independent provider provenance, so two
  providers asserting the same single recording ID support the recording. Covered by
  `test_same_recording_id_from_two_providers_is_corroborated`.
- CR-2B-04, conflict ordering: every conflict in current input is pre-collected as a veto;
  `prior_components` is the explicit prior-generation state that can make a newly discovered
  conflict contested. Separated candidates may retain conflict metadata. Covered by the semantic
  veto/late-contest vectors, `test_identity_conflict_veto_and_late_contested_marking`, and
  `test_separated_veto_candidate_can_retain_conflict_metadata`.
- CR-2B-05, alignment predicates: adjacent mix gaps are no longer measured from the last accepted
  point; native time skew seeds the actual rate; two-point shifted-intercept transitions become
  jumps; an outlier needs a following point consistent with the prior trend; unresolved tail
  transitions remain pending; replay uses the literal inconsistency/recurrence conjunction.
  Covered by the expanded 30/120-second, two-point, outlier, invalid-rate, and event vectors.
- CR-2B-06, controlled source binding: corpus execution checks source SHA-256 and decoded duration
  before recognition. The recognizer accepts only a fixed response map built for validated
  expected window hashes, so unknown hashes return `no_match`. Covered by
  `test_corpus_source_validation_binds_hash_and_duration_to_truth` and
  `test_recorded_responses_reject_unrelated_window_content`.
- CR-2B-07, caller-controlled draft status: `unverified_seed_comparison` is required. Scoring
  derives verification from non-draft truth plus an explicit `frozen: true`, corpus-matching,
  hash-valid manifest; contradictory input is rejected and unverified truth cannot certify.
  Covered by required-marker, contradictory-marker, non-frozen, and frozen-certification tests.
- CR-2B-08, benchmark natural key: the hashed scoring snapshot now includes selected set IDs,
  provider and provider-config version/hash, scan policy, window schedule, transforms, fuser
  thresholds/policy, badge rule, source validation, and request ceiling. Covered by
  `test_benchmark_config_hash_covers_population_provider_schedule_fuser_and_budget`; regenerated
  controlled and Shazam reports now have different hashes.
- CR-2B-09, regression population: comparison requires identical corpus version, profile,
  verification state, set population, split, and stratum. Work/segment precision and recall all
  use paired cluster-bootstrap non-inferiority on those same units. Covered by the self-baseline
  and missing/incompatible-pair tests.
- CR-2B-10, best-point validation: calibrated PIs require the integer centre
  `floor((lo + hi) / 2)`; absent or explicitly uncalibrated PIs require the proved bound. Covered
  by `test_best_points_follow_calibrated_and_uncalibrated_pi_rules`.
- CR-2B-11, overstated tests: adversarial alignment/threshold vectors, a freshly executed local
  controlled pipeline, exact source validation, and an unrelated-content negative response-map
  test now exercise the claimed properties instead of merely parsing stored numbers.
- CR-2B-12, long episodes: evidence hulls strictly longer than 12 minutes emit a deterministic
  `long_episode` rescan request. Covered by
  `test_long_episode_emits_one_deterministic_rescan_request`.
- CR-2B-13, real-set fragmentation: corrected native-rate initialization and pending/replay logic
  collapse the cited continuous recording from four occurrences to one with `T=19`. The
  anonymised anchor-pair excerpt is covered by
  `test_anonymised_real_anchor_excerpt_is_one_continuous_occurrence` and native-rate extraction by
  `test_native_time_skew_sets_the_initial_alignment_rate`.
- CR-2B-14, revision-5.1 badge and version status: `badge` is the work tier, with only an
  uncorroborated `verified` work tier capped to `likely`; `version_status` is a required episode
  field and a separate JSON/Markdown export column. Duration partitioning continues to key off
  `badge`. Covered by the complete badge table, schema/golden tests, pipeline partition test, and
  export-column assertions.

### Review-fix file map

- `src/id_detector/fuse/{alignment,episodes,identity}.py` and `src/id_detector/semantics.py`:
  alignment, evidence assignment, identity provenance/conflicts, rev-5.1 status, and long rescans.
- `src/id_detector/benchmark/{corpus,scorer}.py`: media validation, frozen-truth derivation,
  complete run hashing, paired regression enforcement, certification guard, and PI best points.
- `src/id_detector/local_fixture.py` and `src/id_detector/present.py`: content-bound response maps
  and separate badge/version exports.
- `src/id_detector/contracts.py`, `docs/schemas/{episode,episodes,benchmark_report}.schema.json`,
  and `tests/golden/{episode,episodes}.json`: required contract changes.
- `src/id_detector/truth.py` and `data/corpus/controlled-synth-1/corpus-version.json`: newly
  generated freeze manifests state `frozen: true` explicitly.
- `data/corpus/controlled-synth-1/baseline-free.json`: regenerated 25-set controlled baseline.
- `data/corpus/dev-1/unverified-seed-comparison-free.json`: cache-only real-set report replay,
  still explicitly unverified.
- `tests/test_{semantics,stage2a_scorer,stage2b_alignment,stage2b_corpus,stage2b_fuser,stage2b_pipeline}.py`:
  regression coverage for every code finding fixed in this review.

### How to run the reviewed implementation

~~~powershell
uv run id-detector benchmark run --corpus controlled-synth-1 --profile free --out data/corpus/controlled-synth-1/baseline-free.json --work-root data/local/work-controlled-stage2b
uv run id-detector benchmark run --corpus dev-1 --profile free --set-id dev1-set-004 --max-requests 1 --out data/corpus/dev-1/unverified-seed-comparison-free.json --work-root data/local/work-dev1-live
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python scripts/audit_fixtures.py
~~~

The second command selects the already-cached canonical source when that media key exists under
the requested work root. Its observed replay made zero provider requests.

### Controlled baseline regeneration

~~~text
scored 25 sets; unverified_seed_comparison=false; report=data\corpus\controlled-synth-1\baseline-free.json
controlled config_hash=c76cfa271904317ecf5ea694db07a96db510fe256476d1fe781774537ffc2af6 sets=25 unverified=False work=10000/10000 version=10000/10000 segment=8625/10000
controlled_manifest_frozen=True sets=25
~~~

### Cached real-set before and after

Before the fix:

~~~text
episodes=55
T distribution=1:19,2:8,3:9,4:2,5:9,6:4,7:1,10:2,21:1
tracklist rows=55
badges=unclear:55
durations={evidence_supported_ms:0,predicted_episode_ms:0,unresolved_boundary_ms:770123,unclear_ms:1749000,no_evidence_ms:6000,unscanned_ms:0}
~~~

After cache-only re-fusion:

~~~text
187 matches; 0 failures; 0 physical attempts; 39 episodes
episodes=39
T distribution=1:17,2:4,3:2,4:3,5:3,6:1,7:1,9:1,10:1,11:1,12:1,15:1,19:2,21:1
tracklist rows=39 (39 track, 0 gap)
badges=likely:6,possible:16,unclear:17
version_status=unverified:39
durations={evidence_supported_ms:1578000,predicted_episode_ms:0,unresolved_boundary_ms:770123,unclear_ms:171000,no_evidence_ms:6000,unscanned_ms:0}
cited continuous candidate: occurrences 4 -> 1; T=19; support=[225000,399000]
live config_hash=86ca85117f2b25deac844b57bcd08bf080e4f5ab075026342d813019bf5642c5; requests=0; physical_attempts=0
~~~

### Final verification

~~~text
uv run pytest -q
221 passed, 1 deselected, 1 warning in 52.36s

uv run ruff check .
All checks passed!

uv run ruff format --check .
75 files already formatted

uv run python scripts/audit_fixtures.py
audited 146 files
fixture audit passed

git diff --check
exit 0
~~~

The pytest warning is pydub's Python-3.13 `audioop` deprecation; this project runs Python 3.12.
No live pytest was run. The only real-provider operation was a cache replay, which reported zero
physical attempts.

### Plan-silent review decisions

- Decoded corpus duration may differ from recorded truth duration by at most 500 ms, matching the
  decoder's existing ffprobe-versus-PCM tolerance; SHA-256 must match exactly.
- An odd-width calibrated PI uses floor division for its deterministic integer centre.
- Current-input conflicts always veto. Only explicitly supplied prior recording components can be
  contested by a newly discovered conflict.

### Deferred

None of the P2 findings were deferred. CR-2B-01 is a P1 and is not being mislabelled as deferred.

### Disputed findings

None.

### Blocked finding

- CR-2B-01 correctly identifies a real Stage-2b acceptance gap. It cannot be completed without
  independent annotation evidence: the earlier task explicitly prohibited fabricating human
  truth; the six seed timelines were assembled before recognition and intentionally say
  `draft=true`, `verified_against=null`, and `annotator_ref=null`. The manifest says
  `frozen=false`, and `truth freeze` rejects it. Calling those seeds verified would invalidate the
  benchmark. Evidence: six sets/120 draft episodes remain, the source media hashes match their
  truth records, and the scorer independently derives this corpus as unverified. Stage 2b remains
  incomplete until the owner performs first-pass listening/annotation, or explicitly authorizes
  the review's alternative plan revision. `docs/PLAN.md` was not changed by these review fixes.

### Known gaps after review

- The named full frozen `dev-1` baseline remains blocked on independent first-pass annotation of
  all six local mixes. The exact verification/freeze commands in the earlier **Known gaps**
  section remain current.
- The cache-only real report covers one set and is explicitly an unverified seed comparison, not
  an accuracy claim. The frozen controlled baseline remains the only verified full baseline.

### What the next stage needs to know after review

- Do not begin Stage 3 or use `dev-1` for accuracy claims until CR-2B-01 is resolved by genuine
  annotation and a regenerated hash manifest/full report.
- Pass prior-generation recording components explicitly when a later generation discovers a
  conflict; assertion IDs never encode discovery order.
- Preserve all final matches for episode proof/evidence while selecting one hypothesis per
  logical trial only for votes and alignment.
- Populate every new benchmark's full `run_config`; partial and full populations intentionally
  have different natural keys and cannot pass regression comparison against each other.
