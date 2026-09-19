# Build report — local progress bar, proportional to wall-clock time

Base: `main` at `3ff64d2` (worktree fast-forwarded to it first). Nothing committed, no branch made.

## The owner's request

> "if it'll take 10 min, every 10% ≈ 1 min; if 20 min, every 10% ≈ 2 min"

The analysing page's big percentage leapt and then crawled: the short steps finished in seconds, a
cache resume completed many listening windows in one instant, and the rest arrived at the live
rate. The number was defensible and still felt broken.

## What I found

The bar's arithmetic was already server-side and already `elapsed / (elapsed + remaining)`
(`Job.progress_percent`, U-F9, cycle 3a-ii) — the browser only holds a monotonic floor. What made
it leap and crawl was the **rate** behind `remaining`: `windows_done / seconds since recognise
began`, a run average. That is wrong in exactly the two ways the brief names:

- a cache resume puts e.g. 270 windows into `windows_done` in the first instant, so the "average"
  is enormous, the remaining time looks tiny, and the bar jumps high — then, being monotonic,
  sits there while the real waiting happens;
- the limiter is adaptive (≈45/min falling to ≈20/min when Shazam throttles), and an average takes
  the rest of the run to admit a drop.

It also trusted a rate after 3 windows and 1 second, so two or three quick ticks could set the bar
racing. So the design in the brief was implementable as written, in Python, with no page-side
second model.

## The design I ended with

`percent = elapsed / (elapsed + remaining)`, where:

- **`elapsed` is real time only** — measured phase seconds plus time in the current phase. Windows
  a cache resume finished in an instant add nothing to it, so a run that resumes 60 % done starts
  near 0 %, because all of the waiting is still ahead.
- **`remaining` = windows left ÷ a recent rate, plus the short closing steps.** The rate is measured
  over a **45-second sliding window** of `[time, windows_done]` samples (`RECENT_RATE_SECONDS`),
  taken at each recognise tick, folded to about one per second and pruned to the window plus one
  older anchor. The first sample of a pass already contains the cache burst, so the burst is the
  baseline and is never measured as speed. A new pass (phase re-entered, total changed, count went
  back — e.g. the paid-engine restart that reports `0 of 1`) starts its samples afresh.
- **Stalls decay the rate.** The span runs from the oldest sample towards *now* (less one average
  gap, so ordinary silence between two ticks is not read as slowing). While nothing completes the
  rate falls and the time left honestly grows.
- **Never backwards.** When the rate drops and the estimate lengthens, the displayed value holds and
  time catches up. (`progress_max`, plus an unrounded twin `progress_value`.)
- **Never leaps.** The displayed value may climb at most 3× its natural slope (`_MAX_CATCH_UP`), so
  when a better estimate arrives — the prior gives way to a measured rate, or throttling ends — the
  bar walks to it over a few seconds. This needs the time of the last evaluation (`progress_at`)
  and the unrounded value, because one 2.5 s poll of a 20-minute run moves the bar a fraction of a
  point.
- **Honest early state.** A recent rate is trusted once it spans ≥ 15 s with ≥ 3 windows (or ≥ 30
  windows however short the span — a warm run that finishes listening in seconds). Until then the
  estimate rests on the cautious 18/min prior — the *slow* end of what the limiter does, so the
  early number under-promises — and the page's time-left tile says **"Estimating…"** instead of a
  number.
- **Short stages** keep their existing small share (`PHASE_EXPECTED_SECONDS`, scaled by how fast
  this run has really gone; measured once finished). Recognise dominates. Unchanged.
- **Time left in plain words.** The tile was `~12m 05s`, and only covered the listening windows.
  It is now the whole job's remaining time — the bar's own denominator — worded "about 12 min",
  "about 1 hr 15 min", "under a minute", or "Estimating…". Rounded to the minute so it does not
  flicker on every poll.
