# Stage 10 — Browser-driven web app

*docs/PLAN.md rev 5.2, build-order row 10. Lets the owner (a non-programmer) run the whole pipeline
from the browser: double-click a launcher, the browser opens, paste a mix URL, click **Analyse**,
watch live progress, and land on the Stage 7 result page with click-to-seek and acquire links — all
`127.0.0.1`-only, without touching the CLI again after starting the server. The CLI `analyse` path is
unchanged.*

## What was built (file map)

**Optional pipeline progress hook (task 1) — `src/id_detector/cli.py`, `recognise.py`:**
- `cli._analyse` and `cli._acquire` gain an optional `progress: ProgressFn | None = None`
  (`ProgressFn = Callable[[str, int, int, str], None]`, i.e. `(phase, done, total, message)`).
  Phases reported: `ingest`, `decode`, `windows`, `recognise` (with done/total windows), `hints`,
  `fuse`, `present` (and `enrich` from `_acquire`). A tiny `_report(...)` helper makes every call a
  no-op when `progress is None`, so the CLI path is byte-for-byte unchanged and zero-overhead.
- `recognise.recognise_generation` gains `on_window: Callable[[int, int], None] | None = None`,
  called once per resolved query window (cache hits folded in, then each leased job) — a pure UI
  signal that never touches artefacts, the budget, or request accounting. `_analyse` wires it into
  both the gen-0 and rescan recognitions and only when a `progress` hook is present.
- A `progress` hook may raise `asyncio.CancelledError`; `_analyse`'s existing cancellation handler
  then records a clean `cancelled` invocation and unwinds its locks/DB owner. No artefact contract or
  CLI output changed.

**Job runner (task 2) — `src/id_detector/webapp/jobs.py`:**
- `JobManager`: an in-process, **single** background-thread queue (one job at a time to respect the
  Shazam rate limit; concurrency capped at one worker). `submit(target, profile, acquire=,
  build_index=)` validates and returns a job id; `get`, `recent`, `cancel`, `shutdown`.
- `Job`: in-memory state keyed by id — `status` (`queued|running|succeeded|failed|cancelled`),
  live `phase`/`phase_done`/`phase_total`, `windows_done`/`windows_total`, a bounded ring-buffer
  `log` (200 lines), `result_path`, timestamps, and `status_dict()` (a JSON-safe snapshot with an
  ETA computed from remaining windows at 18 req/min). State survives a page reload; `recent()` lists
  recent jobs.
- `JobContext` is what a runner uses: `progress()` (also a cancellation point), `log()`,
  `check_cancel()`, `set_result()`. Cancellation of a running job is observed at the next progress
  tick (raises `JobCancelled(asyncio.CancelledError)`); a queued job is cancelled immediately.
- **No secrets in state/logs:** the target and every log line pass through `io.redact_text`; the
  runner emits only coarse phase messages (no usernames, comment text, or provider responses).
- `validate_target` (server-side URL validation): accepts `http`/`https` (rejecting
  credential-bearing URLs) or a local file the owner passes (a `file://` URI or an existing path,
  including Windows `C:\…`); rejects everything else.
- The manager takes an injected `runner`, so tests stub the whole pipeline.

**Real pipeline runner (task 2/4) — `src/id_detector/webapp/runner.py`:**
- `make_pipeline_runner(work_root, project_root, config_path)` returns a runner that runs exactly
  what the CLI runs: optional `build-index` → `cli._analyse` (progress-wired) → optional
  `cli._acquire` → records the finished `present/index.html`. `_resolve_settings` mirrors the
  `analyse` command's config+profile precedence. `build-index` is best-effort (needs JDK/Panako +
  network) and logged-but-non-fatal so it only augments the audio analysis.

**Server routes (task 3) — `src/id_detector/present/server.py`:**
- `make_server` / `serve_in_background` gain an optional `job_manager`. With one, the home page
  becomes the analyse app and the new routes turn on; without one the server stays the read-only
  Stage 7 index (so existing Stage 7 tests and `serve --no-analyse` are unchanged). Still loopback
  only (a routable host is refused).
