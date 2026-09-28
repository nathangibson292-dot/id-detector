# Build — the last rough edges of the personal product

Builder report for the change on top of `361fe94` (uncommitted; the orchestrator reviews and merges).
Everything was run **offline**: scripted fake providers, an empty `AUDD_API_TOKEN` in every test
command and in the browser run, and a worktree with no `.env`. **AudD requests made: 0. Shazam
requests made: 0.** The only network use was reading AudD's public documentation pages (docs.audd.io
and AudD's SDK READMEs on GitHub) to find the error codes — no API endpoint was contacted.
Files reserved for the parallel fusion builder (`src/id_detector/fuse/**`, `scripts/score_corpus.py`,
`src/id_detector/recipes.py`) are untouched, as are `profiles/`, the golden Local Free file, the
playlists files, `README.md`, `idea.cmd` and `theme.py`.

## In plain words

1. **A Shazam window that came back as an error is asked once more.** At the end of the free sweep
   every window whose request reached Shazam and came back as an error (a throttle, a server error,
   a body that was not a recognition, a lost connection) gets exactly one more request — through the
   same rate limiter, the same breaker and the same per-mix request budget, and recorded in the
   attempt journal. If the retry also fails, the window stays an error. A refused credential is never
   retried; nothing about paid AudD changed.
2. **MixesDB "ID" rows lose their quote marks.** MixesDB puts unidentified entries in wiki italics
   (`''DJ HEARTSTRING - ID''`); the markup is stripped, apostrophes inside names (`Don't Stop`,
   `Lady B's Lullaby`) are kept, and `Artist - ID` / `Artist - ?` is a placeholder, never a track
   called "ID".
3. **After cancelling AudD.** With no token, `--recipe deep`, the browser's "Max accuracy" and
   `idea cost` all say *"Paid recognition is not set up (no AudD token is configured), so the Free
   result is the final one."* — the browser shows the Free result with that sentence instead of a
   failure or an Approve button. A token AudD no longer accepts (cancelled plan, wrong token, quota
   used up, account blocked) is a definite, no-charge refusal: the paid sweep stops at once, nothing
   is spent, and the owner is told which refusal it was, in words.
4. **The progress bar, watched in a real browser.** Headless Edge on a 25-minute mix: the bar only
   moved forward, tracked the wait closely (10 % at about a tenth of the time, 90 % at 90 %),
   finished at 100, and held its place through a worker restart and a page reload. One real fault
   was found and fixed: after a worker restart the "elapsed" tile dropped back to a few seconds.
5. **Two test hardenings.** The golden test now compares the bytes the Free run actually wrote,
   and a new test kills a real separate process half-way through an approval.

## 1. End-of-sweep retry of Shazam provider errors

`src/id_detector/recognise.py`, `recognise_generation`: after the worker pool drains, a new
`_retry_errors_once()` pass runs inside the same job store and the same `try` block (a cancel still
releases every lease). A window is retried when **all** of these hold:

* its job ended `permanent_failure` in this run's leasable set;
* this run's attempt-journal fold shows its newest attempt **dispatched and resolved** with an outcome
  in `END_OF_SWEEP_RETRY_OUTCOMES` = `malformed, http_5xx, http_429, http_503, timeout_post,
  timeout_pre, connect_error` (the provider was reached and gave an error rather than an answer).
  `auth_error` / `quota_error` are never retried; a window that was never dispatched (budget refusal,
  breaker refusal, signature failure) was not a provider error; a dispatched-but-unresolved
  (ambiguous) attempt is still never re-sent.

Before each retry the pass stops if `IDEA_ENGINE_SHAZAM=off` or the adapter's process breaker gives a
reason (rate trip, latch or daily budget) — so a running Free primary that the breaker lets finish
never gets retries past it — and stops if the per-mix request budget is spent (the store's own
`begin_physical_attempt` ceiling is also still in force; the budget is never extended). The retry
resets the job with the existing `reset_for_refresh`, leases just that query, and runs the ordinary
`_run_job` with `max_retries=0` (exactly one request) and `retry_of=<the failed attempt>`, so the
journal gets a new ordinal parented on the failure and resolved with its own outcome. The only write
rule relaxed: a retry may replace the **error payload** its failure left in `raw/<cache_key>.json`
(`_write_raw_json`); a stored match or no-match is never overwritten.

Tests (`tests/test_final_polish.py`):
* `test_an_errored_window_is_retried_once_at_the_end_of_the_sweep` — script: window 1 malformed then
  match, window 4 malformed twice, window 5 HTTP 401. 7 sweep requests + exactly 2 retries (windows
  1 and 4, in window order), none for the 401; journal: two retry attempts at ordinal 1, parented on
  the malformed attempts, resolved `match` and `malformed`; failures 2.
* `test_the_end_of_sweep_retry_never_goes_past_the_request_budget` — ceiling 7: no retry request.
* `test_the_end_of_sweep_retry_never_goes_around_an_open_breaker` — a breaker that trips on the
  first failure: the Free sweep finishes (7 requests) and no retry is sent.

**Existing assertions changed (exact, not loosened)** — each counted Shazam requests on a script
whose errored windows stay errored forever, so each now includes the one retry per errored window:
* `tests/test_stage1_shazam.py::test_job_executor_enforces_retry_limit`:
  `(MAX_RETRIES+1, MAX_RETRIES+1, "permanent_failure", 1)` → `(MAX_RETRIES+1+1, MAX_RETRIES+1+1,
  "permanent_failure", 2)` — the in-job limit is unchanged; the extra request is the end-of-sweep
  retry under a second lease; the window still ends `permanent_failure`.
* `tests/test_phase0a_status.py::test_free_primary_below_80_percent_is_partial`: `requests == 7` →
  `7 + 2` (status, reason and `failures == 2` unchanged).
* `tests/test_phase0a_status.py::test_deep_free_pass_below_80_percent_is_partial`: `7` → `7 + 7`
  (paid calls, spend and status unchanged).
* `tests/test_phase0b_config.py::test_free_run_with_malformed_replies_is_partial_and_says_so`:
  `7` → `7 + 2` (messages, failures and scanned windows unchanged).
* `tests/test_phase0b_config.py::test_deep_density_two_reports_the_windows_the_paid_sweep_skipped`:
  `7` → `7 + 1` (the paid plan, AudD calls and scanned windows unchanged).

