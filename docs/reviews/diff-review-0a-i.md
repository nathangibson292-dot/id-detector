## A. Contract violations

- **A1 — P0: The implementation rewrites its own gate.** `docs/PLAN-v2.md:535-537` newly declares Windows PowerShell equivalent to `pwsh`, although 0a-i requires the exact command at `docs/PLAN-v2.md:554-556`. No phase scope or D1–D8 decision authorizes changing acceptance criteria. `docs/reviews/build-0a-i.md:188` confirms the exact gate is not green.

- **A2 — P0: Semantically malformed AudD results are cached as matches.** `src/id_detector/paid_clip.py:84-94` classifies every mapping result—including `{}`—as `match`; `src/id_detector/providers/audd.py:313-337` likewise turns `{}` into a match with no artist/title; `src/id_detector/paid_clip.py:275-279` then persists it. Input `{"status":"success","result":{}}` therefore poisons the positive cache and, because matches are not refreshed by default, can permanently suppress a paid re-query. This contradicts §2.3.3’s distinct `malformed` outcome and match/no-match-only cache.

- Frozen profiles are unchanged (`git diff -- profiles` is clean). The Local Free golden does not yet exist, as expected before 0b-ii.

## B. Correctness bugs

- **B1 — P0: Default Shazam no-match refresh can exhaust the old persistent budget without making a request.** `src/id_detector/recognise.py:408` only calls `ensure_budget`; the database uses `INSERT OR IGNORE` at `src/id_detector/jobs.py:325-339`. The new refresh path resets cached no-matches at `src/id_detector/recognise.py:438-445` without extending the ceiling. If the first run consumed `max_requests`, the next default refresh reaches `src/id_detector/jobs.py:575-579`, raises `BudgetExhausted`, and `src/id_detector/recognise.py:321-339` records a permanent failure. Thus `--refresh-states no_match` silently fails exactly when the previous run used its cap.

- **B2 — P0: Local web jobs report paid-provider failure as success and may return stale output.** `_analyse` now returns 3 at `src/id_detector/cli.py:520-532`, but `src/id_detector/webapp/runner.py:151-188` ignores that return value and still calls `_load_cached`. `src/id_detector/webapp/jobs.py:403-425` then marks the job succeeded whenever the runner did not raise. With a prior result present, an unavailable Deep request can be shown as successful with the old page; without one, it succeeds with no result URL.

- Reservation, USD caps, and full ambiguous/terminal outcome settlement are explicitly assigned to 0a-ii/0a-iv/0b and are not treated as omissions here.

## C. Scope

- `docs/PLAN-v2.md:535-537` is an unauthorized contract change and should not be committed with this implementation.

- **C1 — P1: PNG exclusion is unrelated and weakens fixture auditing.** `scripts/audit_fixtures.py:278-288` skips binary files before even checking their filename. A committed `1234567890.wav` or `.png` therefore bypasses the numeric-platform-ID rule. This phase needs WAV content handling, not a blanket future PNG exemption or bypass of path checks.

- The formatter-only changes in `docs/reviews/v2-review-hosting.md` and `tests/test_paid_clip.py` are mechanical; no substantive out-of-phase behavior was found there.

## D. Tests

- The nine new tests are offline and deterministic in their configured single-worker mode. They make substantive assertions for paid success, no-match caching, HTTP 401 status/cost/cache behavior, count accumulation, injection, `/healthz`, and journal assertions.

- Coverage misses all three P0 scenarios above. In particular, the malformed fake at `tests/fakes/providers.py:148-149` only models `result=[]`; no test covers an empty/identity-less mapping. The refresh test uses `max_requests=100` at `tests/test_phase0a_crash_cache.py:60`, masking exhausted-budget refresh. No test exercises the real web runner’s handling of return code 3.

- `tests/test_phase0a_crash_cache.py:92-95` merely runs the current audit; it does not prove that binary filenames remain audited.

- No existing test was deleted or weakened; `tests/test_paid_clip.py` changed formatting only.

- Independent exact runs could not execute: every `uv run …` command exited 1 because this sandbox cannot launch the WinGet `uv.exe` symlink. Direct fallbacks showed Ruff check, Ruff format check, and fixture audit green; pytest also could not collect because the read-only sandbox exposes no writable temporary directory. The exact `pwsh scripts/smoke_serve.ps1` command exited 1 because `pwsh` is absent.

## E. Local mode

- Exact `uv run idea serve --no-open --port 8791` was blocked by the same `uv.exe` launch failure.

- Using the repository executable directly, the server started successfully: `/healthz` returned `200 {"ok": true}`, and `/` returned 200 containing `Drop a mix`. The spawned process was stopped after verification.

## F. Quality

- **F1 — P1: Billing progress text is false for zero-cost outcomes.** `src/id_detector/paid_clip.py:286-288` labels `requests` as “billable.” For the all-401 gate it reports seven billable requests while `billable_units` is zero.

- `src/id_detector/paid_clip.py:11-12` still claims repeated analysis “never re-bills,” contradicting the newly intentional default re-query of cached no-matches.

## Required fixes

1. **P0:** Revert the unapproved `PLAN-v2.md` gate rewrite and obtain a green run of the exact original phase gate.
2. **P0:** Reject identity-less AudD result mappings as malformed before observation creation or cache write, with an empty-object regression test.
3. **P0:** Restore a fresh Shazam request allowance before resetting selected cached states, and test a no-match refresh after exactly exhausting `max_requests`.
4. **P0:** Propagate `_analyse` return code 3 through the web runner so the job fails and cannot attach a stale cached page; add a real-runner regression.
5. **P1:** Audit binary filenames before skipping binary contents and remove the unrelated `.png` exemption unless separately justified and tested.
6. **P1:** Rename the paid progress metric to “requests” or render `billable_units`, and assert the all-401 progress text is not misleading.
7. **P2:** Update the paid-cache module documentation to describe default no-match refresh accurately.
8. **P0:** Re-run `uv run pytest -q`, `uv run ruff check .`, `uv run python scripts/audit_fixtures.py`, and both exact phase-gate commands in a writable environment with working `uv` and `pwsh`.

VERDICT: FIX_FIRST