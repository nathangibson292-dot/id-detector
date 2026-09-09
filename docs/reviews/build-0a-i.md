# Build 0a-i — Crash, cache states, `/healthz`, fixtures, fakes, injection, journal assertions

## File-by-file changes

- `.gitignore` — allowed only the committed 60-second WAV while keeping generated audio, including `tone-3600s.wav`, ignored.
- `docs/schemas/invocation_journal_entry.schema.json` — added the terminal `provider_unavailable` invocation status.
- `docs/reviews/v2-review-hosting.md` — mechanically normalized pre-existing Python-fence formatter drift required by the repository-wide format gate.
- `scripts/assert_journal.py` — added newest-invocation assertions with top-level, counts/costs, and dotted-field lookup.
- `scripts/audit_fixtures.py` — excluded binary PNG/WAV files from text-pattern auditing.
- `scripts/make_audio_fixtures.py` — added deterministic 60-second and on-demand 3,600-second three-tone WAV generation.
- `scripts/smoke_serve.ps1` — added the Windows background serve, health/home polling, assertion, and process cleanup gate.
- `scripts/smoke_serve.sh` — added the POSIX equivalent smoke gate.
- `src/id_detector/cli.py` — fixed paid-primary branch locals, stopped silent paid-to-free fallback, accumulated gap-fill counts, added cache-state selection, provider injection, and `provider_unavailable` journaling.
- `src/id_detector/contracts.py` — allowed `provider_unavailable` in invocation journal entries.
- `src/id_detector/local_index.py` — switched to the relocated paid-result contract.
- `src/id_detector/paid_clip.py` — now owns `PaidScanResult`, validates match/no-match cache states before reuse/write, records request outcomes, and never caches error/malformed bodies.
- `src/id_detector/present/server.py` — added `GET /healthz` returning `200 {"ok": true}`.
- `src/id_detector/providers/audd.py` — rejected malformed clip-result shapes and disabled proxy-environment inheritance.
- `src/id_detector/recognise.py` — added Shazam HTTP-client injection and state-selective cache refresh.
- `src/id_detector/scan.py` — imports the relocated paid-result contract while retaining its compatibility export.
- `src/id_detector/shazam.py` — added an injected `HTTPClientInterface` boundary with request accounting and disabled proxy-environment inheritance.
- `tests/__init__.py` — made repository-local test support importable deterministically.
- `tests/fakes/__init__.py` — defined the shared fakes package.
- `tests/fakes/providers.py` — added scripted `FakeAudD` and `FakeShazamHTTP` implementations matching production signatures.
- `tests/fakes/scripts/all-http-401.json` — added the zero-cost authentication-refusal scenario.
- `tests/fakes/scripts/all-no-match.json` — added the resolved no-match plus Shazam gap-fill scenario.
- `tests/fakes/scripts/gate0a-deep.json` — added the mixed deterministic Deep scenario for subsequent Phase-0 gates.
- `tests/fixtures/audio/tone-60s.wav` — committed the deterministic mono 16 kHz s16 three-tone fixture that yields seven frozen windows.
- `tests/test_paid_clip.py` — mechanically normalized one pre-existing formatter drift required by the repository-wide format gate.
- `tests/test_phase0a_crash_cache.py` — added the complete 0a-i offline gate.

No dependencies were added, so `pyproject.toml`, `uv.lock`, and `uv sync` were intentionally unchanged.

## Tests added

`tests/test_phase0a_crash_cache.py` adds nine deterministic offline tests covering:

- reproducible fixture properties and seven-window geometry;
- the fixture audit's binary-file regression;
- paid-primary success through `_analyse` without the branch-local crash;
- resolved/cacheable AudD no-match results, default no-match refresh, and accumulated Shazam counts;
- all-HTTP-401 `provider_unavailable` exit 3, zero billed units, no cache body, no Shazam downgrade, and no presented result;
- `PaidScanResult` relocation compatibility;
- hidden/guarded CLI fake-provider loading and both injection boundaries;
- AudD/Shazam `trust_env=False` behavior;
- `/healthz` and success/failure behavior of `assert_journal.py`.

## Required command outputs

### 1. `uv run pytest -q`

```text
........................................................................ [ 12%]
........................................................................ [ 24%]
........................................................................ [ 36%]
........................................................................ [ 48%]
........................................................................ [ 61%]
........................................................................ [ 73%]
........................................................................ [ 85%]
........................................................................ [ 97%]
..............                                                           [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
590 passed, 93 deselected, 1 warning in 47.18s
```

### 2. `uv run ruff check .`

```text
All checks passed!
```

### 3. `uv run ruff format --check .`

```text
210 files already formatted
```

The requested preparatory `uv run ruff format .` was needed and reported `3 files reformatted, 207 files left unchanged`.

### 4. `uv run python scripts/audit_fixtures.py`

```text
audited 369 files
fixture audit passed
```

### 5. Phase gate exactly as written

`uv run pytest tests/test_phase0a_crash_cache.py -q`

```text
.........                                                                [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
9 passed, 1 warning in 9.18s
```

`pwsh scripts/smoke_serve.ps1`

