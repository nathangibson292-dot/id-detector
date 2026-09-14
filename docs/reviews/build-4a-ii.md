# Build — 4a-ii FastAPI parity (loopback)

Built the accepted FastAPI + Jinja2 + uvicorn adapter and made it the server behind `idea serve`.
The local route layer submits work to the existing local `JobManager`; request handlers never call
`id_detector.service.run` or a pipeline inline. This was the cycle note's explicit local-mode
allowance and preserves the current form options, progress model, cancellation, and double-click
`idea.cmd` behavior. Hosted mode does not serve local audio.

## Route-by-route parity

| Old handler | New FastAPI route | Preserved behavior | Proving test |
|---|---|---|---|
| `do_GET` `/healthz` | `GET|HEAD /healthz` | 200 JSON `{"ok":true}`, JSON MIME, empty HEAD body | `test_health_home_index_new_and_head` |
| `do_GET` `/csrf` | `GET|HEAD /csrf` | per-app synchronizer token, no CORS | `test_health_home_index_new_and_head`; unchanged `test_phase0a_security.py` |
| `do_GET` `/`, `/index.html` | `GET|HEAD /`, `/index.html` | analyse-enabled home or read-only library, prefill, recent jobs, failures, same headers and empty HEAD | `test_health_home_index_new_and_head` |
| `do_GET` `/new` | `GET|HEAD /new` | 303 to the one home form with percent-encoded prefill; 404 read-only | `test_health_home_index_new_and_head` |
| `_handle_analyse` | `POST /analyse` | only exact `application/json` selects JSON; JSON 200 vs form 303; validation, profile, optional hints, 8 KiB body cap, form error pages | `test_analyse_json_only_for_exact_json_content_type_and_form_redirect`; `test_analyse_validation_and_oversize_keep_json_and_form_contracts` |
| `_handle_job_get` page | `GET|HEAD /jobs/{job_id}` | 200 page/HEAD, 404 unknown, escaped title, CSRF-bearing cancel script, 2.5 s polling | `test_job_page_status_cancel_dismiss_and_2500ms_polling` |
| `_handle_job_get` status | `GET /jobs/{job_id}/status` | 200 status JSON, 404 JSON unknown, resolved local audio URL | `test_job_page_status_cancel_dismiss_and_2500ms_polling`; unchanged `test_stage10_webapp.py` |
| `_handle_job_get` audio | `GET|HEAD /jobs/{job_id}/audio` | verified fetched audio, MIME, byte ranges, no-audio 404 | `test_job_page_status_cancel_dismiss_and_2500ms_polling`; unchanged `test_stage10_webapp.py` |
| `_handle_job_cancel` | `POST /jobs/{job_id}/cancel` | header CSRF, 200 boolean JSON, 403/404 JSON, request body safely consumed | `test_job_page_status_cancel_dismiss_and_2500ms_polling`; unchanged `test_phase0a_security.py` |
| `_handle_job_dismiss` | `POST /jobs/{job_id}/dismiss` | terminal dismissal and 303 `/`; invalid request 400 | `test_job_page_status_cancel_dismiss_and_2500ms_polling` |
| `_handle_library_remove` | `POST /library/remove` | CSRF + key validation, 303, recoverable `.trash/library` move, no mutation on GET | `test_library_removal_is_recoverable_and_protected` |
| `/media/<media_key>/audio` branch | `GET|HEAD /media/{media_key}/audio` | cached-asset verification, MIME, `Accept-Ranges`, 200/206/416 and correct `Content-Range`; 404 hosted | `test_verified_audio_get_head_range_416_and_hosted_policy`; unchanged `test_phase1a_cached_open.py` |
| `_resolve_served_audio` | catch-all `GET|HEAD` local audio | contained local audio only, known audio MIME, range support; disabled hosted | `test_verified_audio_get_head_range_416_and_hosted_policy`; unchanged cached-open/local gate |
| `_resolve_served_file` canonical result | `GET|HEAD /{source}/{media}/present/{file}` | known suffixes/MIME, traversal and symlink rejection, stale page regeneration | `test_result_exports_traversal_head_and_stale_regeneration`; `test_result_route_rejects_a_symlink_escape` |
| direct immutable bundle file | `GET|HEAD /{source}/{media}/present/bundles/{bundle_id}/{file}` | bundle-id validation, manifest verification, manifest-listed file only, containment | `test_verified_audio_get_head_range_416_and_hosted_policy`; `gate_local_mode.ps1` |
| playlist GET shim | `GET|HEAD /playlists[/{path}]` | framework-independent `playlists.handle_get`, status/body/MIME unchanged | `test_playlists_get_post_and_guards`; unchanged `tests/test_playlists.py` |
| playlist POST shim | `POST /playlists[/{path}]` | Origin/Host gate then synchronizer CSRF then `playlists.handle_post`; 200/400/404/405 JSON unchanged | `test_playlists_get_post_and_guards`; unchanged `tests/test_playlists.py` |
| `do_POST` pre-routing gate | `LoopbackPostGuard` middleware | only current-port loopback Host; optional HTTP loopback Origin; foreign requests receive a 403 JSON body | `test_origin_host_and_csrf_refusals_have_bodies_and_do_not_mutate`; unchanged `test_phase0a_security.py` |
| unknown GET/POST and removed `/rescan` | catch-all routes | 404 text body; POST Origin/Host check runs before dispatch; no mutation on GET | `test_unknown_routes_preserve_404_and_post_origin_order`; unchanged security/server suites |

