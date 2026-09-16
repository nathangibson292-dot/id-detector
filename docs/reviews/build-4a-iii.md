# Build — 4a-iii Server-side UI fixes, static assets, headers

Built on main `28d8fe1`. Scope: plan §2.5 / §4.4 items U-F1, F15, F18, F29, F30, F34 (Appendix A) plus
the §4.4 "CSP/security headers" line. Gate: `uv run pytest tests/idea_web/test_headers_forms.py -q`.
Nothing under `src/id_detector/**` changed (plan §4.2 boundary; see the fix pass below), and nothing
under `src/id_detector/playlists/**`, `tests/test_playlists.py`, `README.md`, `idea.cmd` or
`src/id_detector/present/theme.py`. No new dependency: gzip is Starlette's own middleware, already
in the lock. The progress bar's weighting (U-F9) is untouched.

## What changed, file by file

- `src/idea_web/http.py` — `SecurityHeaders` middleware (nosniff, Referrer-Policy, X-Frame-Options,
  Permissions-Policy, default `Cache-Control: no-store`, a `default-src 'none'` CSP for non-documents);
  `content_security_policy()` / `inline_script_hashes()` (a document's CSP names the sha256 of each
  inline script it contains — no `'unsafe-inline'` for script); `html_response`, `deliver`,
  `weak_etag`, `etag_matches`, `not_modified(policy=)`, `with_default_headers`, `internal_error`
  (the answer to an uncaught exception, with every header); `bytes_response` gains `cache=`/`etag=`;
  `install_middleware()` fixes the one stack both apps use (gzip innermost, HEAD guard, loopback POST
  gate, headers outermost) and registers the exception handler.
- `src/idea_web/pages.py` — **new**: the web layer's own page fragments over the legacy ones:
  `phase_label` (the tracker's label rule), `activity_item_html` (the home card, carrying its job's
  step list), `mixes_block` (the legacy library block with no inline handler), `HOME_JS` (card
  polling), `CONFIRM_JS`, `NEW_MIX_JS`, `JOB_JS` (the legacy progress script paced to 2.5 s and its
  log reading `STEPS`), the static `app.<hash>.css|js` bundle, `head_html`.
- `src/idea_web/application.py` — versioned static assets served `public, max-age=1y, immutable`;
  live pages link the stylesheet/script and go through `html_response` (hashed CSP, `no-store`); the
  job page's only inline script is its per-job constants; result files get a weak ETag, `no-cache`
  (canonical) or `private, immutable` (bundle file) and answer 304 to `If-None-Match` — an HTML 304
  carrying the document's own CSP; playlists documents get the hashed policy via `deliver`; the
  bad-mode form error keeps url/tracklist/links (U-F1); middleware via `install_middleware`.
- `src/idea_web/server.py` — `server_header=False` (no `Server: uvicorn` leak; U-F29).
- `src/idea_web/truth_review.py` — same middleware stack; `review_page()` serves the legacy review
  page with its four inline `onclick` attributes replaced by ids and one hashed wiring script.
- `src/idea_web/templates/home.html`, `job.html`, `README.md` — pages link the static stylesheet
  and script; the job page keeps one inline config script; README records the asset boundary.
- `scripts/check_page_js.py` — also `node --check`s the assembled static `app.js`.
- `tests/idea_web/test_headers_forms.py` — new (the gate; 15 tests).
- `tests/idea_web/test_parity.py`, `test_local_queue.py` — the "2.5 s poll" pin now reads the
  static script the page links (the contract is unchanged; its location moved).
- `tests/idea_web/test_legacy_contract.py` — three recorded pins updated: the retired handler sent
  no `X-Content-Type-Options` on the `/new` redirect or on audio; every answer carries it now.

## Decisions

- **U-F29 is narrowed to the live pages (home, job, read-only index); result pages are not
  externalised and `PAGE_VERSION` is not bumped.** Two reasons: (1) a result page is deliberately
  self-contained so a saved result opens offline as a plain file (header of
  `src/id_detector/present/page.py`), and the owner's priority is local use; (2) `PAGE_VERSION`
  lives in `src/id_detector/present/page.py`/`theme.py`, another session's files, and a bump would
  re-render every stored result. Result pages therefore keep their inline CSS/JS byte-for-byte;
  gzip brings the 178 KB page to the ~55 KB the review expected. `test_static_assets_are_versioned_
  immutable_and_linked_by_every_live_page` states this in its docstring.
