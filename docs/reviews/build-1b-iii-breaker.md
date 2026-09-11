# Build 1b-iii ? per-process Shazam breaker

Scope: the remaining breaker half of plan 1b-iii. The scorer implementation and its detailed tests
are unchanged. No dependencies, commits, branches, pushes, live provider calls, or real-URL analyses.
The frozen profiles, plan and corpus are unchanged.

## Changes (one line per file)

- `src/id_detector/shazam_breaker.py`: injected UTC clock, locked rolling-rate/cooldown/daily-budget/latch state, explicit re-enable, and hard-off admission.
- `src/id_detector/shazam.py`: count physical dispatches/resolutions, classify transport outcomes, bypass the old adapter sleep, skip blocked secondary work, retain throttle penalties and direct/no-proxy transport.
- `src/id_detector/recognise.py`: carry the caller-owned breaker across generations/passes and return a skipped-secondary reason.
- `src/id_detector/cli.py`: Free admission refusal with waiting/exit 6, shared run state, and Deep degraded status with the specific breaker rule in the journal/result.
- `src/id_detector/webapp/runner.py`: retain one breaker across the local worker's jobs and report waiting refusals without attaching a result.
- `src/id_detector/providers/base.py`: load/validate the non-secret breaker configuration; existing profile carry-over preserves it.
- `src/id_detector/config_template.py`: document defaults and explicit operator reset, and render all breaker settings in config show.
- `idea.example.toml`: synchronize the documented template and operator instructions.
- `src/id_detector/contracts.py`: allow waiting in the invocation journal.
- `docs/schemas/invocation_journal_entry.schema.json`: regenerate the matching journal schema.
- `tests/fakes/providers.py`: distinguish scripted pre/post timeouts explicitly so pre-receipt timeouts cannot become qualifying failures.
- `tests/test_phase1b_breaker_scorer.py`: offline breaker gate, transport/config/lifecycle regressions, and existing scorer helper's exact fixture reproduction.
- `docs/reviews/build-1b-iii-breaker.md`: this build record and required command outputs.

## Tests added

All new tests are in the phase's named gate file. They cover 19/20 minimum samples, strict >30%
threshold, all qualifying failures and the all-resolved denominator, the five-minute window boundary,
30-minute cooldown, budget consumption including in-flight requests, midnight reset, three distinct
rate opens, persistence of the latch across cooldown and midnight, explicit reset, config-generation
reset consumed once, budget preservation on reset, and daily-budget opens not counting as rate trips.

Integration tests cover each rule skipping Deep's secondary with a journal reason; a budget opening
mid-secondary; an active Free primary completing after both rate and daily-budget opens; the next
Free request refused before intake with waiting/exit 6; shared state across local worker jobs and
isolation between independently owned workers; hard off before bookkeeping (including the raw HTTP
boundary); cache hits excluded; blocked secondary skipping before signature generation/limiter waits;
real transport outcome classification through MockTransport; and local admission refusal refunded
without a fake dispatch or resolution. The timeout-pre fake regression proves it is non-qualifying.
Config loading, validation, profile carry-over and config show defaults/overrides are asserted.

The scorer gate imports `tests.test_score_corpus._run` and `EXPECTED`, reproducing
`tests/fixtures/corpus-mini/expected.json` exactly. `tests/test_score_corpus.py` retains all detailed
scorer coverage; no scorer logic is duplicated or refactored.

## Contract choices and operator action

1. **Daily default:** the plan defines the knob but gives no number: Phase S3 is meant to determine it.
   `docs/spikes/shazam-vps.md` is absent. Use a provisional **2,000 physical attempts per UTC day**, matching
   the existing per-run default, until the owner supplies the measured egress budget. All other numerical
   defaults are the plan's 3000 e4 / 300 s / 1800 s / 20 samples / 3 rate opens.
2. **Local waiting:** refuse the analysis at entry, before intake/cache lookup, journal `waiting` in
   `<work-root>/invocations.jsonl`, print the reason and absence of a queue, and return **6**. There is no
   automatic retry or hosted queue. Existing result pages remain independently openable. A refusal of a
   pre-paid-work `--allow-degrade` restart uses the media journal already established by intake.