## File-by-file changes

- `pyproject.toml`, `uv.lock` — added only the accepted web dependencies and locked them.
- `src/idea_web/application.py` — app factory, explicit local/hosted render policy, loopback POST
  middleware, CSRF checks, form/JSON parsing, page/job/playlist routes, authorised result resolvers,
  verified range responses, recoverable removal, and Jinja rendering.
- `src/idea_web/server.py` — pre-bound loopback socket and uvicorn lifecycle used by CLI and real-HTTP
  tests, including port 0 support and deterministic shutdown.
- `src/idea_web/templates/home.html`, `index.html`, `job.html` — live application page structure.
  Jinja autoescaping is enabled; plain values such as job titles are autoescaped. Only established,
  already-escaped theme/form/card/player/footer fragments and JSON-encoded inline scripts use
  `safe`.
- `src/idea_web/templates/document.html` — wraps framework-independent playlist documents; immutable
  analytical result pages are never re-rendered in request handlers and are served from bundles.
- `src/idea_web/templates/README.md` — records the template/trusted-fragment boundary.
- `src/idea_web/__init__.py` — exports `create_app` and `WebSettings`.
- `src/id_detector/present/server.py` — its public `make_server` compatibility entry now lazily
  returns the uvicorn server, so `idea serve`, `idea.cmd`, and unchanged callers reach one server.
  The progress-page poll was corrected from 1.5 s to the binding 2.5 s.
- `tests/idea_web/test_parity.py` — twelve deterministic httpx tests covering every route and
  parity/security assertion named by the cycle.
- `tests/idea_web/test_worker.py` — narrowed the old “no HTTP framework anywhere in `idea_web`”
  assertion to `idea_web/jobs/**`; the worker boundary remains enforced after `idea_web` gained its
  intended web application.

The Python fragment builders `_form_html`, `_activity_item_html`, `_mixes_block`,
`_job_player_html`, theme helpers, and footer remain in `present/server.py` during parity to preserve
the exact established look. FastAPI no longer uses the old full-document `_home_html`, `_index_html`,
or `_job_page_html`; those remain compatibility/render-test entry points. Playlist page assembly
remains in the separately owned framework-independent playlist module, which this cycle was forbidden
to edit. Result-page HTML remains the immutable pre-rendered bundle artifact required by ADR 0001.

## Dependencies

Pinned minimums in `pyproject.toml`:

- `fastapi>=0.116.1` (resolved `0.141.1`)
- `jinja2>=3.1.6` (resolved `3.1.6`)
- `uvicorn>=0.35.0` (resolved `0.53.0`)