## 2. MixesDB quote marks and "ID" placeholders

What the owner's cached MixesDB hints actually contain (read-only look at `hints.jsonl` in `work/`):
`[43:40] ''DJ HEARTSTRING - ID''` parsed as artist `''DJ HEARTSTRING`, title `ID''`, not a
placeholder; `''Sambaboys - ?''`, `''Benwal - ID''`, and a stray closing `''` on
`Fred Again.. - Angie (I've Been Lost) [Atlantic (Warner Music)]''`. The "quotes" are MediaWiki
emphasis (`''` italic, `'''` bold).

* `src/id_detector/hints/connectors/mixesdb.py`: `strip_wiki_emphasis()` removes runs of two or more
  apostrophes by MediaWiki's own rule (2, 3, 5 are markup; 4 keeps one literal apostrophe; more than
  5 keeps the extras). A single apostrophe is never touched.
* `src/id_detector/hints/parse.py`: `_unquote()` removes quote pairs that wrap a whole field (`"…"`,
  `“…”`, `„…“`, `‘…’`, `«…»`, and `'…'` only when the inside does not itself start or end with an
  apostrophe), applied to the line body and to the split artist and title; `_PLACEHOLDER_TITLE`
  makes a title of `ID` / `?` / `??` (optionally with trailing `(…)` / `[…]`) an `id_unknown`
  placeholder, so it gets `identity_specificity 0` and no identity key — never a track called "ID".

Tests: `test_mixesdb_wiki_quotes_are_stripped_and_apostrophes_in_names_are_kept` (real-shaped
wikitext built from the owner's own MixesDB lines plus a bold and a double-quoted row: exact
artist/title for all 10 rows, no `''` left in any stored field),
`test_a_quoted_id_stays_a_placeholder_and_never_becomes_a_track_called_id` (the six placeholders
are `id_unknown`, specificity 0 and `relations._identity_key(...) is None`; the four named tracks
keep specificity 10 000 and an identity), and
`test_wiki_emphasis_follows_mediawiki_and_only_wrapping_quotes_are_removed`. All 113 existing
hint-parser/connector tests, the hint-corroboration tests and the golden pass unchanged.

Note: there is no hint-parser version, so a cached mix's stored `hints.jsonl` is re-parsed the next
time its hints are fetched, not retroactively.

## 3. Paid recognition not set up, or a token AudD refuses

### The AudD codes and where they come from

| Code | Meaning | Outcome | Source |
|---|---|---|---|
| 900 | invalid API token | `auth_error` | docs.audd.io "Common errors" |
| 901 | no token and the free limit reached | `quota_error` (unchanged) | docs.audd.io "Common errors" |
| 902 | token quota exceeded | `quota_error` | AudD SDKs: audd-go / audd-c "902 — quota exceeded"; audd-java `AudDQuotaError (902)` |
| 903 | token problem | `auth_error` | audd-go / audd-c "900 / 901 / 903 — token problems"; audd-java `AudDAuthenticationError (900, 901, 903)` |
| 904, 905 | subscription does not cover the request (expired / cancelled plan, no access) | `auth_error` | audd-java `AudDSubscriptionError (904, 905)` |
| 19, 31337 | account or caller blocked | `auth_error` | audd-java `AudDBlockedError (19, 31337)` |
| 611 | rate limit ("back off and retry") | `http_429` (retried throttle, not a refusal) | audd-go `ErrRateLimit` "611 — back off and retry"; audd-java `AudDRateLimitError (611)` |

Before this change 902–905, 19 and 31337 fell through to `malformed` — a **billable** outcome that is
not terminal, so a cancelled plan would have been counted as spent clip after clip across the whole
sweep. Now all of them are `auth_error` / `quota_error`: zero-cost in `money.py` (unchanged), terminal
(`TERMINAL_PROVIDER_OUTCOMES`, unchanged), so the sweep stops at once, the journal resolves each
dispatched attempt with that outcome (nothing ambiguous), and the reservation refunds it. The money
authority (`money.py`, `run_ledger.py`, the SQLite fences) and the approval gate are not touched.
`PaidScanResult.refusal_code` carries AudD's code; the pipeline records it as
`counts["paid_refusal_code"]`, and `paid_clip.refusal_words()` / `paid_stop_words()` turn it into
words, e.g. *"Paid recognition stopped at once: AudD says the subscription does not cover this
request (expired or cancelled plan) (AudD error 904). AudD does not charge for a refusal, so nothing
was spent, and no further clip was sent. The Free result is the final one."*

### What the owner sees with no token

* `idea analyse <mix> --recipe deep` (exit 3, as before):
  `Deep stopped after the free pass: Paid recognition is not set up (no AudD token is configured), so
  the Free result is the final one. Nothing was reserved or spent. Your Free result is saved:
  tracklist=…` (was: `…the paid engine is unavailable (not_configured)…`). A refused token prints the
  words above with AudD's code.
* Browser "Max accuracy": the job finishes as **"Free result ready — paid recognition is not set up"**
  (or "— the paid check was refused") with the Free result open and the sentence above; there is no
  Approve button (before: the job showed "That one didn't work … exit code 3"). New `Job.paid_notice`
  (`reason`, `words`), set by `JobContext.set_paid_notice`, published in the job status. The page
  change is in the live job script only (`present/server.py`), not a saved result page, so
  `PAGE_VERSION` is not bumped.
* `idea cost`: the figures, then *"Paid recognition is not set up (no AudD token is configured): a
  Deep run would stop after its free pass, and the Free result is the final one. The figures above
  are what Deep would cost only once a token is set up."*

Checked while building: without a token the existing pipeline already never reaches the price gate
when clips need sending (it stops `not_configured`, or reads stored answers only), so no Approve
button could be offered for an unbuyable plan on that path; the browser's problem was the failure
card. (A guard I first added for it was a no-op, found by its revert proof, and removed.)

