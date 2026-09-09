# Build 0b-i + 0b-iii — AudD adapter v2: anchor, concurrency, retries; Attempt journal and cancellation

Two adjacent plan cycles built in one pass because both live in the AudD clip adapter and the
paid sweep (`paid_clip.py`). Everything is uncommitted on `main` at `97bedd3` for review. No
dependency was added; `docs/PLAN-v2.md`, `profiles/`, `data/corpus/`, `recipes.py` and
`pricing.toml` are untouched. No live provider call was made: every pipeline run used injected
fakes through `_analyse` or `IDEA_TEST_MODE=1 --fake-providers audd,shazam`.

---

## Cycle 0b-i — AudD adapter v2: anchor, concurrency, retries (E-C3 anchor, E-H5)

### File-by-file changes

- `src/id_detector/providers/audd.py` — the §2.3.4 step 2 anchor validity rule: `clip_anchor`
  (an `Anchor` from `timecode` iff it parses to a number of ms, lies in `[0, anchor_max_ms]` and,
  when the match carries a duration, is `≤ duration + anchor_slack_ms`; else `None`),
  `clip_match_duration_ms` (`duration_ms`, `spotify.duration_ms`, `apple_music.durationInMillis`),
  `DEFAULT_ANCHOR_MAX_MS` / `DEFAULT_ANCHOR_SLACK_MS` mirroring the recipe, a 1 000 ms uncertainty
  and the `audd_clip_timecode` method; `clip_response_to_observation(anchor_max_ms=,
  anchor_slack_ms=)` now anchors matches and stamps `native["simultaneous_source"] = "audd"`
  (`CLIP_SIMULTANEOUS_SOURCE`) on every clip observation so AudD and Shazam matches on one window
  are both selected instead of AudD's evicting Shazam's (review C3); module docstring.
- `src/id_detector/paid_clip.py` — the sweep is rewritten around a pool of `concurrency` worker
  tasks over one window queue behind a `TokenBucket(rate=app_config.audd_requests_per_minute,
  capacity=concurrency)` (one token per attempt, `penalize()` on 429/503, `recover()` on a
  resolved clip); the recipe's bounded retry policy (`connect_error`, `timeout_pre`, `http_429`,
  `http_503` only; `≤ max_retries`; the backoff schedule through an injectable `sleep`); retries
  re-admit their unit; ambiguous outcomes are never retried; a terminal-provider outcome halts the
  pool at once; `_error_body_outcome` also knows AudD's own `900` (wrong token) / `901` (limit
  reached) body codes and "limit reached"; `PaidScanResult.attempts` (dispatches incl. retries),
  `.retries`; the summary line gains `; N retries`.
- `src/id_detector/providers/base.py` — `DEFAULT_AUDD_REQUESTS_PER_MINUTE = 120`,
  `AppConfig.audd_requests_per_minute`, `[deep] audd_requests_per_minute` (positive integer).
- `src/id_detector/config_template.py`, `idea.example.toml` — the knob documented under `[deep]`
  and printed by `config show` (template and example stay byte-identical).
- `src/id_detector/cli.py` — `_analyse` hands the sweep the recipe's `audd_concurrency`,
  `retry_policy["audd"]`, `anchor_max_ms` / `anchor_slack_ms`; journals `paid_attempts`; carries
  `audd_requests_per_minute` over a profile (the H6 carry-over list, both call sites).
- `src/id_detector/webapp/runner.py` — the same carry-over in `_resolve_settings`.
- `tests/fakes/providers.py` — a script section may set `latency_ms` (or `FakeAudD(script,
  latency_s=)`), so several fake requests are genuinely in flight; `FakeAudD.in_flight` /
  `max_in_flight`; `no_backoff`, the retry sleeper tests inject (yields, costs no wall time).
- `tests/fakes/scripts/{retry-429-then-match,retry-timeout-pre,retry-503-exhausted,all-http-403,latency-match}.json`
  — the gate scenarios.
