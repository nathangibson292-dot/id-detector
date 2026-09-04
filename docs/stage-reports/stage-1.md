# Stage 1 — Plumbing

## What was built

### File map

- `src/id_detector/ingest.py`: yt-dlp best-audio ingestion with info JSON, no comment crawl,
  platform canonicalisation, source/media hashes, cache lookup, immutable `ingest/source.json`, and
  sidecar.
- `src/id_detector/decode.py`: full non-seeking ffmpeg decode to `decode/audio.pcm` at 16 kHz mono
  s16le, raw-PCM ffprobe duration validation, `decode/pcm.json`, cache validation, and sidecar.
- `src/id_detector/windows.py`: generation-0 12 s / 9 s schedule, end-anchored tail, short-input
  rule, exact sample-index WAV writes, identity sample maps, transform slice/map helpers, JSONL,
  and sidecar. Only the `none` hypothesis is materialised or exposed.
- `src/id_detector/shazam.py`: pinned shazamio adapter using an injected single-attempt httpx
  client, 18/min token bucket, 10/30/60 s timeouts, five retries owned by the job executor, plan
  backoff/Retry-After handling, 5/60 s breaker, native match conversion, and bias-aware anchor
  aggregation.
- `src/id_detector/calibration.py`: live five-position × 6/8/12 s × full/75%/50% insertion suite,
  first-fingerprinted-sample offset measurement, and a position-level time-to-first-success ECDF
  that retains failed cases as censoring; config writes are new-version-only.
- `provider_configs/shazam-v1.json`, `shazam-v2.json`, `shazam-v3.json` and sidecars: immutable
  measurements. V3 is active; v1/v2 remain untouched as historical evidence.
- `src/id_detector/jobs.py`: WAL SQLite, 5 s busy timeout, one asyncio writer/queue, leases,
  15-second heartbeats, startup recovery, explicit outcome-unknown handling, transactional request
  reservations/reconciliation, and the default 2,000-request Shazam ceiling.
- `src/id_detector/submission.py`: failure-injectable durable submit/reconcile/poll protocol used by
  conformance tests.
- `src/id_detector/recognise.py`: cache-key-deduplicated gen-0 queries with observation fan-out,
  invocation-scoped immutable query/raw/index/observation sets, 180-day positive and 30-day
  no-match TTLs, refresh without historical replacement, v3 selection, scoped leasing, and
  attempt accounting.
- `src/id_detector/process.py`: Windows Job Object launcher using `CreateJobObject`,
  `KILL_ON_JOB_CLOSE`, suspended `CreateProcess`, `AssignProcessToJobObject`, and `ResumeThread`;
  cancellable short Win32 waits and bounded psutil tree cleanup elsewhere.
- `src/id_detector/journal.py`: atomic append-only invocation journal with run id, timestamps, tool
  versions, stage timings, request/attempt counts, and integer cost.
- `src/id_detector/cli.py`: `analyse --raw`, `show`, billing-acknowledged `retry`,
  `calibrate-shazam`, source/media process locks, secret-safe journaling, and the existing
  `doctor`; Windows output is forced to UTF-8.
- `src/id_detector/resources/`: wheel-packaged job SQL and immutable Shazam v1/v2/v3 configs;
  runtime loading uses `importlib.resources` rather than a repository-relative path.
- `src/id_detector/contracts.py`, exported invocation schema, and invocation golden: the Stage 0
  journal record was extended with tool versions and timings.
- `tests/test_stage1_windows.py`: schedule/tail/short input, exact sample slices, transform helper
  vectors, Unicode/space/long paths, corrupt input, and short media.
- `tests/test_stage1_jobs.py`: reclaim, budget reserve/reconcile, all five failure points, abrupt
  process death and restart, and exactly-one-submission assertions.
- `tests/test_stage1_shazam.py` plus `tests/fixtures/shazam/`: recorded match/no-match responses,
  measured/unmeasured anchors, TTL/refresh, multiple config generations, fake-server attempt
  accounting including retry, raw artifacts, and Unicode.
