### [P1] Duplicate WAVs violate cache-key uniqueness and can corrupt observations

What: Two identical windows—silence is a common example—produce the same clip `cache_key`, but `build_queries()` emits separate queries/jobs. Both submit, overwrite the same raw file, and create raw-index records with the same natural ID but different `query_id`s. Revision 5 requires `cache_key` to be unique per media key.

Where: [recognise.py:113](C:/Users/natha/Documents/Music/id-detector/src/id_detector/recognise.py:113), [recognise.py:158](C:/Users/natha/Documents/Music/id-detector/src/id_detector/recognise.py:158), [PLAN.md:67](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:67)

Fix: Deduplicate cache fetches by `cache_key` and deterministically fan the cached response out to each equivalent window’s observation. Add repeated/silent-window coverage asserting one physical query and distinct, correctly timed observations.

### [P1] Refresh double-charges historical physical attempts

What: `reset_for_refresh()` deliberately retains `physical_attempts`, but `finish()` adds the lifetime total to `budgets.used_requests`. After two one-attempt executions of one query, the budget records three requests; repeated refreshes can exceed the hard ceiling without corresponding network traffic. The plan requires every physical attempt to be charged exactly once.

Where: [jobs.py:397](C:/Users/natha/Documents/Music/id-detector/src/id_detector/jobs.py:397), [jobs.py:400](C:/Users/natha/Documents/Music/id-detector/src/id_detector/jobs.py:400), [jobs.py:486](C:/Users/natha/Documents/Music/id-detector/src/id_detector/jobs.py:486), [PLAN.md:81](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:81)

Fix: Increment budget usage by the current reservation/delta, while retaining a separate cumulative job total. Test the budget after multiple refreshes and after a refresh that performs no network attempt.

### [P1] The worker can lease obsolete provider-config jobs and crash

What: `lease_next()` considers every pending job in the database. Recognition then indexes the result in the current config’s `query_by_id`; a pending query from v1 during a v2 run raises `KeyError`. The report explicitly acknowledges multiple provider-config generations in one database, and crash recovery can leave old queries pending.

Where: [jobs.py:229](C:/Users/natha/Documents/Music/id-detector/src/id_detector/jobs.py:229), [recognise.py:339](C:/Users/natha/Documents/Music/id-detector/src/id_detector/recognise.py:339), [test_stage1_shazam.py:142](C:/Users/natha/Documents/Music/id-detector/tests/test_stage1_shazam.py:142)

Fix: Scope leasing to the active media/provider/query set or explicitly supersede old pending jobs. Extend the regression with an old pending—not terminal—query.

### [P1] Refresh and config changes overwrite immutable artifacts

What: Queries, raw responses, raw index, and observations are rewritten in place. `--refresh` reuses the same cache key and replaces its raw response; selecting a newer provider config replaces `queries.gen0.jsonl`. Revision 5 calls all of these stage-owned immutable artifacts, so historical job/evidence provenance is lost.

Where: [recognise.py:267](C:/Users/natha/Documents/Music/id-detector/src/id_detector/recognise.py:267), [recognise.py:279](C:/Users/natha/Documents/Music/id-detector/src/id_detector/recognise.py:279), [recognise.py:304](C:/Users/natha/Documents/Music/id-detector/src/id_detector/recognise.py:304), [recognise.py:394](C:/Users/natha/Documents/Music/id-detector/src/id_detector/recognise.py:394), [PLAN.md:50](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:50)

Fix: Introduce an immutable invocation/config/refresh namespace or refuse content-changing replacement. Preserve prior raw responses and query/observation sets with complete sidecars.

### [P1] `L_min_ms` is not a latency distribution

What: The calibration takes percentiles of the input material lengths among successful cases. Even if every duration succeeds, the fixed grid itself forces the upper percentile toward 12 seconds. Failures are discarded, and the live test only checks counts. Thus the committed “measured” configs do not contain the latency quantiles required by the plan.

Where: [calibration.py:238](C:/Users/natha/Documents/Music/id-detector/src/id_detector/calibration.py:238), [test_stage1_live.py:26](C:/Users/natha/Documents/Music/id-detector/tests/test_stage1_live.py:26), [PLAN.md:121](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:121)

Fix: Predefine a latency estimator over independent insertion positions, including censored failures, and test it against controlled success/failure matrices. Regenerate v1/v2—or mark them unmeasured—after correction.

### [P1] Startup recovery can steal a live submission

What: Opening a second store immediately changes every `submission_started` row to `outcome_unknown`, regardless of a fresh heartbeat. The first process then cannot commit `submitted`. Elsewhere, ingest also deletes every matching temporary directory at startup. Revision 5 requires one process/writer and reclaim only after the stale threshold; the implementation neither enforces exclusivity nor respects it here.

Where: [jobs.py:157](C:/Users/natha/Documents/Music/id-detector/src/id_detector/jobs.py:157), [ingest.py:162](C:/Users/natha/Documents/Music/id-detector/src/id_detector/ingest.py:162), [decode.py:93](C:/Users/natha/Documents/Music/id-detector/src/id_detector/decode.py:93), [PLAN.md:81](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:81)

Fix: Enforce a work/media process lock, apply the 2×lease stale test to recovery, and use invocation-owned unique temporary paths rather than deleting possibly active work.

### [P1] Windows Ctrl-C can remain blocked in the executor

