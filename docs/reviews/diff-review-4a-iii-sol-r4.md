## A. Contract violations

- No protected-file violation: `git diff -- src/id_detector` is empty, and `README.md`, `idea.cmd`, `theme.py`, playlists, and playlist tests are unchanged.
- No frozen-artifact or result-byte change was introduced by fix pass 3.
- The prior CSP, headers, forms, static-assets, and `PAGE_VERSION` decisions were not reopened.

## B. Correctness bugs

- Both requested failure paths currently behave correctly. Non-2xx responses are rejected at `src/idea_web/pages.py:128-130`; malformed bodies are rejected at `src/idea_web/pages.py:131-132`; replacement occurs only afterward at `src/idea_web/pages.py:133-135`.
- Failure preserves the existing list and records exponential backoff capped at 60 seconds (`src/idea_web/pages.py:136-139`). Card polling remains armed at `src/idea_web/pages.py:148-162`, preventing permanent stoppage or hot `/activity` retries.
- Directly driving the real script passed for both `500` and `malformed`. No realistic runtime regression from fix pass 3 was found.

## C. Scope

- Fix pass 3 remains confined to the two Round-3 findings: `/activity` failure handling and exhaustive phase-label tests.
- No progress weighting, accounts, sessions, money, provider, queue, playlist, truth-review, audio, or `idea.cmd` work was introduced.
- `git diff --check` passes.

## D. Tests

- Label coverage is now complete. The test pins the full `_job_steps()` set, including `decode`, `windows`, `hints`, and `fuse` (`tests/idea_web/test_headers_forms.py:709-720`), generates cases from both job shapes (`:731-753`), and exercises the Python card, polling rule, and real tracker render (`:746-790`). The test passed directly under Node.
- The failure regression drives the real `pages.HOME_JS` (`tests/idea_web/test_headers_forms.py:896-904`) and asserts preservation, continued polling, backoff, and eventual recovery (`:942-974`).
- **Round-3 P1 is not fully pinned:** the 500 fixture returns plain text (`tests/idea_web/test_headers_forms.py:1005-1008`), which the malformed-body guard also rejects. I removed only the `r.ok` check in memory and the 500 test still passed. Thus the test would not detect deletion of the non-2xx guard at `src/idea_web/pages.py:129`.

## E. Local mode

- Exact `uv run idea serve --no-open --port 8791` could not execute because the WinGet `uv.exe` link is broken.
- A no-write equivalent using the same loopback server bound port 8791 and served `/` with HTTP 200 and the expected CSP. No fix-pass-3 local-mode regression was found.

## F. Quality

- Direct Ruff passed.
- Direct fixture audit passed: 485 files.
- The exact full, web, phase, JavaScript, Ruff, and audit `uv run ...` commands all failed before execution because `uv.exe` is unusable. Direct pytest cannot generally run because the sandbox has no writable temporary directory; direct `check_page_js.py` failed for the same reason.
- Direct execution of the all-label test and both `/activity` failure cases passed. Builder-pasted gate outputs were not relied upon.

## Required fixes

### Realistic

1. **P1** Make the 500 fixture return a syntactically valid but distinct `<ul class="acts">…</ul>` body and assert it is not swapped, so deleting the `r.ok` guard makes the regression fail.

### Adversarial

None.

## Pre-existing, for a later cycle

None reopened under the Round-4 stop rule.

VERDICT: FIX_FIRST