- `tests/test_stage1_process.py`: Windows timeout and real asyncio-cancellation tests for a
  downloader-shaped parent and its ffmpeg-shaped child, including prompt return and descendant
  death.
- `tests/test_stage1_privacy.py`: credential-query/userinfo rejection and invocation URL redaction.
- `tests/test_stage1_wheel.py`: isolated built-wheel resource loading and SQLite-store smoke.
- `tests/test_stage1_live.py`: opt-in `live` insertion-suite smoke, skipped by default.
- `README.md`, `.env.example`, `.gitignore`: Stage 1 commands, opt-in live input, and local work
  exclusions.

## Review fixes

| Finding | Outcome and change | Regression |
|---|---|---|
| P1 Duplicate WAV/cache key | **Fixed.** One query/job/raw entry per cache key; the response fans out deterministically to every equivalent window. | `test_duplicate_wavs_submit_once_and_fan_out_timed_observations` asserts one HTTP attempt and two correctly timed observations. |
| P1 Refresh double-charge | **Fixed.** `finish()` charges only `reserved_units`; cumulative `physical_attempts` remains the job audit total. | `test_refresh_charges_only_new_physical_attempts` covers two refresh attempts and a no-network refresh. |
| P1 Obsolete-config lease | **Fixed.** Recognition leases only the active media/provider/query-id set. | The cache/config regression leaves an old pending job untouched while the active run completes. |
| P1 Immutable artefacts | **Fixed.** Every run writes `recognise/invocations/<key>/...`; cache hits copy historical raw bytes and refresh never replaces them. Immutable writers reject differing existing content. | The refresh regression asserts three namespaces, unchanged first-run bytes, and distinct raw references. |
| P1 Invalid `L_min_ms` | **Fixed.** Latency is a position-level first-success ECDF; failed shorter cases are censored bounds and all-failure positions right-censor unattained quantiles. New immutable v3 was measured live and selected. | Controlled success/failure matrices test the curve, quantiles, and rejected censored p90/p95; live test audits curve totals. |
| P1 Startup steals live submission | **Fixed.** Cross-process locks enforce one owner; all submission recovery applies the 2×lease heartbeat cutoff; ingest/decode/window temps are uniquely owned. | `test_live_store_is_locked_and_fresh_submission_is_not_recovered` plus stale-recovery and crash tests. |
| P1 Ctrl-C blocked | **Fixed.** The two-hour executor wait is replaced by 50 ms asynchronous polling; cancellation closes the Job Object and drains readers. | Windows test cancels a 600 s parent/descendant tree, requires return under 3 s, and proves the descendant died. |
| P1 Credential URLs persisted | **Fixed.** URL userinfo and credential query keys are rejected before ingest writes; journal arguments independently redact both. | Query-token, signed-query, URL-userinfo, and persisted-journal tests. |
| P1 Fixture audit omission | **Fixed.** `tests/fixtures` is now an audit root. | A forbidden `platform_id` placed there is detected. |
| P2 Retry-owner test | **Fixed.** Fake HTTP 500→200 now runs through `recognise_generation_zero()` and `_run_job`. | Asserts two server requests, two physical attempts, one lease attempt, and terminal `no_match`. |
| P2 Long-path claim | **Fixed.** Remaining filesystem, ffmpeg, Shazam and SQLite paths use extended Win32 paths. | A path explicitly over 260 characters exercises ingest, decode, windows, raw cache, SQLite, temp cleanup and recursive cleanup. |
| P2 Wheel resources | **Fixed.** SQL and all measured configs are package resources; CLI no longer derives a repository root from `__file__`. | The test builds a wheel, imports from the wheel outside the repo, selects v3, and creates a SQLite store. |

### Deferred

None.

### Disputed findings

None.

## How to run it

From the repository root in PowerShell:

```powershell
uv sync --dev
uv run id-detector doctor

$track = "PUBLIC_RELEASE_URL"
uv run id-detector calibrate-shazam --track $track --positions "10,40,70,100,140"

$mix = "PUBLIC_MIX_URL"
uv run id-detector analyse $mix --raw
uv run id-detector analyse $mix --raw
uv run id-detector show SOURCE_KEY

uv run id-detector retry --acknowledge-billing JOB_ID

uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
uv run python scripts/audit_fixtures.py
```