- **Phase-4 boundary (plan §4.2): zero-line diff under `src/id_detector/`.** Every behaviour
  first placed in `present/server.py` and `truth_review.py` now lives in `src/idea_web/` — either
  the web layer's own fragment (`pages.activity_item_html`, `HOME_JS`, `CONFIRM_JS`, `NEW_MIX_JS`,
  `head_html`) or a wrapper over the legacy one that removes the inline handlers
  (`pages.mixes_block`, `truth_review.review_page`) or paces its script (`pages.JOB_JS`, the same
  exact-substitution pattern 4a-ii used for the poll). The legacy compatibility renderers are
  untouched and `check_page_js.py` still checks them.
- **One label source (U-F18).** The progress tracker's step list (`legacy._job_steps`) names each
  phase; `pages.phase_label` is the tracker page's own `render()` rule ("In the queue" before a job
  starts, the step's name, else the phase capitalised). The card renderer uses it server-side, the
  card carries its step list (`data-steps`) so the polling script applies the identical rule, and
  the progress log's own label table is replaced by `STEPS`. No second table exists in Python or
  JavaScript (asserted, and the JS rule is executed under Node against the Python rule).
- **CSP shape.** Script is hashed per document (the injection surface); `style-src` keeps
  `'unsafe-inline'` because every page inlines its stylesheet and positions timeline lanes with
  `style=` attributes, which hashes cannot cover. Third parties are exactly the embed hosts
  `page._embed_html` uses (`w.soundcloud.com`, `www.youtube.com`/`-nocookie`,
  `widget.mixcloud.com` / `player-widget.mixcloud.com`). `frame-ancestors 'none'` + `X-Frame-Options:
  DENY`, `base-uri 'none'`, `object-src 'none'`, `form-action 'self'`, `connect-src 'self'`,
  `media-src 'self'`, `img-src 'self' data:` (the favicon).
- **Referrer-Policy is `strict-origin-when-cross-origin`, not `no-referrer`.** Result URLs hold
  source/media keys and must not reach Bandcamp/Beatport (only the origin crosses), while a YouTube
  embed refuses to play with no referrer at all.
- **No HSTS / COOP / COEP.** Local mode is plain HTTP on the loopback; HSTS belongs to the hosted
  TLS terminator (Caddy, 6a). COEP would break the platform embeds.
- **Cache policy.** Dynamic pages and JSON `no-store` (CSRF token, live state); canonical result
  files `no-cache` + weak ETag (a refresh can move the pointer); immutable bundle files
  `private, max-age=1y, immutable`; static assets `public, max-age=1y, immutable` under a
  content-hash URL.
- **U-F34 "+ New mix".** `theme.py` is off-limits, so the top-bar link stays `href="/new"` and
  `NEW_MIX_JS` intercepts the click to focus the field; `/new` still lands on the one form.
- **U-F1, F15, F30** were largely delivered in 3a-ii/4a-ii; this cycle fixed the remaining U-F1
  gap (the bad-mode error dropped the tracklist and links) and pinned all three at the web layer.
- Three recorded pins in `test_legacy_contract.py` were changed deliberately (nosniff on the
  `/new` redirect and on audio answers) because the old handler's omission is what §4.4 fixes.

## Tests (`tests/idea_web/test_headers_forms.py`, 18)

1. `test_every_answer_carries_the_defensive_headers_and_is_not_stored` — 12 answer kinds (pages,
   JSON, redirect, 404, foreign-origin 403, 501) all carry the headers and `no-store`; HTML gets a
   `script-src 'self'` policy, non-HTML the `default-src 'none'` one.
2. `test_the_content_security_policy_names_exactly_each_pages_inline_scripts` — parses home (with
   running/cancelled jobs), job page, read-only index, canonical and bundle result pages, playlists
   page and the inline-error page with `html.parser`; the CSP must name exactly the parsed scripts'
   hashes, no `'unsafe-inline'`/`'unsafe-eval'` for script, and no inline handler or
   `javascript:` URL may exist. Home has zero inline scripts; the job page exactly one.
3. `test_the_truth_review_page_is_served_under_the_same_hashed_policy` — the unwrapped legacy page
   has inline handlers, the served one none; the four controls are wired from a second hashed script.
4. `test_inline_script_hashing_follows_the_html_parsers_rules` — `src=` scripts, empty bodies,
   duplicates, CRLF normalisation, case-insensitive tags.
5. `test_static_assets_are_versioned_immutable_and_linked_by_every_live_page` — content types,
   immutable caching, 404 for unversioned/traversal paths, every live page links them and inlines
   nothing, every fragment present (and the legacy card script absent), 2.5 s poll paced.