`python-multipart` was not added: the parity forms are URL-encoded and parsed under the existing 8
KiB limit. Final `uv sync` output:

```text
Resolved 59 packages in 3ms
Checked 59 packages in 8ms
```

## Playlist guarantees

No file under `src/id_detector/playlists/**`, `tests/test_playlists.py`, `README.md`, `idea.cmd`, or
`src/id_detector/present/theme.py` changed.

1. Every shown track row still comes from `present.page._track_row_html` with
   `id="<episode_id>"`.
2. That same shown-row path still calls `row_actions_html`, emitting `data-candidate-id` and
   `data-episode-id`; hidden rows emit no controls.
3. `present.page` still imports and injects `PLAYLIST_CSS` in the head and `PLAYLIST_JS` at the
   script injection point.
4. The existing `location.protocol` guard hides controls on `file://`, and only projected shown rows
   reach the control call.
5. Canonical links remain `/{source_key}/{media_key}/present/index.html`; the new app additionally
   serves manifest-verified immutable bundle URLs used inside generated pages.

`test_result_rows_keep_all_five_playlist_injection_guarantees` verifies these facts on a real
rendered projection, and `tests/test_playlists.py` passes unmodified through the new uvicorn server.

## Judgement calls and regressions fixed

- Local mode keeps `webapp.jobs.JobManager` because its exact profile/acquisition/manual-tracklist
  behavior is part of parity and the cycle notes expressly allow it. HTTP handlers only enqueue and
  read state; work runs after the request on the manager's single worker thread. The durable hosted
  worker remains framework-free and is not made to serve HTTP.
- `present/server.py` is not deleted yet. Its stdlib main-server factory has been retired and
  repointed, so there are not two reachable IDea servers, but the module still owns parity fragments,
  rescan queue helpers used by the CLI, compatibility render entry points, and `_Handler` inherited
  by the separate owner truth-review tool. Deleting those safely is deferred; the only `idea serve`
  path is FastAPI/uvicorn.
- The Windows `\\?\` failures from `pytest-975`/`pytest-976` are genuinely fixed, not skipped.
  Production containment normalizes both candidate and root through `native_path` before resolving;
  the test constructs its immutable URL after `_strip_extended_prefix`. The test then performs the
  real GET and asserts 200. The final parity run executed all 12 tests.
- The first local browser gate exposed a missing six-segment immutable-bundle route. It now verifies
  `bundle_id`, the complete bundle manifest, and membership in `manifest.files` before serving.
- The gate's safe synthetic `/gate/probe/present/index.html` path showed that containment—not a SHA
  spelling rule—is the old four-segment local route boundary. A regression test pins safe fixture
  keys while traversal and symlink tests pin the security boundary.

## Verification outputs

All commands ran offline with `IDEA_TEST_MODE=1` where tests or gate helpers can exercise pipeline
code. No real URL was analysed, no live provider was called, and no GC command was run.

### 1. `uv run pytest -q`

```text
1210 passed, 93 deselected, 1 warning in 878.93s (0:14:38)
```

The warning is the existing Python 3.13 removal warning for stdlib `audioop` from `pydub`.

### 2. `uv run ruff check .`

```text
All checks passed!
```

### 3. `uv run ruff format --check .`

```text
290 files already formatted
```

### 4. `uv run python scripts/audit_fixtures.py`

```text
audited 455 files
fixture audit passed
```

### 5. Phase gate

`uv run pytest tests/idea_web/test_parity.py -q`:

```text
12 passed, 1 warning in 6.85s
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`:

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1`:

```text
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-63cfccd7cca84ca0957e1b7932b57178
```

Each gate-owned server was stopped by its script; no `idea serve` or truth-review process was left
running.

### 6. Neighboring compatibility gate

```text
193 passed, 1 warning in 130.86s (0:02:10)
```

### 7. `uv run python scripts/check_page_js.py`

```text
page JavaScript check passed: 53 inline scripts across 22 page renders
```

### 8. Wheel gate

```text
Building source distribution...
Building wheel from source distribution...
Successfully built dist\id_detector-0.1.0.tar.gz
Successfully built dist\id_detector-0.1.0-py3-none-any.whl
```