The full live pytest matrix is opt-in:

```powershell
$env:ID_DETECTOR_LIVE_TRACK_URL = "PUBLIC_RELEASE_URL"
uv run pytest -q -m live
```

## What was verified

### Calibration

The harness was run directly against the public released track “Mountkid - Dino [NCS Release]”.
The first immutable measurement exposed a partial-overlap origin error and was retained rather
than edited:

```text
v1: cases=45 successes=40 physical_attempts=45
adapter_bias_ms=-1068 adapter_bias_uncertainty_ms=1998
L_min_ms={p50:6000,p90:9000,p95:12000}
```

The corrected first-fingerprinted-sample calculation was measured afresh:

```text
v2: cases=45 successes=40 physical_attempts=45
adapter_bias_ms=-68 adapter_bias_uncertainty_ms=5
L_min_ms={p50:6000,p90:9000,p95:12000}
```

Those historical configs were not edited. The first review-fix calibration run deliberately
refused to publish because the initial clustered-layout estimator could not estimate p90:

```text
latency p90 is right-censored above 12000 ms
```

That exposed that layouts, rather than insertion positions, were still influencing the unit of
independence. With the position-level time-to-first-success ECDF, the same full live harness wrote
the new immutable active config:

```text
provider_config=provider_configs/shazam-v3.json
adapter_bias_ms=-56 adapter_bias_uncertainty_ms=5
L_min_ms={p50:3000,p90:3000,p95:3000}
cases=45 successes=40 physical_attempts=45
```

At 3,000 ms, all five independent positions had a successful recognition. The five failed cases
at other duration/layout combinations remain in `latency_success_curve` via `n_trials` and
`n_successes`; a position with no success would keep higher quantiles right-censored and prevent a
measured config from being written. The adapter deterministically selects v3 and applies `-56 ms`.

### Roughly one-hour live analysis

The chosen public mix was “DJ Three 60 min Boiler Room mix”, duration 3,587,506 ms after full
decode. The active v2 run used the default budget:

```text
wall_ms=1349672
logical_requests=399
physical_attempts=401
matches=301
no_match=98
failures=0
cost_usd_e2=0
```

The two extra attempts were transient provider retries. Active-query SQLite reconciliation was:

```text
no_match: 98 jobs / 98 physical attempts
succeeded: 301 jobs / 303 physical attempts
```

The generation audit output was:

```text
validated 399 windows 399 queries 399 observations 399 raw responses
sidecars: ingest=True decode=True windows=True queries=True raw_index=True observations=True
tail: start_ms=3575506 support_ms=[3575506,3587506] reason=tail
```

The immediately repeated analysis proved the cache path:

```text
301 matches; 0 failures; 0 physical attempts
duration_ms=2547 cache_hits=399 logical_requests=0 physical_attempts=0
```

### Ctrl-C, crash, and retry ownership

The live mix was interrupted once after 18 completions. The persisted snapshot was:

```text
no_match=2 pending=380 submission_started=1 succeeded=16
physical_attempts=19
```

On restart, the in-flight row became `outcome_unknown`; it was not automatically submitted. The
explicit billing-acknowledged retry returned it to pending, and the run resumed from the 18 cached
responses. Unit failure injection at before-network, during-upload, after-acceptance,
after-remote-id-persistence, and during-polling produced exactly one submission in every case.
An additional test kills a separate Python worker after simulated remote acceptance, restarts the
store, reconciles by cache key, and proves no second submission.

The injected-client fake HTTP server test observed:

```text
server_requests=2 job_physical_attempts=2
```

The first response was retryable failure and the second succeeded, proving that library retries
are disabled and every physical attempt is visible to the store.

### Runtime and required checks

`uv run id-detector doctor`:

```text
uv                  PASS    uv 0.12.5
Python              PASS    3.12.14
ffmpeg              PASS    ffmpeg 8.1.2
ffprobe             PASS    ffprobe 8.1.2
yt-dlp module       PASS    2026.08.19
Node                PASS    v20.18.1
Shazam signature    PASS    shazamio 0.8.1, shazamio-core 1.1.2; 12 s WAV signed offline
Visual C++ runtime  PASS    vcruntime140 and vcruntime140_1 loadable
Free disk           PASS    501 GiB available
```

Post-review `uv run pytest -q`:

```text
140 passed, 1 deselected, 1 warning in 11.12s
```

The warning is pydub’s Python 3.13 `audioop` deprecation; this project is pinned to Python 3.12.

`uv run ruff check .`:

```text
All checks passed!
```

`uv run ruff format --check .`:

```text
51 files already formatted
```

`uv lock --check`:

```text
Resolved 53 packages in 1ms
```

`uv run python scripts/audit_fixtures.py`:

```text
audited 78 files
fixture audit passed
```

The direct v3 CLI calibration was a live network test. Its successful run made 45 Shazam requests
and produced 40 matches; the preceding rejected estimator run also completed its 45-case matrix
but wrote no config. The opt-in pytest live test itself was not rerun because it executes the same
45-request harness; it remains marked `live` and excluded from the default suite.

## Known gaps

- This is plumbing only. There is deliberately no fuser, identities/episodes output, rescan loop,
  or transform CLI; those remain assigned to Stages 2b and 4b.
- Shazam is unofficial and can change or throttle. The pinned libraries, fake-server fixtures,
  single retry owner, breaker, cache, and request ceiling limit the operational risk.
- The original hour-long run and its abrupt-kill observation remain historical v2 evidence. The
  post-review Windows cancellation regression now exercises genuine task cancellation, prompt
  return, and Job Object descendant termination.

## Deviations from plan

- No capability-stage deviation was made. No fuser, episodes, rescan generation, or transformed
  query option was added.
- Stage 0 called the run identifier `invocation_id`; Stage 1 retains that established field as the
  plan’s `run_id` rather than creating a duplicate field. The journal schema was extended only for
  the explicitly required tool-version and timing maps.
- Provider raw JSON can contain decimals while canonical artifacts prohibit floats. Full raw
  response shape is retained with decimals rendered as exact JSON strings; observation-native
  match values use the Stage 0 fixed-point names (`offset_ms`, `frequencyskew_e6`,
  `timeskew_e6`).
- V1/v2 were left byte-for-byte untouched, as permitted by the review instructions. Their latency
  fields were produced by the superseded estimator; v3 records the corrected estimator and is the
  only version selected by the adapter.
- Recognition-owned artifacts now live one level below the literal example paths, under
  `recognise/invocations/<invocation_key>/`. This invocation namespace is the review-requested way
  to reconcile `--refresh` and provider-config changes with immutable historical evidence.
- Runtime copies of provider configs are packaged as resources. Calibration still writes new
  immutable versions to the current project’s `provider_configs/` directory.
- Public input URLs and platform identifiers are intentionally absent from this committed report
  under the committed-corpus policy. The exact invocation remains in ignored local work data.

## What Stage 2a needs to know

- The active provider config is `shazam-v3.json`; use its `-56 ms` bias, `5 ms` uncertainty and
  position-level `L_min_ms={p50:3000,p90:3000,p95:3000}` in controlled scorer vectors. Do not
  consume v1/v2 latency values as active measurements.
- Current observation artifacts contain one final record per gen-0 logical trial, including when
  identical WAVs share one physical query, plus full native Shazam tuples, corrected anchors and
  invocation-local raw-response references. They are inputs, not fused claims.
- Query order follows the plan’s id ordering; human `--raw` output is separately sorted by mix
  time. Windows are sample-exact and the final tail can be non-hop-aligned.
- `jobs.sqlite` may retain prior provider-config query generations. Recognition leasing and
  invocation counts are now scoped to active query ids; later workers must preserve that rule.
- The generic failure-injection harness is available for later scanner adapters, including remote
  reconciliation and poll resumption.