- `GET /` — home: the form (mix URL, profile `<select>` free/max_accuracy, "also fetch acquire
  links", "build reference index first"), a list of recent/running jobs with status, and links to
  finished result pages. Self-contained inline HTML/CSS/JS; no usernames or comment text.
- `POST /analyse` — form or JSON `{url, profile, acquire, build_index}` → validates → submits a job
  → **303 redirect** to `/jobs/<id>` (a JSON request gets `{"id","location"}` instead). Bad URL /
  unknown profile → 400.
- `GET /jobs/<id>` — live progress page (phase, a windows-done/total bar, log tail, ETA at the rate
  limit) that polls `GET /jobs/<id>/status` every ~2 s and links/loads the result when succeeded.
- `GET /jobs/<id>/status` — JSON snapshot. `POST /jobs/<id>/cancel` — cancel.
- The existing read-only `/present/…` serving and `POST /rescan` are unchanged.

**CLI + launcher (task 4) — `src/id_detector/cli.py`, `id-detector.cmd`, `README.md`:**
- `id-detector serve` gains `--analyse/--no-analyse` (default **--analyse**) and `--open/--no-open`
  (default **--open**, opens the browser after the server is listening; a browser failure never stops
  the server), plus `--config`. On shutdown it joins the `JobManager` worker so nothing hangs.
- `id-detector.cmd` (repo root, CRLF): a double-click launcher that `cd`s to the repo and runs
  `uv run id-detector serve --open`.
- `README.md` "Quick start" now leads with the browser flow (double-click `id-detector.cmd` →
  browser opens → paste a URL → Analyse), with the CLI kept as an optional alternative.

**Tests (task 5) — `tests/test_stage10_webapp.py` (23 tests, network-free):**
- Job manager state machine with a fake analyse function: queued→running→succeeded, failure recorded
  (not raised), one-at-a-time queue, cancel (queued and running), progress/window updates, and
  no-secrets-in-status (a credential URL in a log line is redacted).
- `validate_target` accepts http(s)/local file, rejects non-http/file/credentialed/empty.
- Server routes with the fake runner: home renders the form, `POST /analyse` (JSON→id, form→303),
  `/jobs/<id>/status` shape, loopback-only refusal, URL/profile validation → 400, cancel route.
- Home and progress pages contain no usernames/identifier fields — reusing the fixture-audit
  `_HANDLE` / `_ID_FIELD` patterns (style block excluded, as Stage 7 does).
- Every test tears its manager (and server) down with a bounded join and asserts no `webapp-jobs`
  worker thread survives. One `pytest.mark.live` full analyse was deliberately **not** added — the
  real pipeline is covered by the CLI stages; here only the web plumbing is proven with a stub.

## Verification

```
uv run pytest -q            → 507 passed, 93 deselected, 1 warning
uv run pytest tests/test_stage10_webapp.py -q → 23 passed
uv run ruff check .          → All checks passed!
uv run ruff format --check <stage-10 files> → 7 files already formatted
uv run python scripts/audit_fixtures.py → audited 345 files; fixture audit passed (exit 0)
```

**Fast live smoke** (stubbed short job via `serve_in_background`, no real analyse, no network): two
jobs submitted; the second observed behind the first:

```
GET /                -> 200; analyse form present=True
POST /analyse (A/B)  -> two job ids
job B status -> queued     phase=queued     windows=0/0 eta=0s
job B status -> running    phase=recognise  windows=1/4 eta=10s
job B status -> succeeded  phase=done       windows=4/4 eta=0s
job B transitions    -> queued -> running -> succeeded
GET B result page    -> 200
job A final status   -> succeeded; result=/src/mix-a/present/index.html
server thread alive  -> False;  worker thread alive -> False;  clean shutdown -> OK
```

## Guarantees confirmed
- **127.0.0.1-only:** `make_server` refuses any non-loopback host (unchanged); the web app adds no
  new bind. Tested by `test_make_server_only_binds_loopback`.
- **No secrets in job state/logs:** target and log lines are `redact_text`-ed; the runner emits only
  coarse phase messages. Tested by `test_progress_updates_are_visible_and_secret_free`.
- **No hung threads/processes:** the single daemon worker is joined on `JobManager.shutdown`, which
  cancels any in-flight job; `serve` joins it on teardown. Every test asserts the worker is gone.
- **CLI `analyse` path unchanged:** the progress hook defaults to `None` (no-op, zero overhead); no
  artefact contract, command option, or output changed. Full suite (507) green.

## Notes / out of scope
- Five pre-existing Stage 8 Panako files (`scripts/setup_panako.py`, `src/id_detector/candidates.py`,
  `providers/panako.py`, `providers/panako_setup.py`, `tests/test_stage8_panako.py`) are not
  ruff-format-clean in the inherited tree; none were touched here and they are left as-is. All
  Stage 10 files are formatted and `ruff check .` passes repo-wide.
- The `build reference index first` path and a real end-to-end analyse are network/JDK-bound and so
  are exercised only through the CLI stages / live use, not the default test suite.