The exact wheel assertion exited 0 without output. A fresh Python 3.12 venv then installed the wheel
and its installed entry point printed:

```text
Usage: idea [OPTIONS] COMMAND [ARGS]...
Evidence-first DJ-set identification.
```

### 9. Repository state

`git status --short`:

```text
 M pyproject.toml
 M src/id_detector/present/server.py
 M src/idea_web/__init__.py
 M tests/idea_web/test_worker.py
 M uv.lock
?? docs/reviews/build-4a-ii.md
?? src/idea_web/application.py
?? src/idea_web/server.py
?? src/idea_web/templates/
?? tests/idea_web/test_parity.py
```

`git status --short -- work data` produced no output.

`git diff --stat` (Git omits untracked deliverables until they are added):

```text
 pyproject.toml                    |  3 ++
 src/id_detector/present/server.py | 43 +++++++------------
 src/idea_web/__init__.py          |  6 ++-
 tests/idea_web/test_worker.py     |  4 +-
 uv.lock                           | 88 +++++++++++++++++++++++++++++++++++++++
 5 files changed, 113 insertions(+), 31 deletions(-)
```

## Could not do

Nothing in the requested cycle remains undone. No commit, branch, push, live provider request,
real-URL analysis, or real-work-tree GC was performed.

BUILD: COMPLETE

## Review + fix pass (sol xhigh review folded in)

Reviewer-then-fixer: Claude Opus 5, on the uncommitted 4a-ii tree over `19170ea`. The first review was
Codex `gpt-5.6-sol` at xhigh (read-only; it could not run pytest or the PowerShell gates, so its test
claims were inspection-only). Every finding was re-verified here. The HTTP findings were *measured*.
Before anything was retired, the stdlib handler exactly as committed at `19170ea` was loaded and
probed over real HTTP: 67 requests covering GET/HEAD on every route, audio ranges, malformed and
read-only POSTs, oversized and chunked bodies. The same probe then ran against the FastAPI app. The
38 identical and 29 different answers drove the fixes, and the legacy answers are now the pinned
contract (`tests/idea_web/test_legacy_contract.py`).

### Findings, verdicts and fixes