What: `WaitForSingleObject` is placed in `asyncio.to_thread()` with the entire subprocess timeout, up to two hours. Cancelling the coroutine does not cancel that worker thread; closing a handle while another thread waits on it is also unsafe. The timeout tests let the wait expire naturally and do not test task cancellation. The report admits the live “Ctrl-C” was actually an abrupt process kill, so the Stage 1 Ctrl-C gate remains unmet.

Where: [process.py:170](C:/Users/natha/Documents/Music/id-detector/src/id_detector/process.py:170), [process.py:186](C:/Users/natha/Documents/Music/id-detector/src/id_detector/process.py:186), [test_stage1_process.py:14](C:/Users/natha/Documents/Music/id-detector/tests/test_stage1_process.py:14), [stage-1.md:232](C:/Users/natha/Documents/Music/id-detector/docs/stage-reports/stage-1.md:232), [PLAN.md:183](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:183)

Fix: Use cancellable short waits or a registered asynchronous Win32 wait. Test cancellation of a long-running downloader tree and require prompt CLI return plus descendant termination.

### [P1] Credential-bearing URLs are persisted verbatim

What: Arbitrary URL queries are retained in `canonical_url`, the original input is stored in `source.json`, and the complete URL is copied into the invocation journal command. A signed URL or `api_key`/OAuth query therefore violates the plan’s no-secrets-in-artifacts rule despite the existing redaction helper.

Where: [ingest.py:76](C:/Users/natha/Documents/Music/id-detector/src/id_detector/ingest.py:76), [ingest.py:239](C:/Users/natha/Documents/Music/id-detector/src/id_detector/ingest.py:239), [cli.py:55](C:/Users/natha/Documents/Music/id-detector/src/id_detector/cli.py:55), [PLAN.md:53](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:53)

Fix: Reject credential-bearing URLs or persist only a sanitized public form; redact invocation arguments before journaling. Add query-token and URL-userinfo tests.

### [P1] The privacy audit omits the newly added fixtures

What: `tests/fixtures/shazam/*.json` is not under any `SCAN_ROOTS` entry, so the audit reports success without reading the new committed fixtures. The files inspected here are synthetic, but revision 5 requires every committed fixture, truth, and report to be audited.

Where: [audit_fixtures.py:13](C:/Users/natha/Documents/Music/id-detector/scripts/audit_fixtures.py:13), [test_fixture_audit.py:10](C:/Users/natha/Documents/Music/id-detector/tests/test_fixture_audit.py:10), [PLAN.md:198](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:198)

Fix: Include `tests/fixtures` in the audit roots and add a test proving a forbidden identifier placed there is rejected.

### [P2] The fake-server test does not exercise the retry owner

What: The test manually invokes `adapter.recognize_once()` twice. It proves the injected client performs one request per call, but not that the job executor owns retry/backoff and records both attempts, as claimed in the report.

Where: [test_stage1_shazam.py:246](C:/Users/natha/Documents/Music/id-detector/tests/test_stage1_shazam.py:246), [PLAN.md:81](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:81)

Fix: Run one recognition job against the fake server, let `_run_job` perform the retry, then assert server count, job count, terminal state, and retry limit.

### [P2] The claimed long-path test does not cross `MAX_PATH`

What: The test appends only 90 characters and never asserts that the resolved path exceeds 260 characters. It also does not exercise the longest paths—raw cache files, SQLite, ingest, or subprocess arguments—so the Windows path acceptance claim is unsupported.

Where: [test_stage1_windows.py:30](C:/Users/natha/Documents/Music/id-detector/tests/test_stage1_windows.py:30), [PLAN.md:183](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:183)

Fix: Construct and assert an absolute path longer than 260 characters on Windows, then exercise ingest, decode, windows, raw cache, SQLite, and cleanup.

### [P2] Runtime resources disappear from an installed wheel

What: The CLI derives the repository root from `__file__`, and the job store reads `docs/schemas/jobs.sql` from that root. The wheel configuration packages only `src/id_detector`, so an installed wheel lacks both the SQL schema and measured provider configs.

Where: [cli.py:24](C:/Users/natha/Documents/Music/id-detector/src/id_detector/cli.py:24), [jobs.py:96](C:/Users/natha/Documents/Music/id-detector/src/id_detector/jobs.py:96), [pyproject.toml:35](C:/Users/natha/Documents/Music/id-detector/pyproject.toml:35)

Fix: Package runtime resources inside `id_detector` and load them with `importlib.resources`; add a built-wheel smoke test.

## Verified

- All three exact `uv run …` commands exited 1 before executing because the sandbox could not launch the WinGet-linked `uv.exe`: `No application is associated with the specified file for this operation`.
- Direct `.venv\Scripts\python.exe -m ruff check .`: `All checks passed!`
- Direct pytest could not start because the read-only environment has no usable temporary directory. Collection succeeded: `124/125 tests collected (1 deselected)`.
- Direct doctor exited 1: Python, yt-dlp, Node, VC++ runtime, and disk passed; sandbox PATH/temp restrictions caused uv, ffmpeg, ffprobe, and signature checks to fail.
- Direct fixture audit printed `audited 75 files` and `fixture audit passed`, subject to the omitted `tests/fixtures` finding above.
- Final `git status --short` matched the initial status; nothing was modified.

REVIEW VERDICT: FIX_FIRST