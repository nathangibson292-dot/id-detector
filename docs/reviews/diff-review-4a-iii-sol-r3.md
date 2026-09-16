## A. Contract violations

- No protected-file or frozen-artifact violation found. `git diff -- src/id_detector` is empty; playlists, `README.md`, `idea.cmd`, `theme.py`, profiles, and golden fixtures are unchanged.
- `/activity` is read-only, limited to 25 recent jobs, uses the same renderer/order as `/`, exposes titles rather than targets or paths, and receives `no-store`, CSP, and defensive headers (`src/idea_web/application.py:153-165`, `src/idea_web/application.py:370-373`).

## B. Correctness bugs

- **Realistic regression:** `refresh()` accepts every HTTP response as success. A `/activity` 500 resolves normally, its `internal server error` body replaces `ul.acts`, and polling is not re-armed (`src/idea_web/pages.py:121-127`). Executing the real script reproduced `swapped:["internal server error"]`. This violates the explicit requirement that a failed refresh preserve the usable list.

## C. Scope

- The new `/activity` route is in-scope for closing U-F18 terminal transitions.
- No later-cycle progress weighting, accounts, sessions, money, journal, provider, or queue semantics were introduced.
- No second-pass regression found in audio, truth-review, playlists, forms/CSRF, static serving, gzip, ETags, CSP-on-304, or 500-header handling.

## D. Tests

- The A-fails/B-starts test genuinely drives already-rendered cards through `HOME_JS`, compares `/activity` with a fresh server render, and would fail if list refresh were reverted (`tests/idea_web/test_headers_forms.py:800-881`).
- Failed, cancelled, waiting, and succeeded-without-result share the generic transition predicate; succeeded jobs with a result reload the home/library (`src/idea_web/pages.py:108-119`, `src/idea_web/pages.py:135-136`).
- **Round-2 label P1 is not fully closed.** The tracker has `decode`, `windows`, `hints`, and `fuse` steps (`src/id_detector/present/server.py:1327-1343`), but the three-path test omits all four (`tests/idea_web/test_headers_forms.py:706-717`). A wrong phase-specific label hard-coded in the real tracker for any omitted step would remain green.
- The `/activity` harness always returns successful text and has no non-2xx or stalled-response case (`tests/idea_web/test_headers_forms.py:910-915`).

## E. Local mode

- Every exact `uv run …` command failed before execution because the WinGet `uv.exe` link is broken.
- Direct `idea.exe serve --no-open --port 8791` reached database initialization but the read-only sandbox refused creation of `work/.idea`.
- A no-write real-Uvicorn smoke on port 8791 returned `200` for `/` with the expected CSP. No local-startup code regression was found.

## F. Quality

- Direct Ruff passed; fixture audit passed over 485 files; `git diff --check` passed.
- Direct execution of the current label regression and CSP hashing unit passed.
- Full pytest, web pytest, phase gate, and `check_page_js.py` could not run because this sandbox has no writable temporary directory. The builder’s pasted outputs were not relied upon.

## Required fixes

### Realistic

1. **P1** Reject non-2xx or malformed `/activity` responses and retry while preserving the existing list; add a Node regression covering a 500 response.
2. **P1** Generate label-equality cases from every `_job_steps()` entry, including `decode`, `windows`, `hints`, and `fuse`, and execute all three rendering paths.
3. **P2** Re-run the exact full, web, phase, JavaScript, Ruff, fixture-audit, and port-8791 commands in a writable environment with a functional `uv`.

### Adversarial

None.

## Pre-existing, for a later cycle

None reopened under the Round-3 review cap.

VERDICT: FIX_FIRST