Tests: `test_deep_without_a_token_says_plainly_that_the_free_result_is_final` (CLI, fake Shazam
only, no AudD events, $0), `test_idea_cost_says_when_paid_recognition_is_not_set_up` (and not with a
placeholder token — `idea cost` sends nothing either way),
`test_a_token_audd_refuses_stops_the_paid_sweep_and_spends_nothing[900|901|902|903|904|905|19|31337]`
(a fake AudD answering every clip with that error body: exit 3, `provider_unavailable`, 1–4 calls for
a 7-clip gap, `usd_e6_spent == 0`, every dispatch resolved with the terminal outcome, the code in the
journal and in the words), `test_audd_rate_limit_code_is_a_retried_throttle_not_a_refusal`,
`test_the_cli_names_audds_own_refusal_in_words`, and
`test_max_accuracy_shows_the_free_result_with_plain_words_and_no_approve_button[not_configured|quota_error]`
(the real local worker and pipeline runner: job succeeded, no offer, notice words, result URL; the
refused case goes through Approve first, exactly as the owner would).

## 4. The progress bar in a real browser

Setup (all outside the repo, scratch folder): a synthetic 25-minute WAV (distinct chirps every 3 s,
167 listening windows); `idea serve --port 8793 --no-open --runner slowfakes:slow_runner` with
`IDEA_TEST_MODE=1` and `AUDD_API_TOKEN=""`; the runner is the production `make_pipeline_runner` with
`FakeShazamHTTP` (4.5 s latency per answer) and **no** paid adapter; config `requests_per_minute = 45,
concurrency = 3` (≈ 40 windows/min, the owner's real pace). To make this possible `idea serve` gained
a hidden `--runner` option that is refused unless `IDEA_TEST_MODE=1` (test
`test_serve_refuses_a_substitute_runner_outside_test_mode`). A Python driver started the server,
started headless Edge (`--headless=new`, own profile, CDP on 9334), submitted the mix through the
real home-page form (Free), and every 5 s read the page's own `#pct`, `#t-eta`, phase and elapsed
tiles plus the server status; it killed the local worker process (both PIDs of the supervisor's
child) at t = 106 s, reloaded the page 15 s later, and at the end killed Edge and the server by PID
(tree), then confirmed no process was left.

**Final run (after the fix), selected samples** — total wall time 312 s, of which ~33 s was the
worker restart (supervisor restart + lease):

```
  t(s)  bar   time left       phase      elapsed   windows   true fraction t/312
   0.0   0%   …               queue      —           0/0      0 %
   5.0   2%   Estimating…     Prepare    5s          0/0      2 %
  20.2   3%   Estimating…     Listen     21s         6/167    6 %
  30.2  10%   about 4 min     Listen     31s        12/167   10 %
  60.8  22%   about 3 min     Listen     1m 01s     33/167   19 %
 106.2  39%   about 3 min     Listen     1m 46s     60/167   34 %   <- worker killed here
 121.5  39%   about 4 min     Listen     2m 02s     60/167          (bar holds, estimate grows)
 129.8  39%   about 4 min     Listen     2m 10s     60/167          <- page reloaded: still 39 %
 139.9  39%   Estimating…     Prepare    2m 20s     60/167          <- restarted worker resumes
 160.2  42%   about 3 min     Listen     2m 40s     72/167   51 %
 175.3  51%   about 2 min     Listen     2m 55s     81/167   56 %
 226.0  70%   about 1 min     Listen     3m 46s    114/167   72 %
 281.5  88%   under a minute  Listen     4m 41s    150/167   90 %
 307.0  98%   under a minute  Line up    5m 07s    164/167   98 %
 312.0 100%   —               Done       5m 10s    164/167  100 %
```

Findings:
* **Only forward**: 62 samples, the page's percentage never decreased; the server's `progress_pct`
  never decreased either.
* **Never back to zero**: through the worker kill, the restart and a full page reload the bar stayed
  at 39 % and then carried on.
* **Proportional**: 10 % arrived at 30 s (a tenth of 312 s); 22 % at 61 s; 51 % at 175 s; 70 % at
  226 s; 88 % at 282 s. The largest gap is right after the restart (39 % held while ~50 s passed:
  the ~33 s the restarted worker needed to get going, then the fresh rate needing 15 s of samples) —
  the bar honestly waits rather than guessing, and the time-left tile grows ("about 3 min" → "about
  4 min" → "Estimating…") while nothing is being answered.
* **Finishes at 100** with "Done", "Tracklist ready".
* **Fault found and fixed**: in the run before the fix, after the restart the elapsed tile went from
  `2m 14s` to `3s` (and counted up from there) while the bar correctly held 40 %: the in-process
  `JobManager._execute` overwrote `started_at` on every execution, and a restarted local job is
  executed again. Now `started_at` is set only when it is empty (`src/id_detector/webapp/jobs.py`);
  the phase clock still starts fresh. The final run shows `2m 15s` → `2m 20s` across the restart.
  Test: `test_a_restarted_job_keeps_its_first_start_time_so_elapsed_carries_on`.
* Not a fault, noted: the three windows in flight when the worker was killed stay unanswered
  (164/167) — dispatched-but-unresolved Shazam attempts are never re-sent automatically (the 4a-i
  rule), and the new end-of-sweep retry deliberately leaves them alone.
* An earlier calibration run with a much faster fake (133 windows/min against the cautious 18/min
  prior) showed the bar under-promising for the first ~25 s (4 % at 20 s of a 95 s run) and then
  catching up at the capped climb rate (4 → 13 → 26 → 38 % in 15 s). That is the documented
  "early number under-promises" design, visible only when the engine is far faster than Shazam's
  real rate; at the realistic pace above it is within a few points.

Screenshots (`docs/reviews/final-polish-shots/`): `bar-10pct.png` (10 %, "about 4 min"),
`before-worker-restart.png` (39 %), `after-restart-reload.png` (39 % after the kill and a full page
reload, elapsed 2m 10s), `finished.png` ("Tracklist ready").

## 5. Test hardenings

* **Golden compared as produced.** `test_local_free_run_reproduces_the_golden_bytes` now reads the raw
  bytes of the `tracklist.json` the Free run wrote and requires them to equal the committed golden's
  content — with only the run identity and timestamps taken from the produced file — rendered in the
  writer's documented format, spelled out independently in the test (UTF-8, sorted keys, compact
  separators, no ASCII escaping, no trailing newline), plus no `\r` anywhere. The writer was already
  platform-independent (`canonical_json_bytes`), so no writer change was needed; the golden file is
  unchanged (still blob `316b6171…`).
* **A real process killed mid-approval.**
  `test_killing_a_separate_process_mid_approval_then_retrying_pays_at_most_once` starts a separate
  Python process that approves the offer with `insert_job` wrapped to signal and then hang right after
  the paid job is written (inside the approval transaction, before the offer is consumed); the test
  kills that process from outside, checks that no paid job survived and the offer is still open,
  retries (one follow-up), retries again (nothing), runs the queue twice, and proves one paid run:
  7 AudD calls, 1 reservation.

## Revert proofs

`revert_proofs.py` (kept outside the repo) applies one mutation at a time, runs the tests that guard
it, and restores the file byte-for-byte (`git diff --stat` compared before and after: identical).

```
R1 no end-of-sweep retry of errored Shazam windows: FAILS as required -> 1 failed
R2 the retry extends the request budget (goes around it): FAILS as required -> 1 failed
R3 the retry ignores an open breaker: FAILS as required -> 1 failed
R4 MixesDB wiki emphasis kept: FAILS as required -> 2 failed
R5 'Artist - ID' is not a placeholder: FAILS as required -> 1 failed
R6 wrapping quotes kept: FAILS as required -> 2 failed
R7 AudD account/token codes back to 900/901 only: FAILS as required -> 7 failed, 2 passed
    (902, 903, 904, 905, 19, 31337 and 611 fail; 900 and 901 were already handled and pass)
R8 no plain words for a paid stop (the old internal-code message): FAILS as required -> 4 failed
R10 idea cost silent about a missing token: FAILS as required -> 1 failed
R11 the browser runner shows no notice (the job fails with exit code 3 again): FAILS as required -> 2 failed
R12 approval commits the paid job before the offer is consumed (not one transaction): FAILS as required -> 1 failed
R13 a restarted job's start time is reset: FAILS as required -> 1 failed
R14 idea serve accepts a substitute runner outside test mode: FAILS as required -> 1 failed
G1 golden: the tracklist writer pretty-prints (indent 2): the OLD byte test and the semantic test
   still pass (2 passed), the NEW byte test fails (1 failed)
```

Honesty notes: R9 ("the price gate is asked even with no credential") came back STILL PASSES — the
guard it reverted was unreachable (see §3), so the guard and its test were removed rather than kept
as a claim. R2's first form (just deleting the pre-check) would also have passed, because the job
store's own ceiling refuses the request anyway; the recorded mutation makes the retry extend the
budget, which is the failure the test is for. R14's first form hung (with the gate removed the test
really started a server on an ephemeral loopback port); that test process was killed by PID, the
file was restored, and the test now replaces `make_server` with one that fails, so a regression
fails fast instead of serving.