| Tag | Source | Evidence (pre-fix) | Verdict | What changed and why |
|---|---|---|---|---|
| P0-1 web runs the pipeline | sol | `cli.py:871-876` built `make_pipeline_runner` + `JobManager` inside `idea serve`; `application.py:568` → `JobManager.submit`; `webapp/jobs.py:620-624` started the `webapp-jobs` thread, `:657` ran the runner | **Confirmed** — contradicts ADR 0001 ("no FastAPI route, background task, lifespan hook, or thread may run a pipeline") and §4.2 | `idea serve` now enqueues into the 4b-i durable queue (`work/.idea/app.db`) and supervises a separate worker process (architecture below). The web layer holds only a queue adapter; it never constructs or starts a job manager and never imports the runner (asserted in a subprocess) |
| P0-2 GET mutates results | sol | `application.py:261,285` → `legacy._fresh_sets` → `ensure_fresh_page` (`present/server.py:275-282`); canonical GET `application.py:166-170`; `present/refresh.py:38-57` publishes a bundle and moves the pointer; enshrined by `tests/idea_web/test_parity.py:221-233` | **Confirmed** | Every GET serves sealed files as they are. `idea_web.server.refresh_stale_pages` makes older-`PAGE_VERSION` pages current in `LoopbackServer.serve_forever`, *before* uvicorn accepts; the socket is already listening, so a browser that connects meanwhile waits in the backlog. The mutation test was replaced by `test_get_never_publishes_and_the_server_start_makes_stale_pages_current`, which snapshots the whole work tree across GET and HEAD in both modes. `tests/test_stage7_server.py::test_stale_result_page_is_regenerated_on_open` passes **unmodified** through the start-up pass |
| P0-3 two reachable servers | sol | `truth_review.py:705` subclassed `_Handler`; `:764-776` built a `ThreadingHTTPServer` | **Confirmed** | Truth review now runs on FastAPI/uvicorn (`src/idea_web/truth_review.py`) with the same order and protections: Host/Origin gate before any POST, Host check before `/csrf`, token on both mutations, exact media route only, 1 MiB bounded body, exposure written by `TruthReviewSession.reveal_predictions` before any prediction is assembled, the cross-process save lock and the `work/` refusal stay in `TruthReviewSession`. No new path handling. The stdlib handler and server were deleted from both modules. `tests/test_truth_review.py` changed only its import of `serve_truth_review_in_background` (moved entry point); no assertion changed |
| P0-4 unbounded bodies | sol | `application.py:87-95` (`await request.body()` before the 403), `:223-230`, `:597`, `:664`, and also `:497` (read-only `/analyse`) | **Confirmed** | `idea_web.http`: the loopback gate is pure ASGI and drains at most 1 MiB straight off `receive()`; `read_bounded` refuses a declared oversize before buffering and accumulates a chunked body only to the limit; every refusal drains in bounded, discarded increments, then answers with a body. Proven at the ASGI level for 13 POST shapes, plus read-only, `PUT` and truth review: a 100 MiB body is never pulled past 1 MiB + one chunk |
| P0-5 `HEAD /jobs/<id>/status` | sol | measured: legacy 200 JSON with the GET length and an empty body; new 404 `text/plain` | **Confirmed** | All GET routes answer HEAD; `HeadResponseGuard` strips body bytes from the GET answer, so status and headers are identical by construction |
| P1-6 audio read into RAM | sol | `application.py:216-219` read the whole selected range | **Confirmed** | `range_response` streams from disk in 64 KiB chunks (`StreamingResponse`); HEAD and 416 read nothing |
| P1-7 nosniff and cancel ordering | sol | measured: HEAD lacked `X-Content-Type-Options` on home, `/index.html`, job page, playlists, `/playlists/state` and result files; `/jobs/bad/cancel` gave 403 JSON (legacy 404 text) | **Confirmed**, plus three more regressions found by the same probe: a read-only server answered `POST /library/remove` and `/jobs/*/dismiss` with 400 rather than 404 (library removal was reachable, behind CSRF, on a read-only server); read-only `/jobs/*/cancel` answered 403 JSON; read-only `GET /jobs/<id>/status` answered JSON 404 rather than text | HEAD fixed as P0-5. GET and POST dispatch now mirrors the legacy `do_GET`/`do_POST` order exactly: route validated before the token, analyse routes only when analysis is enabled |
| P1-8 parity never ran legacy | sol | `test_parity.py:14,22` imported only the new app | **Confirmed** | Legacy is retired, so its measured answers are pinned (45 cases over real uvicorn HTTP): HEAD status and headers, nosniff, malformed and read-only POST ordering, bounded and oversized bodies, audio ranges. One intended divergence is recorded: legacy ignored chunked bodies |
| P1-9 symlink test passes silently | sol | `test_parity.py:282-286` `except OSError: return` | **Confirmed**, and the new junction test then found a **real containment bug**: `_contained_file` walked `native_path(candidate)`, which calls `resolve()` (`io.py:26`). The walk therefore never saw a symlink or junction component, and `lstat` of that form followed it: a junction aliasing another directory *inside* the work root was served 200 | Inability to symlink is now `pytest.skip` with the reason. New Windows junction test creates real junctions and runs. Containment now does two checks: the resolved target must lie inside the resolved root, and a walk of the *unresolved* spelling must `lstat` no symlink or mount-point component |
| P1-10 hosted advertises audio | sol | `application.py:470-471` resolved audio unconditionally; `webapp/jobs.py:320` emits `audio_url` | **Confirmed** | Hosted mode resolves no job audio and forces `audio_url: null` |
| Scope: 2.5 s poll | sol | edit at `present/server.py:958` | **Confirmed** outside the allowance | Reverted there. The web layer sets the pace on the shared script (`application.py` `_JOB_JS`), raising at import if the script ever stops having exactly one poll to pace |
| P2 cleanup | sol | unused `templates/document.html`; `RunningServer`/`make_server` duplicated in `present/server.py` and `idea_web/server.py`; boundary test only scanned `idea_web/jobs` | **Confirmed** | Wrapper deleted. Lifecycle now lives once in `idea_web/server.py`; `present.server` keeps only lazy compatibility aliases. The boundary test scans `src/id_detector/**` and `src/idea_web/jobs/**` and runs a subprocess proving the worker-side import graph loads no fastapi/starlette/uvicorn |
| XSS (independent check) | this pass | `present/page.py:326-376` `_acquire_links_html` HTML-escaped third-party URLs but never checked the scheme. SoundCloud `purchase_url` is uploader-controlled, so it rendered `href="javascript:…"` on a result page that can fetch `/csrf`. Hardening: `page.py:691-695` "Open the set" link (same); `CONFIG`/`EPISODE_SPANS` JSON in `<script>` not `</`-escaped | **P0, fixed** | `_safe_href` keeps only http(s) URLs with a host (otherwise no chip or link); both script JSON values escape `</`. `PAGE_VERSION` 23 → 24, so the start-up refresh re-renders every existing page once. Everything else was audited as escaped: templates autoescape, and every `\| safe` fragment escapes its own values; page scripts write with `textContent`; truth review escapes and `</`-escapes. **Scope note:** `page.py` is outside the post-Phase-3 `src/id_detector` allowance. It was changed because the brief classes an unescaped path as P0; the orchestrator should ratify |