- `tests/test_phase0a_status.py`, `tests/test_phase0a_money.py`, `tests/test_phase0a_crash_cache.py`,
  `tests/test_phase0a_security.py` — the `_analyse` helpers pass `audd_requests_per_minute=
  1_000_000` and `paid_sleep=no_backoff`; the three call counts the retries change (`all-timeout-pre`
  and `all-http-429`: 7 → 28 = 7 × (1 + 3 retries); the `--allow-degrade` test compares
  `paid_attempts` with the fake's calls); the `money-refunds` test asserts what is invariant under
  the four workers' interleaving (six windows dispatched, two units billed, the seventh never sent).
- `tests/test_paid_clip.py` — its local fake now invokes `on_attempt` (the production contract
  the attempt journal relies on).

### Tests added — `tests/test_phase0b_audd.py` (43)

`http_429` × 2 then `match` → 3 attempts, 1 unit billed, `paid_attempts=9` / `paid_requests=7`,
the retry chain in the journal (ordinals 0-2, each parent the previous attempt, outcomes
`http_429, http_429, match`), the retry progress line, and `assert_journal.py` reading the new
counts; `timeout_pre` retried and resolved by the retry; a window that never stops answering 503
gets exactly 1 + 3 attempts with the recorded backoff `[1, 2, 4]`, every unit refunded, and the run
is `partial` (6/7); billable ambiguous outcomes (`http_500`) are never retried and never sleep.
`http_403` → `auth_error`, cost 0, one dispatch, exit 3, the attempt journalled with its unit
price; every transport status code and every error-body shape (incl. AudD's 900/901) classifies
into the frozen outcomes. The anchor rule over nine valid timecodes (integers, floats, `mm:ss`,
`hh:mm:ss`, exactly `anchor_max_ms`, within `duration + slack` from three duration fields) and
eight invalid ones (absent, null, negative, junk, bool, beyond 24 h, beyond duration + slack), the
bounds coming from the caller with recipe-equal defaults, an unknown duration never blocking.
`simultaneous_source="audd"` on match and no-match observations, and through
`select_logical_trial_points` a Shazam and an AudD match on one window are both selected with no
`hypothesis_rejected` (the legacy source-less shape evicts one). Seven 200 ms clips run four at a
time (`max_in_flight == 4`, under 1 s wall-clock); every attempt takes one token from a bucket
built with the config ceiling and the recipe's capacity, each 429 penalises it, each resolved clip
recovers it. `[deep] audd_requests_per_minute`: default 120 in `AppConfig` and the template,
loads, refuses `0` / `-1` / `true` / a string, is rendered by `config show`, and is carried under
`--profile free` by both `cli.analyse` and `runner._resolve_settings`.

---

## Cycle 0b-iii — Attempt journal and cancellation (E-H4)

### File-by-file changes

- `src/id_detector/contracts.py` — `ProviderOutcome` (moved here from `money.py`),
  `AttemptEventKind`, `ATTEMPT_EVENT_SEQ`, and the `ProviderAttemptEvent` record — one line of
  `recognise/attempts.jsonl`: `event`, `seq`, `at`, `attempt_id`, `query_id` (the clip cache key),
  `run_id`, `provider`, `window_id`, `ordinal`, `parent_attempt_id`, `unit_usd_e6`, `outcome`
  (present exactly on `resolved`; `seq` tied to the kind); registered in `SCHEMA_MODELS` and
  `NATURAL_KEY_FIELDS`.
- `src/id_detector/money.py` — imports `ProviderOutcome` from the contracts.
- `src/id_detector/attempts.py` — **new**: `AttemptJournal` (`prepare` → `dispatched` →
  `resolved`, every event appended and fsynced before the method returns; a retry is a new attempt
  naming its parent), `AttemptState` (`state`, `classification` ∈ `reissue` / `ambiguous` /
  `resolved`), `AttemptLedger` (`latest`, `dangling`, `with_classification`),
  `load_attempt_ledger` (folds events; a torn trailing line is skipped and counted),
  `attempt_id_for = sha256(run_id, query_id, ordinal)`, `attempts_path`.
- `src/id_detector/journal.py` — `append_line`: a true append with flush + `fsync`.
- `src/id_detector/paid_clip.py` — `prepared` is written after the token is taken and right
  before the adapter is called; `dispatched` is written inside the adapter's `on_attempt`, after
  USD admission and before the request enters network I/O; `resolved(outcome)` with the frozen
  outcome. Resume: an earlier run's unresolved attempt on the same clip becomes the new attempt's
  `parent_attempt_id` and is counted `resumed_ambiguous` (it was dispatched) or `resumed_reissued`
  (only prepared). A `CancelToken` (anything with `is_set()`) is polled before every dispatch and
  every retry; a progress/log hook that raises `CancelledError` is folded into the same halt
  instead of tearing down the workers; both stop new dispatches, let the clips in flight resolve
  (journalled, cached, charged), and return `PaidScanResult(cancelled=True)`. `on_window(done,
  total)` is ticked once per finished clip (cache hits included). Real task cancellation (Ctrl-C)
  keeps the previous behaviour: in-flight requests abort, `dispatched` stays without `resolved`.
- `src/id_detector/cli.py` — `_analyse(cancel_token=, paid_sleep=)`; the paid sweep reports per
  clip through the existing `_on_recognise_window`; `_recognise_log` repeats the live done/total on
  every log line (before, each line reset the web ETA to "1 window" — review H4);
  `paid_resumed_ambiguous` / `paid_resumed_reissued` counts; a cancelled sweep raises
  `CancelledError` so the existing handler journals `cancelled` / 130 with the in-flight spend.
- `src/id_detector/webapp/jobs.py` — `JobContext.cancel_token` (the job's `threading.Event`).
- `src/id_detector/webapp/runner.py` — passes `cancel_token=ctx.cancel_token` to `_analyse`.
- `docs/schemas/provider_attempt_event.schema.json` (exported by `scripts/export_schemas.py`; the
  other 27 schemas re-exported byte-identical), `tests/golden/provider_attempt_event.json`,
  `docs/schemas/README.md` — the contract's schema, golden and natural-key row.
- `tests/fakes/scripts/timeout-post-once.json` — the ambiguous-outcome scenario.

### Tests added — `tests/test_phase0b_attempts.py` (14)

Every attempt is `prepared` → `dispatched` → `resolved` with `seq` 0/1/2, non-decreasing `at`, a
shared identity, `unit_usd_e6=5000` and the deterministic id — and at the instant each request is
about to enter network I/O its `dispatched` line is already on disk (asserted from inside the
adapter's `on_attempt`); every line validates as a contract record. `timeout_post` → one attempt,
resolved `timeout_post`, billed, never retried, never cached (`partial` 6/7). Cancel token set at
the third dispatch with 200 ms clips → 3 ≤ `dispatched` ≤ 4, every dispatched attempt resolved,
`cancelled` / 130 / `achieved=null`, spend = dispatched × 5 000, their answers cached, no `present/`;
a token set before the sweep → nothing dispatched, no journal file, `cancelled` with the
reservation released; the web-style raising progress hook → nothing dispatched after the raise
(≥ 4 clips were in flight), all of them resolved and charged. Resume at the sweep level:
dispatched-unresolved → `ambiguous`, prepared-only → `reissue`, each re-run once as a fresh attempt
whose parent is the dangling one (the ledger's `dangling()` clears, the old attempt keeps its
classification as history); a resolved earlier attempt is not a resume; a crash after a dispatch in
a real `_analyse` run leaves exactly one ambiguous attempt and the next run reports
`paid_resumed_ambiguous=1`, re-runs it with the parent link and completes; a refused USD admission
leaves a `prepared`-only attempt (re-issue), never an ambiguous one. Progress is ticked per clip
`(0..7, 7)` and every log line in the phase carries the real total. `JobContext.cancel_token` is
the job's cancel event and the web runner passes it as `cancel_token`. The ledger reader skips a
torn trailing line and folds a retry chain; the contract rejects an outcome on a non-resolved
event, a missing outcome on `resolved`, a `seq` that disagrees, an unknown outcome and a float.

`tests/test_contracts.py` picks the new model up automatically (schema current, golden
round-trips, no floats): +2 cases.

---

## Required command outputs

### 1. `uv run pytest -q`

```text
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
720 passed, 93 deselected, 1 warning in 147.62s (0:02:27)
```

### 2. `uv run ruff check .`

```text
All checks passed!
```

### 3. `uv run ruff format --check .`

```text
225 files already formatted
```

(`uv run ruff format` was run on the two new test files first: `2 files reformatted`.)

### 4. `uv run python scripts/audit_fixtures.py`

```text
audited 374 files
fixture audit passed
```

(re-run after this report was written, since `docs/` is audited: `audited 375 files` /
`fixture audit passed`.)

### 5a. 0b-i gate — `uv run pytest tests/test_phase0b_audd.py -q`

```text
...........................................                              [100%]
43 passed, 1 warning in 13.73s
```

### 5b. 0b-iii gate — `uv run pytest tests/test_phase0b_attempts.py -q`

```text
..............                                                           [100%]
14 passed, 1 warning in 13.73s
```

### 5c. Local mode still serves — `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

(exit 0; the script's own cleanup line 44, `taskkill.exe … 2>$null`, prints a "could not be
terminated" notice to stderr under this host because the server has already exited — pre-existing,
not part of this cycle.)

### 5d. The 0a-iv Phase-0 Deep gate through the real CLI (PowerShell 5.1; regression evidence, with
the new count added to the expectation)

```powershell
$env:IDEA_TEST_MODE = "1"; $env:IDEA_FAKE_SCRIPT = "tests/fakes/scripts/gate0a-deep.json"
$w = Join-Path $env:TEMP ("idea-gate0a-" + [guid]::NewGuid().ToString("N"))
uv run idea analyse tests/fixtures/audio/tone-60s.wav --recipe deep --fake-providers audd,shazam --work-root $w
uv run python scripts/assert_journal.py --work-root $w --expect status=complete algorithm_version=targeting:0,fusion:1 usd_e6_reserved=36750 usd_e6_spent=35000 usd_e2_spent=4 paid_attempts=7 paid_requests=7
```

```text
complete; 6 matches; 0 failures; 2 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=<workroot>\present\tracklist.json
journal assertion passed at <workroot>\invocations.jsonl:1
gate exit=0
```

### 6. `git status --short`

```text
 M docs/schemas/README.md
 M idea.example.toml
 M src/id_detector/cli.py
 M src/id_detector/config_template.py
 M src/id_detector/contracts.py
 M src/id_detector/journal.py
 M src/id_detector/money.py
 M src/id_detector/paid_clip.py
 M src/id_detector/providers/audd.py
 M src/id_detector/providers/base.py
 M src/id_detector/webapp/jobs.py
 M src/id_detector/webapp/runner.py
 M tests/fakes/providers.py
 M tests/test_paid_clip.py
 M tests/test_phase0a_crash_cache.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_security.py
 M tests/test_phase0a_status.py
?? docs/schemas/provider_attempt_event.schema.json
?? src/id_detector/attempts.py
?? tests/fakes/scripts/all-http-403.json
?? tests/fakes/scripts/latency-match.json
?? tests/fakes/scripts/retry-429-then-match.json
?? tests/fakes/scripts/retry-503-exhausted.json
?? tests/fakes/scripts/retry-timeout-pre.json
?? tests/fakes/scripts/timeout-post-once.json
?? tests/golden/provider_attempt_event.json
?? tests/test_phase0b_attempts.py
?? tests/test_phase0b_audd.py
```

(plus `?? docs/reviews/build-0b-i-0b-iii.md`, this report, written after the snapshot.)

### 7. `git diff --stat`

```text
 docs/schemas/README.md             |   1 +
 idea.example.toml                  |   3 +
 src/id_detector/cli.py             |  40 ++-
 src/id_detector/config_template.py |   4 +
 src/id_detector/contracts.py       |  53 ++++
 src/id_detector/journal.py         |  19 ++
 src/id_detector/money.py           |  17 +-
 src/id_detector/paid_clip.py       | 482 ++++++++++++++++++++++++++-----------
 src/id_detector/providers/audd.py  | 108 ++++++++-
 src/id_detector/providers/base.py  |  14 +-
 src/id_detector/webapp/jobs.py     |  10 +
 src/id_detector/webapp/runner.py   |   4 +
 tests/fakes/providers.py           |  33 ++-
 tests/test_paid_clip.py            |   1 +
 tests/test_phase0a_crash_cache.py  |   4 +-
 tests/test_phase0a_money.py        |  19 +-
 tests/test_phase0a_security.py     |   4 +-
 tests/test_phase0a_status.py       |  14 +-
 18 files changed, 651 insertions(+), 179 deletions(-)
```

Untracked additions: `attempts.py` 174 lines, the two test files 1 011, six fake scripts 76, the
schema and golden (one canonical line each). As with 0a-iv, two cycles in one pass exceed the plan's
1,500-line split guideline once tests are counted; the split point, if wanted, is 0b-iii's
`attempts.py` / `contracts.py` / `journal.py` / `jobs.py` / `runner.py` files plus the journal,
cancel and progress paths of `paid_clip.py`, which 0b-i's adapter, config and retry code do not
depend on.

---

## Plan ambiguities resolved

1. **The token bucket's rate.** §2.3.1 fixes the concurrency (4, recipe data) but names no rate for
   the bucket. A rate is speed only, never a result, so it is not recipe data: it is the config
   knob `[deep] audd_requests_per_minute` (default 120 — the ~4 min primary of §2.3.6 for 400
   windows; the AIMD bucket already in `shazam.py` lowers it on a 429/503 and climbs back), carried
   under a profile at both call sites so it is not a new H6-style dropped knob.
2. **"Cancel after 3 clips … ≤ 4 `dispatched`".** Read as: the cancel token fires the instant the
   third clip is dispatched while four clips could be in flight; the bound proves that the token is
   checked before *every* dispatch, not only between windows. What happens to the clips in flight
   follows §3.5: they resolve and settle as spent, and the run ends `cancelled` (exit 130) — so a
   cancelled Deep run journals the true spend and keeps the answers it paid for in the cache. The
   web app's raising progress hook is folded into the same drain instead of cancelling the worker
   tasks, because cancelling an in-flight AudD request only turns a billed answer into an ambiguous
   one. Genuine task cancellation (Ctrl-C) is unchanged.
3. **The resume rule is a reader-side classification, not a new event.** A `dispatched` attempt
   without `resolved` is *ambiguous*: the run that dispatched it charged the unit (its settlement
   charges outstanding units; a crash before settlement is exactly what the journal is for), so
   the next run re-runs the clip as a fresh attempt whose `parent_attempt_id` is the dangling one
   and reports `paid_resumed_ambiguous`. A `prepared`-only attempt was never sent and is re-issued
   the same way (`paid_resumed_reissued`). Nothing is appended on behalf of the earlier run.
4. **`ordinal` and `attempt_id`.** `ordinal` is the attempt's index in this run's retry chain for
   the clip (0, then 1-3 for retries); `attempt_id = sha256(run_id, query_id, ordinal)`; `query_id`
   is the clip cache key as §2.3.3 says. The three events of one attempt repeat its whole identity
   so any single surviving line reconstructs it (the hosted `provider_attempt_events` row shape).
5. **Counts.** `paid_requests` stays "windows sent at least once" (so `not sent` arithmetic and
   the 0a-i summary line are unchanged); the new `paid_attempts` counts dispatches including
   retries. Windows never reached by a halted or cancelled sweep are not failures; a window
   halted mid-retry is.
6. **`simultaneous_source="audd"` is set now** (it is in 0b-i's scope line); the corroboration
   change to read `evidence` instead of `votes` stays with 1b-ii as Appendix A schedules it.
7. **Anchor "numeric".** An integer, decimal or `[hh:]mm:ss` timecode is accepted (AudD reports
   `"00:23"`); anything unparseable, a boolean or a negative value yields `anchor=None` rather than
   a `malformed` outcome — a bad timecode does not un-match a clip. "When the match carries a
   duration" reads `duration_ms`, `spotify.duration_ms` and `apple_music.durationInMillis` (the
   `return=` extras); an absent or unparseable duration never blocks the anchor.
8. **Classification beyond HTTP codes.** AudD answers a wrong or exhausted token as HTTP 200 with
   `error_code` 900 / 901 in the body; these classify as `auth_error` / `quota_error` alongside
   401/403 and 402 so the sweep stops at once in either shape.
9. **Retry sleeper injection.** The recipe's 1/2/4 s backoff would cost the offline suite ~14 s per
   throttled scenario, so the sweep takes `sleep=` and `_analyse` takes `paid_sleep=` — explicit
   dependency injection like `paid_scan_adapters`, never a hidden test-mode branch; the CLI path
   sleeps for real.
10. **A refused admission or a missing clip file** leaves a `prepared`-only attempt (re-issue) and
    is never counted as a request; before, a missing file counted as a request and a failure.
11. **Attempt events are a contract** (`ProviderAttemptEvent` with schema and golden) rather than
    ad-hoc JSON: the ledger validates every line it reads and the hosted table (4b) can reuse it.

## Bugs found and fixed on the way (each with a regression test)

- `cli.py`: every paid-phase log line went through `_report(progress, "recognise", 0, 1, …)`, which
  set the web job's `windows_total` back to 1 (review H4's "ETA ≈ 1 second"); `_recognise_log` now
  repeats the latest per-window tick. Covered by
  `test_progress_is_reported_per_clip_with_the_real_total`.
- `tests/test_paid_clip.py`: its private fake adapter returned without invoking `on_attempt`, which
  production never does; the sweep now refuses that shape unconditionally (the journal's
  `dispatched` lives in that callback), so the fake was fixed to honour the contract.
- Test authoring: `Path.glob` returns nothing for the raw-cache paths on Windows (they exceed
  `MAX_PATH`, the 0a-iv review's P2-4); the new tests list that directory through `native_path`
  like `test_phase0a_crash_cache.py` does.

## Could not do

- **Live confirmation of AudD's shapes** — the `900` / `901` body codes, the `"mm:ss"` timecode and
  the `return=` duration fields are implemented from AudD's documentation; the hard rule forbids a
  real request, so they are asserted against fixtures only.
- **`pwsh scripts/…`** — PowerShell 7 is not installed; Windows PowerShell 5.1 ran the smoke script
  and the 0a-iv gate, as the plan's PowerShell-host note allows.
- **`trust_env=False`** was already in place on both AudD client paths (0a-i) and is asserted by
  the existing `test_provider_http_clients_ignore_proxy_environment`; no second test was added.
- **Working-tree line endings.** While normalising the six edited files that git reported as CRLF
  (`.gitattributes` says `eol=lf`), the script also touched 44 untouched files. Their bytes were
  restored to CRLF as found, except `docs/reviews/README.md`, whose original mixed endings a
  byte-level restore did not reproduce; it was restored with `git checkout --` to its index
  content. Git-visible state was never affected (git normalises both ways), and `git status`
  lists only this cycle's files.

BUILD: COMPLETE

---

## Review + fix pass

Adversarial review of the uncommitted 0b-i + 0b-iii change against `docs/PLAN-v2.md`
(§2.3.1–2.3.5, §3.4/§3.5, §6.1, and the two cycle sections), the earlier cycles' committed
behaviour, and the builder's report above. Every gate below was re-run here, never trusted from
the report. No live provider call was made: every run used injected fakes or
`IDEA_TEST_MODE=1 --fake-providers audd,shazam`.

### Findings

**P0 — none.**

**P1-1 — `_error_body_outcome` / `_protocol_outcome` let a message word outrank an explicit
status code, turning a retryable throttle into a terminal stop.**
`src/id_detector/paid_clip.py:176-189` (pre-fix) tested `"auth" in message` and
`"quota" / "credit" / "limit reached"` *before* the 429 / 503 / 5xx codes. AudD words its
throttle bodies "…limit reached", so
`{"status": "error", "error": {"error_code": 429, "error_message": "Too many requests, your rate
limit reached"}}` classified as `quota_error` — a §2.3.3 terminal-provider outcome. Effect: the
primary stops at the first throttled clip (`partial` / `provider_unavailable_midrun`, or exit 3
`provider_unavailable` when it lands before any resolved attempt) instead of taking the recipe's
three bounded retries; a 400-window Deep sweep could end after one clip and one refunded unit.
`"auth" in message` likewise matched any body whose text merely contains "author".
`_protocol_outcome` (`paid_clip.py:198-214`) had the same precedence.
**Fixed:** in both classifiers the numeric code decides first (900/901/401/403 → `auth_error`,
901/402 → `quota_error`, then 429, 503, 500-599); the word heuristics remain only as the fallback
for a body carrying no code we recognise, so a genuine `{"error_message": "quota"}` body is still
terminal. **Test:** `tests/test_phase0b_audd.py::test_an_explicit_error_code_outranks_the_message_words`
(five bodies through both classifiers, plus the codeless-body fallback).

**P1-2 — an admitted USD unit could be left outstanding and un-journalled, and the next run
re-billed it.**
`paid_clip.py` `_attempt` returned `(None, None, admitted)` whenever the adapter failed with no
provider outcome, and `_process` did `if not admitted or outcome is None: break` — so an adapter
that had already invoked `on_attempt` (USD unit admitted, `dispatched` fsynced) and then failed
left the unit **outstanding for the rest of the run** (one fewer admissible dispatch; the hard cap
could refuse a later clip) with **no `resolved` line**, so the following run classified it
`ambiguous` and re-billed the clip. Production's `recognize_clip` checks the clip file before
`on_attempt`, so it is currently unreachable — but the sweep already hard-guards the opposite
adapter mistake (`RuntimeError` when `on_attempt` is never called), and this one was silent.
Nothing guarded a *double* `on_attempt` either: two `dispatched` lines and two admitted units for
one request.
**Fixed:** an admitted dispatch with no outcome now resolves as `timeout_post` (ambiguous — spent,
never retried), exactly like the Ctrl-C branch, so the unit is always settled and the attempt is
never resumed as a second billable dispatch; `_admit` refuses a second call.
**Tests:** `tests/test_phase0b_attempts.py::test_an_admitted_dispatch_without_an_outcome_is_settled_as_ambiguous`
(asserts `usd_e6_spent == 5_000` *before* settlement — an outstanding unit would read 0 — and that
the ledger has no dangling attempt) and `::test_an_adapter_that_admits_twice_is_refused`.

**P2-1 (fixed) — `queries.gen0.jsonl` order became worker-completion order.** The pool appended to
a shared list, so the artefact's line order now depends on how the four workers interleave.
Fixed by keying `_Sweep.queries` on `window.id` and emitting in `selected` (window-start) order —
the deterministic order the serial loop produced.

**P2-2 (not changed) — resume re-issues an ambiguous attempt, i.e. re-bills it.** A crash between
`dispatched` and `resolved` leaves up to `audd_concurrency` (4) attempts that the next run charges
again. This is the plan's own reading — §2.3.3 ends "attempts with other outcomes were never
cached and are simply re-run", and the alternative (never re-sending) would make one crash
permanently un-resolvable for that clip and hold the run below the 95 % primary fraction for good.
Cost of the conservative choice is ≤ 4 units (~$0.02). Left as built.

**P2-3 (not changed) — genuine task cancellation settles in flight units but does not journal
`resolved`.** Ctrl-C or a worker exception resolves the in-flight units as `timeout_post` in the
admitter, then re-raises without a `resolved` line, so the next run counts them
`resumed_ambiguous` and re-bills. Deliberate and documented by the builder: a real Ctrl-C may not
survive another write, and `dispatched`-without-`resolved` is exactly the state §2.3.3 defines for
it.

**P2-4 (not changed) — the attempt journal's three fsyncs per attempt are synchronous on the event
loop.** A 400-window Deep run does ~1,200 blocking fsyncs, which serialises the four workers
around each write. The `dispatched` fsync is required by §2.3.3; `prepared` / `resolved` could
move to a background writer in a later cycle if the primary's wall clock misses §2.3.6's ~4 min.

**P2-5 (not changed) — one 0a money assertion was weakened.**
`tests/test_phase0a_money.py::test_paid_outcome_costs_refund_zero_cost_units_and_bill_ambiguous`
went from `audd.calls == 6` to `paid_attempts == audd.calls >= 6`, because how many retries of the
429/503/`timeout_pre` windows land before the sixth window's 401 halts the pool is timing
dependent. The money invariants it exists for are still exact (`billed_units == 2`,
`paid_requests == 6`, `partial` / `provider_unavailable_midrun`, the seventh window never sent), so
this is acceptable rather than a hole.

**P2-6 (not changed) — `recognise/attempts.jsonl` grows without bound** across every run over one
media. Retention/pruning is cycle 2b.

**P2-7 (not changed) — a halted or cancelled sweep never ticks `on_window` to `(total, total)`,**
so the browser progress bar stops short of 100 % on a cancel. Cosmetic.

### Adversarial checks that passed (evidence, not assumption)

- **`dispatched` before network I/O.** `paid_clip.py` `_admit` calls `usd_admitter.admit()` then
  `journal.dispatched()` (a true append + `flush` + `os.fsync`, `journal.py:46-61`) *inside* the
  adapter's `on_attempt`, which `providers/audd.py:527-535` awaits between opening the clip and
  the `client.post`. Asserted from inside the adapter by
  `test_every_attempt_is_prepared_dispatched_before_network_io_then_resolved`.
- **Crash between `dispatched` and `resolved` never re-issues a *billed* clip as if unbilled, and
  never loses a refund.** Admission precedes the `dispatched` write, so a crash in between leaves
  only `prepared` → `reissue` (nothing was sent, nothing billed). After the write the attempt is
  `ambiguous` → charged by `UsdAdmitter.settle()` (outstanding units settle as spent,
  `money.py:159-183`) and charged again by the resume; over-charge, never under-charge. Zero-cost
  outcomes always reach `usd_admitter.resolve()` on the same line as `journal.resolved()`
  (`paid_clip.py`, the `_process` loop), and the P1-2 fix closes the one path that skipped both.
- **Total billed ≤ reservation under a 429 storm at concurrency 4.** `UsdAdmitter` holds
  `remaining + spent + outstanding x unit == reserved` across `admit` / `resolve` / `settle` under
  one `Lock`, only `BILLABLE_OUTCOMES` move value into `spent`, and every retry re-admits through
  the same gate. `tests/test_phase0a_money.py::test_throttled_run_refunds_every_unit_and_bills_nothing`
  (28 admitted dispatches, 0 billed, reservation intact) and
  `::test_primary_stops_and_journals_partial_when_admission_cannot_dispatch`.
- **The three moved 0a call counts are right, not papering over.** `all-timeout-pre` and
  `all-http-429`: both outcomes are in `DEEP_RECIPE.retry_policy["audd"].retryable_outcomes` with
  `max_retries=3`, so 7 windows x (1 + 3) = **28** is exactly the recipe's bound — confirmed by
  `test_retries_are_bounded_by_the_recipe_and_sleep_its_backoff`, which pins the chain at ordinals
  0-3 and the backoff at `[1, 2, 4]`. `all-http-401` correctly stays at **1** (`auth_error` is
  terminal, never retried). The `--allow-degrade` assertion moved from `paid_requests` (windows
  sent at least once = 7) to `paid_attempts` (dispatches incl. retries = 28) because only the
  latter equals `audd.calls`; both counts are journalled.
- **Token bucket never exceeds the ceiling.** `TokenBucket(rate_per_minute=app_config.audd_requests_per_minute,
  capacity=concurrency)`: capacity bounds the start-up burst to the pool size, refill is
  `rate_per_second` monotonic, one token per attempt (retries included), `penalize()` on 429/503,
  `recover()` on a resolved clip — asserted by
  `test_every_dispatch_takes_a_token_and_throttles_slow_the_bucket` (9 acquires, 2 penalties,
  7 recoveries) and `test_the_recipe_runs_four_clips_at_once` (`max_in_flight == 4`).
- **Anchor edge cases.** Negative, boolean, unparseable string, absent, `null`, `> anchor_max_ms`,
  and `> duration + anchor_slack_ms` from each of the three duration fields all yield
  `anchor=None`; an unknown/unparseable duration never blocks a valid timecode; the bounds come
  from the recipe and default to the same values. A bad timecode never un-matches a clip, which is
  the right reading of §2.3.4 step 2.
- **Cancellation never leaves a unit admitted-but-unsettled.** The token is polled before every
  dispatch *and* every retry; the web app's raising hook is caught by `_guard` (`JobCancelled`
  subclasses `asyncio.CancelledError`, `webapp/jobs.py:47`) and folded into the same drain instead
  of tearing down siblings that may already be billed; clips in flight resolve, journal, cache and
  charge; `_analyse` then raises `CancelledError` so the run journals `cancelled` / 130 with the
  true spend, and `JobManager._execute` still marks the job `cancelled` (it catches
  `asyncio.CancelledError`), so the browser sees "cancelled", not "failed".
- **`simultaneous_source="audd"` does not change record identity.** `native` is not part of the
  observation natural key (`providers/audd.py:440-445`), so no observation id moves; the fuser
  reads it as the per-trial source key in `fuse/alignment.py:148` and `fuse/episodes.py:276,282`.
- **Line-ending slip fully reverted.** `git diff --stat` and `git diff --stat --ignore-all-space`
  list the identical 18 files with identical file sets (only `paid_clip.py`'s line count differs,
  as expected for real code), i.e. no whitespace-only diff remains; `docs/reviews/README.md` and
  the other 44 touched files are clean in `git status`. Re-running
  `uv run python scripts/export_schemas.py` rewrote all 28 schemas byte-identically (`git status`
  unchanged), confirming only `provider_attempt_event.schema.json` is new.
- **Scope.** Every changed path is inside 0b-i / 0b-iii or is required by them (`contracts.py` for
  the new record, `money.py` only re-importing `ProviderOutcome`, `journal.py` for the durable
  append, `webapp/jobs.py` + `runner.py` for the cancel token, `config_template.py` +
  `idea.example.toml` for the one new knob). `profiles/`, `data/corpus/`, `recipes.py`,
  `pricing.toml` and `docs/PLAN-v2.md` are untouched. No new dependency.

### Changes made in this pass

| File | Change |
|---|---|
| `src/id_detector/paid_clip.py` | P1-1 classifier precedence in `_error_body_outcome` and `_protocol_outcome`; P1-2 admitted-dispatch-without-outcome settles as `timeout_post` and `_admit` refuses a second call; P2-1 `_Sweep.queries` keyed by window id and emitted in window order |
| `tests/test_phase0b_audd.py` | `test_an_explicit_error_code_outranks_the_message_words` (5 parametrised bodies through both classifiers) |
| `tests/test_phase0b_attempts.py` | `test_an_admitted_dispatch_without_an_outcome_is_settled_as_ambiguous`, `test_an_adapter_that_admits_twice_is_refused` |

### Gate outputs after the fixes

```text
$ uv run pytest -q
727 passed, 93 deselected, 1 warning in 130.31s (0:02:10)

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
226 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 375 files
fixture audit passed

$ uv run pytest tests/test_phase0b_audd.py -q          # 0b-i gate
48 passed, 1 warning in 11.24s

$ uv run pytest tests/test_phase0b_attempts.py -q      # 0b-iii gate
16 passed, 1 warning in 10.64s

$ powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

Regression evidence — the 0a-iv Phase-0 Deep gate through the real CLI with fakes
(`IDEA_TEST_MODE=1`, `--fake-providers audd,shazam`, fresh work root):

```text
complete; 6 matches; 0 failures; 2 physical attempts; 1 generations (stop=max_generations);
3 episodes; tracklist=<workroot>\present\tracklist.json
journal assertion passed at <workroot>\invocations.jsonl:1
gate exit=0
```
(`--expect status=complete algorithm_version=targeting:0,fusion:1 usd_e6_reserved=36750
usd_e6_spent=35000 usd_e2_spent=4 paid_attempts=7 paid_requests=7`)

`pwsh` is not installed on this machine; the PowerShell gates ran under Windows PowerShell 5.1 as
the plan's host note allows.

REVIEW: OK_TO_COMMIT