6. `test_gzip_compresses_documents_but_never_audio_ranges_or_small_answers` — gzip on pages, the
   result page and the script; not on small JSON, `identity`, `audio/*`, 206 ranges, or an
   `application/octet-stream` original; HEAD carries the compressed GET headers.
7. `test_real_server_sends_no_server_header_and_compresses` — over uvicorn on a free port.
8. `test_result_files_carry_etags_and_answer_304_when_unchanged` — weak ETag, `no-cache` vs
   `private, immutable`, 304 for GET/HEAD and for an `If-None-Match` list, a new run changes the tag.
9. `test_bad_submissions_rerender_the_one_form_with_every_field_kept` (U-F1).
10. `test_failure_copy_credit_statement_and_no_auto_redirect` (U-F15/F30).
11. `test_home_activity_cards_share_labels_show_titles_and_sort_running_first` (U-F18) — the card's
    label equals the label the served tracker's `STEPS` gives the same job, "In the queue" before
    it; running sorts above newer queued; titles never URLs; cancel → Dismiss → gone.
12. `test_one_form_new_redirects_to_it_and_new_mix_focuses_it` (U-F34) — plus the legacy library
    block still carries `onclick` while the served block never does.
13. `test_a_304_for_a_document_carries_exactly_the_policy_its_200_carried` (fix pass P0) — canonical
    and bundle pages, GET and HEAD; a non-document 304 keeps the resource default.
14. `test_an_uncaught_exception_still_answers_with_every_header` (fix pass P1) — a forced exception
    in a route, GET and HEAD, on both apps.
15. `test_activity_labels_are_the_trackers_labels_in_python_and_in_the_polling_script` (fix
    passes 1 and 2) — for two job shapes and ten status/phase pairs: the card render (Python),
    the polling rule (`HOME_JS` under Node) **and the tracker itself** — the served progress
    script's `render()` executed under Node against a DOM stub, reading `#phase-name` — all agree;
    no literal label table in the bundle.
16. `test_polling_rerenders_the_list_when_a_job_ends_or_another_starts` (fix pass 2) — A runs, B
    waits; A fails, B starts. The already-rendered cards are driven through the real polling
    script under Node with the server's real status JSON: the first round patches in place (no
    swap), the second swaps in exactly the server's fresh list (`GET /activity` == the list in a
    fresh `GET /`): B first, Dismiss on A, no reload.

## Fix pass (review `diff-review-4a-iii-sol`, FIX_FIRST)

1. **P0 — CSP on 304.** `not_modified()` takes the document's policy; the result route computes it
   from the body it already read for the ETag, for HTML only. Test 13.
2. **P1 — headers on uncaught 500s.** `install_middleware` registers `internal_error` as the
   `Exception` handler, which Starlette's outermost `ServerErrorMiddleware` invokes; the handler
   applies `DEFAULT_HEADERS` itself (`with_default_headers`) since it runs outside the stack. Test 14.
3. **P1 — U-F18 labels.** One source: the tracker's step list, through `pages.phase_label`; the
   `_PHASE_LABELS` table is gone, the card carries `data-steps`, the polling script and the progress
   log use `STEPS`. Tests 11 and 15.
4. **P1 — U-F29 narrowed** (see Decisions); test 5's docstring states it; result bytes untouched.
5. **P1 — Phase-4 boundary.** `git diff -- src/id_detector` is empty (verified: 0 lines). New
   `src/idea_web/pages.py` holds the moved behaviour; `idea_web/truth_review.review_page` rewires
   the review page at serve time.
6. **P2** — no action (the orchestrator runs the gates).

## Fix pass 2 (review `diff-review-4a-iii-sol-r2`, FIX_FIRST; two P1s)

1. **P1 — terminal transitions in card polling.** The polling script no longer patches a card
   whose kind changed. `refreshNeeded(before, j)` is true when the polled status differs from
   the card's and is terminal or `running`; the script then fetches **`GET /activity`** — the
   home's activity list rendered fresh by the same `_active_jobs` sort and `activity_list_html`
   the home page uses — and replaces `ul.acts` with it, re-arming polling on the new cards
   (detached cards stop). So a finished card gets its Dismiss control and a newly running card
   moves first exactly as a reload would show them, without one. Success still reloads (the
   library must update). `home.html` renders the list through the same builder, so fragment and
   page cannot drift. Test 16 (DOM transition under Node, "A fails then B starts").
