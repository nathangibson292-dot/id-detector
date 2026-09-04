# Stage 3 - Adapters and shortlist

## What was built

### File map

- `src/id_detector/providers/base.py` and `providers/__init__.py`: common capability and error
  types, non-secret TOML configuration, and the two-part third-party upload consent gate.
- `src/id_detector/providers/audd.py`: AudD Enterprise `file_scanner` query construction for file
  and URL inputs, synchronous job lifecycle, duration-based integer-cent reservation and response
  reconciliation, chunk observations, and explicit unreconcilable `outcome_unknown` handling.
  Its documented anchor convention is mix = chunk `offset`, reference = song `timecode`;
  `start_offset` and `end_offset` delimit the evidence span inside the chunk.
- `src/id_detector/providers/acrcloud.py`: ACRCloud File Scanning reconciliation, upload by exact
  `cache_key`, durable remote id, restartable poll, 30-second to 5-minute backoff with a 48-hour
  ceiling, music/custom-bucket parsing, upload limit, entitlement-field validation, and billing.
  Its reliable anchor convention is mix = `offset * 1000 + sample_begin_time_offset_ms`,
  reference = `db_begin_time_offset_ms`; the play-offset fallback is explicitly unreliable.
- `src/id_detector/providers/panako.py` and `src/id_detector/doctor.py`: disabled
  `local_index_query` capability, configurable local index path, no Java invocation, and the
  required `JDK not found — Panako disabled` doctor result. Reference-pool recognition is excluded
  from v1 pending the owner's JDK decision.
- `src/id_detector/jobs.py`: scanner unit/cost reservations, physical network-attempt accounting,
  reconciliation against reserved integer units/cents, conservative unknown-outcome accounting,
  and durable remote-reference support. Existing clip-recognizer accounting remains compatible.
- `src/id_detector/benchmark/shortlist.py` and `src/id_detector/cli.py`: `benchmark shortlist` runs
  each available engine independently over every controlled set, scores with the Stage 2a scorer,
  and reports per-engine metrics/status/cost, pairwise agreement, and union/oracle coverage.
- `src/id_detector/contracts.py`, `src/id_detector/io.py`,
  `docs/schemas/shortlist_report.schema.json`, and `tests/golden/shortlist_report.json`: strict
  shortlist contract/schema plus expanded credential-name redaction.
- `provider_configs/{audd,acrcloud,panako}-v1.json` and completion sidecars, with packaged copies
  under `src/id_detector/resources/provider_configs/`: immutable, non-secret Stage 3 settings.
- `tests/fixtures/audd/` and `tests/fixtures/acrcloud/`: clearly labelled, authored synthetic,
  shape-accurate response fixtures. They are not represented as recordings of live service calls.
- `tests/test_stage3_providers.py`: request/parser vectors, known-insertion anchor conversions,
  opt-in refusals, integer-cent billing, AudD lost-response handling, ACRCloud reconciliation,
  five failure points with exactly one upload, remote-id restart, and Panako unavailability.
- `tests/test_stage3_entitlements.py`: opt-in live entitlement probes which skip with exact missing
  credential messages. Live tests remain excluded from the default pytest selection.
- `tests/test_stage3_shortlist.py`: committed-report/CLI/status/no-cascade regression coverage.
- `.env.example`, `id-detector.example.toml`, `.gitignore`, and `README.md`: credential names,
  safe disabled-by-default local config, ignored real config, and commands.
- `data/corpus/controlled-synth-1/shortlist.json`: the requested committed-location shortlist.
  Prediction documents and raw provider evidence remain under ignored `data/local/` paths.

`yt-dlp` was already present as a project dependency from the earlier stages and remains invoked
through the uv environment. `shazamio==0.8.1` and `shazamio-core==1.1.2` remain exactly pinned.

## How to run it

From the repository root in PowerShell:

```powershell
uv sync --dev
uv run id-detector doctor
uv run id-detector benchmark shortlist --corpus controlled-synth-1 `
  --out data/corpus/controlled-synth-1/shortlist.json `
  --work-root data/local/work-shortlist-stage3

uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
uv run python scripts/audit_fixtures.py
```

With no paid credentials, the command still evaluates `local_fixture` and Shazam over all sets,
does not cascade or suppress either engine, and records both paid engines as
`not_evaluated (no credentials)`.

## What was verified and how

### Controlled shortlist and live Shazam result

The shortlist was run directly with network access for Shazam:

```powershell
uv run id-detector benchmark shortlist --corpus controlled-synth-1 `
  --out data/corpus/controlled-synth-1/shortlist.json `
  --work-root data/local/work-shortlist-stage3
```

