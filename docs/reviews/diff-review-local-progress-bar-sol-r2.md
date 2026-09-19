## A — behaviour

- **DONE — bounded recent rate.** `_with_sample()` interpolates a horizon sample, keeping each tick’s measurement at no more than 45 seconds (`src/id_detector/webapp/jobs.py:655-681`). After the simulated two-minute stall, the rate was 25.6/min after 22.5 seconds and 45/min after 48 seconds; the bar then caught up and moved normally. The regression test directly checks this recovery (`tests/test_progress_wallclock.py:234-270`).
- **DONE — durable retry.** Earlier work is reduced to `carried_seconds`, while per-attempt phases and rate samples are reset (`src/idea_web/progress.py:62-88,109-124,172-202`). Recognition initially estimates only unfinished windows (`src/id_detector/webapp/jobs.py:350-360`), and smoothing starts when the retry is constructed. The 30%-complete retry remained monotonic, moved no more than two points per poll, gained over 20 points during resumed work, and reached 100 on success (`tests/idea_web/test_ops.py:630-685`).
- **DONE — short-run limiter.** The 120-second smoothing floor bounds running updates to about 6.25 points per 2.5-second poll, including the first poll (`src/id_detector/webapp/jobs.py:57-65,407-417`). All tested short cases stayed within the seven-point integer bound.
- Normal 10- and 20-minute simulations ended their running state at 99%, with maximum truth errors of 1.25 and 1.14 points respectively. The limiter therefore does not leave ordinary runs behind.
- Fast runs can intentionally finish with the running bar behind—for example 18% after an 8.8-second fully cached run—but success immediately reports 100 (`src/id_detector/webapp/jobs.py:393-395`) while the page synchronously hides the scan panel (`src/id_detector/present/server.py:888`). This matches the accepted hand-off design and is not a blocker.

## B — correctness bugs

- No remaining Round 1 P1 and no realistic new bar defect found.
- No protected file was touched. `git diff --check 3ff64d2` also passed.

## C — readers of the changed fields

- **DONE.** This pass introduced retry state rather than another status-JSON semantic change. Both durable and local snapshots derive their field lists from `dataclasses.fields(Job)` (`src/idea_web/progress.py:55-61`; `src/idea_web/jobs/local.py:107-109`), so `carried_seconds` remains aligned.
- Round 1’s conclusions about `eta_seconds`, `eta_estimating`, CSP/static assets, framing, and `PAGE_VERSION` remain unchanged; this pass did not alter those surfaces.

## D — tests

- **DONE.** Every wall-clock simulation now enters success and asserts terminal 100 (`tests/test_progress_wallclock.py:135-150`); the short-run matrix also checks monotonicity, maximum poll movement, and first-poll limiting (`tests/test_progress_wallclock.py:323-379`).
- Retry tests use an injected clock and the real `PageProgress.start()`/`tick()` path (`tests/idea_web/test_ops.py:595-624`); the central retry regression settles both failure and success (`tests/idea_web/test_ops.py:648,684-685`).
- I directly executed the seven focused wall-clock tests and four durable-document/retry tests against the shipped classes; all 11 passed.
- Normal `uv`/pytest was not run because this managed read-only review cannot permit its temporary/cache writes. The direct executions disabled bytecode writes and created no repository changes.

## Follow-ups, not blockers

- A very short job may show a deliberately lagging percentage immediately before the result replaces it. A future animation could soften that transition, but changing it is outside the accepted Round 2 design.

## Required fixes

None.

VERDICT: OK_TO_COMMIT