- **Step tracker untouched.** No markup changed at all — the same `t-eta` tile is reused — so
  `job.html`, the legacy renderer and the step list are byte-identical.

Why server-side rather than in `_JOB_JS`: the local worker is a separate process that publishes a
snapshot, and the web process rebuilds a `Job` from it and evaluates the bar at request time. Rate
samples therefore have to live in the snapshot (they do — JSON-safe lists), and doing the sum in
one place keeps the progress page, the home cards and the hosted queue document on one bar. A
reloaded tab also gets the right number immediately, which a browser-side sliding window would not.

## Files

- `src/id_detector/webapp/jobs.py` — the change. New constants (`RECENT_RATE_SECONDS`,
  `_RATE_MIN_SECONDS` 1 → 15, `_RATE_SURE_WINDOWS`, `_SAMPLE_SPACING_SECONDS`, `_MAX_CATCH_UP`);
  new `Job` fields `rate_samples`, `progress_value`, `progress_at`; new
  `recent_rate_per_minute()`, `estimating()`, `_recognise_rest_seconds()`; `rate_per_minute()` is
  now recent-or-prior (never the run average); `eta_seconds()` is now the whole job's remaining
  time; `progress_percent()` gained the catch-up cap and the unrounded floor; `status_dict()`
  gained `eta_estimating`; `JobContext` takes an injectable `clock` and records samples through
  `_with_sample()`. Also fixed a latent bug: `phase_started_at == 0.0` was treated as "not started"
  (a truthiness test), which had let the old never-backwards test pass as `0 >= 0`.
- `src/id_detector/present/server.py` — `_PROGRESS_JS` gains `etaWords()`; `render()` in `_JOB_JS`
  calls it for the `t-eta` tile. Comment updated. No markup or CSS change.
- `src/idea_web/progress.py` — passes its injected clock to `JobContext` (so samples and phase
  times share one timeline) and to `progress_percent`; a retry does **not** inherit the previous
  attempt's `rate_samples` / `progress_at` (it still inherits measured time and the high-water
  mark); the stale "re-weighting is parked" docstring paragraph is replaced.
- `src/idea_web/pages.py`, `src/idea_web/jobs/local.py`, `src/idea_web/http.py` — **not edited**.
  `ASSET_VERSION` is a content hash of the assembled script, so the static asset URL changed by
  itself; the page policy hashes each inline script from the response body, and the inline config
  script did not change. Framing directives untouched (`frame-ancestors 'self'`, `SAMEORIGIN`,
  `frame-src 'self'`). `_SNAPSHOT_FIELDS` in both modules derive from `dataclasses.fields(Job)`, so
  the new fields ride in the published document without an edit, and the test that pins the two
  lists to each other still holds.
- `PAGE_VERSION` / `theme.py` — not touched and not needed: `_PROGRESS_JS` is only in the live
  pages, never in a saved result page.
- Tests: new `tests/test_progress_wallclock.py` (7 tests); one new test in
  `tests/idea_web/test_ops.py`; `tests/test_stage10_webapp.py` and `tests/test_phase3a_honesty.py`
  updated where they pinned the run-average rate and the windows-only ETA.

## Tests

All on an injected clock, driving the real `Job` through the real `JobContext.progress`, polled at
the page's 2.5 s cadence. No sleeps, no real clock.

- steady run, ~10 min and ~20 min: once measured, displayed % is within 3 points of
  elapsed/total at every poll; each 10 % mark arrives within 6 % of a tenth of the total; no poll
  moves the bar more than 2 points; time left within 30 s of the truth.
- cache-resume burst (270 of 450 instantly): first reading ≤ 5 %, not 60; rate stays ≈45/min;
  then tracks elapsed/total.
- mid-run drop 45 → 20/min: never backwards; rate follows within one window; time left jumps by
  more than 10 min; the bar holds (±1) while the truth is below it, then tracks the new total
  within 3 points.