No sol finding was judged wrong. Confirmed independently:

- No `fastapi`/`starlette`/`uvicorn` import anywhere in `src/id_detector/` or `src/idea_web/jobs/`, checked both as text and at runtime.
- Dependencies added are only `fastapi>=0.116.1`, `jinja2>=3.1.6` and `uvicorn>=0.35.0`. `python-multipart` is not needed: forms are URL-encoded and parsed under the bounded reader.
- Templates and migrations are in the wheel (assertion below).
- `tests/test_phase0a_security.py` and `tests/test_playlists.py` are unmodified (`git diff` empty) and pass.

The coordinator's retro-review awareness points are respected:

- Every write the worker makes on a claimed job goes through the fenced `JobQueue` API: `claim`, `update_progress` (which also requires an unexpired lease), `fail` and `terminal`. `terminal` only gains an optional `progress` in the same fenced UPDATE.
- The two new queue methods touch no claimed work. `cancel_unclaimed` requires `state='intake' AND run_id IS NULL AND claim_token IS NULL`; `dismiss` only touches terminal rows.
- The local worker writes no pointer or index itself. The unchanged local runner publishes exactly as it did in local mode, and the mode flag flows through `LocalCheckpointStore`.
- Lease expiry inside the 4b-i run fences (retro 4b-i P0) is **not** fixed here; that is its follow-up cycle.

### Final local-mode architecture

- **Web process** (`idea serve` → uvicorn → `idea_web.application`). It validates a submission, then `LocalJobs.submit` calls `JobQueue.enqueue`. That writes a `jobs` row in `<work root>/.idea/app.db` with state `intake` and the full job snapshot in `progress`. Pages and `/jobs/<id>/status` rebuild the same `webapp.jobs.Job` object from that row, so the job page, home cards, 2.5 s polling, ETA and the progress bar compute exactly as before. Cancelling a queued job settles it at once (`cancel_unclaimed`); a claimed job gets `request_cancel`. Dismissal applies to terminal jobs only. The web process never imports `id_detector.webapp.runner` or `id_detector.pipeline` (subprocess-asserted).
- **Worker process** (`python -c "from idea_web.jobs.local import main; …"`). It claims with the fenced 30 s lease and runs the owner's unchanged local runner (`make_pipeline_runner`: profiles, Deep, acquisition, reference index, pasted tracklist) through the unchanged `JobManager._execute` state machine. So waiting, failure, cancellation and spend mean what they meant. Every 0.5 s it publishes a changed snapshot through `update_progress` (which also renews the lease). It relays a cancel request to the run's cancel flag within 0.5 s, and settles with `terminal(progress=…)`.
- **How `idea serve` supervises it** (`LocalWorkerSupervisor`):
  - One worker per work root, held by a named-mutex `ProcessLock`; a second window leaves supervision to the first.
  - On start, it stops anything a crashed or closed server left in flight rather than silently resuming paid work: closing ID'er has always stopped its analyses.
  - If the worker dies, it restarts it with 1 → 30 s backoff.
  - On stop, it cancels in-flight work, closes the worker's stdin pipe, waits up to 15 s, then kills.
  - The worker's lifetime is tied to the server four ways: a Windows kill-on-close job object, stdin EOF, a parent-PID watchdog and a 30 s exit guard. The worker runs in its own process group, so Ctrl-C reaches the server, which then stops the worker in order.
  - Proven by `test_idea_serve_supervises_a_separate_worker_process` (real child, restart after kill), `test_the_worker_never_outlives_a_server_that_is_killed`, and a real-CLI probe: `idea serve` started its worker; `Stop-Process` on the server process alone took the worker with it; 0 processes left.