2. **P1 — the label test exercises the real tracker.** Test 15 now also runs the served progress
   script (`pages.JOB_JS`, i.e. the frozen `legacy._JOB_JS` with its two exact substitutions)
   under Node against a minimal DOM stub, calling its `render(j)` for every case and reading what
   it wrote into `#phase-name`; that must equal the card render and the polling rule. A wrong
   label hard-coded in the tracker now fails the test. `src/id_detector/` is read, not edited.
3. **P2** — no action (the orchestrator runs the gates).

17. `test_a_failed_activity_refresh_keeps_the_list_and_keeps_polling[500|malformed]` (fix pass
    3) — the real polling script under Node: a `/activity` 500, and a 200 whose body is not the
    list, change nothing (no swap, both cards still connected, phases intact), polling is re-armed,
    an immediate retry stays inside the back-off (one attempt), and after the clock passes it a
    healthy answer swaps the fresh list in. Fails if either guard is removed.
18. (Test 15, extended in fix pass 3) — cases are generated from every `_job_steps()` entry for
    both job shapes (9 steps + 5 non-steps; 7 steps + 6 non-steps, `decode`/`windows`/`hints`/
    `fuse` included and asserted present) with the step's listed name as the oracle; the Python
    card render, the polling rule under Node and the served tracker's `render()` under Node are
    each compared to it, so one step diverging in one path fails.

## Fix pass 3 (review `diff-review-4a-iii-sol-r3`, FIX_FIRST; two P1s — last pass)

1. **P1 — `/activity` failures.** `refresh()` now throws on a non-2xx answer and on any body that
   is not exactly one `<ul class="acts">…</ul>`, so a failed refresh replaces nothing; the card
   that asked keeps polling (its poll is re-armed before returning), so the refresh is asked for
   again next poll; a failed attempt sets a back-off (2.5 s doubling to a 60 s cap) that later
   attempts wait out; polling never stops. Test 17.
2. **P1 — every step.** Test 15 generates its cases from `_job_steps()` itself for both job
   shapes, asserts the full step set (`decode`, `windows`, `hints`, `fuse` included), uses the
   listed step names as the oracle, and executes all three paths per case. Test 15/18 above.
3. **P2** — no action (the orchestrator runs the gates).

## Pasted outputs (fix pass 3)

Full suite in shards (all foreground); `--collect-only`: `1713/1810 tests collected (97 deselected)`.

```text
tests/idea_web (7 files)                                    226 passed, 1 warning in 230.32s (0:03:50)
tests/test_[a-o]*.py (12 files, named)                      133 passed, 1 warning in 82.11s (0:01:22)
tests/test_phase*.py (15 files, named)                      418 passed, 1 warning in 386.43s (0:06:26)
paid_clip, playlists, projection, stage1-2, stage10         185 passed, 23 deselected, 1 warning in 44.91s
stage3-9, scan*, score_corpus, semantics, service_api       446 passed, 1 skipped, 70 deselected, 1 warning in 117.67s
truth_* (12 files)                                          303 passed, 1 skipped, 4 deselected, 1 warning in 151.54s
total                                                       1711 passed + 2 skipped = 1713 collected
```

Gate: `18 passed`. `ruff check .` → All checks passed; `ruff format --check .` → 351 files already
formatted; fixture audit passed (485 files); `check_page_js.py` passed (53 inline + static app.js);
`git diff -- src/id_detector` → 0 lines; `git diff --stat` → 11 files changed, 362 insertions(+),
61 deletions(-) plus `pages.py`, `test_headers_forms.py`, this report untracked.

## Pasted outputs (fix pass 2)

Full suite in shards (all foreground); `--collect-only`: `1711/1808 tests collected (97 deselected)`.

```text
tests/idea_web (7 files)                                    224 passed, 1 warning in 216.38s (0:03:36)
tests/test_[a-o]*.py (12 files, named)                      133 passed, 1 warning in 66.39s (0:01:06)
tests/test_phase*.py (15 files, named)                      418 passed, 1 warning in 349.49s (0:05:49)
paid_clip, playlists, projection, stage1-2, stage10         185 passed, 23 deselected, 1 warning in 47.24s
stage3-9, scan*, score_corpus, semantics, service_api       446 passed, 1 skipped, 70 deselected, 1 warning in 121.11s
truth_* (12 files)                                          303 passed, 1 skipped, 4 deselected, 1 warning in 147.90s
total                                                       1709 passed + 2 skipped = 1711 collected
```

`git diff -- src/id_detector` → 0 lines.

`uv run ruff check .` → `All checks passed!` · `uv run ruff format --check .` → `351 files already formatted`

`uv run python scripts/audit_fixtures.py`

```text
audited 485 files
fixture audit passed
```