```text
pwsh : The term 'pwsh' is not recognized as the name of a cmdlet, function, script file, or operable program. Check
the spelling of the name, or if a path was included, verify that the path is correct and try again.
At line:2 char:1
+ pwsh scripts/smoke_serve.ps1
+ ~~~~
    + CategoryInfo          : ObjectNotFound: (pwsh:String) [], CommandNotFoundException
    + FullyQualifiedErrorId : CommandNotFoundException
```

PowerShell 7 (`pwsh`) is not installed in this environment. The same script was additionally run with the installed Windows PowerShell host:

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

### 6. `git status --short`

```text
 M .gitignore
 M docs/reviews/v2-review-hosting.md
 M docs/schemas/invocation_journal_entry.schema.json
 M scripts/audit_fixtures.py
 M src/id_detector/cli.py
 M src/id_detector/contracts.py
 M src/id_detector/local_index.py
 M src/id_detector/paid_clip.py
 M src/id_detector/present/server.py
 M src/id_detector/providers/audd.py
 M src/id_detector/recognise.py
 M src/id_detector/scan.py
 M src/id_detector/shazam.py
 M tests/test_paid_clip.py
?? docs/reviews/build-0a-i.md
?? scripts/assert_journal.py
?? scripts/make_audio_fixtures.py
?? scripts/smoke_serve.ps1
?? scripts/smoke_serve.sh
?? tests/__init__.py
?? tests/fakes/
?? tests/fixtures/audio/
?? tests/test_phase0a_crash_cache.py
```

### 7. `git diff --stat`

```text
warning: in the working copy of 'src/id_detector/contracts.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/present/server.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/providers/audd.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/recognise.py', CRLF will be replaced by LF the next time Git touches it
 .gitignore                                        |   1 +
 docs/reviews/v2-review-hosting.md                 |  35 +++---
 docs/schemas/invocation_journal_entry.schema.json |   2 +-
 scripts/audit_fixtures.py                         |   3 +
 src/id_detector/cli.py                            | 129 +++++++++++++++++++---
 src/id_detector/contracts.py                      |   2 +-
 src/id_detector/local_index.py                    |   2 +-
 src/id_detector/paid_clip.py                      | 108 +++++++++++++++---
 src/id_detector/present/server.py                 |   3 +
 src/id_detector/providers/audd.py                 |  13 ++-
 src/id_detector/recognise.py                      |  11 +-
 src/id_detector/scan.py                           |  25 +----
 src/id_detector/shazam.py                         |  29 +++--
 tests/test_paid_clip.py                           |   4 +-
 14 files changed, 282 insertions(+), 85 deletions(-)
```

## Plan ambiguity resolved

- “Cache states (§2.3.3)” was implemented as a validated two-state response cache (`match`/`no_match`) plus `--refresh-states`, defaulting to `no_match`; the older `--refresh` remains the explicit refresh-all switch.
- “No resolved attempt” was read as no valid `match` or `no_match` provider response. A valid no-match keeps the paid path active; authentication/error bodies do not and terminate with `provider_unavailable`/3.
- Phase 0a-i records integer billed units only so its HTTP-401 zero-unit assertion is auditable. Recipe pricing, USD reservation/admission/settlement, and the complete Phase-0 status matrix remain deferred to their named 0a-ii/0a-iv cycles.
- The fixture audit cannot meaningfully scan binary pixels/audio samples as text; PNG and WAV inputs are now skipped while every text/JSON fixture rule remains unchanged.

## Could not do

- The exact `pwsh scripts/smoke_serve.ps1` gate could not run because PowerShell 7 is not installed or on `PATH`. The installed Windows PowerShell host ran the identical script successfully, but this does not make the exact command green.

## Fix pass (after diff review)

This section supersedes the earlier pre-review notes about PNG handling and the unavailable `pwsh`
host. The owner-approved `docs/PLAN-v2.md:535-537` Windows PowerShell equivalence was preserved and
the smoke gate below is green with that host.

- **Review item 2 — identity-less AudD results:** `src/id_detector/providers/audd.py:177` defines the
  shared artist/title identity predicate and `src/id_detector/providers/audd.py:327` rejects an
  identity-less mapping as malformed before creating an observation. `src/id_detector/paid_clip.py:94`
  uses the same predicate before treating a response as a cacheable match. The scripted malformed
  response is now the requested empty object at `tests/fakes/providers.py:148`, with its scenario in
  `tests/fakes/scripts/all-malformed.json:1`. Regression:
  `tests/test_phase0a_crash_cache.py:198::test_identity_less_audd_result_is_malformed_and_never_cached`
  asserts exit 3, zero observations, seven malformed billed attempts, and no raw cache body.
- **Review item 3 — exhausted Shazam refresh budget:** `src/id_detector/recognise.py:409-416` adds one
  fresh `max_requests` allowance before the first terminal job is reset, and both selected-state and
  expired-cache resets use it at `src/id_detector/recognise.py:454` and `:473`. Regression:
  `tests/test_phase0a_crash_cache.py:164::test_shazam_no_match_refresh_gets_a_fresh_allowance_after_exhausting_budget`
  consumes exactly seven of seven requests, then proves the default no-match refresh makes seven new
  requests.