- **How a stale page becomes current without a GET mutation:** the start-up refresh pass above.
- **Is `present/server.py` retired?** Its HTTP server is. The `_Handler` class, `ThreadingHTTPServer` and the in-module `make_server`/`RunningServer` are deleted, and nothing reachable uses the stdlib server. The module remains as the presentation fragments and rescan-queue helpers that the FastAPI pages render from, plus three lazy compatibility names. Tests that must not be modified import from it (`test_phase0a_security.py:25`), as do other test and CLI callers (`test_stage10_webapp.py:19`, `test_phase3a_honesty.py:26`, `test_projection.py:19`, `cli.py:59`). ADR 0001 keeps compatibility code "when a caller or test depends on it". `webapp/jobs.py` likewise remains as the job state machine the worker drives, and as a test double; the web layer neither owns nor starts it.
- **What the owner could notice:**
  1. The first launch after this change re-renders every existing result page once (security fix, `PAGE_VERSION` 24), so that one start takes longer.
  2. A job's page survives a server restart as a cancelled record instead of 404.
  3. A `file://` URI typed into the form is refused on the form rather than failing as a job.
  4. An unsupported HTTP method gets a short text 501.
  No configuration is required, and `idea.cmd` is unchanged.

### Residual notes (not blocking)

- P2: the web process imports `idea_web.jobs.worker` for `JobQueue`. That module imports decode, ingest and hints at import time, although the web never calls them and the pipeline and runner modules stay unloaded. Moving `JobQueue` to its own module would make the boundary structural.
- P2: `GET /media/<key>/audio` uses the existing cached-open `_load_cached`, which may rebuild the media index cache `work/index.json`. That is a cache, not a result, bundle or pointer, and it is pinned by `test_phase1a_cached_open.py` (warm-index and unwritable-root cases).
- Test infrastructure: the shared seed fixtures (`tests/test_stage7_server.py:35`) use plain `Path.mkdir`, so under a very long `--basetemp` they cannot create the hashed result directories (MAX_PATH), and ten parity tests fail inside the fixture before any application code runs. Under the default and a short basetemp all 35 pass. Not a product defect; worth knowing before re-running with a custom basetemp.

### Playlist guarantees against the new server

1. `id="<episode_id>"` on every shown `<tr>`: `present.page._track_row_html` is untouched and results are served byte-for-byte.
2. `data-candidate-id` + `data-episode-id` via `row_actions_html`: untouched.
3. `PLAYLIST_CSS`/`PLAYLIST_JS` injected at both points: untouched.
4. Controls only on shown rows, hidden on `file://`: untouched.
5. `/{source_key}/{media_key}/present/index.html` URL shape: served read-only by the canonical result route.

Proven by `test_result_rows_keep_all_five_playlist_injection_guarantees` and `test_the_canonical_result_url_shape_is_served`. The playlist POST routes sit behind the pure-ASGI Origin/Host gate and then the synchroniser token (`test_playlists_get_post_and_guards`, the bounded-body cases). `tests/test_playlists.py` is unmodified and passes.

