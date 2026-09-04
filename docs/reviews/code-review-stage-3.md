### [P1] AudD manual retry cannot preserve unknown billing exposure

**What:** The plan requires `outcome_unknown` reservations to remain conservatively charged and permits resubmission only through `retry --acknowledge-billing`. `acknowledge_retry()` preserves the original reservation, but AudD unconditionally reserves the same units again. With the shortlist’s exact-size budget, retry raises `BudgetExhausted`; with a manually enlarged budget, `finish()` removes both reservations while charging only the second result, erasing the possible first charge.

**Where (file:line):** [src/id_detector/providers/audd.py:376](C:/Users/natha/Documents/Music/id-detector/src/id_detector/providers/audd.py:376), [src/id_detector/jobs.py:579](C:/Users/natha/Documents/Music/id-detector/src/id_detector/jobs.py:579), [src/id_detector/jobs.py:680](C:/Users/natha/Documents/Music/id-detector/src/id_detector/jobs.py:680), [src/id_detector/benchmark/shortlist.py:371](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/shortlist.py:371)

**Fix:** Record unknown exposure separately or settle it conservatively before creating a new attempt/reservation. Make reservation idempotent across crashes, provide an acknowledgement-gated budget extension, and test both crash-after-reservation and manual AudD retry.

### [P1] Scanner cache keys are generated but never used as caches

**What:** Revision 5 requires file-scan cache keys, 180-day positive caching, 30-day `no_match` caching, errors never cached, and `--refresh`. Scanner execution always leases the existing job. After a successful/no-match run, the terminal job is not leaseable, so rerunning the paid shortlist with the same work root fails with “scanner job is not runnable” instead of using the cached raw response. There is no scanner `--refresh` path.

**Where (file:line):** [src/id_detector/benchmark/shortlist.py:362](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/shortlist.py:362), [src/id_detector/benchmark/shortlist.py:379](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/shortlist.py:379), [src/id_detector/jobs.py:345](C:/Users/natha/Documents/Music/id-detector/src/id_detector/jobs.py:345)

**Fix:** Reuse the common cache-validity logic before leasing, reconstruct observations from valid raw cache entries, reset only expired/explicitly refreshed terminal jobs, expose `--refresh`, and add positive/no-match/error rerun tests.

### [P1] ACRCloud’s 48-hour ceiling resets after every restart

**What:** The plan caps scanner polling at 48 hours. `poll()` starts a new monotonic timer on every invocation, while recovery resumes the remote ID through another fresh invocation. A repeatedly restarted job can therefore poll indefinitely. A timeout also remains in `submitted` rather than reaching a defined terminal state. The scanner runner additionally omits the plan’s 15-second heartbeat around potentially multi-hour work.

**Where (file:line):** [src/id_detector/providers/acrcloud.py:463](C:/Users/natha/Documents/Music/id-detector/src/id_detector/providers/acrcloud.py:463), [src/id_detector/providers/acrcloud.py:479](C:/Users/natha/Documents/Music/id-detector/src/id_detector/providers/acrcloud.py:479), [src/id_detector/providers/acrcloud.py:514](C:/Users/natha/Documents/Music/id-detector/src/id_detector/providers/acrcloud.py:514), [src/id_detector/benchmark/shortlist.py:371](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/shortlist.py:371)

**Fix:** Persist an absolute polling deadline derived from the first acknowledged submission, preserve backoff/deadline across recovery, heartbeat during execution, and terminalize an expired scan with conservative billing.

### [P1] Known provider failures remain active or become `outcome_unknown`

**What:** Revision 5 distinguishes acknowledged/known failures from lost responses. AudD HTTP failures and ACRCloud HTTP, malformed-response, and remote states `-2/-3` raise `ProviderProtocolError`, but the executors only handle ambiguous response loss. Jobs remain leased in `submission_started`/`submitted`; after recovery, AudD can become `outcome_unknown` despite receiving a response, while ACRCloud repeatedly polls an already failed remote job.

**Where (file:line):** [src/id_detector/providers/audd.py:319](C:/Users/natha/Documents/Music/id-detector/src/id_detector/providers/audd.py:319), [src/id_detector/providers/audd.py:383](C:/Users/natha/Documents/Music/id-detector/src/id_detector/providers/audd.py:383), [src/id_detector/providers/acrcloud.py:401](C:/Users/natha/Documents/Music/id-detector/src/id_detector/providers/acrcloud.py:401), [src/id_detector/providers/acrcloud.py:475](C:/Users/natha/Documents/Music/id-detector/src/id_detector/providers/acrcloud.py:475), [src/id_detector/providers/acrcloud.py:566](C:/Users/natha/Documents/Music/id-detector/src/id_detector/providers/acrcloud.py:566)