- two-minute stall: never backwards, time left grows monotonically, bar holds.
- early state: "estimating" until measured; early number never ahead of the truth; three ticks in
  2 s are not a rate (prior 18/min, not 250/min).
- sample list bounded (~one per second of the window), JSON-safe, restarts with each pass.
- `etaWords()` under Node: "Estimating…", "about 10 min", "about 22 min", "about 1 hr 15 min",
  "under a minute", "…", "—".
- `test_ops`: a job rebuilt from the JSON-round-tripped published document gives the same
  percent, rate and time left as the worker's own; a retry carries the mark but not the samples.

Reverting behaviour fails them (checked by temporarily mutating `jobs.py`, then restoring it):

```
== run-average rate: 8 failed
== no catch-up cap: 3 failed
== no never-backwards floor: 4 failed
== trusts two or three ticks: 1 failed
== samples never restart: 1 failed
== stall does not decay: 1 failed
```

## Gate output

Collected total:

```
$ uv run pytest --collect-only -q
1882/1979 tests collected (97 deselected) in 19.83s
```

Suite, in foreground shards covering every collected file (explicit file lists; each waited for):

```
idea_web (all 19 files, one run)      1 failed, 373 passed, 1 warning in 572.31s (0:09:32)
  FAILED tests/idea_web/test_accounts.py::test_two_simultaneous_uses_of_one_reset_link_set_exactly_one_password
A  acrcloud … local_index (13 files)  147 passed, 1 warning in 94.69s
B  paid_clip, phase0a_*, phase0b_*    174 passed, 1 warning in 203.59s
C  phase1a … projection (11 files,
   incl. test_progress_wallclock)     288 passed, 1 warning in 485.94s
D  scan … stage1_* (12 files)         168 passed, 1 deselected, 1 warning in 162.94s
E  stage10, stage2*, stage3*          148 passed, 24 deselected, 1 warning in 47.67s
F  stage4* … stage9*                  278 passed, 1 skipped, 68 deselected, 1 warning in 22.95s
G  truth_* (12 files)                 303 passed, 1 skipped, 4 deselected, 1 warning in 114.25s
```

374 + 147 + 174 + 288 + 168 + 148 + 279 + 304 = **1882**, and 1 + 24 + 68 + 4 = 97 deselected —
both match the collected total.

The one failure is a two-simultaneous-requests race test in the hosted accounts code, which this
change does not touch; it is not one of the two flakes named in the brief, so it is reported rather
than waved through. Re-runs:

```
tests/idea_web/test_accounts.py alone              11 passed in 23.14s
that one test alone, three times                   1 passed / 1 passed / 1 passed
idea_web re-run as two halves (all 19 files):
  accounts … followup_round5 (9 files)             171 passed, 1 warning in 205.22s
  followup_round6 … ops (10 files)                 203 passed, 1 warning in 95.38s     (171 + 203 = 374)
```

So every one of the 1882 collected tests has a passing foreground run; the single failure appeared
only in the 9½-minute all-in-one idea_web run and did not reproduce in five further runs.

```
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
391 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 522 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

```
$ git diff --stat
 src/id_detector/present/server.py |  25 +++--
 src/id_detector/webapp/jobs.py    | 189 +++++++++++++++++++++++++++++++-------
 src/idea_web/progress.py          |  28 ++++--
 tests/idea_web/test_ops.py        |  40 ++++++++
 tests/test_phase3a_honesty.py     |   9 +-
 tests/test_stage10_webapp.py      |  24 +++--
 6 files changed, 255 insertions(+), 60 deletions(-)
$ git status --short
 M src/id_detector/present/server.py
 M src/id_detector/webapp/jobs.py
 M src/idea_web/progress.py
 M tests/idea_web/test_ops.py
 M tests/test_phase3a_honesty.py
 M tests/test_stage10_webapp.py