3. **Ownership/egress:** the single local worker runner owns one breaker for its lifetime and passes it
   through `_analyse` to all Shazam passes. CLI execution owns its invocation's process state; embedded
   callers running multiple analyses pass the same `shazam_breaker` object. Each process represents one
   direct egress; no proxy or cross-process persistence is introduced. Dispatch/resolved methods are the
   seam for the later shared attempt-event implementation. The legacy adapter counter remains for
   compatibility with throttle diagnostics, but its `before_request` sleep is no longer called.
4. **Budget:** count physical dispatches, including retries and unresolved in-flight requests, not cache
   hits or signature failures. Refund a reserved dispatch if the existing local attempt callback refuses
   it. All resolved outcomes enter the rate denominator. Budget blocking lasts to UTC midnight; a
   separately active rate cooldown or latch can keep Shazam blocked beyond midnight. An admitted Free
   primary continues even beyond the daily budget, as required by the explicit running-Free exception.
5. **Explicit re-enable:** increment `[shazam_breaker].reenable_generation` in the active `idea.toml`, save,
   then submit the next job to the same local server. The config is re-read per job; an increase clears
   latch, trip count, rate cooldown and sample history exactly once. It preserves today's used budget.
   In-process operators can equivalently call `ShazamBreaker.reenable()`. No timer clears the latch;
   `IDEA_ENGINE_SHAZAM=off` always wins. A process restart loses this cycle's non-durable state.
6. **Status precedence:** primary shortfall still produces partial per the matrix. When the primary is
   achieved and any secondary work is skipped, the status is degraded even if the remaining sample
   fraction would otherwise meet 80%. Reasons name `shazam_breaker:a_failure_rate`,
   `shazam_breaker:b_daily_budget`, or `shazam_breaker:c_latch`; manual off names `shazam_manual_off`.
   No engine substitution is added.

## Verification