## Gates

`uv run pytest --collect-only -q`: `2132/2229 tests collected (97 deselected)`. The shard file lists
cover every file that collects a test (108 files, checked against the collect-only listing:
identical, no duplicates). Every shard was run in the foreground; s3 and s4 were re-run in full after
the assertion updates above.

```
s1 tests/test_refusion.py                                                   44 passed in 395.71s
s2 test_service_api, test_followup_money_resume                             42 passed in 368.14s
s3 phase0a_status, phase0b_audd, phase0b_attempts, phase0a_money,
   phase1b_breaker_scorer, phase1a_compat                                  203 passed in 247.68s
s4 phase1b_fusion, phase0b_config, phase0a_crash_cache, phase0a_security,
   phase3a_honesty, score_corpus, stage4d_profiles, cost_preview,
   additive_deep, phase1b_targeting, final_polish                          248 passed in 388.73s
s5 idea_web: coalescing, followup_queue_money, worker, refusion_money,
   followup_round2, followup_round4                                        107 passed in 249.50s
s6 idea_web: followup_round5, round6, round7, followup_review_fixes         45 passed in 112.98s
s7 the other 11 idea_web files + test_accuracy_fixes … test_engine_corroboration
                                                                           390 passed in 207.26s
s8 test_fixture_audit … test_stage1_shazam (21 files)                      349 passed in 296.88s
s9 test_stage1_wheel … test_truth_review (37 files)       702 passed, 2 skipped, 5 deselected in 217.77s
total: 2130 passed + 2 skipped = 2132 collected
extra: -m "slow and not live" tests/test_stage2b_pipeline.py tests/test_stage4c_generations.py
                                                                            14 passed in 62.58s
```

(First pass of s3/s4, before the assertion updates: s3 `2 failed, 201 passed`, s4 `3 failed, 245
passed` — the five request-count assertions listed in §1, and
`test_real_web_runner_fails_exit_3_without_attaching_stale_result`, which caught the runner reading
the journal for a refusal code on a run with no reason; the code is now read only for
`auth_error`/`quota_error`.)

```
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
433 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 554 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

```
$ git diff --stat
 src/id_detector/cli.py                      |  61 ++++++++++++--
 src/id_detector/hints/connectors/mixesdb.py |  27 +++++++
 src/id_detector/hints/parse.py              |  54 +++++++++++--
 src/id_detector/paid_clip.py                | 110 +++++++++++++++++++++++--
 src/id_detector/pipeline.py                 |   3 +
 src/id_detector/present/server.py           |   9 +++
 src/id_detector/recognise.py                | 121 ++++++++++++++++++++++++++--
 src/id_detector/webapp/jobs.py              |  20 ++++-
 src/id_detector/webapp/runner.py            |  25 ++++++
 tests/test_golden_local_free.py             |  58 +++++++------
 tests/test_phase0a_status.py                |   7 +-
 tests/test_phase0b_config.py                |   6 +-
 tests/test_stage1_shazam.py                 |  10 ++-
 13 files changed, 457 insertions(+), 54 deletions(-)
$ git status --short
 M src/id_detector/cli.py
 M src/id_detector/hints/connectors/mixesdb.py
 M src/id_detector/hints/parse.py
 M src/id_detector/paid_clip.py
 M src/id_detector/pipeline.py
 M src/id_detector/present/server.py
 M src/id_detector/recognise.py
 M src/id_detector/webapp/jobs.py
 M src/id_detector/webapp/runner.py
 M tests/test_golden_local_free.py
 M tests/test_phase0a_status.py
 M tests/test_phase0b_config.py
 M tests/test_stage1_shazam.py