?? tests/test_progress_wallclock.py
?? docs/reviews/build-local-progress-bar.md
```

## What I did not do, and what to know

- **No live look at the page.** I did not start a server: with fake providers a run finishes in
  under a second, so there is no mid-run page to look at, and live providers are off limits. The
  script is covered by `check_page_js.py`, the Node test of `etaWords()`, and the served-page tests
  in `tests/idea_web/test_headers_forms.py`. A real mix is the remaining check — worth watching
  one cold run and one resumed run.
- **The PowerShell gates were not run**, as instructed.
- **The first seconds of listening are still a guess.** The worker pool's first few requests go
  out together, so the first measured rate can read a little fast; the 15-second minimum and the
  catch-up cap keep that to a point or two early in a run, and it washes out within one window.
- **The "time left" can go up.** That is deliberate: when Shazam throttles, the bar holds and the
  tile tells the truth about the longer wait.
- **`eta_seconds` in the status JSON changed meaning** (whole job, not just listening windows), and
  `eta_estimating` is new. Only the job page reads them.
- Earlier build reports (e.g. `docs/reviews/build-3a-ii.md`) describe the bar as it was then; I
  left historical reports alone.

---

# Fix pass — after the FIX_FIRST review (three P1s, no P0)

Review: `diff-review-local-progress-sol.out.md`. Everything it confirmed sound was left alone (the
steady run, the cached-resume baseline, never 100 % early, the step tracker, the status-field
readers, the page policy, the `0.0` side fix). Where this section disagrees with the text above —
the "one older anchor", "the first evaluation takes the computed value", "a retry inherits measured
time" — this section is the current design.

## P1-1 — the "recent" rate went stale after a stall

**Cause.** `_with_sample()` kept the newest sample older than the 45 s horizon *however old it
was*. After a two-minute stall that sample was two minutes old, so work resuming at 45/min was
averaged with the stall for another whole window (the reviewer read 1.9, 4.5, 6.4, 11.25/min).

**Fix** (`src/id_detector/webapp/jobs.py`, `_with_sample`). The far edge of the window is now a
sample **at the horizon**, linearly interpolated between the last sample before it and the first
one after. At every tick the window is therefore exactly `RECENT_RATE_SECONDS` wide — never wider
— so after a stall the rate climbs back linearly and is the live rate exactly one window after the
work resumes. Between ticks the span still stretches towards *now*, so a stall still decays the
rate while it lasts (unchanged, and still tested).

**Test.** `test_a_stall_never_moves_the_bar_backwards_and_lengthens_the_time_left` no longer ends
during the stall: two minutes of silence, then 45/min again. Half a window after recovery the rate
is already above 18/min; one window (+3 s) after, it is 45/min ± 8 % and stays there; the bar walks
back to the truth at the catch-up pace and is within 3 points of elapsed/total from a minute later;
100 s on it has moved at least 10 points.

## P1-2 — a durable retry could leap to 97 % and stick

**Cause.** A retry carried `phase_seconds`, and that mapping does two jobs: it is the time worked
*and* it tells the estimate which phases are finished. An attempt that died mid-listening left
`phase_seconds["recognise"]`, so during the retry's `intake` the estimate placed the job *after*
recognise — only the closing steps left — and, with `progress_at` discarded, the first evaluation
was exempt from the climb limit. 30 % → 97 %, then the time left went 9 s → 759 s under a stuck bar.

**Fix.**
- New `Job.carried_seconds`: the working time of earlier attempts, as ONE number. `elapsed` is
  `carried_seconds + this attempt's phases + the current phase`. `phase_seconds` is per-attempt
  again, so "which phases are finished" is only ever about the attempt that is running.
- `src/idea_web/progress.py`: a retry no longer inherits `phase`, `phase_done`, `phase_total`,
  `phase_seconds` (or `rate_samples`, `progress_at`). `_worked_seconds()` computes what carries:
  what the dead attempt itself inherited, plus every phase it measured, plus — if it died without
  settling — the open phase up to its last evaluation. Queue backoff between attempts is waiting,
  not working, and is not counted.