- **Review item 4 — web propagation of exit 3:** `src/id_detector/webapp/runner.py:151-172` captures
  `_analyse`'s return code and raises on `provider_unavailable`/3 before acquisition, `_load_cached`, or
  result attachment. Regression:
  `tests/test_phase0a_crash_cache.py:234::test_real_web_runner_fails_exit_3_without_attaching_stale_result`
  runs the real pipeline runner through `JobManager`, proves the job is failed, proves `_load_cached`
  was never called, and proves the pre-existing page was not attached.
- **Review item 5 — binary fixture filenames:** `scripts/audit_fixtures.py:95-99` replaces the
  extension-specific PNG exemption with content-based binary detection. The numeric filename audit
  now runs first at `scripts/audit_fixtures.py:286-289`, so binary contents can be skipped only after
  their path is checked. Regression:
  `tests/test_phase0a_crash_cache.py:110::test_fixture_audit_checks_binary_filenames_before_skipping_contents`
  creates `1234567890.wav` with binary bytes and asserts the filename finding.
- **Review item 6 — truthful progress:** `src/id_detector/paid_clip.py:287-290` now labels the counter
  `requests`, not `billable`. Regression:
  `tests/test_phase0a_crash_cache.py:212::test_all_http_401_is_provider_unavailable_unspent_and_never_cached`
  now asserts the all-401 message says `7 requests` and never says `billable`, while billed units
  remain zero.
- **Review item 7 — cache documentation:** `src/id_detector/paid_clip.py:11-13` now states that only
  validated matches/no-matches are cached, matches are reused by default, and no-matches are refreshed
  by default under `refresh_states`. The new exhausted-budget regression above and the existing
  `tests/test_phase0a_crash_cache.py:141::test_paid_no_match_is_resolved_cached_and_gap_counts_accumulate`
  cover the documented behavior.

### Required command outputs after fixes

#### `uv run pytest -q`

```text
........................................................................ [ 12%]
........................................................................ [ 24%]
........................................................................ [ 36%]
........................................................................ [ 48%]
........................................................................ [ 60%]
........................................................................ [ 72%]
........................................................................ [ 84%]
........................................................................ [ 96%]
..................                                                       [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
594 passed, 93 deselected, 1 warning in 52.40s
```

#### `uv run ruff check .`

```text
All checks passed!
```

#### `uv run ruff format --check .`

```text
211 files already formatted
```

#### `uv run python scripts/audit_fixtures.py`

```text
audited 370 files
fixture audit passed
```

#### `uv run pytest tests/test_phase0a_crash_cache.py -q`

```text
.............                                                            [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
13 passed, 1 warning in 12.14s
```

#### `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

#### `git status --short`

```text
 M .gitignore
 M docs/PLAN-v2.md
 M docs/reviews/v2-review-hosting.md
 M docs/schemas/invocation_journal_entry.schema.json
 M scripts/audit_fixtures.py
 M src/id_detector/cli.py
 M src/id_detector/contracts.py
 M src/id_detector/local_index.py
 M src/id_detector/paid_clip.py
 M src/id_detector/present/server.py
 M src/id_detector/providers/audd.py
 M src/id_detector/recognise.py
 M src/id_detector/scan.py
 M src/id_detector/shazam.py
 M src/id_detector/webapp/runner.py
 M tests/test_paid_clip.py
?? docs/reviews/build-0a-i.md
?? docs/reviews/diff-review-0a-i.md
?? scripts/assert_journal.py
?? scripts/make_audio_fixtures.py
?? scripts/smoke_serve.ps1
?? scripts/smoke_serve.sh
?? tests/__init__.py
?? tests/fakes/
?? tests/fixtures/audio/
?? tests/test_phase0a_crash_cache.py
```

#### `git diff --stat`

```text
warning: in the working copy of 'docs/PLAN-v2.md', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/contracts.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/present/server.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/providers/audd.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/recognise.py', CRLF will be replaced by LF the next time Git touches it
 .gitignore                                        |   1 +
 docs/PLAN-v2.md                                   |   3 +
 docs/reviews/v2-review-hosting.md                 |  35 +++---
 docs/schemas/invocation_journal_entry.schema.json |   2 +-
 scripts/audit_fixtures.py                         |  13 ++-
 src/id_detector/cli.py                            | 129 +++++++++++++++++++---
 src/id_detector/contracts.py                      |   2 +-
 src/id_detector/local_index.py                    |   2 +-
 src/id_detector/paid_clip.py                      | 116 ++++++++++++++++---
 src/id_detector/present/server.py                 |   3 +
 src/id_detector/providers/audd.py                 |  24 +++-
 src/id_detector/recognise.py                      |  24 +++-
 src/id_detector/scan.py                           |  25 +----
 src/id_detector/shazam.py                         |  29 +++--
 src/id_detector/webapp/runner.py                  |   4 +-
 tests/test_paid_clip.py                           |   4 +-
 16 files changed, 323 insertions(+), 93 deletions(-)
```