### Files changed in this pass

- `src/idea_web/http.py` (new): gate, HEAD guard, bounded reader and drain, containment, streamed ranges.
- `src/idea_web/application.py` (rewritten): legacy dispatch order, no GET mutation, queue adapter, hosted audio policy.
- `src/idea_web/server.py` (rewritten): the one lifecycle, `require_loopback`, `refresh_stale_pages`.
- `src/idea_web/truth_review.py` (new): FastAPI port.
- `src/idea_web/jobs/local.py` (new): `LocalJobs`, `LocalWorker`, `LocalWorkerSupervisor`, `main`.
- `src/idea_web/jobs/worker.py`: `enqueue(progress=)`, `terminal(progress=)`, `cancel_unclaimed`, `dismiss`.
- `src/idea_web/__init__.py`: lazy, so the worker imports no framework.
- `src/idea_web/templates/document.html` (deleted) and `templates/README.md`.
- `src/id_detector/cli.py` (`serve`, `truth review`), `src/id_detector/present/server.py` (handler retired, poll pace restored), `src/id_detector/present/__init__.py` (lazy `RunningServer`), `src/id_detector/truth_review.py` (handler retired), `src/id_detector/present/page.py` (XSS).
- Tests:
  - `tests/idea_web/test_parity.py` (rewritten; 35 tests).
  - `tests/idea_web/test_legacy_contract.py` (new; 45).
  - `tests/idea_web/test_local_queue.py` (new; 9) and `tests/idea_web/local_runner_fakes.py`.
  - `tests/idea_web/test_worker.py` (boundary test strengthened).
  - `tests/test_truth_review.py` (import only).

### Gate outputs (this pass; all offline, `IDEA_TEST_MODE=1`, no provider call, no real URL, no GC)

- `uv run pytest -q`: `1287 passed, 93 deselected, 1 warning in 482.47s (0:08:02)` (the warning is the existing pydub `audioop` deprecation).
- `uv run pytest tests/idea_web/test_parity.py -q`: `35 passed, 1 warning in 8.63s`.
- Neighbour set (`test_playlists`, `test_truth_review`, `test_stage7_server`, `test_stage10_webapp`, `test_phase0a_security`, `test_phase3a_honesty`, `test_projection`, `test_golden_local_free`, `idea_web/test_worker`, `test_service_api`): `193 passed, 1 warning in 138.89s`.
- New suites: `tests/idea_web/test_legacy_contract.py` `45 passed`; `tests/idea_web/test_local_queue.py` 9 passed (inside the full run).
- `uv run python scripts/check_page_js.py`: `page JavaScript check passed: 53 inline scripts across 22 page renders`.
- `uv run ruff check .`: `All checks passed!`. `uv run ruff format --check .`: `300 files already formatted`.
- `uv run python scripts/audit_fixtures.py`: `audited 459 files` / `fixture audit passed`.
- `scripts/smoke_serve.ps1`: `smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'` (analyse mode, so the supervised worker was started and stopped).
- `scripts/gate_local_mode.ps1`: `local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok`.
- Real-CLI supervision probe (`uv run idea serve --no-open`, nothing analysed): `worker pid 78028 (python.exe); parent pid 20296 (python.exe) is the server: True` / `durable local queue created: .ideapp.db` / `server killed without cleanup; worker exited with it (no orphan)` / `probe processes left running: 0`.
- `uv build`: sdist and wheel built. The wheel assertion confirmed `idea_web/{__init__,application,http,server,truth_review,database}.py`, `idea_web/jobs/{worker,local}.py`, the three templates and both migration scripts are present and `templates/document.html` is absent (142 entries). A fresh Python 3.12 venv installed the wheel and `idea --help` printed `Usage: idea [OPTIONS] COMMAND [ARGS]...` / `Evidence-first DJ-set identification.`
- `git status --short -- work data`: empty. No `idea serve`, truth-review, worker or uvicorn process left running (`Get-CimInstance Win32_Process` after the gates).
- Not committed, branched or pushed.