- The retry's smoothing starts when the retry is built (`progress_at = clock()`), so its first
  poll is rate-limited like any other.
- `_expected_seconds("recognise")`, before listening is entered, now counts the windows **left**
  (`windows_total − windows_done`), not all of them: a retry's finished windows come back from
  cache at once. For a fresh job the two are the same number. This keeps the estimate continuous
  across the retry — about 30 % when it died, about 30 % during intake — instead of dipping or
  leaping.

**Tests** (`tests/idea_web/test_ops.py`, all through the real `PageProgress` with `start()`,
`tick()` and `settle()`, polled as the web process polls: a job rebuilt from the JSON document
every 2.5 s).
- `test_a_retry_from_the_middle_of_listening_neither_leaps_nor_sticks` — 135 of 450 windows and
  ~209 s of real time, failure, 15 minutes of backoff, retry through intake, cached early steps and
  re-entry into listening at 135, three more minutes at 45/min, then success. Never backwards; on
  intake at most +1 point; no poll moves more than 2 points; it ends within 3 points of the true
  elapsed/total and at least 20 points above where it died; the time left starts at the truth
  (315 windows at the prior + the short steps) and ends at the truth; `complete` → 100.
- `test_a_retry_whose_bar_was_behind_is_rate_limited_from_its_first_poll` — a fully cached attempt
  that died in the closing steps with the bar far behind; also pins the died-without-settling
  carry.
- `test_a_retry_of_a_job_published_by_the_previous_build_does_not_leap` — a document with none of
  the new fields (a job in flight across the upgrade): computed alone the retry would show ~60 %
  against a mark of 12; it starts at 12 and walks.
- `test_a_retry_continues_the_bar_instead_of_resetting_it` now pins `carried_seconds` instead of
  comparing two independent microsecond phase timings (which only ever passed by luck).

## P1-3 — very short and fully cached runs could leap

**Cause.** The climb limit was "3× the natural slope", and the natural slope was `100 / total`.
When listening finishes instantly `total` collapses to a few seconds and 3× that slope is any jump
at all (0 → 66 % at the first poll with 450 cached windows). Separately, the very first evaluation
of a job was exempt from the limit.

**Fix** (`Job.progress_percent`).
- `_SMOOTHING_FLOOR_SECONDS = 120`: the slope is never taken to be steeper than a two-minute job's.
  No poll of a running job moves the bar more than 2.5 points a second — about 6 points per 2.5 s
  poll — however short the job turns out to be. Runs of two minutes or more are unaffected.
- The first evaluation is limited from `started_at`. Only a job with neither `progress_at` nor
  `started_at` takes the computed value as it stands (hand-built jobs in unit tests).
- **The terminal hand-off is deliberately not smoothed.** A job that succeeds reports 100 at once.
  Holding a finished job at 40 % and animating it up would be the dishonest option and would delay
  the result; and the number is never seen jumping, because on success the page replaces the whole
  scan panel — which contains the percentage — with the result (pinned by a test on `_JOB_JS` and
  the rendered markup). A job that fits between two polls shows 0 %, then its result.

**Test.** `test_a_collapsed_estimate_cannot_make_the_bar_leap`: fully cached; fully cached and over
before the next poll; very short (20 windows); below the rate threshold (8 windows — estimating
throughout, so listening ends on the prior and the remainder collapses); a resume with six windows
left; and a fully cached run first polled 2.5 s in. Each runs to success: never backwards, no
running poll moves more than 7 points, the first poll is not exempt, and the hand-off is 100.
`_simulate()` now always marks the job succeeded and records the first terminal reading, and
`_never_backwards()` asserts it, so every simulated run in the file goes through the hand-off.
`tests/test_phase3a_honesty.py`'s warm-cache assertion changed from "≥ 50 % four seconds in" to
"the time left collapses to ≤ 5 s and the bar has moved exactly the 10 points it is allowed".