Key command output:

```text
shortlisted 5 engines on controlled-synth-1; local_fixture=evaluated (fixture oracle; not a production engine), shazam=evaluated, audd=not_evaluated (no credentials), acrcloud=not_evaluated (no credentials), panako=excluded (JDK not found; pending owner's JDK decision); report=data\corpus\controlled-synth-1\shortlist.json
```

The report records 25 sets for each evaluated engine. `local_fixture` emitted 79/79 matches with
work precision/recall 10000/10000. Shazam made 73 physical attempts and emitted 73 observations.
Contrary to the expected no-match outcome, one response was a false identification of synthetic
audio, so its honest match count is 1, false-discovery rate is 10000, and work precision/recall is
0/0. The local-fixture/Shazam work-set Jaccard agreement is 0. Including the explicitly labelled
fixture oracle, union/oracle work-occurrence coverage is 10000. Total actual cost is 0 cents.

The paid-engine conservative estimates for this 25-set corpus are 29 cents for AudD and 29 cents
for ACRCloud. No paid request was made.

### Authored fixtures and durable failure vectors

The default suite covers the AudD multipart fields (`file`, `limit`, `skip`, `every`,
`accurate_offsets`), both anchor formulas, required native fields and identifiers, URL/file gates,
an AudD lost response, ACRCloud exact-name adoption, remote-id recovery, and failure injection
before network, during upload, after acceptance, after remote-id persistence, and during polling.
Each ACRCloud recovery vector records exactly one upload. A one-hour AudD reservation settles at
150 cents; ACRCloud's one-hour cost helper settles at 140 cents. Fixture audit output:

```text
audited 155 files
fixture audit passed
```

### Credential-contingent entitlement smoke

Command:

```powershell
uv run pytest -q -m live tests/test_stage3_entitlements.py -rs
```

Observed result (no network request was attempted because credentials were absent):

```text
ss                                                                       [100%]
SKIPPED [1] tests\test_stage3_entitlements.py:44: AudD not evaluated (no credentials): AUDD_API_TOKEN is not set
SKIPPED [1] tests\test_stage3_entitlements.py:73: ACRCloud not evaluated (no credentials): missing ACRCloud credentials: ACRCLOUD_HOST, ACRCLOUD_ACCESS_KEY, ACRCLOUD_ACCESS_SECRET, ACRCLOUD_CONTAINER_ID
2 skipped, 1 warning in 1.12s
```

With credentials, these opt-in tests validate authentication, container identity, the upload-size
limit, upload/poll access, result shape, and conservative per-probe integer-cent cost. Shape-
accurate required match fields are additionally asserted against the authored fixture because a
tiny generated live probe is not guaranteed to match a catalogue recording.

### Doctor, tests, formatting, lock, and privacy

Relevant doctor output:

```text
yt-dlp module       PASS    2026.08.19
Shazam signature    PASS    shazamio 0.8.1, shazamio-core 1.1.2; 12 s WAV signed offline
Panako              WARN    JDK not found — Panako disabled
```

Required final `uv run pytest -q` output:

```text
........................................................................ [ 29%]
........................................................................ [ 59%]
........................................................................ [ 88%]
...........................                                              [100%]
243 passed, 3 deselected, 1 warning in 52.24s
```

The sole warning is pydub's Python 3.13 `audioop` deprecation; this project runs Python 3.12.

Required final `uv run ruff check .` output and supplementary checks:

```text
All checks passed!
85 files already formatted
Resolved 53 packages in 2ms
git diff --check: exit 0
```

## Review fixes

Applied the findings from `docs/reviews/code-review-stage-3.md` on 2026-09-04. Finding labels
below follow their order within each priority in that review.