?? docs/reviews/build-local-final-polish.md
?? docs/reviews/final-polish-shots/
?? tests/test_final_polish.py
$ git status --short -- data work
(empty)
```

Read-only check of the main checkout: `git status --short -- data work` there is empty, and no file
under its `work/` or `data/corpus/` has a modification time from today (a `find -newermt` over both
trees found none; the newest file in `work/` is from 2026-09-13 and in `data/corpus/` from 2026-09-14).
`id-detector-deepscan` was never opened.


---

## Fix pass 1 (review `review-finalpol-round1.md`, verdict FIX_FIRST)

Kept as the review confirmed: the Shazam retry (once, in window order, through the same adapter,
limiter, breaker, budget and journal; credential refusals and unresolved attempts excluded) and its
five exact assertion changes; the MixesDB italics and placeholder fix; the no-token CLI / `idea cost` /
browser notices; `started_at` preserved with only the phase clock reset; the golden raw-bytes test
(blob `316b6171…` unchanged); the real child-process kill in the approval test. **New provider
requests in this pass: 0** (fakes and an empty `AUDD_API_TOKEN` throughout, including the browser run).
**This section supersedes §3's statement that 902–905, 19 and 31337 cost nothing and that 611 is a
retried throttle.**

### P1 — undocumented AudD codes are no longer refunded

**The billing evidence, and what it does and does not cover:**

* AudD's own documentation, the only pricing statement (docs.audd.io, fetched this pass): *"Normally,
  our plans include as-you-go pricing: some number of requests is included with your monthly bill,
  but you can send requests on top of what's included and will be charged for those separately."*
  It documents the meanings of 900 (*"Invalid API token"*) and 901 (*"No api_token passed, and the
  limit was reached"*) and says **nothing** about whether any error response is billed.
* AudD's written answer to the owner (`docs/legal/audd-commercial-answers-2026-09-11.md`), question
  *"Are HTTP 429/503 responses billed?"* — answer *"No; you should not experience those HTTP errors."*
  That covers the **HTTP status codes** 429 and 503 only (and so `bill_on_throttle = false` for them).
* AudD's SDKs (audd-java `AudDApiError` hierarchy; audd-go / audd-c sentinels) classify 902 (quota),
  903 (token), 904 / 905 (subscription), 19 / 31337 (blocked: request blocking, abuse or a test-scope
  restriction) and 611 (rate limit) as exception categories — they say nothing about billing.

**No AudD document says any of these body codes is unbilled, so none of them is refunded.** 611
arrives as a code in the response body, not as an HTTP 429 status, so the written 429/503 answer does
not cover it; it is treated conservatively too.

`src/id_detector/paid_clip.py`: `AUDD_BILLED_REFUSAL_CODES = {902: quota_error, 903/904/905:
auth_error, 19/31337: blocked, 611: rate_limited}`. `_error_body_outcome` records such an answer as the
billable `malformed` outcome — counted as spent by the unchanged money authority, `settled` on resume,
never retried — and the sweep marks the attempt as a stop (`stop_reasons`), so the paid sweep **stops
at once** with that reason (`provider_stopped`; the run's reason is `quota_error`, `auth_error`,
`blocked` or `rate_limited`). `_AUDD_AUTH_CODES = {401, 403, 900}` and `_AUDD_QUOTA_CODES = {402, 901}`
are back to exactly what the code base had before this build (the zero-cost terminal outcomes of the
existing money authority, untouched). Flag for the owner: that pre-existing zero-cost treatment of
900 / 901 and HTTP 401 / 402 / 403 is not backed by AudD documentation either; I did not change it
because it belongs to the audited money authority and was not introduced by this build — it is a
one-line decision if the owner wants it conservative too.

### P1 — every spend statement comes from the settled spend

`paid_stop_words(reason, code, usd_e6_spent)` and `spent_words(usd_e6_spent)` state the run's frozen
`usd_e6_spent`: *"$0.01 of AudD credit was counted as spent (every request AudD answered, or may have
answered, is counted)."* or *"Nothing was spent."* — never inferred from the fact that a refusal
happened. The CLI passes `RunResult.usd_e6_spent`, the browser runner passes the service result's,
the sweep's log line no longer says "a refusal is not charged", and the page's cost sentence (Python
`_cost_sentence` and the job script's `costSentence`) states a settled non-zero spend before any
"nothing was spent" rule for `provider_unavailable` / `budget_exhausted`. A declined price keeps its
exact wording ("Nothing was reserved or spent.", which `test_cost_preview` pins) only while the
settled spend is zero.

### P1 — `--runner` locked down

`src/idea_web/jobs/local.py`: `TEST_RUNNERS` is a strict allowlist of the four offline fakes in
`tests/idea_web/local_runner_fakes.py` (`slow_runner`, `forever_runner`, `deep_runner`, and the new
`browser_check_runner` that replaces the scratch runner used in the first browser run).
`runner_spec_refusal()` refuses unless `IDEA_TEST_MODE=1`, `AUDD_API_TOKEN` is empty and the spec is on
the list; `_load_runner` also refuses a module that does not resolve inside this checkout's `tests/`.
`idea serve --runner` uses the same check before anything starts. All existing `--runner` users in the
suite already name allowlisted runners.

### Follow-ups

* **No token, some stored paid answers**: the pipeline records `paid_not_sent_no_credential`; the CLI
  prints and the browser shows (reason `not_configured_cached`, heading "Tracklist ready — paid
  recognition is not set up") *"Paid recognition is not set up (no AudD token is configured): 1
  clip(s) Deep would check were not sent, and only paid answers already stored were used, so this
  result is the final one. Nothing was spent."* `idea cost` with an unknown length prints the not-set-up
  notice before exiting 2.
* **Retry pass bounded** (`recognise.py`): at most `max(10, 10 % of the mix's windows)` retries, and the
  pass ends after 3 retries in a row that could not reach Shazam at all (`connect_error` /
  `timeout_pre`). Why both: the breaker does not sample connection failures, so without the streak rule
  a mix whose requests cannot connect would re-ask every errored window; the cap never binds on an
  ordinary mix (about 2.8 % of windows error on the owner's mixes; 10 % of a 600-window mix is 60) yet
  bounds a bad mix to a tenth of its windows. I chose a cap plus a streak over giving connection
  failures breaker treatment because the breaker is shared with the hosted policy and its failure
  definition is part of the frozen D8 rules.
* **Elapsed continuity as evidence**: the browser check was re-run with the allowlisted
  `browser_check_runner`; screenshots now open **only** "Timing details" (the log stays closed — it
  names the synthetic mix's local file path). `after-restart-reload.png` shows 39 % and "2m 05s
  elapsed" after the worker kill (t = 106 s) and a full page reload. The raw trace is committed as
  `docs/reviews/final-polish-shots/browser-trace.json` (64 samples: bar, time left, phase, elapsed,
  server figures, plus the event log; checked: no user name, no file path, PNGs carry only
  IHDR/IDAT/IEND chunks). This run: bar monotonic over 63 samples, 39 % held through the restart,
  elapsed `2m 15s` → `2m 20s` across it, 100 % at 318 s. Every server and browser PID was stopped and a
  process scan found none left.

### Tests (all in `tests/test_final_polish.py`)

* `test_a_token_audd_refuses_stops_the_paid_sweep_at_once[900|901|902|903|904|905|19|31337|611]`
  (replaces the first build's "spends nothing" test and the 611-throttle test): every code stops the
  sweep within the in-flight requests; 900 / 901 settle `auth_error` / `quota_error` at $0; the others
  settle `malformed` and `usd_e6_spent == 5 000 × requests`, and the words state that amount.
* `test_a_refusal_beside_a_charged_in_flight_request_states_the_real_spend` — the real CLI, two AudD
  requests genuinely in flight (`max_in_flight >= 2`): one refused (900, $0) while the other loses its
  answer (`timeout_post`, charged); journal `{auth_error, timeout_post}`, `usd_e6_spent == 5 000`, the
  output says "$0.01 of AudD credit was counted as spent" and never "nothing was spent".
* `test_max_accuracy_states_the_real_spend_of_a_refusal_beside_a_charged_request` — the same through
  the local worker and browser notice.
* `test_the_page_states_a_settled_spend_even_when_the_run_stopped_unavailable`.
* `test_the_worker_loads_only_the_built_in_offline_runners`,
  `test_serve_refuses_an_arbitrary_runner_even_in_test_mode` (`os:system` refused; a server start is
  replaced by a failure so a regression cannot hang).
* `test_the_retry_pass_is_capped`, `test_the_retry_pass_stops_when_shazam_cannot_be_reached`
  (42 unreachable sweep requests, then exactly 3 retries instead of 7).
* `test_no_token_deep_with_some_stored_paid_answers_says_so` (CLI),
  `test_max_accuracy_without_a_token_but_with_stored_answers_shows_the_notice` (browser),
  `test_idea_cost_with_an_unknown_length_still_says_paid_is_not_set_up`.
* Updated: `test_the_cli_names_audds_own_refusal_in_words` now expects "Nothing was spent." (a 402
  refusal settles at $0).

### Revert proofs (fix pass 1)

`revert_fix1.py` (outside the repo) — one mutation at a time, restored byte-for-byte (`git diff
--stat` identical before and after):

```
F1 undocumented AudD codes refunded again (the first build's classification): FAILS as required -> 7 failed, 2 passed
    (902, 903, 904, 905, 19, 31337, 611 fail; 900 and 901 are unchanged and pass)
F2 a billed refusal does not stop the sweep: FAILS as required -> 7 failed, 2 passed
F3 stop words ignore the settled spend: FAILS as required -> 2 failed
    test_a_refusal_beside_a_charged_in_flight_request_states_the_real_spend
    test_max_accuracy_states_the_real_spend_of_a_refusal_beside_a_charged_request
F4 the page's cost sentence ignores the settled spend for an unavailable run: FAILS as required -> 1 failed
F5 any module:factory accepted as the worker's runner in test mode: FAILS as required -> 2 failed
F6 a substitute runner allowed beside a paid credential: FAILS as required -> 1 failed
F7 no cap on the retry pass: FAILS as required -> 1 failed
F8 the retry pass keeps asking when Shazam cannot be reached: FAILS as required -> 1 failed
F9 no notice when no token leaves stored paid answers only: FAILS as required -> 2 failed
F10 idea cost exits on an unknown length before the notice: FAILS as required -> 1 failed
```

The first build's proofs R7, R8 and R14 mutated code this pass rewrote; F1/F2, F3 and F5 replace them.
The JavaScript `costSentence` change is covered by `scripts/check_page_js.py` (syntax) and mirrors the
unit-tested Python `_cost_sentence`; it has no separate behavioural test.

### Gates (fix pass 1)

`uv run pytest --collect-only -q`: `2142/2239 tests collected (97 deselected)`; the nine shard file
lists cover exactly the 108 files that collect tests (diffed against the collect-only listing:
identical, no duplicates).

```
s1 tests/test_refusion.py                                              44 passed in 375.11s
s2 test_service_api, test_followup_money_resume                        42 passed in 395.49s
s3 phase0a_status … phase1a_compat (6 files)                          203 passed in 169.66s
s4 phase1b_fusion … phase1b_targeting, final_polish (11 files)        258 passed in 295.24s
s5 idea_web coalescing … followup_round4 (6 files)                    107 passed in 191.90s
s6 idea_web followup_round5 … followup_review_fixes (4 files)          45 passed in 84.22s
s7 other idea_web files + test_accuracy_fixes … engine_corroboration  390 passed in 140.20s
s8 test_fixture_audit … test_stage1_shazam (21 files)                 349 passed in 112.85s
s9 test_stage1_wheel … test_truth_review (37 files)   702 passed, 2 skipped, 5 deselected in 136.79s
total: 2140 passed + 2 skipped = 2142 collected
extra: -m "slow and not live" test_stage2b_pipeline, test_stage4c_generations   14 passed in 39.38s
```

(s4's first run: `2 failed, 256 passed` — `test_cost_preview::test_cli_confirmation_before_reservation`
for a declined price expected "Nothing was reserved or spent."; the decline wording is kept exactly for
a zero settled spend, and s4 then passed in full as shown.)

```
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
433 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 555 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

```
$ git diff --stat
 src/id_detector/cli.py                      |  94 ++++++++++++++-
 src/id_detector/hints/connectors/mixesdb.py |  27 +++++
 src/id_detector/hints/parse.py              |  54 ++++++++-
 src/id_detector/paid_clip.py                | 176 ++++++++++++++++++++++++++--
 src/id_detector/pipeline.py                 |   4 +
 src/id_detector/present/server.py           |  19 +++
 src/id_detector/recognise.py                | 141 +++++++++++++++++++++-
 src/id_detector/webapp/jobs.py              |  20 +++-
 src/id_detector/webapp/runner.py            |  42 +++++++
 src/idea_web/jobs/local.py                  |  42 ++++++-
 tests/idea_web/local_runner_fakes.py        |  19 +++
 tests/test_golden_local_free.py             |  58 +++++----
 tests/test_phase0a_status.py                |   7 +-
 tests/test_phase0b_config.py                |   6 +-
 tests/test_stage1_shazam.py                 |  10 +-
 15 files changed, 661 insertions(+), 58 deletions(-)
$ git status --short
 M src/id_detector/cli.py
 M src/id_detector/hints/connectors/mixesdb.py
 M src/id_detector/hints/parse.py
 M src/id_detector/paid_clip.py
 M src/id_detector/pipeline.py
 M src/id_detector/present/server.py
 M src/id_detector/recognise.py
 M src/id_detector/webapp/jobs.py
 M src/id_detector/webapp/runner.py
 M src/idea_web/jobs/local.py
 M tests/idea_web/local_runner_fakes.py
 M tests/test_golden_local_free.py
 M tests/test_phase0a_status.py
 M tests/test_phase0b_config.py
 M tests/test_stage1_shazam.py
?? docs/reviews/build-local-final-polish.md
?? docs/reviews/final-polish-shots/
?? tests/test_final_polish.py
$ git status --short -- data work
(empty)
```

`git status --short` for the protected paths (fusion, `scripts/score_corpus.py`, `recipes.py`,
`profiles/`, `tests/golden`, playlists, `README.md`, `idea.cmd`, `theme.py`) is empty; the main
checkout's `git status --short -- data work` is empty.


---

## Fix pass 2 — cache writes

The product was left alone: a real `idea analyse` still maintains the index under the work root it
is given.  The test defect was that CLI tests could inherit checkout-local defaults.  The reported
`test_allow_degrade_cli_flag_is_threaded_and_off_by_default` now gives all three `CliRunner`
invocations one `tmp_path` work root and a missing `tmp_path/idea.toml`; its original assertions are
unchanged.

### Audit and fixes

The complete `CliRunner.invoke` audit found and isolated these tests/helpers.  “Config” below means a
test already had a temporary work root but could still read the repository's `idea.toml`; those now
also receive a temporary `--config`.

* `tests/test_phase0a_status.py::test_allow_degrade_cli_flag_is_threaded_and_off_by_default`
  (the reported defect; work root + config on default, flagged and help invocations).
* `tests/test_phase0a_money.py::{test_recipe_cli_selects_deep_and_retires_max_paid_clips,
  test_legacy_max_accuracy_profile_never_starts_paid_work_without_an_explicit_recipe}`;
  `tests/test_phase0a_security.py::test_engine_acrcloud_is_refused_and_the_consent_flag_is_inert`;
  `tests/test_phase0a_crash_cache.py::test_fake_provider_cli_is_hidden_guarded_and_injects_both_boundaries`;
  `tests/test_phase1b_fusion.py::test_profile_max_accuracy_alone_selects_the_free_recipe`;
  `tests/test_scan.py::test_analyse_rejects_an_unknown_engine_before_running`; and
  `tests/test_stage4d_profiles.py::{test_analyse_rejects_a_profile_that_is_not_a_frozen_artefact,
  test_analyse_accepts_a_frozen_profile_and_derives_its_config}` (work root + config).
* `tests/test_phase0b_audd.py::test_audd_requests_per_minute_is_a_deep_config_knob_carried_under_a_profile`
  and `tests/test_phase0b_config.py::test_cli_analyse_and_the_web_runner_carry_the_five_knobs_and_agree`
  (temporary work root; their configs were already temporary).
* `tests/test_cost_preview.py::{test_cost_cached_key_and_url_read_only,
  test_unknown_length_requires_minutes_without_creating_work,
  test_cli_confirmation_before_reservation,test_gate_is_cli_only_and_free_never_prompts}` (config
  where needed, and work root + config for the two previously defaulted `analyse` calls).
* `tests/test_final_polish.py::_cli_deep` (therefore its three callers), plus
  `test_idea_cost_says_when_paid_recognition_is_not_set_up`,
  `test_a_refusal_beside_a_charged_in_flight_request_states_the_real_spend`,
  `test_serve_refuses_a_substitute_runner_outside_test_mode`,
  `test_serve_refuses_an_arbitrary_runner_even_in_test_mode`,
  `test_no_token_deep_with_some_stored_paid_answers_says_so`, and
  `test_idea_cost_with_an_unknown_length_still_says_paid_is_not_set_up` (config isolation).
* `tests/test_phase1a_compat.py::test_altered_local_source_exit_5_preserves_results`,
  `tests/test_refusion.py::test_owner_visible_upkeep_skips`, and
  `tests/test_service_api.py::test_full_local_service_run_matches_the_cli_bundle_and_honours_run_id`
  (config isolation).
* `tests/test_stage3_shortlist.py::{test_shortlist_cli_contract_uses_requested_corpus_and_output,
  test_shortlist_cli_passes_refresh_and_redacts_failures}` (work root + config), and
  `tests/test_stage4a_pipeline.py::test_cli_exposes_required_stage4a_options` (temporary roots for
  all three help calls and temporary config for `analyse`).
* `tests/test_certification_followup.py::test_idea_benchmark_certify_says_certified_only_when_something_was`,
  `tests/test_truth_corpus_followup_r8.py::test_idea_benchmark_certify_refuses_with_the_disabled_message`,
  and `tests/test_truth_corpus_followup_r10.py::test_no_emitter_claims_certification_while_the_gate_is_closed`
  (temporary certify roots).
* `tests/idea_web/test_backup.py::test_snapshot_commands_run_end_to_end_through_the_idea_cli`
  (temporary roots on the `backup --help` and `restore --help` invocations; the active commands were
  already isolated).

No direct Python call that omitted a `Path("work")` / `DEFAULT_WORK_ROOT` argument was found.  The new
`test_cli_tests_always_inject_work_roots_and_repository_config_paths` statically expands local list
variables and starred CLI argument lists, and fails any visible work-root command without
`--work-root`, or any `analyse` / `cost` / `serve` / `rescan` / `benchmark shortlist` invocation
without `--config`.  This pins the whole audit, not just the originally reported test.

### Session safety net

`tests/conftest.py` now fingerprints `<repository>/work` at session start and compares it at session
finish.  It records `(size, mtime_ns)` for `index.json`, `.idea/app.db`, `app.db-wal` and `app.db-shm`,
and `(name, mtime_ns)` for only the top-level entries.  Its implementation uses only `is_dir`,
`iterdir` and `stat`: it never opens, reads, writes, locks, creates or recursively walks anything in
`work/`.  An absent work root records `None` and makes both hooks a no-op.  A difference sets pytest's
exit status to `TESTS_FAILED` and prints every changed sentinel by name.

The root is injectable through `_fingerprint_work_root(work_root)` and
`_assert_work_root_unchanged(work_root, before)`.  The focused temporary-directory test proves the
absent-root no-op and overwrites a temporary `index.json`; the guard names `work/index.json` and its
old/new size and mtime.

### Revert proofs (fix pass 2)

Each mutation was applied alone and restored immediately; the combined focused run after restoration
was `3 passed, 1 warning in 3.41s`.

```text
R1 remove --work-root from the reported test's shared safe options:
FAILED test_cli_tests_always_inject_work_roots_and_repository_config_paths
test_phase0a_status.py:367 analyse has no --work-root
test_phase0a_status.py:372 analyse has no --work-root
test_phase0a_status.py:385 analyse has no --work-root
1 failed in 3.74s

R2 disable the fingerprint change assertion:
FAILED test_work_cache_guard_is_injectable_catches_a_write_and_ignores_an_absent_root
E Failed: DID NOT RAISE AssertionError
1 failed in 0.71s
```

### Gates (fix pass 2)

`uv run pytest --collect-only -q`:

```text
2144/2241 tests collected (97 deselected) in 3.89s
```

The 109 collecting files were sorted once.  Three slow/special groups cover four files; removing
those leaves 105 files, covered exactly once by the inclusive sorted slices `0..19`, `20..37`,
`38..55`, `56..73`, `74..89`, and `90..104`.  Thus the nine foreground shards have an identical
union to collect-only and no duplicate file.  Their selected totals are
`2142 passed + 2 skipped = 2144`:

```text
s1 tests/test_refusion.py
44 passed, 1 warning in 294.88s (0:04:54)

s2 tests/test_service_api.py tests/test_followup_money_resume.py
42 passed, 1 warning in 1566.43s (0:26:06)

s3 tests/idea_web/test_backup.py
50 passed, 1 warning in 40.55s

s4 remaining[0..19]: tests/idea_web/test_accounts.py ... tests/idea_web/test_worker.py
345 passed, 1 warning in 406.24s (0:06:46)

s5 remaining[20..37]: tests/test_accuracy_fixes.py ... tests/test_io_backstop_followup.py
319 passed, 1 warning in 270.10s (0:04:30)

s6 remaining[38..55]: tests/test_local_index.py ... tests/test_playlists.py
437 passed, 1 warning in 319.92s (0:05:19)

s7 remaining[56..73]: tests/test_progress_wallclock.py ... tests/test_stage2b_fuser.py
280 passed, 1 warning in 71.81s (0:01:11)

s8 remaining[74..89]: tests/test_stage3_providers.py ... tests/test_stage8_candidates.py
285 passed, 1 warning in 28.82s

s9 remaining[90..104]: tests/test_stage8_panako.py ... tests/test_truth_review.py
340 passed, 2 skipped, 5 deselected, 1 warning in 117.21s (0:01:57)
```

Honesty notes on reruns: s2's first resource-starved run returned 0 fake Shazam matches in two
`test_service_api` cases (`2 failed, 40 passed in 1219.17s`); both passed focused (`2 passed in
57.38s`) and the full shard then passed as pasted.  s4's first run hit the existing probabilistic
CSRF-test issue where replacing a random token's last hex character with `0` does not corrupt a token
already ending in `0` (`1 failed, 344 passed`); it passed focused and the full shard then passed as
pasted.  The first affected-files diagnostic had one builder error (a newly used `tmp_path` omitted
from a signature, `470 passed, 1 failed`); the signature was fixed before the clean shards.  No
session printed a work-cache change.

```text
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
434 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 555 files
fixture audit passed

$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

`ruff check` first found one import-order issue in the new test; Ruff fixed that mechanical ordering
and the pasted final run is clean.

```text
$ git status --short
 M src/id_detector/cli.py
 M src/id_detector/hints/connectors/mixesdb.py
 M src/id_detector/hints/parse.py
 M src/id_detector/paid_clip.py
 M src/id_detector/pipeline.py
 M src/id_detector/present/server.py
 M src/id_detector/recognise.py
 M src/id_detector/webapp/jobs.py
 M src/id_detector/webapp/runner.py
 M src/idea_web/jobs/local.py
 M tests/conftest.py
 M tests/idea_web/local_runner_fakes.py
 M tests/idea_web/test_backup.py
 M tests/test_certification_followup.py
 M tests/test_cost_preview.py
 M tests/test_golden_local_free.py
 M tests/test_phase0a_crash_cache.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_security.py
 M tests/test_phase0a_status.py
 M tests/test_phase0b_audd.py
 M tests/test_phase0b_config.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_fusion.py
 M tests/test_refusion.py
 M tests/test_scan.py
 M tests/test_service_api.py
 M tests/test_stage1_shazam.py
 M tests/test_stage3_shortlist.py
 M tests/test_stage4a_pipeline.py
 M tests/test_stage4d_profiles.py
 M tests/test_truth_corpus_followup_r10.py
 M tests/test_truth_corpus_followup_r8.py
?? docs/reviews/build-local-final-polish.md
?? docs/reviews/final-polish-shots/
?? tests/test_cache_safety.py
?? tests/test_final_polish.py
```

`git status --short -- work data/corpus tests/golden src/id_detector/fuse scripts/score_corpus.py
scripts/make_run_list.py src/id_detector/recipes.py` is empty.  No live provider was called;
`IDEA_TEST_MODE=1` and an empty `AUDD_API_TOKEN` were set for every pytest run.