## Each new test fails when its fix is reverted

Checked by temporarily mutating the source and restoring it (scratch script, not committed):

```
== P1-1 stale anchor kept: 1 failed                 (the stall-and-recovery test)
== P1-2 phases carried as finished: 1 failed        (the mid-listening retry test)
== P1-2 worked time not carried: 4 failed
== P1-2 retry smoothing not initialised: 1 failed   (the previous-build retry test)
== P1-2 retry expects every window again: 1 failed
== P1-3 no smoothing floor: 3 failed
== P1-3 first poll exempt: 1 failed
```

and the first pass's six mutations still fail (10 / 8 / 5 / 2 / 1 / 1 tests).

## Gate output (fix pass)

```
$ uv run pytest --collect-only -q
1886/1983 tests collected (97 deselected)
```

Foreground shards, explicit file lists, each waited for:

```
W1 idea_web: accounts … followup_round5 (9 files)   171 passed, 1 warning in 183.77s
W2 idea_web: followup_round6 … ops (10 files)       206 passed, 1 warning in 96.45s
A  acrcloud … local_index (13 files)                147 passed, 1 warning in 75.49s
B  paid_clip, phase0a_*, phase0b_*                  174 passed, 1 warning in 124.35s
C  phase1a … projection (11 files)                  289 passed, 1 warning in 197.76s
D  scan … stage1_* (12 files)                       168 passed, 1 deselected, 1 warning in 96.71s
E  stage10, stage2*, stage3*                        148 passed, 24 deselected, 1 warning in 23.02s
F  stage4* … stage9*                                278 passed, 1 skipped, 68 deselected in 15.70s
G  truth_* (12 files)                               303 passed, 1 skipped, 4 deselected in 104.70s
```

171 + 206 + 147 + 174 + 289 + 168 + 148 + 279 + 304 = **1886**; 1 + 24 + 68 + 4 = 97 deselected.
Both match. No failures and no flakes this pass (the first pass's one-off `test_accounts` race did
not recur).

```
$ uv run ruff check .                       All checks passed!
$ uv run ruff format --check .              392 files already formatted
$ uv run python scripts/audit_fixtures.py   audited 523 files / fixture audit passed
$ uv run python scripts/check_page_js.py    page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

```
$ git diff --stat
 src/id_detector/present/server.py |  25 +++--
 src/id_detector/webapp/jobs.py    | 223 ++++++++++++++++++++++++++++++++------
 src/idea_web/progress.py          |  70 +++++++++---
 tests/idea_web/test_ops.py        | 201 +++++++++++++++++++++++++++++++++-
 tests/test_phase3a_honesty.py     |  17 ++-
 tests/test_stage10_webapp.py      |  24 ++--
 6 files changed, 489 insertions(+), 71 deletions(-)
$ git status --short
 M src/id_detector/present/server.py
 M src/id_detector/webapp/jobs.py
 M src/idea_web/progress.py
 M tests/idea_web/test_ops.py
 M tests/test_phase3a_honesty.py
 M tests/test_stage10_webapp.py
?? docs/reviews/build-local-progress-bar.md
?? tests/test_progress_wallclock.py
```

Nothing committed. No protected file touched; `present/server.py`, the page policy and
`PAGE_VERSION` were not changed in this pass.

## Still true, and worth knowing

- **Local mode restarts from zero.** When the *local* worker dies and the job is re-run,
  `LocalWorker._restarted()` builds a brand-new job, so its bar starts again at 0 (an open tab
  holds its own floor; a reloaded one does not). That predates this work and the review did not
  raise it; `carried_seconds` would make it a small follow-up if the owner wants the local retry to
  continue the bar the way the hosted one now does.
- A very short job's bar lags its estimate by design (the 2.5 points/second bound) and then hands
  over to the result. That is the trade the P1-3 fix makes: a lagging bar on a job that is over in
  seconds, instead of a leap.
- Still no live look at the page, for the reason given above.