`uv run python scripts/check_page_js.py`

```text
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

Phase gate `uv run pytest tests/idea_web/test_headers_forms.py -q`

```text
................                                                         [100%]
16 passed, 1 warning in 12.64s
```

`git status --short` / `git diff --stat`

```text
 M scripts/check_page_js.py
 M src/idea_web/application.py
 M src/idea_web/http.py
 M src/idea_web/server.py
 M src/idea_web/templates/README.md
 M src/idea_web/templates/home.html
 M src/idea_web/templates/job.html
 M src/idea_web/truth_review.py
 M tests/idea_web/test_legacy_contract.py
 M tests/idea_web/test_local_queue.py
 M tests/idea_web/test_parity.py
?? docs/reviews/build-4a-iii.md
?? src/idea_web/pages.py
?? tests/idea_web/test_headers_forms.py
 11 files changed, 362 insertions(+), 61 deletions(-)   (+ pages.py, test_headers_forms.py untracked)
```

## PAGE_VERSION

**No bump needed.** Result-page bytes (`page.py`) are unchanged; every change is server-side.

## Not done / notes for the orchestrator

- The PowerShell gates (`smoke_serve.ps1`, `gate_local_mode.ps1`) were not run here as instructed.
  The real-server test (`serve_in_background`) covers the same `LoopbackServer` path `idea serve`
  uses; the headless-Edge gate is the browser-side proof that the hashed CSP loads every page.
- Hosted-mode HSTS and any nonce-based CSP for future server-rendered pages are for 6a-iii/6a-iv.

## Fix pass 4 (test-only; not re-reviewed)

The round-4 sol-xhigh review raised one P1 and nothing else: the `/activity` failure regression's 500
fixture returned a body that was *also* malformed, so the malformed check alone caught it and deleting
the response-status guard would have left the test passing.

The Fable subagent hit its weekly usage limit before it could act, so the orchestrator made this change
directly. It is test-only; `src/idea_web/pages.py` and every other production file are untouched.

- `tests/idea_web/test_headers_forms.py`: the `500` case now answers with `ERROR_LIST`, a syntactically
  valid `<ul class="acts">` carrying a distinctive card (`error-body-never-shown`), and the test asserts
  that marker never appears in anything swapped into the page. Only the response status can reject it.
- **Revert proof.** With `if(!r.ok) throw …` deleted from `pages.py`, the 500 case fails
  (`1 failed, 1 passed`); restored byte-identical, SHA-256 `669c65701c3ec970`.
- Gate re-run: `uv run pytest tests/idea_web/test_headers_forms.py -q` → **18 passed**.
  `ruff check` and `ruff format --check` on the changed file: clean.

The full suite, both PowerShell gates, the fixture audit and the page-JS check are re-run by the
orchestrator on main after merging; their results are in the commit message.

## Fix pass 5 (orchestrator, from the PowerShell gates): same-origin framing

`scripts/gate_local_mode.ps1` failed on main after merging, with "Probe did not log audio-ok after
metadata and seek". It was reproduced deterministically three times, and it is a real regression of this
cycle, not a flake: the gate's probe frames the cached result page from the same origin and reads the
framed document to find its `<audio>` element.

Headless Edge, run against the failed run's own work tree with console logging, named the cause:

    Framing 'http://127.0.0.1:8795/.../index.html' violates the following Content Security Policy
    directive: "frame-src https://w.soundcloud.com https://www.youtube.com ...". The request has been
    blocked.

Three things were blocking, all now same-origin only:

- `X-Frame-Options: DENY` → `SAMEORIGIN`, and `frame-ancestors 'none'` → `'self'`: a page may be framed
  by its own origin. Another site still cannot frame it.
- `frame-src` listed only the three platform players, so a page could not frame its **own** origin. It is
  now `frame-src 'self'` plus those hosts. This was the blocker the browser named; the probe's status text
  stayed at `waiting` because its iframe never loaded and its `onload` never ran.
- The probe signalled success by loading `http://127.0.0.1:<port+1>/audio-ok` as an image, which
  `img-src 'self' data:` correctly refuses. The page policy was **not** loosened; the probe in
  `scripts/gate_local_mode.ps1` now signals by navigating to that URL, which CSP does not govern. What the
  gate asserts is unchanged: `idea.cmd` starts, the cached mix opens, and its audio loads metadata and seeks.

Tests pin all of it in `tests/idea_web/test_headers_forms.py`: every answer carries `SAMEORIGIN`, no policy
contains `frame-ancestors 'none'`, and every HTML document's policy contains `frame-src 'self'`.