**Fix:** Map acknowledged provider rejections and terminal remote states to `permanent_failure` or an explicit retryable state, reconcile billing, and reserve `outcome_unknown` exclusively for genuinely ambiguous submissions.

### [P1] Provider error bodies bypass credential redaction

**What:** The plan requires secrets to be redacted from logging. Both adapters embed up to 500 characters of the provider response in exceptions, and the shortlist CLI prints those exceptions directly instead of using the existing redactor. An endpoint or proxy that echoes multipart parameters or authorization data can expose credentials in console output.

**Where (file:line):** [src/id_detector/providers/audd.py:319](C:/Users/natha/Documents/Music/id-detector/src/id_detector/providers/audd.py:319), [src/id_detector/providers/acrcloud.py:401](C:/Users/natha/Documents/Music/id-detector/src/id_detector/providers/acrcloud.py:401), [src/id_detector/cli.py:478](C:/Users/natha/Documents/Music/id-detector/src/id_detector/cli.py:478)

**Fix:** Omit provider bodies or pass all exception text through `redact_text()` before display. Add an error-response test containing each supported secret field.

### [P2] Union coverage collapses repeated occurrences

**What:** The plan requires one-to-one, temporally compatible association per occurrence. `_work_sets()` discards occurrence indices and timing, then `_union_coverage()` marks every truth occurrence covered whenever that work label appears anywhere in the set. One predicted occurrence can therefore cover multiple repeats.

**Where (file:line):** [src/id_detector/benchmark/shortlist.py:513](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/shortlist.py:513), [src/id_detector/benchmark/shortlist.py:548](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/shortlist.py:548)

**Fix:** Reuse the scorer’s one-to-one occurrence association, or retain occurrence/timing in the union calculation. Add a vector with two truth occurrences and one prediction.

### [P2] Headline shortlist properties are not exercised by tests

**What:** The “no cascade” test only parses the committed report, so broken orchestration would still pass. The failure tests use a fake adapter and do not assert physical-attempt totals across the five recovery points. The entitlement “upload limit” check only proves the tiny probe is below the local constant.

**Where (file:line):** [tests/test_stage3_shortlist.py:15](C:/Users/natha/Documents/Music/id-detector/tests/test_stage3_shortlist.py:15), [tests/test_stage3_providers.py:353](C:/Users/natha/Documents/Music/id-detector/tests/test_stage3_providers.py:353), [tests/test_stage3_entitlements.py:74](C:/Users/natha/Documents/Music/id-detector/tests/test_stage3_entitlements.py:74)

**Fix:** Inject engine runners and verify every eligible engine executes independently; exercise recovery through mocked HTTP transport while asserting cache-key filenames and physical attempts; add local boundary tests for upload size, cache TTLs, retry billing, and the persistent poll deadline.

### [P2] Windows file preflight can reject long paths and miscount attempts

**What:** The project provides `native_path()` specifically for Windows long paths, but both adapters first call `Path.is_file()`. AudD also increments `physical_attempts` before opening the file, so a sharing violation or deletion race records a network attempt even though no transport I/O occurred.

**Where (file:line):** [src/id_detector/providers/audd.py:301](C:/Users/natha/Documents/Music/id-detector/src/id_detector/providers/audd.py:301), [src/id_detector/providers/acrcloud.py:445](C:/Users/natha/Documents/Music/id-detector/src/id_detector/providers/acrcloud.py:445)

**Fix:** Use the project’s native-path-aware file checks, open the file before counting the attempt, and add Windows long-path and sharing-violation tests.

## Verified

- `git status --short`: 8 modified tracked files and 28 untracked files; no staged changes. Status remained unchanged after review.
- All 28 untracked files were read. Provider-config packaged copies match their root copies, and all three completion-sidecar SHA-256 values match.
- `uv run pytest -q`, `uv run ruff check .`, and `uv run id-detector doctor` each exited 1 because this sandbox could not launch the WinGet-linked `uv.exe`: `No application is associated with the specified file for this operation`.
- Direct fallback `.venv\Scripts\ruff.exe check .`: `All checks passed!`
- Direct pytest could not collect because the read-only sandbox offered no usable temporary directory; the report’s `243 passed, 3 deselected` result was therefore not reproducible here.
- Direct doctor ran but exited 1: `uv`, `ffmpeg`, and `ffprobe` were unavailable on this restricted PATH; Shazam failed for lack of a writable temporary directory; Panako reported `WARN JDK not found — Panako disabled`.
- `.venv\Scripts\python.exe -B scripts\audit_fixtures.py`: `audited 155 files` / `fixture audit passed`.
- `git diff --check`: exit 0.
- Static inspection confirmed the new query natural keys and scanner cache-key inputs match the plan, artifacts remain integer-only, and no uncommitted changes touch the existing `12000/r`, rational sample-map, one-sided-bound, or conflict-veto implementations.

REVIEW VERDICT: FIX_FIRST