| Finding | Outcome and change | Property test |
|---|---|---|
| P1-1 AudD manual-retry billing | Fixed. Scanner reservations are idempotent after a crash. `retry --acknowledge-billing` now atomically settles the unknown reservation as used exposure, clears it from the job, and extends the hard ceiling by exactly one replacement reservation. Later completion accumulates rather than erases that first possible charge. | `test_audd_reservation_is_idempotent_after_crash_before_submission`; `test_audd_acknowledged_retry_preserves_unknown_charge_and_funds_retry` |
| P1-2 scanner caches | Fixed. The shortlist applies the common 180-day positive and 30-day no-match predicate before leasing, rebuilds observations from valid raw JSON, never caches errors, resets expired/malformed/explicitly refreshed terminal jobs, reports zero attempts/cost for a hit, and exposes `benchmark shortlist --refresh`. Cache reads need neither credentials nor upload consent. | `test_scanner_cache_ttl_boundaries_and_errors_never_cache`; parametrized `test_scanner_rerun_reconstructs_positive_and_no_match_from_cache`; `test_scanner_error_is_not_cached_and_next_run_attempts_transport`; CLI refresh test |
| P1-3 ACRCloud 48-hour ceiling and heartbeat | Fixed. `submitted_at` and the first `submission_started_at` are preserved across recovery. Poll delay resumes from durable elapsed time, the absolute deadline is checked before each request/sleep, expiry terminalizes as `permanent_failure` with conservative billing, and both scanner executors heartbeat every 15 seconds. | `test_acrcloud_persistent_deadline_terminalizes_without_another_poll`; `test_scanner_executor_heartbeats_during_long_provider_work`; capped-backoff test |
| P1-4 known provider failures | Fixed. AudD and ACRCloud acknowledged HTTP/protocol failures, malformed final responses, and ACRCloud states `-2`/`-3` terminalize as `permanent_failure` and settle their reservation. Only lost transport responses use `outcome_unknown`; persisted ACRCloud remote ids remain safely pollable. | `test_audd_known_http_failure_is_terminal_not_outcome_unknown`; parametrized `test_acrcloud_known_failures_are_terminal`; existing ambiguous-response recovery vectors |
| P1-5 provider error redaction | Fixed. Provider HTTP exceptions omit response bodies, provider JSON is recursively redacted before use/storage, and shortlist CLI exception text passes through `redact_text()`. | `test_provider_http_error_bodies_never_expose_supported_secret_fields`; `test_shortlist_cli_passes_refresh_and_redacts_failures` covers every supported secret field |
| P2-1 repeated-occurrence union coverage | Fixed. Union coverage retains occurrence indices and evidence spans, coalesces the same cross-engine occurrence, applies the scorer's 30-second compatibility margin, and consumes predictions one-to-one in time order. | `test_union_coverage_associates_repeated_occurrences_one_to_one` |
| P2-2 headline shortlist tests | Fixed. Engine runners are injectable and the orchestration test proves all four eligible runners execute even after a successful engine. Five crash vectors now assert exact physical-attempt totals, exact cache-key filenames, one upload, and conservative exposure. Local upload-limit, TTL, retry-billing, and deadline boundaries were added. | `test_shortlist_executes_every_eligible_engine_without_cascade`; expanded five-point recovery test; boundary tests named above |
| P2-3 Windows file preflight and attempt count | Fixed. Both adapters use `native_path()`-aware file checks; AudD opens the file before calling the physical-attempt callback. ACRCloud already opens before its upload callback and retains the native-path size check. | `test_native_path_preflight_accepts_windows_long_paths`; parametrized `test_file_open_failure_does_not_count_physical_attempt` |

### Review verification

No live test was run during the review-fix pass; all provider traffic in default tests used
`httpx.MockTransport`.

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python scripts/audit_fixtures.py
```

Observed output:

```text
........................................................................ [ 27%]
........................................................................ [ 54%]
........................................................................ [ 81%]
................................................                         [100%]
264 passed, 3 deselected, 1 warning in 55.00s
All checks passed!
86 files already formatted
audited 156 files
fixture audit passed
```

The warning remains pydub's Python 3.13 `audioop` deprecation while this project is pinned to
Python 3.12. `git diff --check` also exited 0.

### Deferred

None. All P2 changes were small and safely covered, so none were deferred.

### Disputed findings

None. Each review finding reproduced by inspection or a new regression test.

### Review-specific plan-silent decision

An expired, malformed, error, or explicitly refreshed paid-scanner result needs a genuinely new
execution while historical used budget must remain auditable. The shortlist therefore extends
that media/provider ceiling by exactly one duration-derived reservation immediately before
resetting the terminal job. Cache hits do not extend ceilings or consume budget.

## What the owner must do to evaluate paid engines

Only do this for audio the owner owns or has permission to upload. Obtain service credentials
directly from the providers; do not put values in TOML, source files, command history, manifests,
or logs. In a PowerShell session, set exactly:

```powershell
$env:AUDD_API_TOKEN = '<AudD Enterprise token>'
$env:ACRCLOUD_HOST = 'api-eu-west-1.acrcloud.com'
$env:ACRCLOUD_ACCESS_KEY = '<File Scanning developer bearer access token>'
$env:ACRCLOUD_ACCESS_SECRET = '<ACRCloud access secret>'
$env:ACRCLOUD_CONTAINER_ID = '<File Scanning container id>'
```

The current ACRCloud File Scanning Console API uses bearer authentication. To preserve the exact
environment contract required by this task, its developer bearer token is supplied through
`ACRCLOUD_ACCESS_KEY`; the secret is loaded from the environment but is not transmitted by the
bearer API. The entitlement smoke is the authoritative validation of the account/container.

Copy and edit the non-secret local config, then run the entitlement probe and shortlist:

```powershell
Copy-Item id-detector.example.toml id-detector.toml
# Edit id-detector.toml so it contains: allow_third_party_upload = true
uv run pytest -q -m live tests/test_stage3_entitlements.py -rs
uv run id-detector benchmark shortlist --corpus controlled-synth-1 `
  --out data/corpus/controlled-synth-1/shortlist.json `
  --config id-detector.toml --i-own-this-audio-or-have-permission `
  --work-root data/local/work-shortlist-stage3-paid
```