Required outputs are recorded below. The pytest suite uses the repository's offline/default selector
(`not slow and not live`), injected FakeAudD/FakeShazamHTTP scripts or MockTransport. Test mode was set
for test runs; the final full-suite launcher explicitly set the AudD token empty in Python's child
environment (avoiding Windows PowerShell's removal of empty environment variables). The server smoke
launcher explicitly exported both an empty AudD token and `IDEA_ENGINE_SHAZAM=off` to its child process.
The server check starts the requested port 8791 and verifies both the home page and health endpoint.

```text
$ uv run pytest -q
........................................................................ [  6%]
........................................................................ [ 13%]
........................................................................ [ 20%]
........................................................................ [ 27%]
........................................................................ [ 34%]
........................................................................ [ 41%]
........................................................................ [ 48%]
........................................................................ [ 55%]
........................................................................ [ 62%]
........................................................................ [ 69%]
........................................................................ [ 76%]
........................................................................ [ 83%]
........................................................................ [ 90%]
........................................................................ [ 97%]
.....................                                                    [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
1029 passed, 93 deselected, 1 warning in 278.09s (0:04:38)

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
254 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 434 files
fixture audit passed

$ uv run pytest tests/test_phase1b_breaker_scorer.py -q
..............................................                           [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
46 passed, 1 warning in 14.45s

$ uv run pytest tests/test_phase0a_status.py tests/test_phase1b_targeting.py tests/test_score_corpus.py tests/test_golden_local_free.py -q
........................................................................ [ 69%]
................................                                         [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
104 passed, 1 warning in 81.30s (0:01:21)

$ powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'

$ git status --short
 M docs/schemas/invocation_journal_entry.schema.json
 M idea.example.toml
 M src/id_detector/cli.py
 M src/id_detector/config_template.py
 M src/id_detector/contracts.py
 M src/id_detector/providers/base.py
 M src/id_detector/recognise.py
 M src/id_detector/shazam.py
 M src/id_detector/webapp/runner.py
 M tests/fakes/providers.py
?? docs/reviews/build-1b-iii-breaker.md
?? src/id_detector/shazam_breaker.py
?? tests/test_phase1b_breaker_scorer.py

$ git diff --stat
warning: in the working copy of 'idea.example.toml', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/cli.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/config_template.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/contracts.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/providers/base.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/recognise.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/shazam.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/webapp/runner.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'tests/fakes/providers.py', CRLF will be replaced by LF the next time Git touches it
 docs/schemas/invocation_journal_entry.schema.json |  2 +-
 idea.example.toml                                 | 16 +++++
 src/id_detector/cli.py                            | 39 +++++++++++
 src/id_detector/config_template.py                | 19 ++++++
 src/id_detector/contracts.py                      |  1 +
 src/id_detector/providers/base.py                 | 12 +++-
 src/id_detector/recognise.py                      |  7 ++
 src/id_detector/shazam.py                         | 79 ++++++++++++++++++++++-
 src/id_detector/webapp/runner.py                  |  5 ++
 tests/fakes/providers.py                          |  2 +-
 10 files changed, 177 insertions(+), 5 deletions(-)

```

`git diff --stat` reports tracked files only. The three new files (breaker module, phase gate and
this report) are shown separately by `git status --short`; none has been staged.


## Could not do

No requested implementation or offline check is intentionally deferred. The owner-only S3 capacity
measurement has not been run: it requires live Shazam calls, forbidden for this build. The provisional
daily default and the process-lifetime state limitation are documented above.

BUILD: COMPLETE
All requested offline checks passed, including the final full-suite run after the implementation edits.


## Review + fix pass (sol xhigh second review folded in)

Adversarial review by the reviewer-then-fixer: own findings formed first, then sol's verified one by
one. Every gate below was **re-run here** (sol could run none: no writable temp dir, no `uv.exe`).
No live provider call: pytest ran with `AUDD_API_TOKEN=` + `IDEA_TEST_MODE=1` and injected fakes; the
two PowerShell gates set their own child env; no real URL was analysed, no real Shazam request made.

### Findings

| Tag | Source | Where | Verdict | What / why |
|---|---|---|---|---|
| **P0** | sol + mine | `shazam_breaker.py:114-124` (before) | **Confirmed** | While rule (b) held, `resolved()` returned before evaluating rule (a), so an admitted Free primary could produce any number of qualifying 20-sample periods without incrementing `_opens`; `_advance()` then cleared `_opens` at 00:00 UTC together with the budget, so the latch could never form. Exactly the state the breaker exists for (D8). |
| **P0-test** | sol | `tests/test_phase1b_breaker_scorer.py:424-433` (before) | **Confirmed** | `test_budget_opens_do_not_count_as_rate_trips` codified the defect (asserted `reason() is None` after three full failure periods). Replaced by two tests that separate its valid claim from its invalid one. |
| **P1** | sol + mine | `cli.py:631-632` (before) vs `cli.py:710-721` | **Confirmed** | Free admission refused before the compatible-result lookup, so a stored `free complete` result - which needs no Shazam request - was refused `waiting`. Sections 3.4/4.2 order intake, then the compatible-result lookup, then serve-or-refuse; only a request that would start a new analysis waits. |
| **P1** | sol + mine | `webapp/runner.py:183-185`, `webapp/jobs.py:405-422` (before) | **Confirmed** | Exit 6 became `RuntimeError`, so the job went `failed`. Section 2.3.5 says the job is `waiting`: a refusal is not a failure (no result, no spend, nothing wrong with the mix). The builder's test asserted only the exception text, hiding it. |
| **P1** | sol + mine | `cli.py:614-624` + `cli.py:929-931` (before) | **Confirmed** | The `--allow-degrade` restart refusal journalled `_settle_money(None)`: zero reserved, and no release of the real Deep reservation (section 2.3.2). Nothing was re-billed, but the journal was dishonest about money already reserved. |
| **P1** | **mine (new)** | `scripts/gate_local_mode.ps1:18` | **Confirmed regression** | The committed local-mode gate sets `IDEA_ENGINE_SHAZAM=off` for **both** of its phases. Until this cycle that variable was a no-op; it is now a real refusal, so the gate's offline golden Free run returned exit 6 and the gate **failed** (`the golden run did not complete (exit code 6)`). The builder never ran this gate. Fixed by setting the kill-switch for the `serve` phase only - the prepare phase cannot reach the network anyway (`_analyse` is handed an injected `FakeShazamHTTP`). |
| P2 | sol | `shazam.py:138-145` (before) | **Confirmed** | `CircuitBreaker.before_request()` became unreachable. Removed; `failures`/`opened_at` stay as per-transport diagnostics (`test_phase0b_config.py:430` asserts `failures`, and four test files construct the dataclass with `open_seconds`), with a docstring saying admission now belongs to `ShazamBreaker`. |
| P2 | sol | `config_template.py:62-75` | **Agree - left provisional** | The 2,000/day default is not measured. No number invented here; see the ratification line below. |
| P2 | mine | `recognise.py:200-325` | Left as a note | A breaker-blocked secondary window raises `ShazamBlocked` per window, which the job executor records as `permanent_failure` plus an `error` observation, so the journal's `failures` count includes windows that were never attempted. No money, status or cache effect (error raw responses are never cache-valid, and the run is already `degraded`), and an honest fix needs a `skipped` job-store state - outside this cycle. |
| P2 | mine | `recognise.py:403-407` | Left as a note | `process_breaker`/`running_free` are ignored when a caller passes its own `adapter=`, and a caller that passes neither silently gets a *fresh* breaker per generation. Production (`cli.py:815`) always passes the run's breaker; `benchmark/shortlist.py:258` and `calibration.py:232` (both quarantined per section 2.6) each build their own. Worth making explicit when 4b-ii replaces the seam. |
| P2 | mine | `shazam_breaker.py:81-90` | Left as a note | `reason()` precedence is latch, budget, rate, so a run blocked by both (b) and (a) journals `b_daily_budget`. Harmless - still blocked, and (a) re-surfaces after midnight - but the reason names the weaker rule. |
| - | mine | `shazam_breaker.py` boundaries | **Verified correct** | `>` is strict (exactly 30 % does not open); `count >= minimum_sample` opens at exactly 20 samples; window eviction is half-open (a sample exactly `window_seconds` old is dropped); the UTC-day rollover clears `_daily_attempts` and `_opens` but never `_latched`; `shazam_off()` short-circuits `dispatch()`, `resolved()` and `InjectedHTTPClient.request()` before any bookkeeping; `trust_env=False` plus `kwargs.pop("proxy")` keep the egress direct and unproxied; no module-level singleton; no AudD path, reservation or `partial` -> `complete` transition is touched. |

### Changes

- `src/id_detector/shazam_breaker.py:114-124` - `resolved()` now returns early only while latched or
  inside an open cooldown, so rule (a) keeps judging every resolved attempt while rule (b) blocks
  new work. Budget exhaustion still cannot be a rate trip: only sampled qualifying failures are.
- `src/id_detector/cli.py:610-632, 722-725` - `refuse_free()` is documented and moved to the
  submission-order point, immediately **after** the compatible-result lookup; it now settles the
  active `usd_admitter` (`cli.py:622`), so the `waiting` journal reports the real reserved/spent
  figures and releases the Deep reservation at the `--allow-degrade` refusal (`cli.py:941`).
- `src/id_detector/webapp/jobs.py:44-56, 418-438` - new `WAITING` job state (in `TERMINAL_STATES`,
  so polling stops) and `JobWaiting`; the manager records `waiting` with the reason in `message` and
  leaves `error` unset.
- `src/id_detector/webapp/runner.py:19-36, 185-188` - exit 6 raises `JobWaiting` (named
  `WAITING_EXIT`) instead of a generic `RuntimeError`.
- `src/id_detector/present/server.py:398, 694-700, 760-761` - the progress page gets a `waiting`
  outcome (it no longer falls through to "Cancelled"), a "Not started" eyebrow and a warn-coloured
  activity bar. Both inline scripts pass `node --check`. No `PAGE_VERSION` bump: these pages render
  per request and result-page bytes are untouched.
- `src/id_detector/shazam.py:129-150` - the dead `CircuitBreaker.before_request()` is removed.
- `scripts/gate_local_mode.ps1:17-27` - the Shazam kill-switch is set for the `serve` phase only;
  the prepare phase stays offline through its injected fake transport.

### Tests

`test_budget_opens_do_not_count_as_rate_trips` was replaced by two tests that keep its valid half
and kill its invalid half, and three integration regressions were added. All five were verified to
**fail** against the pre-fix code and pass after it:

- `tests/test_phase1b_breaker_scorer.py:428` `test_daily_budget_exhaustion_is_never_itself_a_rate_trip`
- `tests/test_phase1b_breaker_scorer.py:439` `test_qualifying_failures_still_trip_and_latch_while_the_budget_is_exhausted`
- `tests/test_phase1b_breaker_scorer.py:492` `test_open_breaker_still_serves_a_compatible_cached_result`
- `tests/test_phase1b_breaker_scorer.py:509` `test_degrade_restart_refusal_settles_the_deep_reservation`
- `tests/test_phase1b_breaker_scorer.py:529` `test_web_job_of_a_refused_request_is_waiting_not_failed`

`test_admitted_free_primary_finishes_despite_opening` lost its "no `source.json`" assertion (intake
now legitimately precedes the refusal) and gained "no `tracklist.json`, nothing reserved, nothing
spent" instead.

### Gate outputs (all re-run after the fixes)

```text
$ AUDD_API_TOKEN= IDEA_TEST_MODE=1 uv run pytest -q
1033 passed, 93 deselected, 1 warning in 265.15s (0:04:25)

$ AUDD_API_TOKEN= IDEA_TEST_MODE=1 uv run pytest tests/test_phase1b_breaker_scorer.py -q
50 passed, 1 warning in 16.52s

$ AUDD_API_TOKEN= IDEA_TEST_MODE=1 uv run pytest tests/test_phase0a_status.py
    tests/test_phase1b_targeting.py tests/test_score_corpus.py tests/test_golden_local_free.py
    tests/test_phase1a_compat.py tests/test_phase1a_bundles.py tests/test_phase1a_cached_open.py
    tests/test_stage10_webapp.py -q
213 passed, 1 warning in 130.42s (0:02:10)

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
254 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 434 files
fixture audit passed

$ AUDD_API_TOKEN= IDEA_ENGINE_SHAZAM=off powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'

$ AUDD_API_TOKEN= powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=...
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-de732a1fa26f4ef993a655a08183b4d8

$ PYTHONIOENCODING=utf-8 uv run python scripts/score_corpus.py --run-list data/local/release-1-runs-free.json --out "$TEMP/1b-iii-check.json"
scored 7 mix(es) [draft truth, matched by work]: likely 8971/10000, listed n/a, recall 6422/10000

$ git status --short -- work data
(empty)
```

Pooled release-1 (free): `likely_precision_e4` **8971**, `listed_precision_e4` **null**,
`work_recall_e4` **6422** - identical to the `0665f04` / 1a-ii record, so neither 1a-i+1a-iii nor
1a-ii regressed. `work/` and `data/` are untouched; nothing was committed, branched or pushed.

### Owner ratification needed

`shazam_breaker.shazam_daily_budget_per_egress = 2000` is still the builder's **provisional** figure
(it borrows the existing per-run default), not a measurement: plan section 5 spike **S3** is meant to
set it and needs live Shazam calls, which are forbidden here. Ship the knob, ratify the number -
until then rule (b) is a guess, and it is the only rule whose threshold is not the plan's own.

One standing convention changes with this cycle: `IDEA_ENGINE_SHAZAM=off` is now a **real refusal**,
so any build or review that needs a Free analysis to *complete* must use `IDEA_TEST_MODE=1` plus
`--fake-providers` / injected fakes rather than the kill-switch. That is what broke the local-mode
gate above.

REVIEW: FIXED
