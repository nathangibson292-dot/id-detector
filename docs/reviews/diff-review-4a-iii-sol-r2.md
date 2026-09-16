## A. Contract violations

- **U-F18 remains incomplete for live state transitions.** Dismiss controls exist only when a terminal card is initially rendered (`src/idea_web/pages.py:55-63`). Card polling updates status and then simply stops for failed/cancelled/waiting jobs (`src/idea_web/pages.py:103-120`); it neither adds Dismiss nor reorders cards.
- The round-1 P0 is fixed: cached HTML responses compute and retain their document CSP (`src/idea_web/application.py:380-384`), with meaningful canonical/bundle GET and HEAD coverage (`tests/idea_web/test_headers_forms.py:620-652`).
- The uncaught-500 fix is correctly placed: the exception response explicitly receives the defaults outside user middleware (`src/idea_web/http.py:195-214`, `src/idea_web/http.py:392-410`) and exposes only a constant body.
- The orchestrator decisions are honored. `git diff -- src/id_detector` is empty, result bytes are unchanged, and no `PAGE_VERSION` bump is needed. Profiles, golden files, and every protected second-session path are untouched.

## B. Correctness bugs

- **Concrete U-F18 failure:** start jobs A then B, with A running and B queued. If A fails, polling marks A failed and stops without adding Dismiss; when B starts, A also remains above B, so the failed card precedes the running card. This contradicts both “running first” and “terminal card can be dismissed” until a manual reload (`src/idea_web/pages.py:106-120`).
- No money, reservation, cap, attempt-journal, provider-call, cache-publication, or cancellation backend logic changed in this diff.
- Non-HTML `304`, audio range, CSRF, bounded-body, and no-store behavior showed no new defect by static review.

## C. Scope

- Static assets, compression, ETags, headers, forms, U-F1/F15/F18/F29/F30/F34, and truth-review CSP compatibility are within 4a-iii.
- No U-F9 weighting, accounts, sessions, or other later-cycle behavior was pulled forward.
- All new behavior is under `src/idea_web/`; there are no remaining edits to `src/id_detector/present/server.py` or `src/id_detector/truth_review.py`.

## D. Tests

- `tests/idea_web/test_headers_forms.py` contains 15 offline tests and covers the requested routes without external network access.
- **The terminal-card test misses the live defect.** It cancels a job and then fetches a fresh home document before asserting Dismiss (`tests/idea_web/test_headers_forms.py:565-572`); it never applies a terminal polling response to the already-rendered card.
- **The claimed three-path label equality test exercises only two paths.** It checks Python card rendering and executes `HOME_JS.phaseLabel` under Node (`tests/idea_web/test_headers_forms.py:719-754`), but never executes the progress tracker’s active-label logic at `src/id_detector/present/server.py:913-926`. Hard-coding a wrong tracker label there would leave this test green.
- No existing test was deleted or evidently weakened.
- Every exact `uv run …` command was attempted, but the WinGet `uv.exe` symlink could not launch: “No application is associated with the specified file.” Direct fallbacks gave: Ruff passed; fixture audit passed over 485 files; the two no-temp phase tests passed; and all 54 rendered/static JavaScript payloads passed Node syntax checking. Full pytest, the phase gate, and the web suite could not run because this read-only sandbox has no usable temporary directory.

## E. Local mode

- The exact `uv run idea serve --no-open --port 8791` could not launch because of the broken `uv.exe`. Direct `idea.exe serve` reached local database initialization but the sandbox refused creation of `work/.idea`.
- A no-write real-Uvicorn smoke returned `200` for `/`; an active-app smoke returned `200` for `/` and its versioned JavaScript, with immutable caching and no `Server` header.
- The served CSP permits the same-origin static script and local media; audio range routing, truth review, playlists, and `idea.cmd` are unchanged or covered by the shared middleware. No local-mode P0 was found.

## F. Quality

- `git diff --check` passed, as did direct Ruff.
- The new shared phase-label implementation is structurally sound, but its regression claim is stronger than its test.
- The terminal polling behavior needs a regression test covering DOM/state transition rather than only fresh server rendering.

## Required fixes

### Realistic

1. **P1** On every terminal polling transition, reload/re-render or add the Dismiss control and reorder cards so a newly running job remains first; add a transition regression test.
2. **P1** Execute the real progress tracker label-rendering path in the equality test and compare it with both initial card rendering and card polling.
3. **P2** Re-run the exact full, phase, web, JavaScript, Ruff, fixture-audit, and port-8791 commands in a writable environment with a working `uv` launcher.

### Adversarial

None.

VERDICT: FIX_FIRST