Conservative expected reservations are 1 cent for each tiny entitlement probe, 29 cents for the
full controlled corpus on AudD, and 29 cents for the full controlled corpus on ACRCloud: at most
58 cents for the two full trial runs, plus the probes. The estimates use the plan's 150 cents per
audio hour for AudD and anecdotal 140 cents per audio hour for ACRCloud, rounded upward per file.
Provider-console pricing and entitlement limits must be checked by the owner before enabling the
gate.

## Plan-silent decisions

- A missing config file is equivalent to `allow_third_party_upload = false`. Both the config bit
  and the per-invocation CLI flag are checked before a file is opened or a network attempt counted.
- AudD selects the highest-score song if a returned chunk contains multiple songs, breaking ties
  by canonical JSON bytes, while retaining the full songs array under `native`.
- ACRCloud paired sample/database offsets are reliable anchors. A `play_offset_ms` fallback is
  retained as evidence but marked unreliable. Music and own-bucket hits are simultaneous sources,
  not mutually exclusive alternatives.
- Pairwise agreement is corpus-wide per-set Jaccard agreement over normalized emitted work sets.
  Union/oracle coverage is work-occurrence recall across independently evaluated engines. At this
  controlled-only stage they are equal and include the explicitly labelled fixture oracle.
- Paid expected cost is conservatively rounded upward for each corpus file, not only after summing
  the corpus duration. Reservations therefore cannot understate integer-cent exposure.
- Scanner raw responses and prediction documents stay in ignored local work paths. Only aggregate,
  URL-free, pseudonymous shortlist output is committed to the corpus directory.

## Known gaps

- AudD and ACRCloud were not live-evaluated because the owner supplied no credentials. Their
  request and result behavior is tested against authored shape-accurate fixtures, not represented
  as captured provider traffic. Entitlement, current pricing, and real catalogue coverage remain
  unknown until the owner follows the gated procedure above.
- A tiny generated ACRCloud probe can validate access/upload/polling but may legitimately return no
  match. Required match fields are fixture-validated; a catalogue or own-bucket match is required
  to observe them live.
- Panako is a capability skeleton only. No JDK was installed and no Java subprocess was started.
  Reference-pool recognition is excluded from v1 pending the owner's JDK decision.
- `dev-1` remains unverified as recorded by Stage 2b. The controlled corpus is the only eligible
  exact truth used here, so these numbers do not estimate real-mix catalogue coverage.
- The one Shazam synthetic false positive shows why controlled-signal behavior must not be reported
  as real-audio accuracy and why raw evidence remains auditable.

## Deviations from plan

- The Stage 3 Build order row names `dev-1`, but that corpus is still a six-set unverified draft
  blocked on owner annotation. The task explicitly required the current run on
  `controlled-synth-1`; Stage 3 therefore does not invent accuracy numbers from the draft corpus.
- The explicit credential/JDK contingency supersedes live paid and Panako evaluation: both paid
  adapters are complete against authored fixtures, missing credentials skip cleanly, and Panako is
  excluded. `docs/PLAN.md` was not changed.

## What the next stage needs to know

- Use `ObservationRecord` anchors exactly as documented above and preserve all `native` timing and
  score fields when scanner observations enter fusion. `transform` is null for whole-file scans.
- Never cascade shortlist engines. Availability changes status; it does not let Shazam suppress a
  paid or local engine.
- AudD response loss is unreconcilable and remains `outcome_unknown`; do not auto-resubmit it.
  ACRCloud can safely resume polling a persisted remote id and must reconcile by exact cache-key
  filename before any re-upload.
- Keep scanner budgets in integer provider units/cents and reserve before network submission.
- Paid profiles must remain non-default and behind both consent gates. Do not broaden credential
  discovery beyond the documented environment variables.
- Resolve the owner's JDK decision before introducing any reference-pool build/query path. Until
  then, the v1 reference-pool status is explicitly excluded.
