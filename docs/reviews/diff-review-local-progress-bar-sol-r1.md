## A — behaviour

- Steady 10/20-minute runs use `elapsed / (elapsed + remaining)` as requested (`src/id_detector/webapp/jobs.py:362-387`). The 45→20/min transition uses recent samples and cannot move backwards (`src/id_detector/webapp/jobs.py:229-260,388-396`).
- A 60%-cached resume is correctly baselined: entering recognition resets samples and records the already-completed count as the first sample (`src/id_detector/webapp/jobs.py:560-569`).
- Stalls lengthen ETA because the sample span advances toward `now` (`src/id_detector/webapp/jobs.py:241-254`), while the monotonic floor holds the percentage.
- Progress cannot report 100 before success and always reports 100 after success (`src/id_detector/webapp/jobs.py:375-397`).
- The named step tracker is unchanged (`src/id_detector/present/server.py:843-853`).
- No protected file was touched; therefore no P0.

## B — correctness bugs

1. **P1 — the “recent” rate remains stale after a stall.** `_with_sample()` retains the newest sample older than the 45-second horizon regardless of its age (`src/id_detector/webapp/jobs.py:647-649`). After a two-minute stall, resumed work is therefore measured against a sample more than two minutes old for approximately another 45 seconds. In a source-level simulation, resumed 45/min work was reported as 1.9, 4.5, 6.4, then 11.25/min before finally returning to 45/min. The percentage consequently stays pinned while dozens of windows complete. That is not a rate over “about the last 45 s.”

2. **P1 — durable retries can leap to 97% and then stick.** Retry resumes `phase_seconds` and the high-water mark while discarding `progress_at` (`src/idea_web/progress.py:58-78,140-158`). A failed recognition phase is frozen into `phase_seconds` (`src/idea_web/progress.py:203-215`); during the retry’s auxiliary `intake` phase, `_remaining_seconds()` treats the latest measured phase as already passed (`src/id_detector/webapp/jobs.py:356-360`). Because `progress_at` is `None`, the first evaluation bypasses the climb limiter (`src/id_detector/webapp/jobs.py:389-393`). A representative 30%-complete retry produced 30→97% on intake, followed by ETA rising from 9s to 759s while the bar remained at 97%.

3. **P1 — very short/cache-complete runs can leap.** The limiter divides by the newly collapsed total (`src/id_detector/webapp/jobs.py:386-394`), so when recognition finishes instantly, “3× natural pace” can permit a huge single-poll increase. Using the added test’s short-phase timings with all 450 windows cached produced 0→66% at the first 2.5s poll. Runs completing before another poll may instead remain far behind and then bypass the limiter through the unconditional success path (`src/id_detector/webapp/jobs.py:375-377`).

- The `phase_started_at == 0.0` side fix is correct: both ETA and progress now test `is not None` (`src/id_detector/webapp/jobs.py:293-295,381-383`). The other occurrences in that file also use explicit `None` checks (`src/id_detector/webapp/jobs.py:544,570`).

## C — readers of the changed fields

- `status_dict()` now emits whole-job `eta_seconds` plus `eta_estimating` (`src/id_detector/webapp/jobs.py:414-418`).
- The only production reader found is `etaWords()` and its job-page call (`src/id_detector/present/server.py:729-737,946`). Activity cards read `progress_pct`, not ETA. No reader still formats ETA as recognition-only.
- `src/idea_web/progress.py` carries the estimator’s underlying `Job` fields rather than interpreting either JSON field; its snapshot schema remains aligned with local mode through `dataclasses.fields(Job)` (`src/idea_web/progress.py:51-57`).
- Static assets remain content-versioned from their actual bytes (`src/idea_web/pages.py:256-270`). CSP hashes are generated from each response body (`src/idea_web/http.py:101-133,178`); Node syntax checking of the changed shared scripts passed.
- Same-origin framing remains allowed both ways: `SAMEORIGIN`, `frame-ancestors 'self'`, and `frame-src 'self'` remain present (`src/idea_web/http.py:85,130-133`).
- `PAGE_VERSION` remains 24 and its file is unchanged (`src/id_detector/present/page.py:50`). No bump is needed because saved result-page code did not change.

## D — tests

- I could not run pytest: the system Python lacks pytest, and `uv.exe` could not be launched in this sandbox. This review is static plus read-only execution of the estimator with dependency stubs.
- The new steady-run test directly asserts the owner’s rule and uses an injected clock (`tests/test_progress_wallclock.py:29-35,147-167`). The updated existing wall-clock and ETA tests were strengthened rather than weakened.
- Coverage misses all three failures:
  - The simulation never marks its job succeeded (`tests/test_progress_wallclock.py:128-133`) and explicitly checks only pre-terminal values (`tests/test_progress_wallclock.py:136-140`).
  - The stall test ends during the stall and never tests recovery (`tests/test_progress_wallclock.py:221-236`).
  - The retry test uses a microsecond/0% attempt (`tests/idea_web/test_ops.py:521-557`), while the new retry assertion stops immediately after construction and never calls `start()` or `tick()` (`tests/idea_web/test_ops.py:593-597`).

## Required fixes

- **P1:** Keep the rate window genuinely bounded after inactivity—reset or synthesize a horizon baseline—and add a stall-then-45/min-recovery test that converges within the configured recent window.
- **P1:** Separate cumulative elapsed time from current-attempt phase completion on retry, initialize retry smoothing, and test a nonzero mid-recognition failure through retry intake and recognition re-entry.
- **P1:** Prevent a collapsed estimate from permitting a large nonterminal poll jump, and add very-short, fully cached, and sub-threshold-rate cases through the terminal handoff.

VERDICT: FIX_FIRST