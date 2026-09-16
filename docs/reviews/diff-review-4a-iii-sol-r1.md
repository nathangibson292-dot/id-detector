## A. Contract violations

- **U-F18 is incomplete.** Home cards use `"Listening"` and other gerund labels from `src/id_detector/present/server.py:740-754`, while the progress tracker still uses `"Listen"`, `"Fetch"`, `"Decode"`, etc. at `src/id_detector/present/server.py:1342-1358`. U-F18 explicitly requires reusing the progress page’s labels, not merely sharing labels between initial cards and polling.
- **U-F29 remains incomplete for result pages.** The new asset extraction explicitly excludes result pages (`src/idea_web/application.py:93-107`), while results still embed their CSS and at least three scripts (`src/id_detector/present/page.py:1435-1448`). The original finding specifically identified those repeated result-page bytes. The test institutionalizes this omission at `tests/idea_web/test_headers_forms.py:206-208`.
- **The Phase-4 ownership boundary is violated.** The plan limits post-Phase-3 changes under `src/id_detector/` to `service.py`, `recipes.py`, the serve entry point and `PROJECT_ROOT` (`docs/PLAN-v2.md:435-437`), but this diff modifies `src/id_detector/present/server.py` and `src/id_detector/truth_review.py`. No owner exception is recorded.
- No D1–D8, profile, golden-artifact, or second-session-file violation was found. The five explicitly protected files are untouched.

## B. Correctness bugs

- **P0: a normal cached result reload receives a CSP that disables the page.** A result’s first `200` receives its document-specific CSP, but an `If-None-Match` hit returns the bare `not_modified()` response at `src/idea_web/application.py:410-417`. `not_modified()` supplies only ETag/cache headers (`src/idea_web/http.py:185-188`), so middleware adds the non-document fallback `default-src 'none'` policy from `src/idea_web/http.py:77-84`. Because canonical results are `no-cache`, revalidation is routine; cached response metadata is updated from the `304`, leaving inline styles/scripts, embeds, and `/media/.../audio` blocked.
- **Unhandled `500` responses do not receive the promised “every answer” headers.** `SecurityHeaders` is installed as ordinary user middleware (`src/idea_web/http.py:343-381`), inside Starlette’s outer `ServerErrorMiddleware`. A realistic remove/read race can make the file disappear between resolution and `open()` (`src/idea_web/application.py:405-409`); the resulting framework-generated `500` bypasses `SecurityHeaders`.
- No reservation, cap, journal, attempt-state, cancellation, or provider-rebilling logic is changed by this diff.

## C. Scope

- Gzip, ETags, static routes, CSP, forms, and transport headers belong to 4a-iii.
- Progress weighting, accounts, and sessions were not pulled forward.
- The two `src/id_detector/` presentation edits are outside the §4.3 Phase-4 boundary and should be moved to the web layer or explicitly approved.
- No `PAGE_VERSION` bump is needed for the changes actually made because result bytes remain unchanged. Completing the omitted result-page portion of U-F29 would require a bump and regeneration strategy.

## D. Tests

- `tests/idea_web/test_headers_forms.py` contains the named 12 tests and is offline/deterministic by inspection.
- The ETag test checks only status, ETag, and cache control on `304` (`tests/idea_web/test_headers_forms.py:371-401`); it misses the broken CSP.
- The “every answer” test exercises handled responses only (`tests/idea_web/test_headers_forms.py:132-169`), not an unhandled exception.
- The U-F18 test proves only that cards and card-polling share `_PHASE_LABELS` (`tests/idea_web/test_headers_forms.py:525-531`); it never compares them with `_job_steps`.
- Static-asset coverage is limited to “live pages” (`tests/idea_web/test_headers_forms.py:284-298`) and explicitly accepts inline result assets.
- Existing polling and legacy-header assertions were relocated or updated without an evident behavioral weakening.
- Exact `uv run` invocations for the full suite, phase gate, web suite, Ruff, JS audit, fixture audit, and serve smoke all failed before execution because the sandbox cannot launch the WinGet `uv.exe` symlink. Direct virtualenv Ruff passed and the fixture audit passed over 485 files. Direct pytest and page-JS checks could not create a temporary directory in the read-only sandbox.

## E. Local mode

- `uv run idea serve --no-open --port 8791` could not execute because of the blocked `uv.exe`.
- The direct `idea.exe` entry point reached `LocalJobs` but failed when the read-only sandbox denied creation of `work/.idea`; therefore startup and `/` serving could not be independently verified.
- Static inspection found no direct change to `idea.cmd`, playlist routing, or audio range streaming, but the cached-result CSP bug will break result-page scripts, styling, and audio after revalidation.

## F. Quality

- `_PHASE_LABELS`, `_job_steps`, and the `safeLog` JavaScript table are three competing label sources despite the claimed single-language contract.
- The new web layer deepens its dependency on private legacy fragments through `STATIC_JS` and `STATIC_CSS` (`src/idea_web/application.py:98-107`) instead of retiring `present/server.py`.
- `git diff --check` passed; no obvious dead code or formatting defect was found.
- Regression coverage is missing for HTML `304` CSP preservation and framework-generated `500` headers.

## Required fixes

### Realistic

1. **P0** Preserve the original document-specific CSP on HTML `304` responses and add a test asserting the `200` and `304` CSPs are identical.
2. **P1** Ensure uncaught `500` responses also receive all defensive and no-store headers, with a forced-exception regression test.
3. **P1** Make activity cards derive their exact labels from the progress tracker’s canonical labels and test equality across both render paths.
4. **P1** Complete U-F29 for served result pages, or obtain an explicit owner decision narrowing it; if result bytes change, bump `PAGE_VERSION` and preserve offline-file behavior.
5. **P1** Move the new presentation/truth-review behavior out of the forbidden `src/id_detector/` files, or record an explicit owner exception to the §4.3 boundary.
6. **P2** Re-run the exact requested gates and the port-8791 `/` smoke test in a writable environment with a functional `uv` launcher.

### Adversarial

None.

VERDICT: FIX_FIRST